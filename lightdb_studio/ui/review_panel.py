"""SQL 审查面板"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QDialog, QFormLayout, QLineEdit, QTextEdit,
    QDialogButtonBox, QMessageBox, QTabWidget, QLabel, QComboBox,
    QSplitter, QPlainTextEdit
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from datetime import datetime

from ..core.review_workflow import ReviewWorkflow, ReviewRequest


class ReviewSubmitDialog(QDialog):
    def __init__(self, sql: str, connection: str = '',
                 database: str = '', parent=None):
        super().__init__(parent)
        self.setWindowTitle("提交 SQL 审查")
        self.resize(640, 520)
        self._build_ui(sql, connection, database)

    def _build_ui(self, sql, conn, db):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("简述变更内容，如: 修复用户表索引")
        form.addRow("标题:", self.title_edit)

        self.conn_edit = QLineEdit(conn)
        form.addRow("连接:", self.conn_edit)

        self.db_edit = QLineEdit(db)
        form.addRow("数据库:", self.db_edit)

        self.reason_edit = QTextEdit()
        self.reason_edit.setMaximumHeight(80)
        self.reason_edit.setPlaceholderText("变更原因 / 影响范围 / 回滚方案")
        form.addRow("说明:", self.reason_edit)
        layout.addLayout(form)

        layout.addWidget(QLabel("SQL 内容:"))
        self.sql_edit = QTextEdit()
        self.sql_edit.setFont(QFont("Consolas", 10))
        self.sql_edit.setPlainText(sql)
        layout.addWidget(self.sql_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("提交")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_data(self):
        return {
            'title': self.title_edit.text().strip() or '未命名请求',
            'sql': self.sql_edit.toPlainText().strip(),
            'connection': self.conn_edit.text().strip(),
            'database': self.db_edit.text().strip(),
            'reason': self.reason_edit.toPlainText().strip(),
        }


class ReviewPanel(QWidget):
    """审查面板"""

    execute_approved = Signal(str)   # 允许执行的 SQL

    def __init__(self, workflow: ReviewWorkflow, conn_getter=None,
                 parent=None):
        super().__init__(parent)
        self.workflow = workflow
        self.conn_getter = conn_getter
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Tab
        self.tabs = QTabWidget()

        # Tab1: 待审查
        pending_w = QWidget()
        pl = QVBoxLayout(pending_w)

        self.pending_table = QTableWidget()
        self.pending_table.setColumnCount(5)
        self.pending_table.setHorizontalHeaderLabels(
            ['ID', '标题', '提交人', '提交时间', '操作'])
        self.pending_table.setColumnWidth(0, 100)
        self.pending_table.setColumnWidth(1, 240)
        self.pending_table.setColumnWidth(2, 100)
        self.pending_table.setColumnWidth(3, 150)
        self.pending_table.horizontalHeader().setStretchLastSection(True)
        self.pending_table.itemSelectionChanged.connect(
            self._on_pending_select)
        pl.addWidget(self.pending_table)

        # SQL 详情
        pl.addWidget(QLabel("SQL 详情:"))
        self.pending_sql = QPlainTextEdit()
        self.pending_sql.setReadOnly(True)
        self.pending_sql.setFont(QFont("Consolas", 10))
        self.pending_sql.setMaximumHeight(120)
        pl.addWidget(self.pending_sql)

        # 操作
        op_row = QHBoxLayout()
        approve_btn = QPushButton("✅ 批准")
        approve_btn.clicked.connect(self._approve)
        op_row.addWidget(approve_btn)

        reject_btn = QPushButton("❌ 拒绝")
        reject_btn.clicked.connect(self._reject)
        op_row.addWidget(reject_btn)

        op_row.addStretch()
        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self.refresh)
        op_row.addWidget(refresh_btn)
        pl.addLayout(op_row)

        self.tabs.addTab(pending_w, "待审查")

        # Tab2: 已批准
        approved_w = QWidget()
        al = QVBoxLayout(approved_w)

        self.approved_table = QTableWidget()
        self.approved_table.setColumnCount(5)
        self.approved_table.setHorizontalHeaderLabels(
            ['ID', '标题', '审核人', '审核时间', '操作'])
        self.approved_table.setColumnWidth(0, 100)
        self.approved_table.setColumnWidth(1, 240)
        self.approved_table.horizontalHeader().setStretchLastSection(True)
        self.approved_table.itemSelectionChanged.connect(
            self._on_approved_select)
        al.addWidget(self.approved_table)

        al.addWidget(QLabel("SQL:"))
        self.approved_sql = QPlainTextEdit()
        self.approved_sql.setReadOnly(True)
        self.approved_sql.setFont(QFont("Consolas", 10))
        self.approved_sql.setMaximumHeight(120)
        al.addWidget(self.approved_sql)

        run_btn = QPushButton("▶ 立即执行")
        run_btn.clicked.connect(self._execute_approved)
        al.addWidget(run_btn)

        self.tabs.addTab(approved_w, "已批准")

        # Tab3: 全部历史
        hist_w = QWidget()
        hl = QVBoxLayout(hist_w)

        self.hist_table = QTableWidget()
        self.hist_table.setColumnCount(6)
        self.hist_table.setHorizontalHeaderLabels(
            ['ID', '标题', '状态', '提交人', '审核人', '时间'])
        self.hist_table.setColumnWidth(0, 100)
        self.hist_table.setColumnWidth(1, 200)
        self.hist_table.setColumnWidth(2, 90)
        self.hist_table.horizontalHeader().setStretchLastSection(True)
        hl.addWidget(self.hist_table)

        self.tabs.addTab(hist_w, "历史")

        layout.addWidget(self.tabs)

    # ==================== 刷新 ====================

    def refresh(self):
        self._refresh_pending()
        self._refresh_approved()
        self._refresh_history()

    def _refresh_pending(self):
        pending = self.workflow.list_all('pending')
        self.pending_table.setRowCount(len(pending))
        for r, req in enumerate(pending):
            self.pending_table.setItem(r, 0, QTableWidgetItem(req.id))
            self.pending_table.setItem(r, 1, QTableWidgetItem(req.title))
            self.pending_table.setItem(r, 2, QTableWidgetItem(req.submitter))
            ts = datetime.fromtimestamp(
                req.submitted_at).strftime('%m-%d %H:%M:%S')
            self.pending_table.setItem(r, 3, QTableWidgetItem(ts))
            self.pending_table.setItem(
                r, 4, QTableWidgetItem(req.connection or '-'))

    def _refresh_approved(self):
        approved = self.workflow.list_all('approved')
        self.approved_table.setRowCount(len(approved))
        for r, req in enumerate(approved):
            self.approved_table.setItem(r, 0, QTableWidgetItem(req.id))
            self.approved_table.setItem(r, 1, QTableWidgetItem(req.title))
            self.approved_table.setItem(r, 2, QTableWidgetItem(req.reviewer))
            ts = datetime.fromtimestamp(
                req.reviewed_at).strftime('%m-%d %H:%M:%S')
            self.approved_table.setItem(r, 3, QTableWidgetItem(ts))
            self.approved_table.setItem(
                r, 4, QTableWidgetItem(req.connection or '-'))

    def _refresh_history(self):
        all_reqs = self.workflow.list_all()
        self.hist_table.setRowCount(len(all_reqs))
        for r, req in enumerate(all_reqs):
            self.hist_table.setItem(r, 0, QTableWidgetItem(req.id))
            self.hist_table.setItem(r, 1, QTableWidgetItem(req.title))

            status_item = QTableWidgetItem(req.status)
            colors = {
                'pending': '#dcdcaa',
                'approved': '#89d185',
                'rejected': '#f48771',
                'executed': '#569cd6',
                'cancelled': '#969696',
            }
            status_item.setForeground(
                QColor(colors.get(req.status, '#d4d4d4')))
            self.hist_table.setItem(r, 2, status_item)

            self.hist_table.setItem(r, 3, QTableWidgetItem(req.submitter))
            self.hist_table.setItem(r, 4, QTableWidgetItem(req.reviewer))

            ts = datetime.fromtimestamp(
                req.submitted_at).strftime('%m-%d %H:%M')
            self.hist_table.setItem(r, 5, QTableWidgetItem(ts))

    # ==================== 交互 ====================

    def _on_pending_select(self):
        r = self.pending_table.currentRow()
        if r < 0:
            return
        req_id = self.pending_table.item(r, 0).text()
        req = self.workflow.get(req_id)
        if req:
            self.pending_sql.setPlainText(
                f"说明: {req.reason}\n\n{req.sql}")

    def _on_approved_select(self):
        r = self.approved_table.currentRow()
        if r < 0:
            return
        req_id = self.approved_table.item(r, 0).text()
        req = self.workflow.get(req_id)
        if req:
            self.approved_sql.setPlainText(req.sql)

    def _approve(self):
        r = self.pending_table.currentRow()
        if r < 0:
            return
        req_id = self.pending_table.item(r, 0).text()

        comment, ok = self._ask_comment("批准意见")
        if not ok:
            return

        if self.workflow.approve(req_id, 'admin', comment):
            QMessageBox.information(self, "完成", "已批准")
            self.refresh()

    def _reject(self):
        r = self.pending_table.currentRow()
        if r < 0:
            return
        req_id = self.pending_table.item(r, 0).text()

        comment, ok = self._ask_comment("拒绝原因")
        if not ok:
            return

        if self.workflow.reject(req_id, 'admin', comment):
            QMessageBox.information(self, "完成", "已拒绝")
            self.refresh()

    def _ask_comment(self, title):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(400, 200)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"{title}:"))
        edit = QTextEdit()
        layout.addWidget(edit)
        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)
        if dlg.exec() == QDialog.Accepted:
            return edit.toPlainText().strip(), True
        return '', False

    def _execute_approved(self):
        r = self.approved_table.currentRow()
        if r < 0:
            return
        req_id = self.approved_table.item(r, 0).text()
        req = self.workflow.get(req_id)
        if not req:
            return

        conn = self.conn_getter() if self.conn_getter else None
        if conn is None:
            QMessageBox.warning(self, "提示", "无可用连接")
            return

        ret = QMessageBox.question(
            self, "确认执行",
            f"将在连接 '{req.connection}' 上执行:\n\n{req.sql[:300]}\n\n"
            "确定吗？")
        if ret != QMessageBox.Yes:
            return

        try:
            up = req.sql.strip().upper().rstrip(';')
            if up.startswith(('SELECT', 'SHOW', 'DESC', 'EXPLAIN', 'WITH')):
                cols, rows = conn.query(req.sql)
                QMessageBox.information(
                    self, "完成", f"查询返回 {len(rows)} 行")
            else:
                rows, err = conn.execute(req.sql)
                if err:
                    raise RuntimeError(err)
                QMessageBox.information(
                    self, "完成", f"影响 {rows} 行")

            self.workflow.mark_executed(req_id, True)
            self.refresh()
            self.execute_approved.emit(req.sql)
        except Exception as e:
            self.workflow.mark_executed(req_id, False, str(e))
            QMessageBox.critical(self, "失败", str(e))
            self.refresh()