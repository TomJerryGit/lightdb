"""任务 C：连接对话框的**「数据库类型」下拉**（离屏 GUI 测试）。

背景（用户实际踩到的坑）：对话框预填的端口是 `ConnectionInfo` 的历史默认
**8888**，而本机 LightDB 服务端默认监听 **8899** → 用户点「连接」就是
``127.0.0.1:8888 上没有服务端在监听（连接被拒绝…）``。

本文件覆盖：
  1. 打开对话框默认就是 **LightDB + 端口 8899**（直接对应用户那次 8888 失败）；
  2. 切 MySQL / MariaDB → 3306；切回 LightDB → 8899；
  3. 「暂不支持」（PostgreSQL/Oracle/SQLite）→ 「确定」禁用 + 给出原因，
     且不假装能连；
  4. LightDB 提示行 = `core.db_errors.server_start_command(host, port)`，
     端口联动（改成 8899/其它时该行跟着变）；
  5. 切换类型只覆盖「空着/仍是旧默认值」的字段 —— 用户手填的库名/密码保留；
  6. 产出 `ConnectionInfo` 仍是 8 字段，且能 `config.save/load` 往返；
  7. 类型不落盘：重开对话框按端口反推（8899→LightDB，3306→MySQL，其它→兼容）。

环境：`QT_QPA_PLATFORM=offscreen`；对话框本身不碰主目录，只有 config 往返
那条用例需要按 `tests/test_studio_smoke.py::_redirect_home` 的思路把
`~/.lightdb_studio` 改道到 pytest 临时目录（受限沙箱里 `~` 可能不可写）。
"""
import json
import os
import sys
from pathlib import Path

# 必须在导入 PySide6 之前设置（离屏后端：没桌面会话也能真实构造 QWidget）
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

import lightdb_studio.config  # noqa: E402,F401  必须早于 _redirect_home 的改道
from lightdb_studio.core import db_types  # noqa: E402
from lightdb_studio.core.db_errors import server_start_command  # noqa: E402

# 必须保持强引用：QApplication 被 GC 掉之后再建 QWidget 会直接崩溃
_APP = None

HOME_PREFIX = 'lightdb_studio'
CONN_KEYS = {'name', 'host', 'port', 'user', 'password', 'database',
             'charset', 'ssl'}


# ==================== 环境 ====================

def qapp():
    """整个会话只保留一份 QApplication；进不去 offscreen 就明确 skip。"""
    global _APP
    pytest.importorskip('PySide6')
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    if QGuiApplication.platformName() != 'offscreen':
        pytest.skip(f'当前 Qt 平台是 {QGuiApplication.platformName()!r}，'
                    f'不是 offscreen，跑不了离屏 GUI 用例')
    return _APP


def _redirect_home(tmp_home):
    """把「主目录下的落盘路径」改道到 tmp_home（含 `~/.lightdb_studio`）。

    与 `tests/test_studio_smoke.py::_redirect_home` 同一思路：先改
    `Path.home()`，再把已经 import 过的 `lightdb_studio.*` 模块里那些
    import 期算好的 Path 常量逐个改道（都走 MonkeyPatch，能整体还原）。

    `lightdb_studio.config` 在模块顶层就 import 了（见本文件顶部），因为它
    的 `CONFIG_DIR/CONFIG_FILE` 是 import 期算好的；等 `_redirect_home` 跑完
    再 import 就晚了，`AppConfig.save()` 仍会写到真实主目录。
    """
    mp = pytest.MonkeyPatch()
    real_home = Path.home()
    mp.setattr(Path, 'home', classmethod(lambda cls: tmp_home))

    import lightdb_studio.ui.main_window          # noqa: F401 触发依赖导入

    moved = 0
    for module in list(sys.modules.values()):
        name = getattr(module, '__name__', '') or ''
        if not name.startswith(HOME_PREFIX):
            continue
        for attr, value in list(vars(module).items()):
            if attr.startswith('_') or not isinstance(value, Path):
                continue
            try:
                rel = value.relative_to(real_home)
            except ValueError:
                continue
            mp.setattr(module, attr, tmp_home / rel)
            moved += 1

    # 兜底：这三个常量是 AppConfig 的直接依据，显式再指一次（也走 mp，能还原）
    from lightdb_studio import config as config_mod
    cfg_dir = tmp_home / '.lightdb_studio'
    mp.setattr(config_mod, 'CONFIG_DIR', cfg_dir)
    mp.setattr(config_mod, 'CONFIG_FILE', cfg_dir / 'connections.json')
    mp.setattr(config_mod, 'SETTINGS_FILE', cfg_dir / 'settings.json')
    print(f'  → 主目录落盘路径改道 {moved} 处 → {tmp_home}')
    return mp


def _dialog(info=None):
    """构造连接对话框（不 exec：离屏下 exec 会挡事件循环）。

    `info` 省略时传 `None`（= 新建连接路径），由对话框自己造一个**全新的**
    `ConnectionInfo` —— 测试里不能写 `info=ConnectionInfo()` 之类的默认参数：
    它在 import 那一刻求值，会在用例间共享同一个可变对象。
    """
    qapp()
    from lightdb_studio.ui.connection_dialog import ConnectionDialog

    return ConnectionDialog(None, info)


def _select_type(dlg, kind):
    """按下拉里的 kind 选项选中它（模拟用户点选），返回下标。"""
    idx = dlg.type_combo.findData(kind)
    assert idx >= 0, f'下拉里没有 {kind}'
    dlg.type_combo.setCurrentIndex(idx)
    return idx


# ==================== 0. 纯函数表（无 GUI） ====================

def test_table_self_check_and_defaults_are_exact():
    """类型表自检通过 + 默认值逐条对照（不需要 GUI，失败信息最直接）。"""
    problems = db_types.validate_table()
    assert problems == [], problems
    print('\n【C-0】类型表：')
    for kind in db_types.kinds():
        entry = db_types.DB_TYPES[kind]
        d = entry['defaults']
        print(f"      {kind:12s} 支持={entry['supported']!s:5s} "
              f"host={d.get('host')!r} port={d.get('port')} "
              f"user={d.get('user')!r} charset={d.get('charset')!r}")

    assert db_types.DEFAULT_KIND == 'lightdb'
    assert db_types.kinds()[0] == 'lightdb', '下拉第一项不是默认类型'
    assert db_types.display_name('lightdb') == 'LightDB（本机）'

    assert db_types.defaults_for('lightdb') == {
        'host': '127.0.0.1', 'port': 8899, 'user': 'root',
        'password': '', 'database': '', 'charset': 'utf8mb4'}
    assert db_types.defaults_for('mysql') == {
        'host': '127.0.0.1', 'port': 3306, 'user': 'root',
        'password': '', 'database': '', 'charset': 'utf8mb4'}
    assert db_types.defaults_for('mariadb') == db_types.defaults_for('mysql')
    # 「其它 MySQL 兼容」不承诺能力：默认值只用于预填
    assert db_types.is_supported(db_types.COMPAT_KIND) is True

    # 绝不返回到 ConnectionInfo 里不存在的键（避免污染 8 键 schema）
    for kind in db_types.kinds():
        assert set(db_types.defaults_for(kind)) <= set(db_types.FIELD_ORDER)


@pytest.mark.parametrize('kind', ['postgresql', 'oracle', 'sqlite'])
def test_unsupported_kinds_are_marked_and_explained(kind):
    """不支持的三个类型：支持下拉里有、标注「暂不支持」、原因写明。"""
    entry = db_types.DB_TYPES[kind]
    assert entry['supported'] is False
    assert '暂不支持' in entry['display']
    assert db_types.is_supported(kind) is False
    reason = db_types.unsupported_reason(kind)
    print(f'\n【C-0b】{kind}: {reason}')
    assert reason and '暂不支持' not in reason, reason
    assert 'pymysql' in reason or 'sqlite3' in reason, reason
    assert db_types.unsupported_reason('lightdb') == ''


@pytest.mark.parametrize('port,expected', [
    (8899, 'lightdb'), (3306, 'mysql'), (5432, db_types.COMPAT_KIND),
    (9999, db_types.COMPAT_KIND), (None, None),
])
def test_kind_for_port_reverse_inference(port, expected):
    """反推规则写死：8899→LightDB，3306→MySQL，其它→其它 MySQL 兼容。"""
    assert db_types.kind_for_port(port) == expected
    assert db_types.saved_kind_for_port(port) == expected


# ==================== 1. 默认就是 LightDB / 8899 ====================

def test_dialog_defaults_to_lightdb_on_8899():
    """**用户那次 8888 失败的直接回归**：默认类型与默认端口必须是 LightDB/8899。"""
    dlg = _dialog()
    try:
        print(f"\n【C-1】开框默认：类型={dlg.type_combo.currentText()!r} "
              f"kind={dlg.current_db_type()!r} 端口={dlg.port_spin.value()} "
              f"主机={dlg.host_edit.text()!r} 用户={dlg.user_edit.text()!r} "
              f"字符集={dlg.charset_combo.currentText()!r} "
              f"库名={dlg.db_edit.text()!r} 密码={dlg.pwd_edit.text()!r} "
              f"确定可用={dlg.ok_btn.isEnabled()}")

        assert dlg.current_db_type() == 'lightdb'
        assert dlg.type_combo.currentIndex() == 0
        assert 'LightDB' in dlg.type_combo.currentText()
        assert dlg.port_spin.value() == 8899, '默认端口不是 8899（又回到 8888 的坑）'
        assert dlg.port_spin.value() != 8888
        assert dlg.host_edit.text() == '127.0.0.1'
        assert dlg.user_edit.text() == 'root'
        assert dlg.pwd_edit.text() == ''
        assert dlg.charset_combo.currentText() == 'utf8mb4'
        assert dlg.db_edit.text() == ''
        assert dlg.ok_btn.isEnabled()

        # 不支持的类型在下拉里可见并已标注
        labels = [dlg.type_combo.itemText(i)
                  for i in range(dlg.type_combo.count())]
        print(f'  → 下拉 {len(labels)} 项: {labels}')
        assert any('暂不支持' in t for t in labels), labels
        assert len(labels) == len(db_types.DB_TYPES)
    finally:
        dlg.close()


def test_dialog_lightdb_hint_gives_copyable_start_command_and_follows_port():
    """LightDB 那行提示 = server_start_command(host, port)，且端口联动。"""
    dlg = _dialog()
    try:
        hint = dlg.type_hint_label.text()
        print(f'\n【C-2】提示行（默认 8899）：{hint}')
        assert '\n' not in hint, '提示必须是一行 label，不弹窗'
        assert server_start_command('127.0.0.1', 8899) in hint
        assert '--port 8899' in hint and '--data-dir data_demo' in hint
        assert '未运行' in hint and '照抄' in hint

        dlg.port_spin.setValue(9999)
        hint2 = dlg.type_hint_label.text()
        print(f'  → 改成 9999：{hint2}')
        assert server_start_command('127.0.0.1', 9999) in hint2
        assert '--port 9999' in hint2
        assert '--port 8899' not in hint2, '端口改了但提示行没跟着变'

        dlg.host_edit.setText('10.0.0.7')
        dlg.port_spin.setValue(8899)
        hint3 = dlg.type_hint_label.text()
        print(f'  → 主机改成 10.0.0.7：{hint3}')
        assert '10.0.0.7:8899' in hint3
        assert server_start_command('10.0.0.7', 8899) in hint3

        # 切到 MySQL：不再给本引擎的启动命令（那不是 MySQL 的启动方式）
        _select_type(dlg, 'mysql')
        hint4 = dlg.type_hint_label.text()
        print(f'  → 切 MySQL：{hint4}')
        assert '--mode single' not in hint4, 'MySQL 不该冒充本引擎的启动命令'
        assert '3306' in hint4
    finally:
        dlg.close()


# ==================== 2. 切换类型 → 端口 3306 / 8899 ====================

def test_switching_types_sets_3306_and_back_to_8899():
    """LightDB(8899) → MySQL(3306) → MariaDB(3306) → LightDB(8899)。"""
    dlg = _dialog()
    try:
        assert dlg.port_spin.value() == 8899

        _select_type(dlg, 'mysql')
        print(f'\n【C-3】切 MySQL：端口={dlg.port_spin.value()} '
              f'用户={dlg.user_edit.text()!r} 字符集={dlg.charset_combo.currentText()!r}')
        assert dlg.port_spin.value() == 3306
        assert dlg.user_edit.text() == 'root'
        assert dlg.charset_combo.currentText() == 'utf8mb4'

        _select_type(dlg, 'mariadb')
        print(f'  → 切 MariaDB：端口={dlg.port_spin.value()}')
        assert dlg.port_spin.value() == 3306

        _select_type(dlg, 'lightdb')
        print(f'  → 切回 LightDB：端口={dlg.port_spin.value()}')
        assert dlg.port_spin.value() == 8899

        # 兼容引擎：保留当前值（端口仍是 3306），只加提示
        _select_type(dlg, db_types.COMPAT_KIND)
        print(f'  → 切「其它 MySQL 兼容」：端口={dlg.port_spin.value()} '
              f'提示={dlg.type_hint_label.text()[:40]!r}…')
        assert dlg.port_spin.value() == 3306
        assert dlg.type_hint_label.text().strip()
        assert dlg.ok_btn.isEnabled()
    finally:
        dlg.close()


def test_switching_does_not_clobber_user_typed_fields():
    """用户手填的库名/密码/主机不被类型切换覆盖（更安全的行为）。"""
    dlg = _dialog()
    try:
        dlg.db_edit.setText('my_own_db')
        dlg.pwd_edit.setText('s3cret')
        dlg.host_edit.setText('10.0.0.7')
        _select_type(dlg, 'mysql')
        print(f'\n【C-4】手填后切 MySQL：库名={dlg.db_edit.text()!r} '
              f'密码={dlg.pwd_edit.text()!r} 主机={dlg.host_edit.text()!r} '
              f'端口={dlg.port_spin.value()}')
        assert dlg.db_edit.text() == 'my_own_db', '类型切换清掉了用户填的库名'
        assert dlg.pwd_edit.text() == 's3cret', '类型切换清掉了用户填的密码'
        assert dlg.host_edit.text() == '10.0.0.7'
        assert dlg.port_spin.value() == 3306, '端口属于「类型默认字段」，应被更新'

        _select_type(dlg, 'lightdb')
        assert dlg.port_spin.value() == 8899
        assert dlg.db_edit.text() == 'my_own_db'
        assert dlg.pwd_edit.text() == 's3cret'
        # 提示行用的是用户自己的主机
        assert '10.0.0.7:8899' in dlg.type_hint_label.text()
    finally:
        dlg.close()


# ==================== 3. 暂不支持 → OK 禁用 + 原因 ====================

@pytest.mark.parametrize('kind', ['postgresql', 'oracle', 'sqlite'])
def test_unsupported_kind_disables_ok_and_explains(kind):
    """选中「暂不支持」：OK 禁用、原因可读、按钮 tooltip 也写明。"""
    dlg = _dialog()
    try:
        idx = _select_type(dlg, kind)
        label = dlg.type_combo.itemText(idx)
        hint = dlg.type_hint_label.text()
        print(f'\n【C-5】选 {label!r}：OK 可用={dlg.ok_btn.isEnabled()} '
              f'提示={hint}')

        assert '暂不支持' in label, '下拉项没有标注「暂不支持」'
        assert not dlg.ok_btn.isEnabled(), '不支持的类型竟然还能点「确定」'
        assert '暂不支持' in hint
        assert db_types.unsupported_reason(kind) in hint, '提示行没写原因'
        tooltip = dlg.ok_btn.toolTip()
        assert '暂不支持' in tooltip and db_types.unsupported_reason(kind) in tooltip
        assert dlg.type_combo.itemData(idx, Qt.ToolTipRole), '下拉项缺少说明 tooltip'

        # 切回支持的类型 → OK 恢复可用（不是一次禁用永久禁用）
        _select_type(dlg, 'lightdb')
        assert dlg.ok_btn.isEnabled()
        assert '暂不支持' not in dlg.type_hint_label.text()
    finally:
        dlg.close()


@pytest.mark.parametrize('kind', ['postgresql', 'oracle', 'sqlite'])
def test_unsupported_accept_guard_refuses_and_explains(kind, monkeypatch):
    """就算绕过禁用的按钮直接走 `_on_accept()`，也**不假装能连**：
    弹出「暂不支持 + 原因」，且对话框不 accept。"""
    dlg = _dialog()
    try:
        _select_type(dlg, kind)
        shown = []
        monkeypatch.setattr(
            'lightdb_studio.ui.connection_dialog.QMessageBox.warning',
            lambda parent, title, text, *a, **k: shown.append((title, text)))
        accepted = []
        monkeypatch.setattr(dlg, 'accept', lambda: accepted.append(True))
        dlg.name_edit.setText('不该能建')
        dlg._on_accept()

        print(f'\n【C-5b】{kind}: 弹窗={shown}  accepted={accepted}')
        assert shown, '不支持的类型的 OK 兜底没有给出说明'
        title, text = shown[0]
        assert title == '暂不支持'
        assert '暂不支持' in text
        assert db_types.unsupported_reason(kind) in text
        assert accepted == [], '不支持的类型竟然 accept 了'
    finally:
        dlg.close()


# ==================== 4. ConnectionInfo：8 字段 + save/load 往返 ====================
def test_dialog_output_is_8_fields_and_roundtrips_config(tmp_path):
    """对话框产出的 `ConnectionInfo` 仍是 8 键，能原样 save/load（schema 不变）。"""
    tmp_home = tmp_path / 'home'
    tmp_home.mkdir()
    mp = _redirect_home(tmp_home)
    dlg = None
    try:
        dlg = _dialog()
        assert dlg.current_db_type() == 'lightdb'
        dlg.name_edit.setText('本机 LightDB')
        assert dlg.port_spin.value() == 8899
        dlg._on_accept()                       # 校验通过 → accept()
        info = dlg.get_connection()
        from dataclasses import asdict, fields

        keys = {f.name for f in fields(info)}
        print(f'\n【C-6】对话框产出：{asdict(info)}')
        assert keys == CONN_KEYS, sorted(keys)
        assert len(asdict(info)) == 8
        assert (info.host, info.port, info.user) == ('127.0.0.1', 8899, 'root')
        assert (info.password, info.database, info.charset, info.ssl) == \
            ('', '', 'utf8mb4', False)
        assert 'kind' not in asdict(info) and 'db_type' not in asdict(info)

        from lightdb_studio.config import AppConfig, CONNECTION_FIELDS

        assert CONNECTION_FIELDS == CONN_KEYS
        cfg = AppConfig()
        cfg.add_connection(info)
        loaded = AppConfig()
        back = loaded.find('本机 LightDB')
        assert back is not None, 'save/load 往返后找不到这条连接'
        assert asdict(back) == asdict(info), (asdict(back), asdict(info))

        raw = json.loads((tmp_home / '.lightdb_studio' / 'connections.json')
                         .read_text(encoding='utf-8'))
        print(f'  → 落盘顶层键 {sorted(raw)}；每条键 '
              f'{sorted(raw["connections"][0])}')
        assert set(raw) == {'connections', 'last_connection'}
        assert set(raw['connections'][0]) == CONN_KEYS
    finally:
        if dlg is not None:
            dlg.close()
        mp.undo()


def test_reopening_dialog_reverse_infers_type_from_port():
    """类型不落盘：重开对话框按端口反推（8899→LightDB，3306→MySQL，其它→兼容）。"""
    from lightdb_studio.config import ConnectionInfo

    dlg = _dialog(ConnectionInfo(name='本地', host='127.0.0.1', port=8899))
    try:
        print(f'\n【C-7】reopen(port=8899) → {dlg.current_db_type()!r} '
              f'端口={dlg.port_spin.value()}')
        assert dlg.current_db_type() == 'lightdb'
        assert dlg.port_spin.value() == 8899
    finally:
        dlg.close()

    dlg = _dialog(ConnectionInfo(name='mysql', host='127.0.0.1', port=3306))
    try:
        print(f'  → reopen(port=3306) → {dlg.current_db_type()!r} '
              f'端口={dlg.port_spin.value()}')
        assert dlg.current_db_type() == 'mysql'
        assert dlg.port_spin.value() == 3306
    finally:
        dlg.close()

    dlg = _dialog(ConnectionInfo(name='远程其它', host='10.0.0.7', port=9999))
    try:
        print(f'  → reopen(port=9999) → {dlg.current_db_type()!r} '
              f'主机={dlg.host_edit.text()!r} 端口={dlg.port_spin.value()}')
        assert dlg.current_db_type() == db_types.COMPAT_KIND
        assert dlg.port_spin.value() == 9999, '兼容引擎不该覆盖用户原有端口'
        assert dlg.host_edit.text() == '10.0.0.7', '不该覆盖用户原有主机'
        assert dlg.type_hint_label.text().strip()
    finally:
        dlg.close()
