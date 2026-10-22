# -*- coding: utf-8 -*-
"""可视化 EXPLAIN（G2-b）：真实执行 EXPLAIN，表格 + 关键列摘要，不伪造。

- 只展示引擎真实返回的列/行；异常原样显式提示（不生成假执行计划/假成本）；
- 关键列摘要：table/type/possible_keys/key/rows/Extra（缺列时显式说明）；
- 无行/无结果也显式提示，不假装有计划。
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QPlainTextEdit,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

KEY_COLUMNS = ('table', 'type', 'possible_keys', 'key', 'rows', 'Extra')


def run_explain(conn, sql):
    """执行 `EXPLAIN <sql>`；返回 {'ok','columns','rows','error','summary'}。"""
    sql = str(sql or '').strip().rstrip(';')
    if not sql:
        return {'ok': False, 'columns': [], 'rows': [],
                'error': 'SQL 为空，无法 EXPLAIN', 'summary': []}
    if sql.upper().startswith('EXPLAIN'):
        explain_sql = sql
    else:
        explain_sql = 'EXPLAIN ' + sql
    try:
        columns, rows = conn.query(explain_sql)
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'columns': [], 'rows': [],
                'error': f'EXPLAIN 执行失败: {exc}', 'summary': [],
                'sql': explain_sql}
    columns = [str(c) for c in columns or []]
    rows = [list(r) for r in rows or []]
    summary = summarize_explain(columns, rows)
    return {'ok': True, 'columns': columns, 'rows': rows, 'error': '',
            'summary': summary, 'sql': explain_sql}


def _find_col(columns, target):
    for i, name in enumerate(columns):
        if name.lower() == target.lower():
            return i
    return -1


def summarize_explain(columns, rows):
    """提取关键列摘要（真实值；缺失列显式标注，不编造）。"""
    idx = {key: _find_col(columns, key) for key in KEY_COLUMNS}
    missing = [k for k, v in idx.items() if v < 0]
    summary = []
    for r, row in enumerate(rows or []):
        item = {'row': r + 1}
        for key, i in idx.items():
            item[key] = row[i] if 0 <= i < len(row) else None
        item['flags'] = []
        if str(item.get('type') or '').upper() == 'ALL':
            item['flags'].append('全表扫描（type=ALL）')
        if item.get('key') in (None, ''):
            item['flags'].append('未使用索引（key 为空）')
        try:
            if int(item.get('rows')) >= 1000:
                item['flags'].append(f"预估扫描行数较大（rows={item['rows']}）")
        except (TypeError, ValueError):
            pass
        summary.append(item)
    return {'items': summary, 'missing_columns': missing,
            'available_columns': list(columns)}


class ExplainViewDialog(QDialog):
    """结果表格 + 关键列摘要；不画伪执行计划图。"""

    def __init__(self, conn, sql, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.sql = str(sql or '')
        self.result = None
        self.setWindowTitle('可视化 EXPLAIN')
        self.resize(900, 560)
        self._build_ui()
        self.run_explain()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self.sql_label = QLabel(self.sql.strip() or '（空 SQL）')
        self.sql_label.setWordWrap(True)
        self.sql_label.setFont(QFont('Consolas', 9))
        layout.addWidget(self.sql_label)
        self.status = QLabel('')
        self.status.setStyleSheet('color: #89d185;')
        layout.addWidget(self.status)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table, 1)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMaximumHeight(140)
        self.summary.setFont(QFont('Consolas', 9))
        layout.addWidget(QLabel('关键列摘要（基于真实 EXPLAIN 结果，不伪造成本）：'))
        layout.addWidget(self.summary)
        row = QHBoxLayout()
        self.copy_btn = QPushButton('复制摘要')
        self.copy_btn.clicked.connect(self._copy_summary)
        row.addWidget(self.copy_btn)
        row.addStretch()
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)

    def run_explain(self):
        self.result = run_explain(self.conn, self.sql)
        self.render_result()
        return self.result

    def render_result(self):
        result = self.result or {}
        if not result.get('ok'):
            self.status.setText(result.get('error', 'EXPLAIN 失败'))
            self.status.setStyleSheet('color: #f48771;')
            self.table.clear()
            self.table.setRowCount(0)
            self.summary.setPlainText('')
            return
        columns = result.get('columns') or []
        rows = result.get('rows') or []
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem(
                    'NULL' if value is None else str(value))
                if str(columns[c]).lower() in ('rows', 'id', 'select_type'):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        summary = result.get('summary') or {}
        lines = []
        for item in summary.get('items') or []:
            parts = [f"#{item['row']}"]
            for key in KEY_COLUMNS:
                if item.get(key) is not None:
                    parts.append(f'{key}={item[key]}')
            if item.get('flags'):
                parts.append('⚠ ' + '；'.join(item['flags']))
            lines.append(' | '.join(parts))
        if summary.get('missing_columns'):
            lines.append('缺少关键列（引擎未返回）: '
                         + ', '.join(summary['missing_columns']) + '（不编造）')
        if not rows:
            lines.append('EXPLAIN 返回 0 行（没有可展示的计划）')
        self.summary.setPlainText('\n'.join(lines))
        self.status.setText(f'EXPLAIN 返回 {len(rows)} 行 / {len(columns)} 列')
        self.status.setStyleSheet('color: #89d185;')

    def _copy_summary(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.summary.toPlainText())
        return self.summary.toPlainText()
