"""数据编辑器 - 可视化增删改"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QMessageBox, QAbstractItemView, QSpinBox
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush

from ..db.table_ops import (
    build_select, build_insert, build_update, build_delete,
)


class DataEditor(QWidget):
    """数据网格编辑器"""

    status_changed = Signal(str)
    data_changed = Signal()   # 数据变更通知（让主窗口刷新表结构）

    def __init__(self, conn, table_name, columns_meta):
        super().__init__()
        self.conn = conn
        self.table_name = table_name
        self.columns_meta = columns_meta
        self.column_names = [c['name'] for c in columns_meta]
        self.pk_cols = [c['name'] for c in columns_meta
                        if c.get('key') == 'PRI']

        self.modified = {}     # {row_index: {col: new_value}}
        self.new_rows = set()
        self.deleted_rows = set()

        self.page_size = 200
        self.page_offset = 0
        self.total_count = 0
        self._row_data = []

        self._build_ui()
        self.reload()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 工具栏
        toolbar = QWidget()
        toolbar.setStyleSheet(
            "background: #2d2d30; border-bottom: 1px solid #3c3c3c;")
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(6, 4, 6, 4)
        tl.setSpacing(6)

        self.save_btn = QPushButton("💾 提交")
        self.save_btn.clicked.connect(self._save)
        tl.addWidget(self.save_btn)

        self.revert_btn = QPushButton("↩ 撤销")
        self.revert_btn.clicked.connect(self._revert)
        tl.addWidget(self.revert_btn)

        self.add_btn = QPushButton("➕ 新增")
        self.add_btn.clicked.connect(self._add_row)
        tl.addWidget(self.add_btn)

        self.del_btn = QPushButton("➖ 删除")
        self.del_btn.clicked.connect(self._delete_rows)
        tl.addWidget(self.del_btn)

        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.clicked.connect(self.reload)
        tl.addWidget(self.refresh_btn)

        tl.addStretch()

        tl.addWidget(QLabel("每页:"))
        self.page_spin = QSpinBox()
        self.page_spin.setRange(10, 5000)
        self.page_spin.setValue(200)
        self.page_spin.setSingleStep(50)
        self.page_spin.valueChanged.connect(self._change_page_size)
        tl.addWidget(self.page_spin)

        self.prev_btn = QPushButton("◀")
        self.prev_btn.clicked.connect(self._prev_page)
        self.prev_btn.setMaximumWidth(40)
        tl.addWidget(self.prev_btn)

        self.page_label = QLabel("1/1")
        self.page_label.setStyleSheet("color: #d4d4d4; padding: 0 6px;")
        tl.addWidget(self.page_label)

        self.next_btn = QPushButton("▶")
        self.next_btn.clicked.connect(self._next_page)
        self.next_btn.setMaximumWidth(40)
        tl.addWidget(self.next_btn)

        layout.addWidget(toolbar)

        # 表格
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table)

        # 状态栏
        info_bar = QWidget()
        info_bar.setStyleSheet(
            "background: #252526; border-top: 1px solid #3c3c3c;")
        il = QHBoxLayout(info_bar)
        il.setContentsMargins(8, 3, 8, 3)
        self.info_label = QLabel("就绪")
        self.info_label.setStyleSheet("color: #969696;")
        il.addWidget(self.info_label)
        il.addStretch()
        self.pending_label = QLabel("")
        self.pending_label.setStyleSheet("color: #dcdcaa;")
        il.addWidget(self.pending_label)
        layout.addWidget(info_bar)

    # ==================== 加载 ====================

    def reload(self):
        try:
            cols, rows = self.conn.query(
                build_select(self.table_name, limit=self.page_size,
                             offset=self.page_offset))
            try:
                _, cnt = self.conn.query(
                    f"SELECT COUNT(*) FROM `{self.table_name}`")
                self.total_count = cnt[0][0] if cnt else 0
            except Exception:
                self.total_count = len(rows)

            self._populate(rows)

            total_pages = max(1, (self.total_count + self.page_size - 1) //
                              self.page_size)
            cur_page = self.page_offset // self.page_size + 1
            self.page_label.setText(f"{cur_page}/{total_pages}")
            self.info_label.setText(
                f"共 {self.total_count} 行，当前显示 {len(rows)} 行")

            self.modified.clear()
            self.new_rows.clear()
            self.deleted_rows.clear()
            self._update_pending()
        except Exception as e:
            QMessageBox.critical(self, "加载失败", str(e))

    def _populate(self, rows):
        self.table.blockSignals(True)
        self.table.clear()
        self.table.setColumnCount(len(self.column_names))
        self.table.setHorizontalHeaderLabels(self.column_names)
        self.table.setRowCount(len(rows))

        self._row_data = []
        for ri, row in enumerate(rows):
            self._row_data.append({
                'is_new': False,
                'values': list(row),
                'orig_values': list(row),
            })
            for ci, val in enumerate(row):
                item = QTableWidgetItem()
                if val is None:
                    item.setText("NULL")
                    item.setForeground(QColor("#808080"))
                    f = item.font()
                    f.setItalic(True)
                    item.setFont(f)
                elif isinstance(val, bytes):
                    item.setText(f"<binary {len(val)}B>")
                else:
                    item.setText(str(val))
                item.setData(Qt.UserRole, val)
                self.table.setItem(ri, ci, item)

        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        for ci in range(self.table.columnCount()):
            if self.table.columnWidth(ci) > 300:
                self.table.setColumnWidth(ci, 300)

    # ==================== 编辑 ====================

    def _on_item_changed(self, item):
        ri = item.row()
        ci = item.column()
        if ri >= len(self._row_data):
            return
        row_info = self._row_data[ri]
        new_text = item.text()

        col_meta = self.columns_meta[ci]
        val = self._parse_value(new_text, col_meta['type'])
        if new_text == "NULL":
            val = None

        row_info['values'][ci] = val

        if row_info['is_new']:
            pass
        else:
            if val != row_info['orig_values'][ci]:
                self.modified.setdefault(ri, {})[self.column_names[ci]] = val
            else:
                if ri in self.modified:
                    self.modified[ri].pop(self.column_names[ci], None)
                    if not self.modified[ri]:
                        del self.modified[ri]

        self._update_pending()
        self._mark_row(ri)

    def _parse_value(self, text, col_type):
        if text == "NULL" or text == "":
            return None
        t = col_type.upper()
        try:
            if 'INT' in t:
                return int(text)
            if 'FLOAT' in t or 'DOUBLE' in t or 'DECIMAL' in t:
                return float(text)
            return text
        except (ValueError, TypeError):
            return text

    def _mark_row(self, ri):
        if ri >= len(self._row_data):
            return
        row_info = self._row_data[ri]
        color = None
        if ri in self.deleted_rows:
            color = QColor("#5a1d1d")
        elif row_info['is_new']:
            color = QColor("#1d3a1d")
        elif ri in self.modified:
            color = QColor("#3a3a1d")

        for ci in range(self.table.columnCount()):
            item = self.table.item(ri, ci)
            if item:
                item.setBackground(QBrush(color) if color else QBrush())

    def _add_row(self):
        ri = self.table.rowCount()
        self.table.insertRow(ri)
        self._row_data.append({
            'is_new': True,
            'values': [None] * len(self.column_names),
            'orig_values': [None] * len(self.column_names),
        })
        for ci in range(self.table.columnCount()):
            item = QTableWidgetItem("NULL")
            item.setForeground(QColor("#808080"))
            self.table.setItem(ri, ci, item)
        self.new_rows.add(ri)
        self._mark_row(ri)
        self._update_pending()
        self.table.scrollToBottom()

    def _delete_rows(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        if not rows:
            QMessageBox.information(self, "提示", "请先选中要删除的行")
            return
        for ri in rows:
            self.deleted_rows.add(ri)
            self._mark_row(ri)
        self._update_pending()

    def _update_pending(self):
        n_mod = len(self.modified)
        n_new = len(self.new_rows)
        n_del = len(self.deleted_rows)
        if n_mod or n_new or n_del:
            self.pending_label.setText(
                f"待提交: 修改 {n_mod} 行, 新增 {n_new} 行, 删除 {n_del} 行")
        else:
            self.pending_label.setText("")

    # ==================== 提交 ====================

    def _save(self):
        if not (self.modified or self.new_rows or self.deleted_rows):
            QMessageBox.information(self, "提示", "没有待提交的修改")
            return

        if not self.pk_cols:
            QMessageBox.warning(
                self, "错误",
                "该表没有主键，无法安全地修改/删除。\n请使用 SQL 编辑器手动操作。")
            return

        stmts = []

        # 删除
        for ri in self.deleted_rows:
            if ri >= len(self._row_data):
                continue
            row_info = self._row_data[ri]
            if row_info['is_new']:
                continue
            pk_values = [row_info['orig_values'][self.column_names.index(pk)]
                         for pk in self.pk_cols]
            stmts.append(build_delete(self.table_name, self.pk_cols, pk_values))

        # 修改
        for ri, changes in self.modified.items():
            if ri >= len(self._row_data):
                continue
            row_info = self._row_data[ri]
            pk_values = [row_info['orig_values'][self.column_names.index(pk)]
                         for pk in self.pk_cols]
            stmts.append(build_update(
                self.table_name, self.pk_cols, pk_values,
                list(changes.keys()), list(changes.values())))

        # 新增
        for ri in self.new_rows:
            if ri >= len(self._row_data):
                continue
            row_info = self._row_data[ri]
            cols, vals = [], []
            for ci, c in enumerate(self.column_names):
                v = row_info['values'][ci]
                if c in self.pk_cols and v is None:
                    continue
                cols.append(c)
                vals.append(v)
            if cols:
                stmts.append(build_insert(self.table_name, cols, vals))

        if not stmts:
            return

        preview = '\n'.join(stmts[:15])
        if len(stmts) > 15:
            preview += f"\n... 还有 {len(stmts) - 15} 条"

        ret = QMessageBox.question(
            self, "确认提交",
            f"将执行 {len(stmts)} 条 SQL：\n\n{preview[:2000]}",
            QMessageBox.Yes | QMessageBox.No)
        if ret != QMessageBox.Yes:
            return

        errors = []
        for stmt in stmts:
            _, err = self.conn.execute(stmt)
            if err:
                errors.append(f"{stmt[:80]}... → {err}")
                if len(errors) > 5:
                    break

        if errors:
            QMessageBox.critical(
                self, "部分失败",
                f"{len(errors)} 条失败:\n" + '\n'.join(errors[:5]))
        else:
            QMessageBox.information(self, "成功",
                                    f"已提交 {len(stmts)} 条 SQL")
            self.data_changed.emit()

        self.reload()

    def _revert(self):
        if not (self.modified or self.new_rows or self.deleted_rows):
            return
        ret = QMessageBox.question(self, "确认", "放弃所有未提交的修改？")
        if ret == QMessageBox.Yes:
            self.reload()

    # ==================== 分页 ====================

    def _change_page_size(self, v):
        self.page_size = v
        self.page_offset = 0
        self.reload()

    def _prev_page(self):
        if self.page_offset <= 0:
            return
        self.page_offset = max(0, self.page_offset - self.page_size)
        self.reload()

    def _next_page(self):
        if self.page_offset + self.page_size >= self.total_count:
            return
        self.page_offset += self.page_size
        self.reload()

    def get_current_data(self):
        """供导出使用：返回 (columns, rows)"""
        rows = [r['values'] for r in self._row_data
                if self._row_data.index(r) not in self.deleted_rows]
        return self.column_names, rows