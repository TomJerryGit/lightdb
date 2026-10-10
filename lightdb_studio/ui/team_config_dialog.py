"""团队配置对话框"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QMessageBox, QListWidget, QListWidgetItem,
    QCheckBox, QGroupBox, QFormLayout, QTabWidget, QWidget
)
from PySide6.QtCore import Qt

from ..core.team_config import TeamConfigManager


class TeamConfigDialog(QDialog):
    def __init__(self, connections: list, parent=None):
        super().__init__(parent)
        self.connections = connections
        self.mgr = TeamConfigManager()
        self.setWindowTitle("团队配置")
        self.resize(680, 500)
        self._build_ui()
        self._refresh_profiles()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        tabs = QTabWidget()

        # Tab1: 导出/导入连接
        io_widget = QWidget()
        io_layout = QVBoxLayout(io_widget)

        # 导出
        exp_group = QGroupBox("导出连接配置")
        exp_form = QFormLayout(exp_group)

        self.exp_path = QLineEdit()
        row = QHBoxLayout()
        row.addWidget(self.exp_path)
        b = QPushButton("...")
        b.setMaximumWidth(40)
        b.clicked.connect(self._browse_export)
        row.addWidget(b)
        exp_form.addRow("文件:", row)

        self.exp_inc_pwd = QCheckBox("包含密码（需填密钥）")
        exp_form.addRow("", self.exp_inc_pwd)

        self.exp_key = QLineEdit()
        self.exp_key.setEchoMode(QLineEdit.Password)
        self.exp_key.setPlaceholderText("加密密钥")
        exp_form.addRow("密钥:", self.exp_key)

        exp_btn = QPushButton("📤 导出全部连接")
        exp_btn.clicked.connect(self._do_export)
        exp_form.addRow("", exp_btn)

        io_layout.addWidget(exp_group)

        # 导入
        imp_group = QGroupBox("导入连接配置")
        imp_form = QFormLayout(imp_group)

        self.imp_path = QLineEdit()
        row2 = QHBoxLayout()
        row2.addWidget(self.imp_path)
        b2 = QPushButton("...")
        b2.setMaximumWidth(40)
        b2.clicked.connect(self._browse_import)
        row2.addWidget(b2)
        imp_form.addRow("文件:", row2)

        self.imp_key = QLineEdit()
        self.imp_key.setEchoMode(QLineEdit.Password)
        imp_form.addRow("密钥:", self.imp_key)

        imp_btn = QPushButton("📥 导入")
        imp_btn.clicked.connect(self._do_import)
        imp_form.addRow("", imp_btn)

        io_layout.addWidget(imp_group)
        io_layout.addStretch()
        tabs.addTab(io_widget, "导入 / 导出")

        # Tab2: 团队配置库
        profile_widget = QWidget()
        profile_layout = QVBoxLayout(profile_widget)

        profile_layout.addWidget(QLabel(
            "保存到共享目录（团队每个人都能看到）:"))

        self.profile_list = QListWidget()
        profile_layout.addWidget(self.profile_list)

        row3 = QHBoxLayout()

        save_btn = QPushButton("💾 保存当前连接为配置")
        save_btn.clicked.connect(self._save_profile)
        row3.addWidget(save_btn)

        load_btn = QPushButton("📂 加载选中配置")
        load_btn.clicked.connect(self._load_profile)
        row3.addWidget(load_btn)
        row3.addStretch()
        profile_layout.addLayout(row3)

        tabs.addTab(profile_widget, "团队配置库")

        layout.addWidget(tabs)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)

    def _browse_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出连接", "team_connections.json",
            "JSON (*.json)")
        if path:
            self.exp_path.setText(path)

    def _browse_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入连接", "", "JSON (*.json)")
        if path:
            self.imp_path.setText(path)

    def _do_export(self):
        path = self.exp_path.text().strip()
        if not path:
            QMessageBox.warning(self, "提示", "请选择文件")
            return
        try:
            n = self.mgr.export_connections(
                self.connections, path,
                include_passwords=self.exp_inc_pwd.isChecked(),
                encrypt_key=self.exp_key.text())
            QMessageBox.information(self, "完成", f"已导出 {n} 个连接")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))

    def _do_import(self):
        path = self.imp_path.text().strip()
        if not path:
            QMessageBox.warning(self, "提示", "请选择文件")
            return
        try:
            conns = self.mgr.import_connections(path, self.imp_key.text())
            # 通过信号或直接返回给调用方
            self.imported_connections = conns
            QMessageBox.information(
                self, "完成", f"解析到 {len(conns)} 个连接。\n"
                "请返回主窗口完成导入。")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))

    def _refresh_profiles(self):
        self.profile_list.clear()
        for name in self.mgr.list_profiles():
            item = QListWidgetItem(f"📁 {name}")
            item.setData(Qt.UserRole, name)
            self.profile_list.addItem(item)

    def _save_profile(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "保存配置", "配置名称:", text="team_default")
        if not ok or not name.strip():
            return
        try:
            p = self.mgr.save_team_profile(
                name.strip(), self.connections, self.exp_key.text())
            QMessageBox.information(self, "完成", f"已保存到:\n{p}")
            self._refresh_profiles()
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))

    def _load_profile(self):
        item = self.profile_list.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        conns = self.mgr.load_profile(name)
        if not conns:
            QMessageBox.warning(self, "提示", "配置为空")
            return
        self.imported_connections = conns
        QMessageBox.information(self, "加载成功",
                                f"已解析 {len(conns)} 个连接")
        self.accept()