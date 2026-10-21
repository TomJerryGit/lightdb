# -*- coding: utf-8 -*-
"""插件管理面板（P2-8：列表 / 启用禁用 / 重载 / 刷新 / 打开目录 / 安装）

操作结果一律显式返回（dict + 界面提示），不弹假成功；插件错误在列表和详情
中可见。面板可注入 `PluginManager`（测试用 tmp 插件目录/状态文件）。
"""
import os
import shutil
import subprocess

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QMessageBox, QTextEdit, QFileDialog,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont

from ..core.plugin_manager import PluginManager

_STATUS_TEXT = {'loaded': '✅ 已加载', 'disabled': '⏸ 已禁用',
                'failed': '❌ 失败'}
_STATUS_COLOR = {'loaded': '#89d185', 'disabled': '#d7ba7d',
                 'failed': '#f48771'}


class PluginPanel(QDialog):
    def __init__(self, plugin_mgr: PluginManager, parent=None):
        super().__init__(parent)
        self.mgr = plugin_mgr
        self.last_result = None
        self.setWindowTitle("插件管理")
        self.resize(980, 620)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            f"插件目录: <code>{self.mgr.dir}</code><br>"
            "将 .py 插件放入该目录；启用/禁用状态会保存到 "
            f"<code>{self.mgr.state_path}</code>。")
        info.setTextFormat(Qt.RichText)
        info.setAlignment(Qt.AlignLeft)
        info.setStyleSheet("color: #969696; padding: 4px;")
        layout.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ['插件名', '版本', '状态', '文件', '错误'])
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 320)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_select)
        layout.addWidget(self.table)

        layout.addWidget(QLabel("详情:"))
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(QFont("Consolas", 9))
        self.detail.setMaximumHeight(130)
        layout.addWidget(self.detail)

        row = QHBoxLayout()
        self.enable_btn = QPushButton("✅ 启用")
        self.enable_btn.clicked.connect(self._on_enable)
        row.addWidget(self.enable_btn)
        self.disable_btn = QPushButton("⏸ 禁用")
        self.disable_btn.clicked.connect(self._on_disable)
        row.addWidget(self.disable_btn)
        self.reload_btn = QPushButton("🔄 重载")
        self.reload_btn.clicked.connect(self._on_reload)
        row.addWidget(self.reload_btn)
        self.refresh_btn = QPushButton("🔁 刷新")
        self.refresh_btn.clicked.connect(self._on_refresh)
        row.addWidget(self.refresh_btn)
        self.open_dir_btn = QPushButton("📁 打开目录")
        self.open_dir_btn.clicked.connect(self._open_dir)
        row.addWidget(self.open_dir_btn)
        self.install_btn = QPushButton("📥 安装插件")
        self.install_btn.clicked.connect(self._install)
        row.addWidget(self.install_btn)
        row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        layout.addLayout(row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #9cdcfe; padding: 4px;")
        layout.addWidget(self.status_label)

    # ==================== 列表 / 详情 ====================

    def refresh(self):
        plugins = self.mgr.get_all()
        self.table.setRowCount(len(plugins))
        for r, info in enumerate(plugins):
            self.table.setItem(r, 0, QTableWidgetItem(info.name))
            self.table.setItem(r, 1, QTableWidgetItem(str(info.version)))
            status = _STATUS_TEXT.get(info.status, info.status)
            st_item = QTableWidgetItem(status)
            st_item.setForeground(QColor(_STATUS_COLOR.get(
                info.status, '#d4d4d4')))
            st_item.setData(Qt.UserRole, info.key)
            self.table.setItem(r, 2, st_item)
            self.table.setItem(r, 3, QTableWidgetItem(info.path))
            err_item = QTableWidgetItem((info.error or '').split('\n')[0])
            if info.error:
                err_item.setForeground(QColor('#f48771'))
            self.table.setItem(r, 4, err_item)
        warnings = list(getattr(self.mgr, 'warnings', []))[-3:]
        self.status_label.setText('⚠ ' + '；'.join(warnings) if warnings else '')
        return plugins

    def _selected_info(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 2)
        key = item.data(Qt.UserRole) if item is not None else None
        if not key:
            return None
        return self.mgr.get(key)

    def _on_select(self):
        info = self._selected_info()
        if info is None:
            return
        lines = [
            f"名称: {info.name}",
            f"版本: {info.version}",
            f"作者: {info.author}",
            f"描述: {info.description}",
            f"文件: {info.path}",
            f"状态: {info.status}（enabled={info.enabled}, loaded={info.loaded}）",
        ]
        if info.error:
            lines.append(f"\n错误:\n{info.error}")
        if info.last_error:
            lines.append(f"\n上次错误:\n{info.last_error}")
        self.detail.setPlainText('\n'.join(lines))

    # ==================== 操作（返回显式结果） ====================

    def set_enabled_selected(self, enabled: bool) -> dict:
        info = self._selected_info()
        if info is None:
            return {'ok': False, 'error': '未选择插件'}
        result = self.mgr.set_enabled(info.key, enabled)
        self.last_result = result
        self.refresh()
        self._select_key(info.key)
        return result

    def reload_selected(self) -> dict:
        info = self._selected_info()
        if info is None:
            return {'ok': False, 'error': '未选择插件'}
        result = self.mgr.reload_one(info.key)
        self.last_result = result
        self.refresh()
        self._select_key(info.key)
        return result

    def refresh_plugins(self) -> list:
        plugins = self.mgr.load_all()
        self.refresh()
        return plugins

    def _select_key(self, key):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 2)
            if item is not None and item.data(Qt.UserRole) == key:
                self.table.setCurrentCell(row, 2)
                return True
        return False

    def install_plugin_files(self, paths) -> dict:
        """把插件文件复制到插件目录并重新加载（跳过文件对话框，便于测试）。"""
        paths = [str(p) for p in (paths or [])]
        installed = 0
        errors = []
        for path in paths:
            try:
                shutil.copy2(path, str(self.mgr.dir / os.path.basename(path)))
                installed += 1
            except Exception as exc:                  # noqa: BLE001
                errors.append(f'{path}: {exc}')
        loaded = []
        if installed:
            loaded = self.mgr.load_all()
        self.refresh()
        return {'ok': installed == len(paths) and not errors,
                'installed': installed, 'errors': errors,
                'loaded': [info.key for info in loaded]}

    def open_plugin_dir(self) -> dict:
        path = str(self.mgr.dir)
        try:
            if os.name == 'nt':
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open', path])
            return {'ok': True, 'path': path, 'error': ''}
        except Exception as exc:                      # noqa: BLE001
            return {'ok': False, 'path': path, 'error': str(exc)}

    # ---------- UI 包装（显式提示） ----------

    def _on_enable(self):
        result = self.set_enabled_selected(True)
        if result.get('ok'):
            QMessageBox.information(self, '已启用',
                                    f"插件已启用: {result.get('info').name}")
        else:
            QMessageBox.critical(self, '启用失败',
                                 result.get('error') or '未知错误')

    def _on_disable(self):
        result = self.set_enabled_selected(False)
        if result.get('ok'):
            QMessageBox.information(self, '已禁用',
                                    f"插件已禁用: {result.get('info').name}")
        else:
            QMessageBox.critical(self, '禁用失败',
                                 result.get('error') or '未知错误')

    def _on_reload(self):
        result = self.reload_selected()
        if result.get('ok'):
            QMessageBox.information(self, '重载完成',
                                    f"插件已重载: {result.get('info').name}")
        else:
            QMessageBox.critical(self, '重载失败',
                                 result.get('error') or '未知错误')

    def _on_refresh(self):
        plugins = self.refresh_plugins()
        info = self._selected_info()
        key = info.key if info is not None else ''
        if key:
            self._select_key(key)
        QMessageBox.information(self, '已刷新',
                                f"共 {len(plugins)} 个插件（含失败/禁用）")

    def _open_dir(self):
        result = self.open_plugin_dir()
        if not result.get('ok'):
            QMessageBox.warning(self, '提示',
                                f"无法打开目录: {result.get('error')}")

    def _install(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择插件文件", "", "Python (*.py)")
        if not paths:
            return
        result = self.install_plugin_files(paths)
        if result.get('errors'):
            QMessageBox.warning(
                self, '部分失败',
                '\n'.join(result['errors'][:10]))
        if result.get('installed'):
            QMessageBox.information(
                self, '完成',
                f"安装 {result['installed']} 个插件；已重新加载 " +
                ', '.join(result.get('loaded') or []))
