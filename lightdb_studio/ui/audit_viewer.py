"""审计日志查看器"""
import time
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QDialogButtonBox, QLabel, QLineEdit, QComboBox,
    QDateTimeEdit, QCheckBox, QMessageBox, QFileDialog, QTextEdit,
    QSplitter, QWidget
)
from PySide6.QtCore import Qt, QDateTime
from PySide6.QtGui import QColor, QFont
from datetime import datetime


class AuditViewer(QDialog):
    def __init__(self, conn_getter, parent=None):
        super().__init__(parent)
        self.conn_getter = conn_getter
        self.records = []
        self.filtered = []
        self.setWindowTitle("审计日志查看器")
        self.resize(1100, 720)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 过滤栏
        filter_bar = QHBoxLayout()

        filter_bar.addWidget(QLabel("用户:"))
        self.user_combo = QComboBox()
        self.user_combo.setEditable(True)
        self.user_combo.setMinimumWidth(120)
        filter_bar.addWidget(self.user_combo)

        filter_bar.addWidget(QLabel("操作:"))
        self.cmd_combo = QComboBox()
        self.cmd_combo.setEditable(True)
        self.cmd_combo.addItems(
            ['全部', 'SELECT', 'INSERT', 'UPDATE', 'DELETE',
             'CREATE TABLE', 'DROP TABLE', 'GRANT', 'REVOKE'])
        self.cmd_combo.setMinimumWidth(140)
        filter_bar.addWidget(self.cmd_combo)

        filter_bar.addWidget(QLabel("SQL 关键字:"))
        self.kw_edit = QLineEdit()
        self.kw_edit.setPlaceholderText("搜索 SQL 内容...")
        self.kw_edit.setMinimumWidth(180)
        filter_bar.addWidget(self.kw_edit)

        self.only_fail_check = QCheckBox("仅显示失败")
        filter_bar.addWidget(self.only_fail_check)

        apply_btn = QPushButton("🔍 过滤")
        apply_btn.clicked.connect(self._apply_filter)
        filter_bar.addWidget(apply_btn)

        clear_btn = QPushButton("清空条件")
        clear_btn.clicked.connect(self._clear_filter)
        filter_bar.addWidget(clear_btn)
        filter_bar.addStretch()
        layout.addLayout(filter_bar)

        # 按钮行
        btn_row = QHBoxLayout()
        load_btn = QPushButton("🔄 加载审计日志")
        load_btn.clicked.connect(self._load)
        btn_row.addWidget(load_btn)

        export_btn = QPushButton("📤 导出为 CSV")
        export_btn.clicked.connect(self._export_csv)
        btn_row.addWidget(export_btn)

        export_html_btn = QPushButton("📄 导出为 HTML")
        export_html_btn.clicked.connect(self._export_html)
        btn_row.addWidget(export_html_btn)
        btn_row.addStretch()

        self.count_label = QLabel("0 条记录")
        self.count_label.setStyleSheet("color: #969696;")
        btn_row.addWidget(self.count_label)
        layout.addLayout(btn_row)

        # 主表格
        splitter = QSplitter(Qt.Vertical)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ['时间', '用户', '操作', '耗时(ms)', '结果', 'SQL', '错误'])
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 60)
        self.table.setColumnWidth(5, 400)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_select)
        splitter.addWidget(self.table)

        # 详情
        detail_w = QWidget()
        dl = QVBoxLayout(detail_w)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(QLabel("详情:"))
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(QFont("Consolas", 10))
        dl.addWidget(self.detail)
        splitter.addWidget(detail_w)

        splitter.setSizes([500, 200])
        layout.addWidget(splitter)

        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.reject)
        layout.addWidget(close_btn)

    def _load(self):
        conn = self.conn_getter()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择连接")
            return

        try:
            cols, rows = conn.query("SHOW AUDIT LOG")
        except Exception as e:
            QMessageBox.warning(self, "提示",
                                f"审计日志不可用: {e}")
            return

        # 转换
        self.records = []
        for r in rows:
            # 兼容不同字段顺序
            rec = {
                'ts': r[0] if len(r) > 0 else 0,
                'user': r[1] if len(r) > 1 else '',
                'cmd': r[2] if len(r) > 2 else '',
                'success': r[3] if len(r) > 3 else 'YES',
                'elapsed': r[4] if len(r) > 4 else 0,
                'sql': r[5] if len(r) > 5 else '',
                'error': r[6] if len(r) > 6 else '',
            }
            self.records.append(rec)

        # 填充用户下拉
        users = sorted({r['user'] for r in self.records})
        self.user_combo.clear()
        self.user_combo.addItem('全部')
        self.user_combo.addItems(users)

        self._apply_filter()

    def _apply_filter(self):
        user = self.user_combo.currentText().strip()
        cmd = self.cmd_combo.currentText().strip()
        kw = self.kw_edit.text().strip().lower()
        only_fail = self.only_fail_check.isChecked()

        self.filtered = []
        for r in self.records:
            if user and user != '全部' and user.lower() != r['user'].lower():
                continue
            if cmd and cmd != '全部' and cmd.upper() != r['cmd'].upper():
                continue
            if kw and kw not in str(r['sql']).lower():
                continue
            if only_fail and r['success'] != 'NO':
                continue
            self.filtered.append(r)

        self._refresh_table()
        self.count_label.setText(
            f"显示 {len(self.filtered)} / {len(self.records)} 条")

    def _clear_filter(self):
        self.user_combo.setCurrentIndex(0)
        self.cmd_combo.setCurrentIndex(0)
        self.kw_edit.clear()
        self.only_fail_check.setChecked(False)
        self._apply_filter()

    def _refresh_table(self):
        self.table.setRowCount(len(self.filtered))
        for r, rec in enumerate(self.filtered):
            # 时间
            ts = rec['ts']
            try:
                if isinstance(ts, (int, float)):
                    tstr = datetime.fromtimestamp(ts).strftime(
                        '%Y-%m-%d %H:%M:%S')
                else:
                    tstr = str(ts)
            except Exception:
                tstr = str(ts)
            self.table.setItem(r, 0, QTableWidgetItem(tstr))

            self.table.setItem(r, 1, QTableWidgetItem(str(rec['user'])))
            self.table.setItem(r, 2, QTableWidgetItem(str(rec['cmd'])))

            try:
                el = f"{float(rec['elapsed']):.2f}"
            except (ValueError, TypeError):
                el = str(rec['elapsed'])
            self.table.setItem(r, 3, QTableWidgetItem(el))

            success = rec['success'] in ('YES', 1, True, 'success')
            item = QTableWidgetItem('✓' if success else '✗')
            item.setForeground(
                QColor("#89d185") if success else QColor("#f48771"))
            self.table.setItem(r, 4, item)

            sql_item = QTableWidgetItem(str(rec['sql'])[:200])
            self.table.setItem(r, 5, sql_item)

            err_item = QTableWidgetItem(str(rec['error'])[:100])
            if rec['error']:
                err_item.setForeground(QColor("#f48771"))
            self.table.setItem(r, 6, err_item)

    def _on_select(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(self.filtered):
            return
        rec = self.filtered[r]
        lines = [
            f"时间: {rec['ts']}",
            f"用户: {rec['user']}",
            f"操作: {rec['cmd']}",
            f"耗时: {rec['elapsed']} ms",
            f"结果: {rec['success']}",
            f"",
            f"SQL:",
            f"{rec['sql']}",
        ]
        if rec['error']:
            lines.append(f"\n错误:\n{rec['error']}")
        self.detail.setPlainText('\n'.join(lines))

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出审计日志", "audit.csv", "CSV (*.csv)")
        if not path:
            return
        import csv
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                w.writerow(['时间', '用户', '操作', '耗时(ms)',
                            '结果', 'SQL', '错误'])
                for rec in self.filtered:
                    w.writerow([rec['ts'], rec['user'], rec['cmd'],
                                rec['elapsed'], rec['success'],
                                rec['sql'], rec['error']])
            QMessageBox.information(self, "完成", f"已导出 {len(self.filtered)} 条")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))

    def _export_html(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出审计报告", "audit.html", "HTML (*.html)")
        if not path:
            return

        from ..core.reports import ReportEngine
        engine = ReportEngine()
        cols = ['时间', '用户', '操作', '耗时(ms)', '结果', 'SQL', '错误']
        rows = []
        for rec in self.filtered:
            rows.append([rec['ts'], rec['user'], rec['cmd'],
                         rec['elapsed'], rec['success'],
                         rec['sql'], rec['error']])
        try:
            engine.export_html(cols, rows, path, title="审计日志报告")
            QMessageBox.information(self, "完成", f"已导出:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))