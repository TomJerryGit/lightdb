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
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QSpinBox, QComboBox,
    QDialogButtonBox, QVBoxLayout, QHBoxLayout, QPushButton,
    QMessageBox, QCheckBox, QLabel
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
        return ConnectionInfo(
            name=self.name_edit.text().strip(),
            host=self.host_edit.text().strip() or '127.0.0.1',
            port=self.port_spin.value(),
            user=self.user_edit.text().strip() or 'root',
            password=self.pwd_edit.text(),
            database=self.db_edit.text().strip(),
            charset=self.charset_combo.currentText(),
            ssl=self.ssl_check.isChecked(),
        )

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
        database=source.database, charset=source.charset, ssl=source.ssl)
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
    dlg = cls(parent, info=ConnectionInfo(**info.__dict__))
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
