"""连接配置对话框"""
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QSpinBox, QComboBox,
    QDialogButtonBox, QVBoxLayout, QHBoxLayout, QPushButton,
    QMessageBox, QCheckBox, QLabel
)
from PySide6.QtCore import Qt

from ..config import ConnectionInfo
from ..db.connection import DBConnection


class ConnectionDialog(QDialog):
    def __init__(self, parent=None, info: ConnectionInfo = None):
        super().__init__(parent)
        self.setWindowTitle("连接到 LightDB")
        self.setMinimumWidth(420)
        self.info = info or ConnectionInfo()

        self._build_ui()
        self._fill_fields()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setSpacing(10)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如: 本地开发库")
        form.addRow("连接名称:", self.name_edit)

        self.host_edit = QLineEdit()
        form.addRow("主机:", self.host_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(8888)
        form.addRow("端口:", self.port_spin)

        self.user_edit = QLineEdit()
        form.addRow("用户名:", self.user_edit)

        self.pwd_edit = QLineEdit()
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        self.pwd_edit.setPlaceholderText("（可空）")
        form.addRow("密码:", self.pwd_edit)

        self.db_edit = QLineEdit()
        self.db_edit.setPlaceholderText("（可空）")
        form.addRow("默认库:", self.db_edit)

        self.charset_combo = QComboBox()
        self.charset_combo.addItems(['utf8mb4', 'utf8', 'gbk', 'latin1'])
        form.addRow("字符集:", self.charset_combo)

        self.ssl_check = QCheckBox("启用 SSL")
        form.addRow("", self.ssl_check)

        layout.addLayout(form)

        # 按钮
        btn_row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._test_connection)
        btn_row.addWidget(self.test_btn)

        btn_row.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        btn_row.addWidget(buttons)

        layout.addLayout(btn_row)

    def _fill_fields(self):
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
            QMessageBox.critical(self, "失败", f"连接失败:\n{e}")
        finally:
            self.test_btn.setEnabled(True)
            self.test_btn.setText("测试连接")

    def _on_accept(self):
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