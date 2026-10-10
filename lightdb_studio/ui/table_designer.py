"""表设计器"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QComboBox, QPushButton, QLineEdit, QCheckBox, QLabel, QMessageBox,
    QDialogButtonBox, QTextEdit, QAbstractItemView, QWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from ..db.table_ops import build_create_table


TYPE_OPTIONS = ['INT', 'BIGINT', 'VARCHAR', 'TEXT', 'DATETIME', 'DATE',
                'DECIMAL', 'FLOAT', 'DOUBLE', 'BOOLEAN', 'JSON']


class TableDesigner(QDialog):
    """新建表设计器"""

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("新建表 - 表设计器")
        self.resize(860, 640)
        self._build_ui()
        self._add_column(default_id=True)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 表名
        top = QHBoxLayout()
        top.addWidget(QLabel("表名:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如: users")
        top.addWidget(self.name_edit)
        layout.addLayout(top)

        # 列编辑表格
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            '列名', '类型', '长度', '主键', '非空', '自增', '默认值'
        ])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3, 60)
        self.table.setColumnWidth(4, 60)
        self.table.setColumnWidth(5, 60)
        layout.addWidget(self.table)

        # 按钮行
        btn_row = QHBoxLayout()
        for text, slot in [
            ("➕ 添加列", self._add_column),
            ("➖ 删除列", self._del_column),
            ("⬆ 上移", lambda: self._move_column(-1)),
            ("⬇ 下移", lambda: self._move_column(1)),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            btn_row.addWidget(btn)

        btn_row.addStretch()

        preview_btn = QPushButton("👁 预览 SQL")
        preview_btn.clicked.connect(self._preview_sql)
        btn_row.addWidget(preview_btn)
        layout.addLayout(btn_row)

        # SQL 预览
        layout.addWidget(QLabel("SQL 预览:"))
        self.sql_preview = QTextEdit()
        self.sql_preview.setReadOnly(True)
        self.sql_preview.setFont(QFont("Consolas", 10))
        self.sql_preview.setMaximumHeight(140)
        self.sql_preview.setStyleSheet(
            "background: #1e1e1e; color: #ce9178;")
        layout.addWidget(self.sql_preview)

        # 确定/取消
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("创建表")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _make_checkbox_widget(self, checked=False):
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setAlignment(Qt.AlignCenter)
        cb = QCheckBox()
        cb.setChecked(checked)
        l.addWidget(cb)
        return w, cb

    def _add_column(self, default_id=False):
        r = self.table.rowCount()
        self.table.insertRow(r)

        self.table.setItem(r, 0, QTableWidgetItem(
            "id" if default_id else ""))

        type_combo = QComboBox()
        type_combo.addItems(TYPE_OPTIONS)
        type_combo.setCurrentText('INT' if default_id else 'VARCHAR')
        self.table.setCellWidget(r, 1, type_combo)

        self.table.setItem(r, 2, QTableWidgetItem(
            "" if default_id else "255"))

        w1, _ = self._make_checkbox_widget(default_id)
        self.table.setCellWidget(r, 3, w1)
        w2, _ = self._make_checkbox_widget(default_id)
        self.table.setCellWidget(r, 4, w2)
        w3, _ = self._make_checkbox_widget(default_id)
        self.table.setCellWidget(r, 5, w3)

        self.table.setItem(r, 6, QTableWidgetItem(""))

    def _del_column(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        if not rows:
            QMessageBox.information(self, "提示", "请先选择要删除的列")
            return
        for r in rows:
            self.table.removeRow(r)

    def _move_column(self, delta):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if len(rows) != 1:
            return
        r = rows[0]
        nr = r + delta
        if nr < 0 or nr >= self.table.rowCount():
            return
        self._swap_rows(r, nr)
        self.table.selectRow(nr)

    def _get_row(self, r):
        name = self.table.item(r, 0).text() if self.table.item(r, 0) else ""
        tw = self.table.cellWidget(r, 1)
        ctype = tw.currentText() if tw else "INT"
        length = self.table.item(r, 2).text() if self.table.item(r, 2) else ""
        def _ck(col):
            w = self.table.cellWidget(r, col)
            return w.findChild(QCheckBox).isChecked() if w else False
        default = self.table.item(r, 6).text() if self.table.item(r, 6) else ""
        return (name, ctype, length, _ck(3), _ck(4), _ck(5), default)

    def _set_row(self, r, data):
        name, ctype, length, pk, nn, ai, default = data
        self.table.setItem(r, 0, QTableWidgetItem(name))
        cw = self.table.cellWidget(r, 1)
        if cw:
            cw.setCurrentText(ctype)
        self.table.setItem(r, 2, QTableWidgetItem(length))
        for col, checked in [(3, pk), (4, nn), (5, ai)]:
            w = self.table.cellWidget(r, col)
            if w:
                cb = w.findChild(QCheckBox)
                if cb:
                    cb.setChecked(checked)
        self.table.setItem(r, 6, QTableWidgetItem(default))

    def _swap_rows(self, r1, r2):
        d1 = self._get_row(r1)
        d2 = self._get_row(r2)
        self._set_row(r1, d2)
        self._set_row(r2, d1)

    def _collect_columns(self):
        cols = []
        for r in range(self.table.rowCount()):
            name = (self.table.item(r, 0).text().strip()
                    if self.table.item(r, 0) else "")
            if not name:
                continue
            tw = self.table.cellWidget(r, 1)
            ctype = tw.currentText() if tw else "INT"
            length = (self.table.item(r, 2).text().strip()
                      if self.table.item(r, 2) else "")
            def _ck(col):
                w = self.table.cellWidget(r, col)
                return w.findChild(QCheckBox).isChecked() if w else False
            default = (self.table.item(r, 6).text().strip()
                       if self.table.item(r, 6) else "")
            cols.append({
                'name': name,
                'type': ctype,
                'length': length,
                'primary': _ck(3),
                'notnull': _ck(4),
                'auto_increment': _ck(5),
                'default': default,
            })
        return cols

    def _generate_sql(self):
        table_name = self.name_edit.text().strip()
        if not table_name:
            return None
        cols = self._collect_columns()
        if not cols:
            return None
        return build_create_table(table_name, cols)

    def _preview_sql(self):
        sql = self._generate_sql()
        self.sql_preview.setPlainText(
            sql or "-- 请填写表名和至少一列")

    def _on_accept(self):
        sql = self._generate_sql()
        if not sql:
            QMessageBox.warning(self, "提示", "请填写表名和至少一列")
            return
        _, err = self.conn.execute(sql)
        if err:
            QMessageBox.critical(self, "创建失败", err)
            return
        QMessageBox.information(self, "成功", "表已创建")
        self.accept()