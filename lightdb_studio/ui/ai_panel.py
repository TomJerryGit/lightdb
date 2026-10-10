"""AI 助手面板"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QLabel, QComboBox, QTabWidget, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QSplitter
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor

from ..core.ai_assist import SQLAssistant


class AIPanel(QWidget):

    sql_generated = Signal(str)   # 生成的 SQL
    explain_requested = Signal()  # 请求解释当前 SQL

    def __init__(self, schema_provider=None, parent=None):
        super().__init__(parent)
        self.assistant = SQLAssistant(schema_provider)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Tab：自然语言 / SQL 解释 / 优化建议
        self.tabs = QTabWidget()

        # Tab1: 自然语言转 SQL
        nl_widget = QWidget()
        nl_layout = QVBoxLayout(nl_widget)

        nl_layout.addWidget(QLabel(
            "<b>用中文描述你想做什么</b><br>"
            "例如: 查询 users 表前 10 行<br>"
            "例如: 统计 orders 表有多少条"))
        self.nl_input = QTextEdit()
        self.nl_input.setMaximumHeight(80)
        self.nl_input.setPlaceholderText(
            "查询 users 表里 age 大于 20 的用户，按 age 降序")
        nl_layout.addWidget(self.nl_input)

        row = QHBoxLayout()
        gen_btn = QPushButton("✨ 生成 SQL")
        gen_btn.clicked.connect(self._gen_sql)
        row.addWidget(gen_btn)

        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(lambda: self.nl_input.clear())
        row.addWidget(clear_btn)
        row.addStretch()
        nl_layout.addLayout(row)

        nl_layout.addWidget(QLabel("生成的 SQL:"))
        self.sql_output = QTextEdit()
        self.sql_output.setFont(QFont("Consolas", 10))
        self.sql_output.setStyleSheet(
            "background: #1e1e1e; color: #ce9178;")
        nl_layout.addWidget(self.sql_output)

        btn_row = QHBoxLayout()
        insert_btn = QPushButton("📝 插入到编辑器")
        insert_btn.clicked.connect(self._insert_sql)
        btn_row.addWidget(insert_btn)
        run_btn = QPushButton("▶ 直接执行")
        run_btn.clicked.connect(self._run_sql)
        btn_row.addWidget(run_btn)
        btn_row.addStretch()
        nl_layout.addLayout(btn_row)

        self.tabs.addTab(nl_widget, "自然语言")

        # Tab2: SQL 解释
        ex_widget = QWidget()
        ex_layout = QVBoxLayout(ex_widget)

        ex_layout.addWidget(QLabel(
            "<b>SQL 中文解释</b><br>"
            "从编辑器复制 SQL 到下面，或点击下方按钮："))

        self.explain_input = QTextEdit()
        self.explain_input.setFont(QFont("Consolas", 10))
        self.explain_input.setPlaceholderText(
            "SELECT * FROM users WHERE age > 20 ORDER BY age DESC LIMIT 10")
        self.explain_input.setMaximumHeight(120)
        ex_layout.addWidget(self.explain_input)

        btn_row2 = QHBoxLayout()
        use_editor_btn = QPushButton("从编辑器拉取")
        use_editor_btn.clicked.connect(self.explain_requested.emit)
        btn_row2.addWidget(use_editor_btn)

        explain_btn = QPushButton("💡 解释")
        explain_btn.clicked.connect(self._explain_sql)
        btn_row2.addWidget(explain_btn)
        btn_row2.addStretch()
        ex_layout.addLayout(btn_row2)

        self.explain_output = QTextEdit()
        self.explain_output.setReadOnly(True)
        self.explain_output.setFont(QFont("Microsoft YaHei", 10))
        ex_layout.addWidget(self.explain_output)

        self.tabs.addTab(ex_widget, "SQL 解释")

        # Tab3: 优化建议
        opt_widget = QWidget()
        opt_layout = QVBoxLayout(opt_widget)

        opt_layout.addWidget(QLabel(
            "<b>SQL 优化建议</b><br>"
            "基于规则检查潜在的 SQL 问题"))

        self.opt_input = QTextEdit()
        self.opt_input.setFont(QFont("Consolas", 10))
        self.opt_input.setMaximumHeight(120)
        opt_layout.addWidget(self.opt_input)

        opt_btn = QPushButton("🔧 分析")
        opt_btn.clicked.connect(self._optimize)
        opt_layout.addWidget(opt_btn)

        self.opt_output = QListWidget()
        self.opt_output.setFont(QFont("Microsoft YaHei", 10))
        opt_layout.addWidget(self.opt_output)

        self.tabs.addTab(opt_widget, "优化建议")

        layout.addWidget(self.tabs)

        # 底部说明
        tip = QLabel(
            "ℹ️ 基于规则引擎，无需网络。可对接 LLM API 获得更强能力。")
        tip.setStyleSheet("color: #666666; font-size: 11px; padding: 4px;")
        tip.setWordWrap(True)
        layout.addWidget(tip)

    def _gen_sql(self):
        text = self.nl_input.toPlainText().strip()
        if not text:
            return
        sql = self.assistant.nl2sql(text)
        self.sql_output.setPlainText(sql)

    def _insert_sql(self):
        sql = self.sql_output.toPlainText().strip()
        if sql:
            self.sql_generated.emit(sql)

    def _run_sql(self):
        sql = self.sql_output.toPlainText().strip()
        if sql:
            # 通过主窗口执行
            self.sql_generated.emit(sql)

    def _explain_sql(self):
        sql = self.explain_input.toPlainText().strip()
        if not sql:
            return
        explanation = self.assistant.explain_sql(sql)
        # 简单 Markdown 渲染
        html = explanation.replace('**', '').replace('\n', '<br>')
        # 保留 markdown 强调的替换
        import re
        html = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', explanation)
        html = html.replace('\n', '<br>')
        self.explain_output.setHtml(
            f"<div style='color:#d4d4d4;'>{html}</div>")

    def _optimize(self):
        sql = self.opt_input.toPlainText().strip()
        if not sql:
            return
        self.opt_output.clear()
        suggestions = self.assistant.optimize_suggestions(sql)
        for s in suggestions:
            item = QListWidgetItem(s)
            item.setFont(QFont("Microsoft YaHei", 10))
            if s.startswith('🚨'):
                item.setForeground(QColor("#f48771"))
            elif s.startswith('⚠️'):
                item.setForeground(QColor("#dcdcaa"))
            elif s.startswith('✅'):
                item.setForeground(QColor("#89d185"))
            self.opt_output.addItem(item)

    def set_explain_text(self, sql):
        """从编辑器拉取 SQL 到解释区"""
        self.explain_input.setPlainText(sql)

    def set_opt_text(self, sql):
        self.opt_input.setPlainText(sql)

    def refresh_schema(self):
        """刷新 schema 缓存"""
        pass  # SQLAssistant 按需拉取