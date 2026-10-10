"""影响分析对话框"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QTabWidget,
    QWidget, QTextEdit, QDialogButtonBox, QMessageBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont

from ..core.lineage_enhanced import EnhancedLineageStore


class ImpactDialog(QDialog):
    def __init__(self, lineage: EnhancedLineageStore,
                 table: str = '', column: str = '', parent=None):
        super().__init__(parent)
        self.lineage = lineage
        self.setWindowTitle("影响分析")
        self.resize(900, 640)
        self._build_ui()
        if table:
            self.table_edit.setText(table)
        if column:
            self.column_edit.setText(column)
        if table:
            self._analyze()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 查询栏
        top = QHBoxLayout()
        top.addWidget(QLabel("表:"))
        self.table_edit = QLineEdit()
        self.table_edit.setPlaceholderText("表名")
        self.table_edit.returnPressed.connect(self._analyze)
        top.addWidget(self.table_edit)

        top.addWidget(QLabel("列:"))
        self.column_edit = QLineEdit()
        self.column_edit.setPlaceholderText("列名（空=表级）")
        self.column_edit.returnPressed.connect(self._analyze)
        top.addWidget(self.column_edit)

        analyze_btn = QPushButton("🔍 分析")
        analyze_btn.clicked.connect(self._analyze)
        top.addWidget(analyze_btn)
        top.addStretch()
        layout.addLayout(top)

        # Tab
        self.tabs = QTabWidget()

        # 影响分析
        impact_w = QWidget()
        il = QVBoxLayout(impact_w)

        self.impact_tree = QTreeWidget()
        self.impact_tree.setHeaderLabels(
            ['表/列', '类型', '转换', 'SQL 预览'])
        self.impact_tree.setColumnWidth(0, 240)
        self.impact_tree.setColumnWidth(1, 80)
        self.impact_tree.setColumnWidth(2, 80)
        self.impact_tree.setColumnWidth(3, 300)
        il.addWidget(self.impact_tree)

        self.tabs.addTab(impact_w, "下游影响")

        # 上游追溯
        upstream_w = QWidget()
        ul = QVBoxLayout(upstream_w)
        self.upstream_tree = QTreeWidget()
        self.upstream_tree.setHeaderLabels(
            ['表/列', '层级', '转换', 'SQL 预览'])
        self.upstream_tree.setColumnWidth(0, 240)
        self.upstream_tree.setColumnWidth(1, 60)
        self.upstream_tree.setColumnWidth(2, 80)
        self.upstream_tree.setColumnWidth(3, 300)
        ul.addWidget(self.upstream_tree)
        self.tabs.addTab(upstream_w, "上游来源")

        # 报告
        report_w = QWidget()
        rl = QVBoxLayout(report_w)
        self.report_view = QTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setFont(QFont("Microsoft YaHei", 10))
        rl.addWidget(self.report_view)
        self.tabs.addTab(report_w, "报告")

        layout.addWidget(self.tabs)

        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.reject)
        layout.addWidget(close_btn)

    def _analyze(self):
        table = self.table_edit.text().strip()
        column = self.column_edit.text().strip() or None
        if not table:
            return

        # 下游
        impact = self.lineage.impact_analysis(table, column)
        self._display_impact(impact)

        # 上游
        if column:
            upstream = self.lineage.upstream_of_column(table, column)
        else:
            upstream = []
        self._display_upstream(upstream)

        # 报告
        self._generate_report(table, column, impact, upstream)

    def _display_impact(self, impact):
        self.impact_tree.clear()

        # 直接
        if impact['direct']:
            root = QTreeWidgetItem(self.impact_tree)
            root.setText(0, f"⚠️ 直接影响 ({len(impact['direct'])} 个)")
            root.setForeground(0, QColor('#f48771'))
            for d in impact['direct']:
                item = QTreeWidgetItem(root)
                item.setText(0, f"  {d['table']}.{d['column']}")
                item.setText(1, "直接")
                item.setText(2, d.get('transform', ''))
                item.setForeground(0, QColor('#ce9178'))

        # 间接
        if impact['indirect']:
            root = QTreeWidgetItem(self.impact_tree)
            root.setText(0, f"🔗 间接影响 ({len(impact['indirect'])} 个)")
            root.setForeground(0, QColor('#dcdcaa'))
            for d in impact['indirect']:
                item = QTreeWidgetItem(root)
                item.setText(0, f"  {d['table']}.{d['column']}")
                item.setText(1, f"第 {d.get('level', '?')} 层")
                item.setText(2, d.get('transform', ''))
                item.setForeground(0, QColor('#569cd6'))

        self.impact_tree.expandAll()

    def _display_upstream(self, upstream):
        self.upstream_tree.clear()
        if not upstream:
            item = QTreeWidgetItem(self.upstream_tree)
            item.setText(0, "无上游记录")
            return

        by_level = {}
        for u in upstream:
            by_level.setdefault(u['level'], []).append(u)

        for level in sorted(by_level.keys()):
            root = QTreeWidgetItem(self.upstream_tree)
            root.setText(0, f"第 {level} 层 ({len(by_level[level])})")
            root.setForeground(0, QColor('#89d185'))

            for u in by_level[level]:
                item = QTreeWidgetItem(root)
                item.setText(0, f"  {u['table']}.{u['column']}")
                item.setText(1, str(u['level']))
                item.setText(2, u.get('transform', ''))
                item.setText(3, u.get('sql', '')[:100])

        self.upstream_tree.expandAll()

    def _generate_report(self, table, column, impact, upstream):
        lines = []
        target = f"{table}.{column}" if column else table
        lines.append(f"# 影响分析报告")
        lines.append("")
        lines.append(f"**目标**: `{target}`")
        lines.append("")

        lines.append(f"## 下游影响")
        lines.append("")
        if impact['direct']:
            lines.append("### 直接影响")
            lines.append("")
            for d in impact['direct']:
                lines.append(
                    f"- `{d['table']}.{d['column']}` "
                    f"({d.get('transform', 'direct')})")
        if impact['indirect']:
            lines.append("")
            lines.append("### 间接影响")
            lines.append("")
            for d in impact['indirect']:
                lines.append(
                    f"- `{d['table']}.{d['column']}` "
                    f"(第 {d.get('level')} 层)")

        if not impact['direct'] and not impact['indirect']:
            lines.append("(无下游依赖)")
        lines.append("")

        lines.append(f"## 上游来源")
        lines.append("")
        if upstream:
            for u in upstream:
                lines.append(
                    f"- `{u['table']}.{u['column']}` "
                    f"(第 {u['level']} 层, {u.get('transform', '')})")
        else:
            lines.append("(无上游记录)")

        lines.append("")
        lines.append("## 修改建议")
        lines.append("")
        if impact['direct'] or impact['indirect']:
            lines.append("⚠️ 该字段有下游依赖，修改前需要:")
            lines.append("1. 通知下游表的负责人")
            lines.append("2. 评估是否需要同步修改下游 SQL / ETL")
            lines.append("3. 在测试环境验证")
        else:
            lines.append("✅ 该字段无下游依赖，修改相对安全")

        self.report_view.setPlainText('\n'.join(lines))