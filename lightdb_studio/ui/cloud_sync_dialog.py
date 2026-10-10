"""云同步对话框"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QMessageBox, QTabWidget, QWidget,
    QFormLayout, QCheckBox, QListWidget, QListWidgetItem, QGroupBox,
    QTextEdit, QDialogButtonBox
)
from PySide6.QtCore import Qt
from datetime import datetime

from ..core.cloud_sync import CloudSync


class CloudSyncDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sync = CloudSync()
        self.setWindowTitle("云同步 / 备份")
        self.resize(680, 520)
        self._build_ui()
        self._refresh_backups()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        tabs = QTabWidget()

        # Tab1: 导出/导入
        io_widget = QWidget()
        io_layout = QVBoxLayout(io_widget)

        # 导出
        exp_group = QGroupBox("导出配置")
        exp_form = QFormLayout(exp_group)

        self.exp_path = QLineEdit()
        exp_row = QHBoxLayout()
        exp_row.addWidget(self.exp_path)
        exp_browse = QPushButton("...")
        exp_browse.setMaximumWidth(40)
        exp_browse.clicked.connect(self._browse_export)
        exp_row.addWidget(exp_browse)
        exp_form.addRow("保存到:", exp_row)

        self.exp_pwd = QLineEdit()
        self.exp_pwd.setEchoMode(QLineEdit.Password)
        exp_form.addRow("加密密码:", self.exp_pwd)

        self.exp_inc_pwd = QCheckBox("包含连接密码（不推荐）")
        exp_form.addRow("", self.exp_inc_pwd)

        exp_btn = QPushButton("📤 立即导出")
        exp_btn.clicked.connect(self._do_export)
        exp_form.addRow("", exp_btn)

        io_layout.addWidget(exp_group)

        # 导入
        imp_group = QGroupBox("导入配置")
        imp_form = QFormLayout(imp_group)

        self.imp_path = QLineEdit()
        imp_row = QHBoxLayout()
        imp_row.addWidget(self.imp_path)
        imp_browse = QPushButton("...")
        imp_browse.setMaximumWidth(40)
        imp_browse.clicked.connect(self._browse_import)
        imp_row.addWidget(imp_browse)
        imp_form.addRow("读取自:", imp_row)

        self.imp_pwd = QLineEdit()
        self.imp_pwd.setEchoMode(QLineEdit.Password)
        imp_form.addRow("解密密码:", self.imp_pwd)

        self.imp_overwrite = QCheckBox("覆盖现有配置")
        imp_form.addRow("", self.imp_overwrite)

        imp_btn = QPushButton("📥 立即导入")
        imp_btn.clicked.connect(self._do_import)
        imp_form.addRow("", imp_btn)

        io_layout.addWidget(imp_group)
        io_layout.addStretch()

        tabs.addTab(io_widget, "导出 / 导入")

        # Tab2: 备份历史
        bak_widget = QWidget()
        bak_layout = QVBoxLayout(bak_widget)

        bak_layout.addWidget(QLabel(
            "每次导入前会自动备份当前配置到以下目录："))

        self.bak_list = QListWidget()
        bak_layout.addWidget(self.bak_list)

        bak_row = QHBoxLayout()
        restore_btn = QPushButton("↩️ 恢复到选中备份")
        restore_btn.clicked.connect(self._restore_backup)
        bak_row.addWidget(restore_btn)

        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self._refresh_backups)
        bak_row.addWidget(refresh_btn)
        bak_row.addStretch()
        bak_layout.addLayout(bak_row)

        tabs.addTab(bak_widget, "备份历史")

        layout.addWidget(tabs)

        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.reject)
        layout.addWidget(close_btn)

    def _browse_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出到", "lightdb_config.zip",
            "LightDB 配置 (*.zip)")
        if path:
            self.exp_path.setText(path)

    def _browse_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择配置文件", "",
            "LightDB 配置 (*.zip *.bin);;所有文件 (*.*)")
        if path:
            self.imp_path.setText(path)

    def _do_export(self):
        path = self.exp_path.text().strip()
        pwd = self.exp_pwd.text()
        if not path or not pwd:
            QMessageBox.warning(self, "提示", "请填写路径和密码")
            return
        try:
            result = self.sync.export_all(
                path, pwd, include_passwords=self.exp_inc_pwd.isChecked())
            QMessageBox.information(
                self, "完成",
                f"已导出:\n{result['output']}\n"
                f"包含 {len(result['files'])} 个文件\n"
                f"大小: {result['size']} 字节")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))

    def _do_import(self):
        path = self.imp_path.text().strip()
        pwd = self.imp_pwd.text()
        if not path or not pwd:
            QMessageBox.warning(self, "提示", "请填写路径和密码")
            return

        ret = QMessageBox.question(
            self, "确认",
            "导入会覆盖当前配置（会先自动备份），是否继续？")
        if ret != QMessageBox.Yes:
            return

        try:
            result = self.sync.import_all(
                path, pwd, overwrite=self.imp_overwrite.isChecked())
            if result.get('ok'):
                QMessageBox.information(
                    self, "完成",
                    f"已恢复 {len(result['restored'])} 个文件\n"
                    f"备份目录: {result['backup_dir']}\n\n"
                    "请重启应用以加载新配置。")
                self._refresh_backups()
            else:
                QMessageBox.critical(self, "失败", result.get('error', ''))
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))

    def _refresh_backups(self):
        self.bak_list.clear()
        backups = self.sync.list_backups()
        for b in backups:
            ts = datetime.fromtimestamp(b['time']).strftime(
                '%Y-%m-%d %H:%M:%S')
            item = QListWidgetItem(
                f"[{ts}] {len(b['files'])} 文件 | {b['path']}")
            item.setData(Qt.UserRole, b['path'])
            self.bak_list.addItem(item)

    def _restore_backup(self):
        item = self.bak_list.currentItem()
        if not item:
            return
        path = item.data(Qt.UserRole)
        ret = QMessageBox.question(
            self, "确认", f"恢复到备份？\n{path}")
        if ret != QMessageBox.Yes:
            return
        result = self.sync.restore_backup(path)
        if result.get('ok'):
            QMessageBox.information(self, "完成",
                                    f"已恢复 {len(result['restored'])} 文件")
        else:
            QMessageBox.critical(self, "失败", result.get('error', ''))