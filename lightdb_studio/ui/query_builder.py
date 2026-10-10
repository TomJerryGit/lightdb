"""
可视化查询构建器
- 选择表
- 勾选列
- 添加 WHERE 条件
- ORDER BY / LIMIT
- 生成 SQL
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QListWidget, QListWidgetItem, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QDialogButtonBox, QGroupBox,
    QSpinBox, QWidget, QAbstractItemView, QMessageBox, QCheckBox,
    QLineEdit, QInputDialog
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont


OP_OPTIONS = ['=', '!=', '>', '>=', '<', '<=', 'LIKE', 'IN', 'IS NULL', 'IS NOT NULL']


class QueryBuilderDialog(QDialog):
    def __init__(self, conn, table_name, columns_meta, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.table_name = table_name
        self.columns_meta = columns_meta
        self.column_names = [c['name'] for c in columns_meta]
        self.setWindowTitle(f"查询构建器 - {table_name}")
        self.resize(900, 700)
        self._build_ui()
        self._rebuild_sql()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 列选择
        cols_group = QGroupBox("选择列")
        cg_layout = QHBoxLayout(cols_group)

        self.cols_list = QListWidget()
        self.cols_list.setSelectionMode(QAbstractItemView.MultiSelection)
        for c in self.column_names:
            item = QListWidgetItem(c)
            item.setCheckState(Qt.Unchecked)
            self.cols_list.addItem(item)
        self.cols_list.itemChanged.connect(self._rebuild_sql)
        cg_layout.addWidget(self.cols_list, 1)

        cg_btns = QVBoxLayout()
        a = QPushButton("全选")
        a.clicked.connect(lambda: self._toggle_all(True))
        cg_btns.addWidget(a)
        a = QPushButton("全不选")
        a.clicked.connect(lambda: self._toggle_all(False))
        cg_btns.addWidget(a)
        cg_btns.addStretch()
        cg_layout.addLayout(cg_btns)

        layout.addWidget(cols_group)

        # WHERE 条件
        where_group = QGroupBox("WHERE 条件（AND 连接）")
        wg_layout = QVBoxLayout(where_group)

        self.cond_table = QTableWidget()
        self.cond_table.setColumnCount(5)
        self.cond_table.setHorizontalHeaderLabels(
            ['列', '操作', '值', 'AND/OR', ''])
        self.cond_table.setColumnWidth(0, 150)
        self.cond_table.setColumnWidth(1, 100)
        self.cond_table.setColumnWidth(2, 200)
        self.cond_table.setColumnWidth(3, 70)
        self.cond_table.setColumnWidth(4, 50)
        self.cond_table.horizontalHeader().setStretchLastSection(False)
        wg_layout.addWidget(self.cond_table)

        wbtn = QHBoxLayout()
        a = QPushButton("➕ 添加条件")
        a.clicked.connect(self._add_condition)
        wbtn.addWidget(a)
        a = QPushButton("➖ 删除条件")
        a.clicked.connect(self._del_condition)
        wbtn.addWidget(a)
        wbtn.addStretch()
        wg_layout.addLayout(wbtn)

        layout.addWidget(where_group)

        # ORDER BY + LIMIT
        bottom = QHBoxLayout()

        order_group = QGroupBox("ORDER BY")
        og_layout = QHBoxLayout(order_group)
        self.order_col = QComboBox()
        self.order_col.addItems(['(无)'] + self.column_names)
        self.order_col.currentTextChanged.connect(self._rebuild_sql)
        og_layout.addWidget(self.order_col)

        self.order_dir = QComboBox()
        self.order_dir.addItems(['ASC', 'DESC'])
        self.order_dir.currentTextChanged.connect(self._rebuild_sql)
        og_layout.addWidget(self.order_dir)
        bottom.addWidget(order_group)

        limit_group = QGroupBox("LIMIT")
        lg_layout = QHBoxLayout(limit_group)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 1000000)
        self.limit_spin.setValue(1000)
        self.limit_spin.valueChanged.connect(self._rebuild_sql)
        lg_layout.addWidget(self.limit_spin)
        bottom.addWidget(limit_group)

        bottom.addStretch()
        layout.addLayout(bottom)

        # SQL 预览
        layout.addWidget(QLabel("生成的 SQL:"))
        self.sql_view = QTextEdit()
        self.sql_view.setFont(QFont("Consolas", 10))
        self.sql_view.setMaximumHeight(120)
        self.sql_view.setStyleSheet("background: #1e1e1e; color: #ce9178;")
        layout.addWidget(self.sql_view)

        # 按钮
        buttons = QDialogButtonBox()
        run_btn = QPushButton("▶ 执行查询")
        run_btn.clicked.connect(self._run)
        buttons.addButton(run_btn, QDialogButtonBox.AcceptRole)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        buttons.addButton(close_btn, QDialogButtonBox.RejectRole)
        layout.addWidget(buttons)

    def _toggle_all(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.cols_list.count()):
            self.cols_list.item(i).setCheckState(state)

    def _add_condition(self):
        r = self.cond_table.rowCount()
        self.cond_table.insertRow(r)

        # 列
        col_combo = QComboBox()
        col_combo.addItems(self.column_names)
        col_combo.currentTextChanged.connect(self._rebuild_sql)
        self.cond_table.setCellWidget(r, 0, col_combo)

        # 操作
        op_combo = QComboBox()
        op_combo.addItems(OP_OPTIONS)
        op_combo.currentTextChanged.connect(self._rebuild_sql)
        self.cond_table.setCellWidget(r, 1, op_combo)

        # 值
        val_edit = QLineEdit()
        val_edit.textChanged.connect(self._rebuild_sql)
        self.cond_table.setCellWidget(r, 2, val_edit)

        # AND/OR
        logic_combo = QComboBox()
        logic_combo.addItems(['AND', 'OR'])
        logic_combo.currentTextChanged.connect(self._rebuild_sql)
        self.cond_table.setCellWidget(r, 3, logic_combo)

        # 删除按钮
        del_btn = QPushButton("✕")
        del_btn.clicked.connect(lambda: self._del_row(r))
        self.cond_table.setCellWidget(r, 4, del_btn)

        self._rebuild_sql()

    def _del_row(self, r):
        self.cond_table.removeRow(r)
        # 重新绑定删除按钮
        for i in range(self.cond_table.rowCount()):
            btn = self.cond_table.cellWidget(i, 4)
            if btn:
                btn.clicked.disconnect()
                btn.clicked.connect(lambda checked=False, row=i:
                                    self._del_row(row))
        self._rebuild_sql()

    def _del_condition(self):
        rows = sorted({i.row() for i in self.cond_table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.cond_table.removeRow(r)
        self._rebuild_sql()

    def _rebuild_sql(self, *_):
        # SELECT 列
        selected = []
        for i in range(self.cols_list.count()):
            item = self.cols_list.item(i)
            if item.checkState() == Qt.Checked:
                selected.append(item.text())
        if not selected:
            selected = ['*']

        sql = f"SELECT {', '.join('`' + c + '`' for c in selected)}\n"
        sql += f"FROM `{self.table_name}`"

        # WHERE
        conds = []
        for r in range(self.cond_table.rowCount()):
            col = self.cond_table.cellWidget(r, 0)
            op = self.cond_table.cellWidget(r, 1)
            val = self.cond_table.cellWidget(r, 2)
            logic = self.cond_table.cellWidget(r, 3)
            if not (col and op):
                continue
            c = col.currentText()
            o = op.currentText()
            v = val.text().strip() if val else ''

            if o == 'IS NULL':
                cond_str = f"`{c}` IS NULL"
            elif o == 'IS NOT NULL':
                cond_str = f"`{c}` IS NOT NULL"
            elif o == 'IN':
                cond_str = f"`{c}` IN ({v})"
            elif o == 'LIKE':
                cond_str = f"`{c}` LIKE '{v}'"
            elif o in ('>', '>=', '<', '<='):
                cond_str = f"`{c}` {o} {v}"
            elif o in ('=', '!='):
                # 自动判断引号
                try:
                    int(v)
                    cond_str = f"`{c}` {o} {v}"
                except (ValueError, TypeError):
                    try:
                        float(v)
                        cond_str = f"`{c}` {o} {v}"
                    except (ValueError, TypeError):
                        cond_str = f"`{c}` {o} '{v}'"
            else:
                cond_str = f"`{c}` {o} '{v}'"

            if conds:
                logic_str = logic.currentText() if logic else 'AND'
                conds.append(f"{logic_str} {cond_str}")
            else:
                conds.append(cond_str)

        if conds:
            sql += "\nWHERE " + '\n  '.join(conds)

        # ORDER BY
        oc = self.order_col.currentText()
        if oc and oc != '(无)':
            sql += f"\nORDER BY `{oc}` {self.order_dir.currentText()}"

        # LIMIT
        sql += f"\nLIMIT {self.limit_spin.value()}"

        self.sql_view.setPlainText(sql)

    def _run(self):
        sql = self.sql_view.toPlainText().strip()
        if not sql:
            return
        try:
            cols, rows = self.conn.query(sql)
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return
        # 用一个简单的模态查看结果
        result_dlg = QueryResultDialog(cols, rows, sql, self)
        result_dlg.exec()

    def get_sql(self) -> str:
        return self.sql_view.toPlainText().strip()


class QueryResultDialog(QDialog):
    """简单的结果查看对话框"""
    def __init__(self, columns, rows, sql, parent=None):
        super().__init__(parent)
        self.setWindowTitle("查询结果")
        self.resize(900, 500)
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>{sql[:200]}</b>"))

        table = QTableWidget()
        table.setRowCount(len(rows))
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(columns)
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(
                    'NULL' if v is None else str(v)))
        table.resizeColumnsToContents()
        layout.addWidget(table)

        info = QLabel(f"返回 {len(rows)} 行")
        info.setStyleSheet("color: #969696; padding: 4px;")
        layout.addWidget(info)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)