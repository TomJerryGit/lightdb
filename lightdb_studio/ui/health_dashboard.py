"""数据库健康度面板"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QWidget,
    QProgressBar, QTextEdit, QFileDialog, QMessageBox, QGroupBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont

from ..core.health_score import HealthScorer


class HealthDashboard(QDialog):
    def __init__(self, conn_getter, parent=None):
        super().__init__(parent)
        self.conn_getter = conn_getter
        self.report = None
        self.setWindowTitle("数据库健康度")
        self.resize(940, 680)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 顶部
        top = QHBoxLayout()
        run_btn = QPushButton("🔍 开始体检")
        run_btn.clicked.connect(self._run)
        top.addWidget(run_btn)

        export_btn = QPushButton("📤 导出报告")
        export_btn.clicked.connect(self._export)
        top.addWidget(export_btn)
        top.addStretch()
        layout.addLayout(top)

        # 总分
        score_group = QGroupBox("综合得分")
        sg = QHBoxLayout(score_group)

        self.total_score_bar = QProgressBar()
        self.total_score_bar.setRange(0, 100)
        self.total_score_bar.setValue(0)
        sg.addWidget(self.total_score_bar, 1)

        self.total_score_label = QLabel("--")
        self.total_score_label.setFont(
            QFont("Microsoft YaHei", 28, QFont.Bold))
        sg.addWidget(self.total_score_label)
        layout.addWidget(score_group)

        # 分类得分
        cat_group = QGroupBox("分类得分")
        cg = QHBoxLayout(cat_group)
        self.cat_labels = {}
        for key in ('design', 'index', 'data', 'size'):
            w = QWidget()
            wl = QVBoxLayout(w)
            wl.setContentsMargins(8, 2, 8, 2)
            wl.setSpacing(0)
            title = {
                'design': '表设计', 'index': '索引',
                'data': '数据', 'size': '规模'
            }[key]
            lbl = QLabel(title)
            lbl.setStyleSheet("color: #969696; font-size: 11px;")
            lbl.setAlignment(Qt.AlignCenter)
            val = QLabel("--")
            val.setStyleSheet(
                "font-size: 18px; font-weight: bold;")
            val.setAlignment(Qt.AlignCenter)
            wl.addWidget(lbl)
            wl.addWidget(val)
            cg.addWidget(w)
            self.cat_labels[key] = val
        layout.addWidget(cat_group)

        # Tab
        self.tabs = QTabWidget()

        # 问题列表
        issues_w = QWidget()
        il = QVBoxLayout(issues_w)
        self.issue_table = QTableWidget()
        self.issue_table.setColumnCount(5)
        self.issue_table.setHorizontalHeaderLabels(
            ['严重度', '分类', '表', '问题', '建议'])
        self.issue_table.setColumnWidth(0, 70)
        self.issue_table.setColumnWidth(1, 80)
        self.issue_table.setColumnWidth(2, 130)
        self.issue_table.setColumnWidth(3, 300)
        self.issue_table.horizontalHeader().setStretchLastSection(True)
        il.addWidget(self.issue_table)
        self.tabs.addTab(issues_w, "问题列表")

        # Markdown 报告
        report_w = QWidget()
        rl = QVBoxLayout(report_w)
        self.report_view = QTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setFont(QFont("Consolas", 9))
        rl.addWidget(self.report_view)
        self.tabs.addTab(report_w, "报告")

        layout.addWidget(self.tabs)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)

    def _run(self):
        conn = self.conn_getter()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择连接")
            return

        try:
            scorer = HealthScorer(conn)
            self.report = scorer.analyze()
            self._display()
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))

    def _display(self):
        r = self.report

        # 总分
        score = r.score
        self.total_score_bar.setValue(score)
        color = ('#89d185' if score >= 80
                 else '#dcdcaa' if score >= 60 else '#f48771')
        self.total_score_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background: {color}; }}")
        self.total_score_label.setText(str(score))
        self.total_score_label.setStyleSheet(
            f"color: {color};")

        # 分类
        for k, label in self.cat_labels.items():
            v = r.category_scores.get(k, 0)
            color = ('#89d185' if v >= 80
                     else '#dcdcaa' if v >= 60 else '#f48771')
            label.setText(str(v))
            label.setStyleSheet(
                f"color: {color}; font-size: 18px; font-weight: bold;")

        # 问题
        self.issue_table.setRowCount(len(r.issues))
        for i, issue in enumerate(r.issues):
            icon = {'critical': '🔴', 'warning': '🟡',
                    'info': '🔵'}.get(issue.severity, '⚪')
            self.issue_table.setItem(i, 0, QTableWidgetItem(icon))
            self.issue_table.setItem(i, 1, QTableWidgetItem(issue.category))
            self.issue_table.setItem(i, 2, QTableWidgetItem(issue.table))
            self.issue_table.setItem(i, 3, QTableWidgetItem(issue.message))
            self.issue_table.setItem(
                i, 4, QTableWidgetItem(issue.suggestion or ''))

        # 报告
        try:
            scorer = HealthScorer(self.conn_getter())
            report_md = scorer.generate_report(r)
            self.report_view.setPlainText(report_md)
        except Exception:
            pass

    def _export(self):
        if not self.report:
            QMessageBox.warning(self, "提示", "请先运行体检")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出报告", "health_report.md", "Markdown (*.md)")
        if not path:
            return
        try:
            scorer = HealthScorer(self.conn_getter())
            md = scorer.generate_report(self.report)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(md)
            QMessageBox.information(self, "完成", f"已导出:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))