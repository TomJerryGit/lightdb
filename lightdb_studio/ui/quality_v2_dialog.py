"""数据质量 2.0 对话框"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QTabWidget, QWidget,
    QProgressDialog, QMessageBox, QFileDialog, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
    QComboBox, QLineEdit, QFormLayout, QDialogButtonBox,
    QGroupBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont

from ..core.quality_v2 import (
    QualityEngineV2, PatternRule, BUILTIN_PATTERNS)


class RuleEditorDialog(QDialog):
    """规则编辑"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建规则")
        self.resize(500, 360)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit()
        form.addRow("规则名:", self.name_edit)

        self.col_edit = QLineEdit()
        self.col_edit.setPlaceholderText("如: *email* 或 phone")
        form.addRow("列匹配:", self.col_edit)

        self.template_combo = QComboBox()
        self.template_combo.addItem('(自定义)', None)
        for p in BUILTIN_PATTERNS:
            self.template_combo.addItem(
                f"{p['desc']}", p)
        self.template_combo.currentIndexChanged.connect(
            self._on_template_change)
        form.addRow("模板:", self.template_combo)

        self.regex_edit = QLineEdit()
        self.regex_edit.setPlaceholderText("正则表达式")
        self.regex_edit.setFont(QFont("Consolas", 10))
        form.addRow("正则:", self.regex_edit)

        self.sev_combo = QComboBox()
        self.sev_combo.addItems(['error', 'warning', 'info'])
        self.sev_combo.setCurrentText('warning')
        form.addRow("严重度:", self.sev_combo)

        layout.addLayout(form)
        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_template_change(self):
        p = self.template_combo.currentData()
        if p:
            self.regex_edit.setText(p['regex'])
            if not self.name_edit.text():
                self.name_edit.setText(p['desc'])

    def get_rule(self) -> PatternRule:
        return PatternRule(
            name=self.name_edit.text().strip() or '自定义规则',
            column_pattern=self.col_edit.text().strip(),
            regex=self.regex_edit.text().strip(),
            description=self.name_edit.text().strip(),
            severity=self.sev_combo.currentText(),
        )


class QualityV2Dialog(QDialog):
    """数据质量 2.0"""

    def __init__(self, conn_getter, parent=None):
        super().__init__(parent)
        self.conn_getter = conn_getter
        self.engine = QualityEngineV2()
        self.summary = None
        self.setWindowTitle("数据质量 2.0")
        self.resize(1020, 720)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 顶部
        top = QHBoxLayout()
        top.addWidget(QLabel("范围:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItems(['整库', '当前表'])
        top.addWidget(self.scope_combo)

        run_btn = QPushButton("🔍 开始检查")
        run_btn.clicked.connect(self._run)
        top.addWidget(run_btn)

        rules_btn = QPushButton("⚙ 规则")
        rules_btn.clicked.connect(self._edit_rules)
        top.addWidget(rules_btn)

        dist_btn = QPushButton("📊 分布分析")
        dist_btn.clicked.connect(self._distribution)
        top.addWidget(dist_btn)

        export_btn = QPushButton("📤 导出报告")
        export_btn.clicked.connect(self._export)
        top.addWidget(export_btn)
        top.addStretch()
        layout.addLayout(top)

        # 统计
        self.stats_label = QLabel("尚未检查")
        self.stats_label.setStyleSheet(
            "background: #252526; color: #dcdcaa; padding: 8px; "
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
        self.tree.setColumnWidth(2, 320)
        tl.addWidget(self.tree)
        self.tabs.addTab(tree_w, "分类")

        # 表格
        table_w = QWidget()
        tbl = QVBoxLayout(table_w)
        tbl.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ['级别', '分类', '表', '列', '问题', '建议'])
        self.table.setColumnWidth(0, 70)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 280)
        self.table.horizontalHeader().setStretchLastSection(True)
        tbl.addWidget(self.table)
        self.tabs.addTab(table_w, "列表")

        # 报告
        report_w = QWidget()
        rl = QVBoxLayout(report_w)
        rl.setContentsMargins(0, 0, 0, 0)
        self.report_view = QTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setFont(QFont("Consolas", 9))
        rl.addWidget(self.report_view)
        self.tabs.addTab(report_w, "报告")

        # 分布
        dist_w = QWidget()
        dl = QVBoxLayout(dist_w)
        self.dist_view = QTextEdit()
        self.dist_view.setReadOnly(True)
        self.dist_view.setFont(QFont("Consolas", 9))
        dl.addWidget(self.dist_view)
        self.tabs.addTab(dist_w, "分布")

        layout.addWidget(self.tabs)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)

    def _get_conn(self):
        conn = self.conn_getter()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择连接")
            return None
        self.engine.set_connection(conn)
        return conn

    def _run(self):
        conn = self._get_conn()
        if conn is None:
            return

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
                issues = self.engine.check_table(table)
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
                self.summary = self.engine.check_database(cb)

            progress.close()
            self._display()
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "失败", str(e))

    def _display(self):
        s = self.summary
        self.stats_label.setText(
            f"表数 {s['total_tables']} | "
            f"问题 {s['total_issues']} | "
            f"🔴 {s['errors']} | "
            f"🟡 {s['warnings']} | "
            f"🔵 {s['infos']}")

        # 树
        by_cat = {}
        for i in s['issues']:
            by_cat.setdefault(i.category, []).append(i)

        for cat, issues in by_cat.items():
            parent = QTreeWidgetItem(self.tree)
            parent.setText(0, f"📁 {cat} ({len(issues)})")
            parent.setText(1, '')
            for issue in issues:
                child = QTreeWidgetItem(parent)
                icon = {'error': '🔴', 'warning': '🟡',
                        'info': '🔵'}.get(issue.level, '⚪')
                t = f"{issue.table}"
                if issue.column:
                    t += f".{issue.column}"
                child.setText(0, t)
                child.setText(1, icon)
                child.setText(2, issue.message)
                child.setText(3, issue.suggestion or '')
                if issue.level == 'error':
                    child.setForeground(2, QColor('#f48771'))

        self.tree.expandAll()

        # 表格
        self.table.setRowCount(len(s['issues']))
        for r, issue in enumerate(s['issues']):
            icon = {'error': '🔴', 'warning': '🟡',
                    'info': '🔵'}.get(issue.level, '⚪')
            self.table.setItem(r, 0, QTableWidgetItem(icon))
            self.table.setItem(r, 1, QTableWidgetItem(issue.category))
            self.table.setItem(r, 2, QTableWidgetItem(issue.table))
            self.table.setItem(r, 3, QTableWidgetItem(issue.column or ''))
            self.table.setItem(r, 4, QTableWidgetItem(issue.message))
            self.table.setItem(r, 5, QTableWidgetItem(issue.suggestion or ''))

        # 报告
        report = self.engine.generate_report(s)
        self.report_view.setPlainText(report)

    def _edit_rules(self):
        dlg = RuleManagerDialog(self.engine, self)
        dlg.exec()

    def _distribution(self):
        conn = self._get_conn()
        if conn is None:
            return

        win = self.window()
        table = None
        if hasattr(win, '_current_table'):
            table = win._current_table()
        if not table:
            QMessageBox.information(self, "提示", "请先选中表")
            return

        # 让用户选择列
        try:
            cols = conn.get_columns(table)
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return

        col_names = [c['name'] for c in cols]
        from PySide6.QtWidgets import QInputDialog
        col, ok = QInputDialog.getItem(
            self, "选择列", f"分析 {table} 的列:", col_names, 0, False)
        if not ok:
            return

        dist = self.engine.distribution(table, col)
        if dist is None:
            QMessageBox.warning(self, "提示", "无法分析")
            return

        lines = [
            f"# {table}.{col} 分布",
            "",
            f"总行数: {dist.total:,}",
            f"空值: {dist.null_count:,}",
            f"唯一值: {dist.distinct_count:,}",
        ]
        if dist.min_value is not None:
            lines.append(f"最小值: {dist.min_value}")
            lines.append(f"最大值: {dist.max_value}")
        if dist.avg_value is not None:
            lines.append(f"平均值: {dist.avg_value:.2f}")

        lines.append("")
        lines.append("## 分布")
        lines.append("")
        max_cnt = max(dist.histogram.values()) if dist.histogram else 1
        for label, cnt in dist.histogram.items():
            bar_len = int(cnt / max_cnt * 40)
            bar = '█' * bar_len
            lines.append(f"{str(label)[:30]:<30} {bar} {cnt:,}")

        self.dist_view.setPlainText('\n'.join(lines))
        self.tabs.setCurrentIndex(3)

    def _export(self):
        if not self.summary:
            QMessageBox.warning(self, "提示", "请先运行检查")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出报告", "quality_report.md", "Markdown (*.md)")
        if not path:
            return
        try:
            md = self.engine.generate_report(self.summary)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(md)
            QMessageBox.information(self, "完成", f"已导出:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))


class RuleManagerDialog(QDialog):
    """规则管理"""

    def __init__(self, engine: QualityEngineV2, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("质量规则")
        self.resize(720, 520)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        add_btn = QPushButton("➕ 新建规则")
        add_btn.clicked.connect(self._add)
        top.addWidget(add_btn)

        auto_btn = QPushButton("🤖 自动配置")
        auto_btn.clicked.connect(self._auto)
        top.addWidget(auto_btn)

        del_btn = QPushButton("🗑 删除选中")
        del_btn.clicked.connect(self._delete)
        top.addWidget(del_btn)
        top.addStretch()
        layout.addLayout(top)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ['名称', '列匹配', '正则', '严重度', '启用'])
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 140)
        self.table.setColumnWidth(2, 260)
        self.table.setColumnWidth(3, 80)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.accept)
        layout.addWidget(close_btn)

    def _refresh(self):
        self.table.setRowCount(len(self.engine.rules))
        for r, rule in enumerate(self.engine.rules):
            self.table.setItem(r, 0, QTableWidgetItem(rule.name))
            self.table.setItem(r, 1, QTableWidgetItem(rule.column_pattern))
            self.table.setItem(r, 2, QTableWidgetItem(rule.regex))
            self.table.setItem(r, 3, QTableWidgetItem(rule.severity))
            self.table.setItem(
                r, 4, QTableWidgetItem('✓' if rule.enabled else '✗'))

    def _add(self):
        dlg = RuleEditorDialog(self)
        if dlg.exec() == RuleEditorDialog.Accepted:
            rule = dlg.get_rule()
            if rule.column_pattern and rule.regex:
                self.engine.add_rule(rule)
                self._refresh()

    def _auto(self):
        n = self.engine.auto_setup_rules()
        self._refresh()
        QMessageBox.information(
            self, "完成", f"自动添加了 {n} 条规则")

    def _delete(self):
        r = self.table.currentRow()
        if r < 0:
            return
        self.engine.remove_rule(r)
        self._refresh()