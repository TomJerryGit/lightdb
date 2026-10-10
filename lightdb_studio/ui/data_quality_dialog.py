"""数据质量检查对话框"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QProgressDialog, QMessageBox, QFileDialog, QTabWidget,
    QWidget, QTextEdit, QComboBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont

from ..core.data_quality import DataQualityChecker


class DataQualityDialog(QDialog):
    def __init__(self, conn_getter, parent=None):
        super().__init__(parent)
        self.conn_getter = conn_getter
        self.checker = None
        self.summary = None
        self.setWindowTitle("数据质量检查")
        self.resize(960, 660)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 顶部
        top = QHBoxLayout()
        top.addWidget(QLabel("检查范围:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItems(['整库', '当前表'])
        top.addWidget(self.scope_combo)

        run_btn = QPushButton("🔍 开始检查")
        run_btn.clicked.connect(self._run)
        top.addWidget(run_btn)

        export_btn = QPushButton("📤 导出报告 (Markdown)")
        export_btn.clicked.connect(self._export)
        top.addWidget(export_btn)
        top.addStretch()
        layout.addLayout(top)

        # 统计
        self.stats_label = QLabel("尚未检查")
        self.stats_label.setStyleSheet(
            "background: #252526; color: #dcdcaa; padding: 8px;"
            "font-family: Consolas;")
        layout.addWidget(self.stats_label)

        # Tab
        self.tabs = QTabWidget()

        # 树视图
        tree_w = QWidget()
        tl = QVBoxLayout(tree_w)
        tl.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['对象', '级别', '问题', '建议'])
        self.tree.setColumnWidth(0, 240)
        self.tree.setColumnWidth(1, 80)
        self.tree.setColumnWidth(2, 300)
        tl.addWidget(self.tree)
        self.tabs.addTab(tree_w, "分类视图")

        # 表格视图
        table_w = QWidget()
        tabl = QVBoxLayout(table_w)
        tabl.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ['级别', '表', '列', '问题', '建议'])
        self.table.setColumnWidth(0, 70)
        self.table.setColumnWidth(1, 140)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 280)
        self.table.horizontalHeader().setStretchLastSection(True)
        tabl.addWidget(self.table)
        self.tabs.addTab(table_w, "列表视图")

        # Markdown 报告
        report_w = QWidget()
        rl = QVBoxLayout(report_w)
        rl.setContentsMargins(0, 0, 0, 0)
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

        self.checker = DataQualityChecker(conn)
        self.tree.clear()
        self.table.setRowCount(0)

        progress = QProgressDialog("检查中...", "取消", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)

        try:
            if self.scope_combo.currentText() == '当前表':
                win = self.window()
                table = None
                if hasattr(win, '_current_table'):
                    table = win._current_table()
                if not table:
                    QMessageBox.information(self, "提示", "请先选中表")
                    return
                issues = self.checker.check_table(table)
                self.summary = {
                    'total_tables': 1,
                    'total_issues': len(issues),
                    'errors': sum(1 for i in issues if i.level == 'error'),
                    'warnings': sum(1 for i in issues
                                    if i.level == 'warning'),
                    'infos': sum(1 for i in issues if i.level == 'info'),
                    'issues': issues,
                }
            else:
                def cb(i, total, tname):
                    progress.setValue(int(i / total * 100))
                    progress.setLabelText(f"检查: {tname}")
                self.summary = self.checker.check_database(cb)

            progress.close()
            self._display()
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "失败", str(e))

    def _display(self):
        s = self.summary
        self.stats_label.setText(
            f"表数: {s['total_tables']}  |  "
            f"问题总数: {s['total_issues']}  |  "
            f"🔴 错误: {s['errors']}  |  "
            f"🟡 警告: {s['warnings']}  |  "
            f"🔵 提示: {s['infos']}")

        # 树视图：按表分组
        by_table = {}
        for issue in s['issues']:
            by_table.setdefault(issue.table, []).append(issue)

        for table, issues in by_table.items():
            n_err = sum(1 for i in issues if i.level == 'error')
            n_warn = sum(1 for i in issues if i.level == 'warning')
            title = f"📋 {table}  ({len(issues)} 问题"
            if n_err: title += f", {n_err} 错误"
            if n_warn: title += f", {n_warn} 警告"
            title += ")"

            parent = QTreeWidgetItem(self.tree)
            parent.setText(0, title)
            if n_err:
                parent.setForeground(0, QColor("#f48771"))
            elif n_warn:
                parent.setForeground(0, QColor("#dcdcaa"))

            for issue in issues:
                child = QTreeWidgetItem(parent)
                icon = {'error': '🔴', 'warning': '🟡',
                        'info': '🔵'}.get(issue.level, '⚪')
                child.setText(0, f"  {issue.column or '(表级)'}")
                child.setText(1, icon)
                child.setText(2, issue.message)
                child.setText(3, issue.suggestion or '')
                if issue.level == 'error':
                    child.setForeground(2, QColor("#f48771"))

        self.tree.expandAll()

        # 表格视图
        self.table.setRowCount(len(s['issues']))
        for r, issue in enumerate(s['issues']):
            icon = {'error': '🔴', 'warning': '🟡',
                    'info': '🔵'}.get(issue.level, '⚪')
            self.table.setItem(r, 0, QTableWidgetItem(icon))
            self.table.setItem(r, 1, QTableWidgetItem(issue.table))
            self.table.setItem(r, 2, QTableWidgetItem(issue.column or ''))
            self.table.setItem(r, 3, QTableWidgetItem(issue.message))
            self.table.setItem(r, 4, QTableWidgetItem(issue.suggestion or ''))

        # 报告
        report = self.checker.generate_report(s)
        self.report_view.setPlainText(report)

    def _export(self):
        if not self.summary:
            QMessageBox.warning(self, "提示", "请先运行检查")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出报告", "data_quality.md", "Markdown (*.md)")
        if not path:
            return
        try:
            report = self.checker.generate_report(self.summary)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(report)
            QMessageBox.information(self, "完成", f"已导出: {path}")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))