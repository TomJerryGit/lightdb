"""AI Agent 对话面板"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QLabel, QComboBox, QTabWidget, QListWidget, QListWidgetItem,
    QProgressDialog, QMessageBox, QApplication
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor

from ..core.ai_agent import AIAgent, DiagnosisReport


class AgentChatPanel(QWidget):
    """AI Agent 面板"""

    sql_generated = Signal(str)

    def __init__(self, agent: AIAgent, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.messages = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # 顶部
        top = QHBoxLayout()

        self.status_label = QLabel("Agent 就绪")
        self.status_label.setStyleSheet("color: #89d185;")
        top.addWidget(self.status_label)
        top.addStretch()

        clear_btn = QPushButton("清空对话")
        clear_btn.setMaximumWidth(80)
        clear_btn.clicked.connect(self._clear)
        top.addWidget(clear_btn)
        layout.addLayout(top)

        # Tab
        self.tabs = QTabWidget()

        # 对话
        chat_w = QWidget()
        cl = QVBoxLayout(chat_w)
        cl.setContentsMargins(0, 0, 0, 0)

        self.chat_view = QTextEdit()
        self.chat_view.setReadOnly(True)
        self.chat_view.setFont(QFont("Microsoft YaHei", 10))
        self.chat_view.setStyleSheet(
            "QTextEdit { background: #1e1e1e; color: #d4d4d4; "
            "border: 1px solid #3c3c3c; padding: 8px; }")
        cl.addWidget(self.chat_view, 1)

        input_row = QHBoxLayout()
        self.input_edit = QTextEdit()
        self.input_edit.setPlaceholderText(
            "问我任何数据库运维问题...\n"
            "例如：为什么 users 查询慢？orders 表加什么索引？\n"
            "Ctrl+Enter 发送")
        self.input_edit.setMaximumHeight(80)
        self.input_edit.keyPressEvent = self._wrap_key(
            self.input_edit.keyPressEvent)
        input_row.addWidget(self.input_edit, 1)

        send_btn = QPushButton("发送")
        send_btn.setMinimumHeight(80)
        send_btn.clicked.connect(self._send)
        input_row.addWidget(send_btn)
        cl.addLayout(input_row)

        self.tabs.addTab(chat_w, "对话")

        # 自主诊断
        diag_w = QWidget()
        dl = QVBoxLayout(diag_w)

        diag_top = QHBoxLayout()
        diag_top.addWidget(QLabel("诊断范围:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItems(
            ['完整诊断', '仅性能', '仅安全', '仅健康度'])
        diag_top.addWidget(self.scope_combo)

        run_diag_btn = QPushButton("🔍 开始诊断")
        run_diag_btn.clicked.connect(self._run_diagnosis)
        diag_top.addWidget(run_diag_btn)
        diag_top.addStretch()
        dl.addLayout(diag_top)

        self.diag_view = QTextEdit()
        self.diag_view.setReadOnly(True)
        self.diag_view.setFont(QFont("Microsoft YaHei", 10))
        self.diag_view.setStyleSheet(
            "QTextEdit { background: #1e1e1e; color: #d4d4d4; "
            "border: 1px solid #3c3c3c; padding: 8px; }")
        dl.addWidget(self.diag_view, 1)

        self.tabs.addTab(diag_w, "自主诊断")

        # 索引推荐
        rec_w = QWidget()
        rl = QVBoxLayout(rec_w)

        rec_top = QHBoxLayout()
        rec_top.addWidget(QLabel("表名:"))
        self.table_edit = QLineEdit()
        self.table_edit.setPlaceholderText("输入表名")
        rec_top.addWidget(self.table_edit)

        rec_btn = QPushButton("💡 推荐索引")
        rec_btn.clicked.connect(self._recommend)
        rec_top.addWidget(rec_btn)
        rec_top.addStretch()
        rl.addLayout(rec_top)

        self.rec_list = QListWidget()
        self.rec_list.setFont(QFont("Consolas", 10))
        self.rec_list.itemDoubleClicked.connect(self._insert_rec)
        rl.addWidget(self.rec_list)

        self.tabs.addTab(rec_w, "索引推荐")

        layout.addWidget(self.tabs)

    def _wrap_key(self, original):
        def new(event):
            if (event.key() == Qt.Key_Return and
                    event.modifiers() & Qt.ControlModifier):
                self._send()
                return
            original(event)
        return new

    # ==================== 对话 ====================

    def _send(self):
        text = self.input_edit.toPlainText().strip()
        if not text:
            return

        self.input_edit.clear()
        self.messages.append({'role': 'user', 'content': text})
        self._append('你', text, '#569cd6')
        self._append('Agent', '思考中...', '#89d185')

        def on_done(response):
            self.messages.append(
                {'role': 'assistant', 'content': response})
            self._rewrite()
            # 尝试提取 SQL
            sql = self._extract_sql(response)
            if sql:
                self.sql_generated.emit(sql)

        def on_error(err):
            self._append('Agent', f'❌ 错误: {err}', '#f48771')

        self.agent.ask(text, on_done, on_error)

    def _append(self, role, content, color):
        html = f"""
        <div style="margin: 8px 0;">
          <div style="color: {color}; font-weight: bold;">{role}:</div>
          <div style="margin: 4px 0 0 0; white-space: pre-wrap;">
            {self._escape(content)}
          </div>
        </div>
        """
        self.chat_view.append(html)
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _escape(self, s):
        return (s.replace('&', '&amp;')
                 .replace('<', '&lt;')
                 .replace('>', '&gt;'))

    def _rewrite(self):
        self.chat_view.clear()
        for m in self.messages:
            if m['role'] == 'user':
                self._append('你', m['content'], '#569cd6')
            else:
                self._append('Agent', m['content'], '#89d185')

    def _clear(self):
        self.messages = []
        self.chat_view.clear()

    def _extract_sql(self, text):
        import re
        m = re.search(r'```(?:sql)?\s*(.+?)```',
                      text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return ''

    # ==================== 诊断 ====================

    def _run_diagnosis(self):
        scope_map = {
            '完整诊断': 'full',
            '仅性能': 'performance',
            '仅安全': 'security',
            '仅健康度': 'health',
        }
        scope = scope_map[self.scope_combo.currentText()]

        self.diag_view.clear()
        self.diag_view.append("🔍 正在诊断...\n")

        try:
            report = self.agent.diagnose(scope)
            self._display_diagnosis(report)
        except Exception as e:
            self.diag_view.append(f"❌ 诊断失败: {e}")

    def _display_diagnosis(self, report: DiagnosisReport):
        lines = []
        severity_icon = {
            'info': '🔵', 'warning': '🟡', 'critical': '🔴',
        }.get(report.severity, '⚪')

        lines.append(f"# {severity_icon} {report.title}")
        lines.append("")
        lines.append(f"**摘要**: {report.summary}")
        lines.append("")

        if report.findings:
            lines.append("## 发现")
            lines.append("")
            for f in report.findings:
                icon = {
                    'info': '🔵', 'warning': '🟡', 'critical': '🔴',
                }.get(f['level'], '⚪')
                lines.append(f"### {icon} {f['message']}")
                if f.get('detail'):
                    lines.append(f"> {f['detail']}")
                lines.append("")

        if report.recommendations:
            lines.append("## 建议")
            lines.append("")
            for r in report.recommendations:
                lines.append(f"- **{r['title']}**: {r['description']}")
            lines.append("")

        if report.ai_analysis:
            lines.append("## AI 分析")
            lines.append("")
            lines.append(report.ai_analysis)

        text = '\n'.join(lines)
        self.diag_view.setPlainText(text)

    # ==================== 索引推荐 ====================

    def _recommend(self):
        table = self.table_edit.text().strip()
        if not table:
            QMessageBox.information(self, "提示", "请输入表名")
            return

        self.rec_list.clear()
        recs = self.agent.recommend_indexes(table)
        if not recs:
            self.rec_list.addItem(
                "未找到足够数据。请先执行一些查询以收集使用情况。")
            return

        for r in recs:
            text = (f"列: {r['column']} (评分 {r['score']})\n"
                    f"SQL: {r['sql']}\n"
                    f"原因: {r['reason']}")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, r['sql'])
            self.rec_list.addItem(item)

        # 分区建议
        parts = self.agent.recommend_partitioning(table)
        for p in parts:
            text = f"【分区建议】\n{p['sql']}\n原因: {p['reason']}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, p['sql'])
            self.rec_list.addItem(item)

    def _insert_rec(self, item):
        sql = item.data(Qt.UserRole)
        if sql:
            self.sql_generated.emit(sql)
            QApplication.clipboard().setText(sql)