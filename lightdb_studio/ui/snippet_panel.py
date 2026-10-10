"""SQL 片段面板"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLineEdit, QPushButton, QMenu, QMessageBox, QInputDialog,
    QDialog, QFormLayout, QTextEdit, QComboBox, QDialogButtonBox,
    QFileDialog, QApplication, QLabel
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from ..core.snippets import SnippetManager, Snippet


class SnippetEditorDialog(QDialog):
    def __init__(self, snippet: Snippet = None, parent=None):
        super().__init__(parent)
        self.snippet = snippet
        self.setWindowTitle("编辑片段" if snippet else "新建片段")
        self.resize(620, 480)
        self._build_ui()
        if snippet:
            self._fill(snippet)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("名称:", self.name_edit)

        self.category_combo = QComboBox()
        self.category_combo.setEditable(True)
        self.category_combo.addItems(['查询', 'DML', 'DDL', '高级', '事务', '通用'])
        form.addRow("分类:", self.category_combo)

        self.desc_edit = QLineEdit()
        form.addRow("描述:", self.desc_edit)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("逗号分隔，如: select,where")
        form.addRow("标签:", self.tags_edit)

        layout.addLayout(form)

        layout.addWidget(QLabel("SQL 内容:"))
        self.sql_edit = QTextEdit()
        self.sql_edit.setFont(QFont("Consolas", 10))
        layout.addWidget(self.sql_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _fill(self, s: Snippet):
        self.name_edit.setText(s.name)
        self.category_combo.setCurrentText(s.category)
        self.desc_edit.setText(s.description)
        self.tags_edit.setText(', '.join(s.tags or []))
        self.sql_edit.setPlainText(s.sql)

    def get_snippet(self) -> Snippet:
        tags = [t.strip() for t in self.tags_edit.text().split(',') if t.strip()]
        return Snippet(
            name=self.name_edit.text().strip(),
            sql=self.sql_edit.toPlainText().strip(),
            category=self.category_combo.currentText().strip() or '通用',
            description=self.desc_edit.text().strip(),
            tags=tags,
        )


class SnippetPanel(QWidget):
    """SQL 片段面板"""

    snippet_activated = Signal(str)     # SQL 内容

    def __init__(self, snippet_mgr: SnippetManager, parent=None):
        super().__init__(parent)
        self.mgr = snippet_mgr
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 搜索 + 新建
        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索片段...")
        self.search_edit.textChanged.connect(self._on_search)
        row.addWidget(self.search_edit)

        new_btn = QPushButton("➕")
        new_btn.setFixedWidth(32)
        new_btn.setToolTip("新建片段")
        new_btn.clicked.connect(self._new_snippet)
        row.addWidget(new_btn)

        import_btn = QPushButton("📥")
        import_btn.setFixedWidth(32)
        import_btn.setToolTip("导入片段")
        import_btn.clicked.connect(self._import_snippets)
        row.addWidget(import_btn)

        export_btn = QPushButton("📤")
        export_btn.setFixedWidth(32)
        export_btn.setToolTip("导出片段")
        export_btn.clicked.connect(self._export_snippets)
        row.addWidget(export_btn)

        layout.addLayout(row)

        # 列表
        self.list_widget = QListWidget()
        self.list_widget.setFont(QFont("Microsoft YaHei", 9))
        self.list_widget.itemDoubleClicked.connect(self._on_double_click)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_menu)
        layout.addWidget(self.list_widget)

    def refresh(self, keyword: str = ''):
        self.list_widget.clear()
        snippets = self.mgr.search(keyword)

        # 按分类分组
        by_cat = {}
        for s in snippets:
            by_cat.setdefault(s.category, []).append(s)

        for cat in sorted(by_cat.keys()):
            # 分类标题
            header = QListWidgetItem(f"📁 {cat}")
            header.setFlags(Qt.ItemIsEnabled)
            header.setBackground(Qt.darkGray)
            header.setForeground(Qt.white)
            self.list_widget.addItem(header)

            for s in by_cat[cat]:
                item = QListWidgetItem(f"   {s.name}")
                item.setData(Qt.UserRole, s.sql)
                item.setToolTip(f"{s.description}\n\n{s.sql[:200]}")
                self.list_widget.addItem(item)

    def _on_search(self, text):
        self.refresh(text)

    def _on_double_click(self, item):
        sql = item.data(Qt.UserRole)
        if sql:
            self.snippet_activated.emit(sql)

    def _new_snippet(self):
        dlg = SnippetEditorDialog(parent=self)
        if dlg.exec() == SnippetEditorDialog.Accepted:
            s = dlg.get_snippet()
            if not s.name:
                QMessageBox.warning(self, "提示", "请填写名称")
                return
            self.mgr.add(s)
            self.refresh()

    def _edit_snippet(self, item):
        sql = item.data(Qt.UserRole)
        # 找匹配
        for i, s in enumerate(self.mgr.user_snippets):
            if s.sql == sql:
                dlg = SnippetEditorDialog(s, self)
                if dlg.exec() == SnippetEditorDialog.Accepted:
                    self.mgr.update(i, dlg.get_snippet())
                    self.refresh()
                return
        QMessageBox.information(self, "提示", "内置片段无法编辑，请另存为新片段")

    def _delete_snippet(self, item):
        sql = item.data(Qt.UserRole)
        for s in self.mgr.user_snippets:
            if s.sql == sql:
                ret = QMessageBox.question(
                    self, "确认", f"删除片段 '{s.name}'？")
                if ret == QMessageBox.Yes:
                    self.mgr.remove(s)
                    self.refresh()
                return
        QMessageBox.information(self, "提示", "内置片段无法删除")

    def _show_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if not item:
            return
        sql = item.data(Qt.UserRole)
        if not sql:
            return

        menu = QMenu(self)
        a_insert = menu.addAction("插入到编辑器")
        a_copy = menu.addAction("复制到剪贴板")
        menu.addSeparator()
        a_edit = menu.addAction("编辑")
        a_team = menu.addAction("保存为团队片段")
        a_delete = menu.addAction("删除")

        action = menu.exec(self.list_widget.mapToGlobal(pos))

        if action == a_insert:
            self.snippet_activated.emit(sql)
        elif action == a_copy:
            QApplication.clipboard().setText(sql)
        elif action == a_edit:
            self._edit_snippet(item)
        elif action == a_team:
            self._save_as_team(item)
        elif action == a_delete:
            self._delete_snippet(item)

    def _save_as_team(self, item):
        sql = item.data(Qt.UserRole)
        name, ok = QInputDialog.getText(
            self, "团队片段", "团队名称:",
            text="默认团队")
        if not ok or not name.strip():
            return
        # 找完整 Snippet
        for s in self.mgr.get_all():
            if s.sql == sql:
                self.mgr.save_as_team_snippet(s, name.strip())
                QMessageBox.information(
                    self, "完成",
                    f"已保存到团队片段: {name}")
                self.refresh()
                return

    def _import_snippets(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入片段", "", "JSON (*.json)")
        if not path:
            return
        try:
            n = self.mgr.import_from_file(path)
            QMessageBox.information(self, "完成", f"导入 {n} 个片段")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def _export_snippets(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出片段", "snippets.json", "JSON (*.json)")
        if not path:
            return
        try:
            self.mgr.export_to_file(path)
            QMessageBox.information(self, "完成", f"已导出到:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))