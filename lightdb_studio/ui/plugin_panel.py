"""插件管理面板"""
import os
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QDialogButtonBox, QMessageBox, QTextEdit,
    QFileDialog, QWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont

from ..core.plugin_manager import PluginManager


class PluginPanel(QDialog):
    def __init__(self, plugin_mgr: PluginManager, parent=None):
        super().__init__(parent)
        self.mgr = plugin_mgr
        self.setWindowTitle("插件管理")
        self.resize(760, 520)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            f"插件目录: <code>{self.mgr.dir}</code><br>"
            "将 .py 插件文件放入该目录，即可自动加载。")
        info.setTextFormat(Qt.RichText)
        info.setStyleSheet("color: #969696; padding: 4px;")
        layout.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ['插件名', '版本', '作者', '描述', '状态'])
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 120)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_select)
        layout.addWidget(self.table)

        # 详情
        layout.addWidget(QLabel("详情:"))
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(QFont("Consolas", 9))
        self.detail.setMaximumHeight(120)
        layout.addWidget(self.detail)

        # 按钮
        row = QHBoxLayout()

        reload_btn = QPushButton("🔄 重新加载")
        reload_btn.clicked.connect(self._reload)
        row.addWidget(reload_btn)

        open_dir_btn = QPushButton("📁 打开目录")
        open_dir_btn.clicked.connect(self._open_dir)
        row.addWidget(open_dir_btn)

        install_btn = QPushButton("📥 安装插件")
        install_btn.clicked.connect(self._install)
        row.addWidget(install_btn)

        row.addStretch()

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        layout.addLayout(row)

    def refresh(self):
        plugins = self.mgr.get_all()
        self.table.setRowCount(len(plugins))
        for r, info in enumerate(plugins):
            self.table.setItem(r, 0, QTableWidgetItem(info.name))
            self.table.setItem(r, 1, QTableWidgetItem(info.version))
            self.table.setItem(r, 2, QTableWidgetItem(info.author))
            self.table.setItem(r, 3, QTableWidgetItem(info.description))

            status = "✅ 已加载" if (info.instance and not info.error) \
                else f"❌ {info.error[:30]}"
            st_item = QTableWidgetItem(status)
            if info.error:
                st_item.setForeground(QColor("#f48771"))
            else:
                st_item.setForeground(QColor("#89d185"))
            self.table.setItem(r, 4, st_item)

    def _on_select(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        if not rows:
            return
        r = list(rows)[0]
        plugins = self.mgr.get_all()
        if r >= len(plugins):
            return
        info = plugins[r]
        lines = [
            f"名称: {info.name}",
            f"版本: {info.version}",
            f"作者: {info.author}",
            f"描述: {info.description}",
            f"文件: {info.path}",
            f"状态: {'已加载' if info.instance else '失败'}",
        ]
        if info.error:
            lines.append(f"\n错误:\n{info.error}")
        self.detail.setPlainText('\n'.join(lines))

    def _reload(self):
        try:
            self.mgr.reload_all()
            self.refresh()
            QMessageBox.information(self, "完成", "插件已重新加载")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))

    def _open_dir(self):
        import subprocess
        path = str(self.mgr.dir)
        try:
            if os.name == 'nt':
                os.startfile(path)
            elif os.name == 'posix':
                subprocess.Popen(['xdg-open', path])
        except Exception as e:
            QMessageBox.warning(self, "提示", f"无法打开目录: {e}")

    def _install(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择插件文件", "", "Python (*.py)")
        if not paths:
            return
        import shutil
        n = 0
        for p in paths:
            try:
                shutil.copy2(p, str(self.mgr.dir / os.path.basename(p)))
                n += 1
            except Exception as e:
                QMessageBox.warning(self, "部分失败", str(e))
        if n:
            self.mgr.reload_all()
            self.refresh()
            QMessageBox.information(self, "完成", f"安装 {n} 个插件")