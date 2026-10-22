"""连接配置对话框

顶部多了一个**「数据库类型」下拉**（默认「LightDB（本机）」）。用户此前踩过
的坑：对话框预填的端口是 `ConnectionInfo` 的历史默认 8888，而本机服务端默认
监听 8899 → 一点「连接」就是「127.0.0.1:8888 上没有服务端在监听（连接被
拒绝）」。现在默认类型 LightDB 直接把端口填成 **8899**，并在对话框里给一行
可照抄的启动命令。

类型定义与默认值全部在 `core/db_types.py`（纯函数表，无 Qt），这里只做 Qt
侧的薄封装：
  * 切换类型 → **只覆盖「空着或仍是上一个类型默认值」的字段**，不动用户手填
    的库名/密码/主机（见 `_is_untouched`）；
  * 不支持的类型（PostgreSQL/Oracle/SQLite）→ 禁用「确定」+ 写明原因，
    不假装能连；
  * 类型**不写进 `connections.json`**（schema 不变），重开对话框按端口反推
    （`db_types.kind_for_port`：8899→LightDB，3306→MySQL，其它→其它 MySQL
    兼容）。
"""
import json
import os
from datetime import datetime

from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QSpinBox, QComboBox,
    QDialogButtonBox, QVBoxLayout, QHBoxLayout, QPushButton,
    QMessageBox, QCheckBox, QLabel, QFileDialog
)
from PySide6.QtCore import Qt

from ..config import ConnectionInfo
from ..core import db_types
from ..core.db_errors import server_start_command
from ..db.connection import DBConnection
from .error_prompts import show_connection_error


class ConnectionDialog(QDialog):
    def __init__(self, parent=None, info: ConnectionInfo = None):
        super().__init__(parent)
        self.setWindowTitle("连接到 LightDB")
        self.setMinimumWidth(460)
        self._is_new = info is None
        self.info = info if info is not None else ConnectionInfo()
        # 类型不落盘：**打开已有连接**时按端口反推（`db_types.kind_for_port`：
        # 8899→LightDB，3306→MySQL，其它→其它 MySQL 兼容）。新建连接
        # （没传 info）直接用默认类型 LightDB —— 不能用 `ConnectionInfo()`
        # 的历史默认端口 8888 去反推（那会落到「其它 MySQL 兼容」，
        # 用户又回到「默认 8888 连不上」的坑里）。
        if info is not None:
            self._kind = (db_types.kind_for_port(self.info.port)
                          or db_types.DEFAULT_KIND)
        else:
            self._kind = db_types.DEFAULT_KIND

        self._build_ui()
        self._fill_fields()

    # ---------- UI ----------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setSpacing(10)

        # 数据库类型：默认选中「LightDB（本机）」（表里第一项 + DEFAULT_KIND）
        self.type_combo = QComboBox()
        for kind in db_types.kinds():
            entry = db_types.DB_TYPES[kind]
            self.type_combo.addItem(entry['display'], kind)
            tip = entry['reason']
            if not entry['supported']:
                tip = '暂不支持：%s\n（下拉里仍列出，是为了让你知道它连不上，'\
                      '而不是以为能连）' % entry['reason']
            self.type_combo.setItemData(self.type_combo.count() - 1, tip,
                                        Qt.ToolTipRole)
        self.type_combo.setToolTip('选择要连接的数据库类型；切换会填好该类型的'
                                   '默认值（已手填的字段不会被覆盖）')
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("数据库类型:", self.type_combo)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如: 本地开发库")
        form.addRow("连接名称:", self.name_edit)

        self.host_edit = QLineEdit()
        form.addRow("主机:", self.host_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        # 端口一变，「启动命令」那行提示跟着变（lightdb 类型）
        self.port_spin.valueChanged.connect(lambda _v: self._refresh_type_hint())
        form.addRow("端口:", self.port_spin)

        self.user_edit = QLineEdit()
        form.addRow("用户名:", self.user_edit)

        self.pwd_edit = QLineEdit()
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        self.pwd_edit.setPlaceholderText("（可空）")
        self.pwd_edit.setObjectName('pwd_edit')
        pwd_row = QHBoxLayout()
        pwd_row.setContentsMargins(0, 0, 0, 0)
        pwd_row.addWidget(self.pwd_edit, 1)
        self.pwd_toggle_btn = QPushButton("显示")
        self.pwd_toggle_btn.setCheckable(True)
        self.pwd_toggle_btn.setFixedWidth(52)
        self.pwd_toggle_btn.setToolTip("显示/隐藏口令（只影响本对话框显示）")
        self.pwd_toggle_btn.toggled.connect(self._toggle_password_visibility)
        pwd_row.addWidget(self.pwd_toggle_btn)
        form.addRow("密码:", pwd_row)

        self.db_edit = QLineEdit()
        self.db_edit.setPlaceholderText("（可空）")
        form.addRow("默认库:", self.db_edit)

        self.charset_combo = QComboBox()
        self.charset_combo.addItems(['utf8mb4', 'utf8', 'gbk', 'latin1'])
        form.addRow("字符集:", self.charset_combo)

        self.ssl_check = QCheckBox("启用 SSL")
        form.addRow("", self.ssl_check)
        # G2：SSL 客户端高级字段（验证文件存在/可读；pymysql ssl 参数）
        self._ssl_form = form
        self._ssl_rows = []
        for label, attr, btn_attr in (
                ("CA 证书:", 'ssl_ca_edit', 'ssl_ca_btn'),
                ("客户端证书:", 'ssl_cert_edit', 'ssl_cert_btn'),
                ("客户端私钥:", 'ssl_key_edit', 'ssl_key_btn')):
            edit = QLineEdit()
            edit.setPlaceholderText('（可选）')
            btn = QPushButton("浏览...")
            setattr(self, attr, edit)
            setattr(self, btn_attr, btn)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(edit, 1)
            row.addWidget(btn)
            form.addRow(label, row)
            self._ssl_rows.append((edit, btn))
        self.ssl_ca_edit.setPlaceholderText("CA 证书路径（可选）")
        self.ssl_cert_edit.setPlaceholderText("客户端证书（可选）")
        self.ssl_key_edit.setPlaceholderText("客户端私钥（可选）")
        self.ssl_ca_btn.clicked.connect(
            lambda: self._browse_ssl_file(self.ssl_ca_edit, 'CA 证书'))
        self.ssl_cert_btn.clicked.connect(
            lambda: self._browse_ssl_file(self.ssl_cert_edit, '客户端证书'))
        self.ssl_key_btn.clicked.connect(
            lambda: self._browse_ssl_file(self.ssl_key_edit, '客户端私钥'))
        self.ssl_check.toggled.connect(self._update_ssl_controls)

        layout.addLayout(form)

        # 即时提示（一行 label，不弹窗）：LightDB 给可照抄的启动命令；端口
        # 联动；不支持的类型给原因。
        self.type_hint_label = QLabel()
        self.type_hint_label.setWordWrap(True)
        self.type_hint_label.setTextFormat(Qt.PlainText)
        self.type_hint_label.setStyleSheet('color: #8a8a8a;')
        self.type_hint_label.setObjectName('type_hint_label')
        layout.addWidget(self.type_hint_label)

        # 按钮
        btn_row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._test_connection)
        btn_row.addWidget(self.test_btn)

        btn_row.addStretch()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = self.buttons.button(QDialogButtonBox.Ok)
        self.ok_btn.setText("确定")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        btn_row.addWidget(self.buttons)

        layout.addLayout(btn_row)

    def _fill_fields(self):
        # 新建连接（没给 info）：先按默认类型填好默认值（端口 8899 → 用户点
        # 连接就能连上本机服务端），再走下面的赋值兜底。
        # 注意不能用 `self.info == ConnectionInfo()` 判断「是不是新建」——
        # 上面这几个 set 会就地改 `self.info`，比完就不是「全新」了。
        if self._is_new:
            # previous_kind 传 `self._kind`：此时各框都是空的（只有端口是
            # QSpinBox 的下限 1），按「空着/仍是该类型默认值」判定即全部可填。
            self._apply_defaults(self._kind, self._kind)
            # 默认端口来自类型表（8899），不是 `ConnectionInfo` 的历史默认
            # 8888；同步回 info，免得下面按 `self.info.port` 又写回 8888。
            self.info.port = self.port_spin.value()
        self.name_edit.setText(self.info.name)
        self.host_edit.setText(self.info.host)
        self.port_spin.setValue(self.info.port)
        self.user_edit.setText(self.info.user)
        self.pwd_edit.setText(self.info.password)
        self.db_edit.setText(self.info.database)
        idx = self.charset_combo.findText(self.info.charset or 'utf8mb4')
        if idx >= 0:
            self.charset_combo.setCurrentIndex(idx)
        self.ssl_check.setChecked(bool(self.info.ssl))
        ssl_opts = getattr(self.info, 'ssl_options', None) or {}
        if isinstance(ssl_opts, dict):
            self.ssl_ca_edit.setText(str(ssl_opts.get('ca') or ''))
            self.ssl_cert_edit.setText(str(ssl_opts.get('cert') or ''))
            self.ssl_key_edit.setText(str(ssl_opts.get('key') or ''))
        self._update_ssl_controls()

        # 预选类型（反推得到）。切换会 `_apply_defaults`：这里字段刚按
        # info 填好，所以只有「仍等于旧类型默认值」的字段会被新类型覆盖。
        idx = self.type_combo.findData(self._kind)
        if idx >= 0 and idx != self.type_combo.currentIndex():
            self.type_combo.setCurrentIndex(idx)      # 走 _on_type_changed
        self._refresh_type_hint()

    # ---------- 类型联动 ----------

    def current_db_type(self):
        """当前选中的类型 kind（`db_types.DB_TYPES` 的键）。"""
        return db_types.current_kind(self.type_combo)

    # 字段名 → 取值/写值（顺序固定，便于测试与阅读）
    def _field_getters(self):
        return {
            'host': self.host_edit.text,
            'port': self.port_spin.value,
            'user': self.user_edit.text,
            'password': self.pwd_edit.text,
            'database': self.db_edit.text,
            'charset': self.charset_combo.currentText,
        }

    def _field_setters(self):
        return {
            'host': self.host_edit.setText,
            'port': self.port_spin.setValue,
            'user': self.user_edit.setText,
            'password': self.pwd_edit.setText,
            'database': self.db_edit.setText,
            'charset': self.charset_combo.setCurrentText,
        }

    def _baseline_for(self, field):
        """字段的「出厂默认」（用来判断用户是否动过这个框）。"""
        base = db_types.defaults_for(db_types.DEFAULT_KIND)
        return base.get(field)

    def _field_floors(self):
        """数值字段的「空值」= 控件下限（`QSpinBox` 没内容时就是 minimum）。"""
        return {'port': self.port_spin.minimum()}

    def _is_untouched(self, field, value, previous_kind, floor=None) -> bool:
        """该字段是否「用户没动过」= 空（或数值字段仍在下限）or 仍是**上一个
        类型的默认值**。

        只要用户改过（比如填了自己的库名/密码/主机），就不再被类型切换覆盖。
        新建对话框时 `previous_kind` 传的就是即将生效的那个类型，因此
        「空着」和「等于即将填进去的默认值」两种写法都不会误伤用户输入。

        注意：判空不能写 `value in ('', None)` —— 整数 `1 == True`，端口 1
        会被当成「空」，于是类型切换不敢改端口（默认 8899 就填不进去）。
        """
        if value is None or value == '':
            return True
        if floor is not None and value == floor:
            return True
        prev = db_types.defaults_for(previous_kind).get(field) \
            if previous_kind else None
        if prev is None:
            prev = self._baseline_for(field)
        return value == prev

    def _apply_defaults(self, kind, previous_kind) -> int:
        """把 `kind` 的默认值填进**未被用户改动**的字段，返回改动个数。

        返回 0 也正常（例如 MySQL→MariaDB 默认值完全一样）。
        """
        defaults = db_types.defaults_for(kind)
        getters, setters = self._field_getters(), self._field_setters()
        floors = self._field_floors()
        changed = 0
        for field, want in defaults.items():
            getter, setter = getters.get(field), setters.get(field)
            if getter is None or setter is None:
                continue
            before = getter()
            if before == want:
                continue
            if not self._is_untouched(field, before, previous_kind,
                                      floors.get(field)):
                continue                      # 用户填过 → 保留，不动
            setter(want)
            changed += 1
        return changed

    def _on_type_changed(self, _index=None):
        """切换类型：只覆盖「空着/仍是旧默认值」的字段，然后刷新提示与「确定」。"""
        kind = self.current_db_type()
        previous, self._kind = self._kind, kind
        self._apply_defaults(kind, previous)
        self._refresh_type_hint()

    def _refresh_type_hint(self):
        """刷新那一行提示 + 按「支持与否」启用/禁用「确定」。"""
        kind = self.current_db_type()
        host = self.host_edit.text().strip() or '127.0.0.1'
        port = self.port_spin.value()
        text = db_types.run_hint_text(kind, host, port,
                                      server_start_command(host, port))
        self.type_hint_label.setText(text)
        self.type_hint_label.setToolTip(text)

        supported = db_types.is_supported(kind)
        self.ok_btn.setEnabled(supported)
        self.ok_btn.setToolTip('' if supported
                               else '暂不支持：%s' % db_types.unsupported_reason(kind))
        return text

    def _collect(self) -> ConnectionInfo:
        info = ConnectionInfo(
            name=self.name_edit.text().strip(),
            host=self.host_edit.text().strip() or '127.0.0.1',
            port=self.port_spin.value(),
            user=self.user_edit.text().strip() or 'root',
            password=self.pwd_edit.text(),
            database=self.db_edit.text().strip(),
            charset=self.charset_combo.currentText(),
            ssl=self.ssl_check.isChecked(),
            ssl_options=self.build_ssl_params(silent=True),
        )
        return info

    # ---------- G2：SSL 高级字段 ----------

    def _update_ssl_controls(self, *_args):
        enabled = self.ssl_check.isChecked()
        for edit, btn in getattr(self, '_ssl_rows', []):
            edit.setEnabled(enabled)
            btn.setEnabled(enabled)

    def _browse_ssl_file(self, edit, label):
        path, _ = QFileDialog.getOpenFileName(
            self, f'选择{label}', edit.text() or '',
            '证书/私钥 (*.pem *.crt *.cer *.key *.p12);;所有文件 (*.*)')
        if path:
            edit.setText(path)
        return path

    def validate_ssl_paths(self):
        """校验 SSL 路径；返回 (ok, error)。仅在校验 SSL 开启时要求文件存在可读。"""
        if not self.ssl_check.isChecked():
            return True, ''
        ca = self.ssl_ca_edit.text().strip()
        cert = self.ssl_cert_edit.text().strip()
        key = self.ssl_key_edit.text().strip()
        if bool(cert) != bool(key):
            return False, '客户端证书与私钥必须同时提供（或都留空）'
        for label, path in (('CA 证书', ca), ('客户端证书', cert),
                            ('客户端私钥', key)):
            if not path:
                continue
            if not os.path.isfile(path):
                return False, f'{label}不存在: {path}'
            try:
                if not os.access(path, os.R_OK):
                    return False, f'{label}不可读: {path}'
            except Exception as exc:                  # noqa: BLE001
                return False, f'{label}不可读: {path}（{exc}）'
        return True, ''

    def build_ssl_params(self, silent=False):
        """构造 pymysql ssl 参数字典；路径非法时显式报错（silent=True 返回空）。"""
        if not self.ssl_check.isChecked():
            return {}
        ok, error = self.validate_ssl_paths()
        if not ok:
            if silent:
                return {}
            raise ValueError(error)
        params = {'check_hostname': False}
        for field, key in (('ssl_ca_edit', 'ca'),
                           ('ssl_cert_edit', 'cert'),
                           ('ssl_key_edit', 'key')):
            value = getattr(self, field).text().strip()
            if value:
                params[key] = value
        return params

    def _toggle_password_visibility(self, visible: bool):
        """显示/隐藏口令。只改输入框回显方式，不改变保存值。"""
        self.pwd_edit.setEchoMode(
            QLineEdit.Normal if visible else QLineEdit.Password)
        self.pwd_toggle_btn.setText("隐藏" if visible else "显示")

    def _test_connection(self):
        info = self._collect()
        if not info.host or not info.user:
            QMessageBox.warning(self, "提示", "主机和用户名不能为空")
            return

        self.test_btn.setEnabled(False)
        self.test_btn.setText("测试中...")
        try:
            conn = DBConnection(info)
            conn.connect()
            version = conn.server_version
            conn.close()
            QMessageBox.information(self, "成功",
                                    f"连接成功！\n服务器版本: {version}")
        except Exception as e:
            # 不再只丢驱动原文：结论 + 下一步（本地 host 直接给可照抄的启动
            # 命令）+ 一字不删的原始报文，文本可选中复制。
            show_connection_error(self, e, host=info.host, port=info.port,
                                  title="失败")
        finally:
            self.test_btn.setEnabled(True)
            self.test_btn.setText("测试连接")

    def _on_accept(self):
        # 「确定」在不支持的类型上本来就是禁用的；这里再兜一层，避免快捷键
        # 之类的旁路把它绕过去（连不上就说连不上，不假装）。
        if not db_types.is_supported(self.current_db_type()):
            QMessageBox.warning(
                self, "暂不支持",
                '暂不支持：%s' % db_types.unsupported_reason(
                    self.current_db_type()))
            return
        ok, ssl_error = self.validate_ssl_paths()
        if not ok:
            QMessageBox.warning(self, 'SSL 配置错误', ssl_error)
            return
        info = self._collect()
        if not info.name:
            QMessageBox.warning(self, "提示", "请填写连接名称")
            return
        if not info.host or not info.user:
            QMessageBox.warning(self, "提示", "主机和用户名不能为空")
            return
        self.info = info
        self.accept()

    def get_connection(self) -> ConnectionInfo:
        return self.info


# ==================== G1：连接编辑 / 复制 / 删除（供主窗口接线） ====================

def validate_connection_name(config, name, exclude=None):
    """名称非空/唯一校验。返回 (ok, error)。"""
    name = str(name or '').strip()
    if not name:
        return False, '连接名称不能为空'
    for conn in getattr(config, 'connections', []):
        if conn.name == name and conn.name != exclude:
            return False, f'连接名称已存在: {name}'
    return True, ''


def duplicate_connection(config, source_name, new_name=None):
    """复制连接（名称唯一校验；保存写回 config）。"""
    source = config.find(source_name) if hasattr(config, 'find') else None
    if source is None:
        return {'ok': False, 'error': f'连接不存在: {source_name}'}
    new_name = str(new_name or (source_name + ' - 副本')).strip()
    ok, error = validate_connection_name(config, new_name)
    if not ok:
        return {'ok': False, 'error': error}
    info = ConnectionInfo(
        name=new_name, host=source.host, port=source.port,
        user=source.user, password=source.password,
        database=source.database, charset=source.charset, ssl=source.ssl,
        ssl_options=dict(getattr(source, 'ssl_options', None) or {}))
    try:
        config.add_connection(info)
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'error': f'保存连接失败: {exc}'}
    return {'ok': True, 'connection': info, 'name': new_name, 'error': ''}


def delete_connection(config, name, ask=True, confirmer=None):
    """删除连接（二次确认含名称；保存后不丢其它连接）。"""
    target = None
    for conn in getattr(config, 'connections', []):
        if conn.name == name:
            target = conn
            break
    if target is None:
        return {'ok': False, 'error': f'连接不存在: {name}'}
    if ask:
        question = confirmer or (lambda title, text:
                                 QMessageBox.question(None, title, text))
        ret = question('删除连接',
                       f'确定删除连接 {name}？仅删除本地配置，不删除数据库。')
        if ret != QMessageBox.Yes:
            return {'ok': False, 'error': '用户取消', 'cancelled': True}
    try:
        config.remove_connection(name)
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'error': f'删除连接失败: {exc}'}
    return {'ok': True, 'name': name, 'error': ''}


def edit_connection(parent, config, name, dialog_class=None):
    """编辑选中连接：打开预填对话框；保存后写回 config。"""
    info = None
    for conn in getattr(config, 'connections', []):
        if conn.name == name:
            info = conn
            break
    if info is None:
        return {'ok': False, 'error': f'连接不存在: {name}'}
    cls = dialog_class or ConnectionDialog
    dlg = cls(parent, info=ConnectionInfo(
        name=info.name, host=info.host, port=info.port, user=info.user,
        password=info.password, database=info.database,
        charset=info.charset, ssl=info.ssl,
        ssl_options=dict(getattr(info, 'ssl_options', None) or {})))
    # 保留高级 SSL 配置（若原连接带有时）
    if getattr(info, 'ssl_options', None):
        dlg.info.ssl_options = dict(info.ssl_options)

        for edit_name, key in (('ssl_ca_edit', 'ca'),
                               ('ssl_cert_edit', 'cert'),
                               ('ssl_key_edit', 'key')):
            edit = getattr(dlg, edit_name, None)
            if edit is not None:
                edit.setText(str(info.ssl_options.get(key) or ''))
    if not dlg.exec():
        return {'ok': False, 'error': '用户取消', 'cancelled': True}
    edited = dlg.get_connection()
    ok, error = validate_connection_name(config, edited.name, exclude=name)
    if not ok:
        return {'ok': False, 'error': error}
    try:
        config.add_connection(edited)
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'error': f'保存连接失败: {exc}'}
    return {'ok': True, 'connection': edited, 'name': edited.name, 'error': ''}


# ==================== G2：连接导入 / 导出（JSON） ====================

EXPORT_VERSION = 1
EXPORT_REQUIRED = ('name', 'host', 'port', 'user')


def _connection_to_export_dict(conn, include_passwords=False):
    d = {
        'name': conn.name,
        'host': conn.host,
        'port': int(conn.port),
        'user': conn.user,
        'database': conn.database,
        'charset': conn.charset,
        'ssl': bool(conn.ssl),
    }
    ssl_options = getattr(conn, 'ssl_options', None)
    if ssl_options:
        d['ssl_options'] = dict(ssl_options)
    if include_passwords and conn.password:
        from ..core.credentials import encrypt_password
        d['password'] = encrypt_password(conn.password)
    return d


def export_connections_json(connections, filepath, include_passwords=False):
    """导出连接为 JSON；默认不包含密码（敏感信息显式提示）。"""
    try:
        payload = {
            'version': EXPORT_VERSION,
            'app': 'LightDB Studio',
            'exported_at': datetime.now().isoformat(timespec='seconds'),
            'passwords_included': bool(include_passwords),
            'warning': ('包含 DPAPI 加密的密码串；文件仍属敏感信息，请妥善保管'
                        if include_passwords else
                        '未导出密码；导入后需重新填写密码'),
            'connections': [_connection_to_export_dict(
                c, include_passwords=include_passwords)
                for c in connections],
        }
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'error': f'导出连接失败: {exc}',
                'path': str(filepath), 'count': 0}
    return {'ok': True, 'path': str(filepath),
            'count': len(payload['connections']),
            'passwords_included': bool(include_passwords),
            'warning': payload['warning'],
            'sensitive': bool(include_passwords), 'error': ''}


def _parse_connection_item(item, index):
    if not isinstance(item, dict):
        raise ValueError(f'第 {index + 1} 条连接不是对象')
    name = str(item.get('name') or '').strip()
    if not name:
        raise ValueError(f'第 {index + 1} 条连接缺少 name')
    host = str(item.get('host') or '').strip()
    user = str(item.get('user') or '').strip()
    if not host or not user:
        raise ValueError(f'连接 {name} 缺少 host/user')
    try:
        port = int(item.get('port', 0))
    except (TypeError, ValueError):
        raise ValueError(f'连接 {name} 的 port 非法')
    if not (1 <= port <= 65535):
        raise ValueError(f'连接 {name} 的 port 超出范围: {port}')
    known = {'name', 'host', 'port', 'user', 'password', 'database',
             'charset', 'ssl', 'ssl_options'}
    warnings = []
    unknown = sorted(set(item) - known)
    if unknown:
        warnings.append(f'连接 {name} 忽略未知字段: {unknown}')
    info = ConnectionInfo(
        name=name, host=host, port=port, user=user,
        password=str(item.get('password') or ''),
        database=str(item.get('database') or ''),
        charset=str(item.get('charset') or 'utf8mb4'),
        ssl=bool(item.get('ssl')))
    ssl_options = item.get('ssl_options')
    if isinstance(ssl_options, dict):
        info.ssl_options = dict(ssl_options)
    needs_password = not info.password
    if info.password:
        from ..core.credentials import decrypt_password, is_encrypted
        if is_encrypted(info.password):
            try:
                info.password = decrypt_password(info.password)
            except Exception as exc:                  # noqa: BLE001
                raise ValueError(f'连接 {name} 的密码无法解密（请重新导出/手填）: {exc}')
            needs_password = False
        else:
            warnings.append(f'连接 {name} 使用明文密码（保存时将自动加密）')
    return info, needs_password, warnings


def parse_connections_payload(data):
    """解析导出 JSON（不修改 config）；返回 (items, warnings, needs_password_names)。"""
    if not isinstance(data, dict):
        raise ValueError('连接文件必须是 JSON 对象')
    version = data.get('version')
    if version != EXPORT_VERSION:
        raise ValueError(f'不支持的连接文件版本: {version!r}（支持 {EXPORT_VERSION}）')
    raw = data.get('connections')
    if not isinstance(raw, list) or not raw:
        raise ValueError('连接文件没有 connections 数组或为空')
    items, warnings, needs = [], [], []
    for i, item in enumerate(raw):
        info, need_pw, warns = _parse_connection_item(item, i)
        items.append(info)
        warnings.extend(warns)
        if need_pw:
            needs.append(info.name)
    return items, warnings, needs


def import_connections_json(config, filepath, conflict='ask', confirm=None):
    """导入连接；失败不改动现有连接；冲突策略 skip/rename/overwrite/ask。

    `confirm(question: str) -> bool`：overwrite/ask 等需要用户确认；未提供时
    overwrite 视为取消，ask 默认自动重命名（安全默认）。
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        items, warnings, needs = parse_connections_payload(data)
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'error': f'导入失败（现有连接未改动）: {exc}',
                'imported': 0}
    existing = {c.name for c in getattr(config, 'connections', [])}
    imported, skipped, renamed = [], [], []
    planned = []
    for info in items:
        if info.name not in existing:
            planned.append(info)
            continue
        policy = conflict
        if policy == 'ask':
            policy = ('overwrite' if (confirm and confirm(
                f'连接 {info.name} 已存在，是否覆盖？')) else 'rename')
        if policy == 'skip':
            skipped.append(info.name)
        elif policy == 'overwrite':
            if confirm is not None and not confirm(
                    f'确认覆盖已有连接 {info.name}？（会替换本地配置）'):
                return {'ok': False, 'error': '用户取消导入',
                        'cancelled': True, 'imported': 0}
            target = config.find(info.name)
            planned.append(info)
            imported.append(info.name)
            if target is None:
                imported.pop()
                planned.pop()
        elif policy == 'rename':
            base = info.name
            n = 2
            while f'{base} ({n})' in existing or any(
                    f'{base} ({n})' == p.name for p in planned):
                n += 1
            info.name = f'{base} ({n})'
            planned.append(info)
            renamed.append(info.name)
        else:
            return {'ok': False, 'error': f'未知的冲突策略: {conflict!r}',
                    'imported': 0}
    backup = list(getattr(config, 'connections', []))
    try:
        for info in planned:
            if config.find(info.name) is not None:
                idx = [i for i, c in enumerate(config.connections)
                       if c.name == info.name][0]
                config.connections[idx] = info
            else:
                config.connections.append(info)
        config.save()
    except Exception as exc:                          # noqa: BLE001
        config.connections = backup
        return {'ok': False, 'error': f'保存失败，已回滚（现有连接未改动）: {exc}',
                'imported': 0}
    return {'ok': True, 'imported': len(planned), 'imported_names': [
                i.name for i in planned], 'skipped': skipped,
            'renamed': renamed, 'needs_password': needs,
            'warnings': warnings, 'error': ''}

