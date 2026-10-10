"""SQL 性能分析视图"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
    QPushButton, QTabWidget, QTableWidget, QTableWidgetItem,
    QGroupBox, QProgressBar, QMessageBox
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor

from ..core.sql_profiler import SQLProfiler


class ProfilerView(QWidget):
    """SQL 性能分析面板"""

    def __init__(self, conn_getter, parent=None):
        super().__init__(parent)
        self.conn_getter = conn_getter
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        # 顶部
        top = QHBoxLayout()
        top.addWidget(QLabel("SQL:"))
        analyze_btn = QPushButton("🔍 分析当前 SQL")
        analyze_btn.clicked.connect(self._analyze_current)
        top.addStretch()
        top.addWidget(analyze_btn)
        layout.addLayout(top)

        self.sql_edit = QTextEdit()
        self.sql_edit.setFont(QFont("Consolas", 10))
        self.sql_edit.setMaximumHeight(100)
        self.sql_edit.setPlaceholderText("粘贴或输入 SQL 进行分析...")
        layout.addWidget(self.sql_edit)

        # 评分区
        score_group = QGroupBox("性能评分")
        sl = QHBoxLayout(score_group)

        self.score_bar = QProgressBar()
        self.score_bar.setRange(0, 100)
        self.score_bar.setValue(0)
        self.score_bar.setTextVisible(True)
        sl.addWidget(self.score_bar, 1)

        self.score_label = QLabel("--")
        self.score_label.setFont(QFont("Microsoft YaHei", 20, QFont.Bold))
        sl.addWidget(self.score_label)
        layout.addWidget(score_group)

        # Tab
        self.tabs = QTabWidget()

        # 概览
        overview_w = QWidget()
        ol = QVBoxLayout(overview_w)
        self.overview = QTextEdit()
        self.overview.setReadOnly(True)
        self.overview.setFont(QFont("Consolas", 10))
        ol.addWidget(self.overview)
        self.tabs.addTab(overview_w, "概览")

        # 执行计划
        plan_w = QWidget()
        pl = QVBoxLayout(plan_w)
        self.plan_view = QTextEdit()
        self.plan_view.setReadOnly(True)
        self.plan_view.setFont(QFont("Consolas", 9))
        pl.addWidget(self.plan_view)
        self.tabs.addTab(plan_w, "执行计划")

        # 建议
        sugg_w = QWidget()
        sug = QVBoxLayout(sugg_w)
        self.suggestions = QTextEdit()
        self.suggestions.setReadOnly(True)
        self.suggestions.setFont(QFont("Microsoft YaHei", 10))
        sug.addWidget(self.suggestions)
        self.tabs.addTab(sugg_w, "优化建议")

        layout.addWidget(self.tabs, 1)

        # 底部
        bottom = QHBoxLayout()
        copy_btn = QPushButton("📋 复制建议")
        copy_btn.clicked.connect(self._copy_suggestions)
        bottom.addWidget(copy_btn)
        bottom.addStretch()
        layout.addLayout(bottom)

    def _analyze_current(self):
        win = self.window()
        if hasattr(win, '_current_widgets'):
            editor, _ = win._current_widgets()
            if editor:
                self.sql_edit.setPlainText(editor.toPlainText())
        self._analyze()

    def analyze_sql(self, sql: str):
        self.sql_edit.setPlainText(sql)
        self._analyze()

    def _analyze(self):
        sql = self.sql_edit.toPlainText().strip().rstrip(';')
        if not sql:
            QMessageBox.information(self, "提示", "请输入 SQL")
            return

        conn = self.conn_getter()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择连接")
            return

        self.score_bar.setValue(0)
        self.score_label.setText("...")

        try:
            profiler = SQLProfiler(conn)
            result = profiler.profile(sql, execute=True)
            self._display(result)
        except Exception as e:
            QMessageBox.critical(self, "分析失败", str(e))

    def _display(self, result):
        # 评分
        score = result.score
        self.score_bar.setValue(score)
        if score >= 80:
            color = '#89d185'
        elif score >= 60:
            color = '#dcdcaa'
        else:
            color = '#f48771'
        self.score_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background: {color}; }}")
        self.score_label.setText(f"{score}")
        self.score_label.setStyleSheet(f"color: {color};")

        # 概览
        lines = [
            f"执行耗时: {result.elapsed_ms:.2f} ms",
            f"返回行数: {result.rows_returned}",
            f"扫描行数: {result.rows_scanned or '-'}",
            f"扫描类型: {result.scan_type or '-'}",
            f"使用索引: {result.index_used or '(无)'}",
        ]
        if result.warnings:
            lines.append("")
            lines.append("⚠️ 警告:")
            for w in result.warnings:
                lines.append(f"  - {w}")
        self.overview.setPlainText('\n'.join(lines))

        # 执行计划
        if result.plan:
            self.plan_view.setPlainText('\n'.join(result.plan))
        else:
            self.plan_view.setPlainText("(无执行计划)")

        # 建议
        if result.suggestions:
            html_lines = []
            for s in result.suggestions:
                if s.startswith('⚠️') or '警告' in s:
                    html_lines.append(
                        f"<p style='color:#dcdcaa'>⚠️ {s}</p>")
                else:
                    html_lines.append(
                        f"<p style='color:#89d185'>💡 {s}</p>")
            self.suggestions.setHtml(''.join(html_lines))
        else:
            self.suggestions.setPlainText("✅ 未发现明显问题")

    def _copy_suggestions(self):
        text = self.suggestions.toPlainText()
        if text:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(text)
            QMessageBox.information(self, "完成", "已复制")