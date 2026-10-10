"""查询历史面板"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem, QLineEdit,
    QHBoxLayout, QPushButton, QMenu, QMessageBox, QApplication
)
from PySide6.QtCore import Qt, Signal
from datetime import datetime


class QueryHistoryPanel(QWidget):

    sql_selected = Signal(str)

    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.history = history
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索历史...")
        self.search_edit.textChanged.connect(self._on_search)
        row.addWidget(self.search_edit)

        clear_btn = QPushButton("清空")
        clear_btn.setMaximumWidth(60)
        clear_btn.clicked.connect(self._clear)
        row.addWidget(clear_btn)
        layout.addLayout(row)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._on_double_click)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_menu)
        layout.addWidget(self.list_widget)

    def refresh(self):
        self.list_widget.clear()
        for entry in self.history.recent(200):
            self._add_entry(entry)

    def _add_entry(self, entry):
        ts = datetime.fromtimestamp(entry['ts']).strftime('%m-%d %H:%M:%S')
        sql = entry['sql'].replace('\n', ' ')
        if len(sql) > 70:
            sql = sql[:70] + '...'
        prefix = "✓" if entry.get('success') else "✗"
        text = f"[{ts}] {prefix} {sql}"
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, entry['sql'])
        if not entry.get('success'):
            item.setForeground(Qt.red)
        self.list_widget.addItem(item)

    def _on_search(self, text):
        self.list_widget.clear()
        results = self.history.search(text) if text else self.history.recent(200)
        for entry in list(results)[-200:][::-1]:
            self._add_entry(entry)

    def _on_double_click(self, item):
        sql = item.data(Qt.UserRole)
        if sql:
            self.sql_selected.emit(sql)

    def _show_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if not item:
            return
        sql = item.data(Qt.UserRole)
        menu = QMenu(self)
        act_insert = menu.addAction("插入到编辑器")
        act_fav = menu.addAction("收藏")
        act_copy = menu.addAction("复制")
        action = menu.exec(self.list_widget.mapToGlobal(pos))

        if action == act_insert:
            self.sql_selected.emit(sql)
        elif action == act_fav:
            self.history.add_favorite(sql)
            QMessageBox.information(self, "已收藏", "已添加到收藏夹")
        elif action == act_copy:
            QApplication.clipboard().setText(sql)

    def _clear(self):
        ret = QMessageBox.question(self, "确认", "清空所有历史？")
        if ret == QMessageBox.Yes:
            self.history.clear()
            self.refresh()