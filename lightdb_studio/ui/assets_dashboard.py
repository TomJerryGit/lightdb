"""数据资产仪表盘"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QTableWidget, QTableWidgetItem, QLabel, QPushButton,
    QGroupBox, QFileDialog, QMessageBox, QTextEdit, QListWidget,
    QListWidgetItem
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont

from ..core.assets import AssetAnalyzer


class AssetsDashboard(QDialog):
    def __init__(self, catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.analyzer = AssetAnalyzer(catalog)
        self.setWindowTitle("数据资产报表")
        self.resize(960, 680)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self._refresh)
        top.addWidget(refresh_btn)

        export_btn = QPushButton("📤 导出报告")
        export_btn.clicked.connect(self._export)
        top.addWidget(export_btn)
        top.addStretch()
        layout.addLayout(top)

        # 顶部统计
        self.stats_group = QGroupBox("概览")
        sg = QHBoxLayout(self.stats_group)
        self.stats_labels = {}
        for key, label in [
                ('tables', '表'), ('columns', '列'),
                ('rows', '总行数'), ('size', '估算存储')]:
            w = QWidget()
            wl = QVBoxLayout(w)
            wl.setContentsMargins(8, 4, 8, 4)
            wl.setSpacing(0)
            t = QLabel(label)
            t.setStyleSheet("color: #969696; font-size: 11px;")
            t.setAlignment(Qt.AlignCenter)
            v = QLabel("--")
            v.setStyleSheet(
                "color: #4ec9b0; font-size: 20px; font-weight: bold;")
            v.setAlignment(Qt.AlignCenter)
            wl.addWidget(t)
            wl.addWidget(v)
            sg.addWidget(w)
            self.stats_labels[key] = v
        layout.addWidget(self.stats_group)

        # Tab
        tabs = QTabWidget()

        # Top 表
        top_w = QWidget()
        tl = QVBoxLayout(top_w)
        tl.addWidget(QLabel("按查询次数:"))
        self.top_query_table = QTableWidget()
        self.top_query_table.setColumnCount(4)
        self.top_query_table.setHorizontalHeaderLabels(
            ['表', '库', '域', '查询次数'])
        self.top_query_table.horizontalHeader().setStretchLastSection(True)
        tl.addWidget(self.top_query_table)
        tabs.addTab(top_w, "使用排行")

        # Top 数据量
        top_rows_w = QWidget()
        trl = QVBoxLayout(top_rows_w)
        trl.addWidget(QLabel("按行数:"))
        self.top_rows_table = QTableWidget()
        self.top_rows_table.setColumnCount(4)
        self.top_rows_table.setHorizontalHeaderLabels(
            ['表', '库', '域', '行数'])
        self.top_rows_table.horizontalHeader().setStretchLastSection(True)
        trl.addWidget(self.top_rows_table)
        tabs.addTab(top_rows_w, "数据量排行")

        # 闲置表
        idle_w = QWidget()
        il = QVBoxLayout(idle_w)
        il.addWidget(QLabel("30 天未使用:"))
        self.idle_list = QListWidget()
        self.idle_list.setFont(QFont("Consolas", 10))
        il.addWidget(self.idle_list)
        tabs.addTab(idle_w, "闲置表")

        # 业务域
        domain_w = QWidget()
        dl = QVBoxLayout(domain_w)
        self.domain_table = QTableWidget()
        self.domain_table.setColumnCount(4)
        self.domain_table.setHorizontalHeaderLabels(
            ['业务域', '表数', '行数', '列数'])
        self.domain_table.horizontalHeader().setStretchLastSection(True)
        dl.addWidget(self.domain_table)
        tabs.addTab(domain_w, "业务域分布")

        # 报告
        report_w = QWidget()
        rl = QVBoxLayout(report_w)
        self.report_view = QTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setFont(QFont("Consolas", 9))
        rl.addWidget(self.report_view)
        tabs.addTab(report_w, "报告")

        layout.addWidget(tabs)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)

    def _refresh(self):
        stats = self.analyzer.stats()

        self.stats_labels['tables'].setText(str(stats.total_tables))
        self.stats_labels['columns'].setText(str(stats.total_columns))
        self.stats_labels['rows'].setText(self._fmt(stats.total_rows))
        self.stats_labels['size'].setText(
            self._fmt_size(stats.total_size_estimate))

        # Top 查询
        top = self.analyzer.top_tables('query_count', 20)
        self.top_query_table.setRowCount(len(top))
        for i, t in enumerate(top):
            self.top_query_table.setItem(i, 0, QTableWidgetItem(t['table']))
            self.top_query_table.setItem(i, 1, QTableWidgetItem(t['database']))
            self.top_query_table.setItem(i, 2, QTableWidgetItem(t['domain']))
            self.top_query_table.setItem(
                i, 3, QTableWidgetItem(f"{t['value']:,}"))

        # Top 行数
        top_rows = self.analyzer.top_tables('rows', 20)
        self.top_rows_table.setRowCount(len(top_rows))
        for i, t in enumerate(top_rows):
            self.top_rows_table.setItem(i, 0, QTableWidgetItem(t['table']))
            self.top_rows_table.setItem(i, 1, QTableWidgetItem(t['database']))
            self.top_rows_table.setItem(i, 2, QTableWidgetItem(t['domain']))
            self.top_rows_table.setItem(
                i, 3, QTableWidgetItem(f"{t['value']:,}"))

        # 闲置表
        self.idle_list.clear()
        idle = self.analyzer.idle_tables(30)
        for t in idle:
            text = (f"{t['key']:<40} {t['rows']:>12,} 行  "
                    f"[{t['reason']}]")
            item = QListWidgetItem(text)
            item.setForeground(QColor('#dcdcaa'))
            self.idle_list.addItem(item)
        if not idle:
            self.idle_list.addItem("✅ 无闲置表")

        # 业务域
        domain_data = list(stats.by_domain.items())
        domain_data.sort(key=lambda x: -x[1]['rows'])
        self.domain_table.setRowCount(len(domain_data))
        for i, (d, info) in enumerate(domain_data):
            self.domain_table.setItem(i, 0, QTableWidgetItem(d))
            self.domain_table.setItem(
                i, 1, QTableWidgetItem(str(info['tables'])))
            self.domain_table.setItem(
                i, 2, QTableWidgetItem(f"{info['rows']:,}"))
            self.domain_table.setItem(
                i, 3, QTableWidgetItem(str(info['cols'])))

        # 报告
        report = self.analyzer.asset_report()
        self.report_view.setPlainText(report)

    def _fmt(self, n):
        if n >= 1_000_000_000:
            return f"{n/1e9:.1f}B"
        if n >= 1_000_000:
            return f"{n/1e6:.1f}M"
        if n >= 1_000:
            return f"{n/1e3:.1f}K"
        return str(n)

    def _fmt_size(self, b):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} PB"

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出", "asset_report.md", "Markdown (*.md)")
        if not path:
            return
        try:
            report = self.analyzer.asset_report()
            with open(path, 'w', encoding='utf-8') as f:
                f.write(report)
            QMessageBox.information(self, "完成", f"已导出:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))