"""协作面板 - 会话共享 + 评论"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QTextEdit, QInputDialog, QMessageBox,
    QDialog, QFormLayout, QLineEdit, QCheckBox, QDialogButtonBox,
    QSplitter, QMenu, QFileDialog
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QAction
from datetime import datetime

from ..core.collaboration import CollaborationStore


class ShareSessionDialog(QDialog):
    def __init__(self, conn_name='', db='', sql='', parent=None):
        super().__init__(parent)
        self.setWindowTitle("共享会话")
        self.resize(560, 400)
        self._build_ui(conn_name, db, sql)

    def _build_ui(self, conn, db, sql):
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("如：2025 Q1 数据修复")
        form.addRow("会话名称:", self.name_edit)

        self.conn_edit = QLineEdit(conn)
        form.addRow("连接:", self.conn_edit)

        self.db_edit = QLineEdit(db)
        form.addRow("数据库:", self.db_edit)

        self.owner_edit = QLineEdit("me")
        form.addRow("拥有者:", self.owner_edit)

        self.readonly_check = QCheckBox("只读（协作者无法修改 SQL）")
        form.addRow("", self.readonly_check)

        layout.addLayout(form)

        layout.addWidget(QLabel("初始 SQL:"))
        self.sql_edit = QTextEdit()
        self.sql_edit.setFont(QFont("Consolas", 10))
        self.sql_edit.setPlainText(sql)
        self.sql_edit.setMaximumHeight(120)
        layout.addWidget(self.sql_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("创建")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_data(self):
        return {
            'name': self.name_edit.text().strip() or '未命名',
            'connection': self.conn_edit.text().strip(),
            'database': self.db_edit.text().strip(),
            'sql': self.sql_edit.toPlainText().strip(),
            'owner': self.owner_edit.text().strip() or 'me',
            'readonly': self.readonly_check.isChecked(),
        }


class CollaborationPanel(QWidget):
    session_selected = Signal(object)

    def __init__(self, store: CollaborationStore, parent=None):
        super().__init__(parent)
        self.store = store
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # 顶部操作
        top = QHBoxLayout()

        new_btn = QPushButton("➕ 新建共享")
        new_btn.clicked.connect(self._new_session)
        top.addWidget(new_btn)

        import_btn = QPushButton("📥 导入")
        import_btn.clicked.connect(self._import_session)
        top.addWidget(import_btn)

        top.addStretch()

        refresh_btn = QPushButton("🔄")
        refresh_btn.setMaximumWidth(32)
        refresh_btn.clicked.connect(self.refresh)
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        # 列表
        self.session_list = QListWidget()
        self.session_list.itemSelectionChanged.connect(self._on_select)
        self.session_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._show_menu)
        layout.addWidget(self.session_list)

        # 详情
        splitter = QSplitter(Qt.Vertical)

        detail_w = QWidget()
        dl = QVBoxLayout(detail_w)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(QLabel("SQL:"))
        self.sql_view = QTextEdit()
        self.sql_view.setReadOnly(True)
        self.sql_view.setFont(QFont("Consolas", 10))
        self.sql_view.setMaximumHeight(100)
        dl.addWidget(self.sql_view)

        # 评论列表
        dl.addWidget(QLabel("评论:"))
        self.comment_list = QListWidget()
        dl.addWidget(self.comment_list)

        # 评论输入
        comment_row = QHBoxLayout()
        self.comment_edit = QLineEdit()
        self.comment_edit.setPlaceholderText("添加评论... @提及用 @name")
        comment_row.addWidget(self.comment_edit)
        add_comment_btn = QPushButton("发送")
        add_comment_btn.clicked.connect(self._add_comment)
        comment_row.addWidget(add_comment_btn)
        dl.addLayout(comment_row)

        splitter.addWidget(detail_w)
        layout.addWidget(splitter, 1)

    def refresh(self):
        self.session_list.clear()
        for s in self.store.list_all():
            n_unresolved = sum(1 for c in s.comments if not c.resolved)
            text = f"📁 {s.name}"
            if n_unresolved:
                text += f"  💬 {n_unresolved}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, s.id)
            item.setToolTip(
                f"连接: {s.connection}\n"
                f"数据库: {s.database}\n"
                f"拥有者: {s.owner}")
            self.session_list.addItem(item)

    def _on_select(self):
        item = self.session_list.currentItem()
        if not item:
            return
        sid = item.data(Qt.UserRole)
        s = self.store.get(sid)
        if not s:
            return
        self.sql_view.setPlainText(s.sql)
        self._refresh_comments(s)
        self.session_selected.emit(s)

    def _refresh_comments(self, session):
        self.comment_list.clear()
        for c in session.comments:
            ts = datetime.fromtimestamp(c.created_at).strftime(
                '%m-%d %H:%M')
            icon = '✅' if c.resolved else '💬'
            text = f"{icon} [{ts}] {c.author}: {c.content}"
            if c.mentions:
                text += f"  (@{', @'.join(c.mentions)})"
            item = QListWidgetItem(text)
            if c.resolved:
                item.setForeground(QColor("#969696"))
            else:
                item.setForeground(QColor("#dcdcaa"))
            item.setData(Qt.UserRole, c.id)
            # 显示回复
            self.comment_list.addItem(item)
            for r in c.replies:
                r_ts = datetime.fromtimestamp(r['ts']).strftime('%m-%d %H:%M')
                r_item = QListWidgetItem(
                    f"    ↳ [{r_ts}] {r['author']}: {r['content']}")
                r_item.setForeground(QColor("#569cd6"))
                self.comment_list.addItem(r_item)

    def _new_session(self):
        # 从主窗口获取当前信息
        win = self.window()
        conn_name = ''
        db = ''
        sql = ''
        if hasattr(win, '_get_current_conn'):
            conn = win._get_current_conn()
            if conn:
                conn_name = conn.config.name
                db = conn.config.database or ''
        if hasattr(win, '_current_widgets'):
            editor, _ = win._current_widgets()
            if editor:
                sql = editor.toPlainText().strip()

        dlg = ShareSessionDialog(conn_name, db, sql, self)
        if dlg.exec() == ShareSessionDialog.Accepted:
            data = dlg.get_data()
            self.store.create_session(**data)
            self.refresh()

    def _import_session(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入会话", "", "JSON (*.json)")
        if not path:
            return
        try:
            self.store.import_session(path)
            self.refresh()
            QMessageBox.information(self, "完成", "已导入")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))

    def _show_menu(self, pos):
        item = self.session_list.itemAt(pos)
        if not item:
            return
        sid = item.data(Qt.UserRole)
        s = self.store.get(sid)
        if not s:
            return

        menu = QMenu(self)
        a_export = menu.addAction("📤 导出会话")
        a_delete = menu.addAction("🗑 删除")
        action = menu.exec(self.session_list.mapToGlobal(pos))

        if action == a_export:
            path, _ = QFileDialog.getSaveFileName(
                self, "导出", f"{s.name}.json", "JSON (*.json)")
            if path:
                self.store.export_session(sid, path)
                QMessageBox.information(self, "完成", f"已导出: {path}")
        elif action == a_delete:
            ret = QMessageBox.question(
                self, "确认", f"删除会话 '{s.name}'？")
            if ret == QMessageBox.Yes:
                self.store.delete(sid)
                self.refresh()

    def _add_comment(self):
        item = self.session_list.currentItem()
        if not item:
            return
        sid = item.data(Qt.UserRole)
        content = self.comment_edit.text().strip()
        if not content:
            return

        # 提取 @提及
        import re
        mentions = re.findall(r'@(\w+)', content)

        self.store.add_comment(sid, target_id='sql',
                                content=content, mentions=mentions)
        self.comment_edit.clear()
        s = self.store.get(sid)
        self._refresh_comments(s)