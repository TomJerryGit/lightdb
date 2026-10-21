"""数据编辑器 - 可视化增删改（P1-5：类型编辑 + 单事务原子提交）"""
import json
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QMessageBox, QAbstractItemView, QSpinBox,
    QComboBox, QDateEdit, QDateTimeEdit, QLineEdit,
    QStyledItemDelegate, QStyleOptionViewItem,
)
from PySide6.QtCore import Qt, Signal, QDate, QDateTime
from PySide6.QtGui import QColor, QBrush, QIntValidator, QDoubleValidator

from ..db.table_ops import (
    build_select, build_insert, build_update, build_delete,
)

# ==================== P1-5 类型识别 / 校验（纯函数，可单测） ====================

_INT_TYPES = ('INT', 'BIGINT', 'SMALLINT', 'TINYINT', 'MEDIUMINT', 'BIT')
_FLOAT_TYPES = ('FLOAT', 'DOUBLE', 'DECIMAL', 'NUMERIC', 'REAL')
_DATETIME_TYPES = ('DATETIME', 'TIMESTAMP')
_DATE_TYPES = ('DATE',)


def editor_kind_for_type(col_type) -> str:
    """返回列编辑控件类别：datetime/date/bool/int/float/json/text。"""
    t = str(col_type or '').upper()
    if 'JSON' in t:
        return 'json'
    if 'BOOL' in t or t == 'TINYINT(1)':
        return 'bool'
    if any(k in t for k in _DATETIME_TYPES):
        return 'datetime'
    if any(k in t for k in _DATE_TYPES):
        return 'date'
    if any(k in t for k in _FLOAT_TYPES):
        return 'float'
    if any(k in t for k in _INT_TYPES):
        return 'int'
    return 'text'


_DATE_FORMATS = ('%Y-%m-%d', '%Y/%m/%d')
_DATETIME_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S',
                     '%Y-%m-%d %H:%M', '%Y-%m-%d')


def validate_typed_value(col_type, text):
    """按列类型校验单元格文本。

    返回 ``(ok, value, error)``：
      * 文本 `NULL`/空字符串 → `(True, None, '')`（显式 NULL，沿用旧行为）；
      * 数值非法 / 日期格式错 / JSON 非法 → `(False, None, 原因)`；
      * 日期时间统一规范化为 `YYYY-MM-DD` / `YYYY-MM-DD HH:MM:SS` 字符串。
    """
    if text is None:
        return True, None, ''
    s = str(text).strip()
    if s == '' or s.upper() == 'NULL':
        return True, None, ''
    kind = editor_kind_for_type(col_type)
    try:
        if kind == 'bool':
            low = s.lower()
            if low in ('1', 'true', 'yes', 'y', '是'):
                return True, True, ''
            if low in ('0', 'false', 'no', 'n', '否'):
                return True, False, ''
            return False, None, '布尔值必须是 TRUE/FALSE/1/0/NULL'
        if kind == 'int':
            return True, int(s), ''
        if kind == 'float':
            return True, float(s), ''
        if kind == 'date':
            for fmt in _DATE_FORMATS:
                try:
                    return True, datetime.strptime(s, fmt).strftime('%Y-%m-%d'), ''
                except ValueError:
                    continue
            return False, None, '日期格式应为 YYYY-MM-DD'
        if kind == 'datetime':
            for fmt in _DATETIME_FORMATS:
                try:
                    dt = datetime.strptime(s, fmt)
                    return True, dt.strftime('%Y-%m-%d %H:%M:%S'), ''
                except ValueError:
                    continue
            return False, None, '日期时间格式应为 YYYY-MM-DD HH:MM:SS'
        if kind == 'json':
            try:
                json.loads(s)
            except Exception as exc:                  # noqa: BLE001
                return False, None, f'JSON 非法: {exc}'
            return True, s, ''
        return True, s, ''
    except (ValueError, TypeError):
        return False, None, f'{col_type} 值非法: {s!r}'


def execute_atomic(conn, statements) -> dict:
    """在**一个事务**内执行 statements（BEGIN → 逐条 → COMMIT）。

    任一语句失败 → ROLLBACK，返回 ``ok=False, rolled_back=True``。
    `DBConnection.execute()` 返回 `(rowcount, error_text)`，错误文本非空视为失败。
    返回 dict: ok / executed / errors / rolled_back。
    """
    statements = [s for s in (statements or []) if s]
    executed = 0

    def _run(sql):
        res = conn.execute(sql)
        if isinstance(res, tuple):
            n = res[0] if len(res) > 0 else 0
            err = res[1] if len(res) > 1 else None
        else:
            n, err = res, None
        return n, err

    try:
        _n, err = _run('BEGIN')
        if err:
            raise RuntimeError(str(err))
        for stmt in statements:
            _n, err = _run(stmt)
            if err:
                raise RuntimeError(str(err))
            executed += 1
        _n, err = _run('COMMIT')
        if err:
            raise RuntimeError(str(err))
        return {'ok': True, 'executed': executed, 'errors': [],
                'rolled_back': False}
    except Exception as exc:                          # noqa: BLE001
        try:
            _run('ROLLBACK')
        except Exception:
            pass
        return {'ok': False, 'executed': executed,
                'errors': [str(exc)], 'rolled_back': True}


class TypedCellDelegate(QStyledItemDelegate):
    """按列类型提供编辑控件（DATE/DATETIME/BOOL/INT/FLOAT/JSON/文本）。"""

    def __init__(self, columns_meta, parent=None):
        super().__init__(parent)
        self.columns_meta = columns_meta

    def _kind(self, index):
        try:
            return editor_kind_for_type(
                self.columns_meta[index.column()].get('type'))
        except Exception:
            return 'text'

    def createEditor(self, parent, option, index):
        kind = self._kind(index)
        try:
            if kind == 'bool':
                editor = QComboBox(parent)
                editor.addItems(['NULL', 'TRUE', 'FALSE'])
                return editor
            if kind == 'datetime':
                editor = QDateTimeEdit(parent)
                editor.setCalendarPopup(True)
                editor.setDisplayFormat('yyyy-MM-dd HH:mm:ss')
                editor.setDateTime(QDateTime.currentDateTime())
                return editor
            if kind == 'date':
                editor = QDateEdit(parent)
                editor.setCalendarPopup(True)
                editor.setDisplayFormat('yyyy-MM-dd')
                editor.setDate(QDate.currentDate())
                return editor
            if kind == 'int':
                editor = QLineEdit(parent)
                editor.setValidator(QIntValidator(editor))
                editor.setPlaceholderText('整数 或 NULL')
                return editor
            if kind == 'float':
                editor = QLineEdit(parent)
                editor.setValidator(QDoubleValidator(editor))
                editor.setPlaceholderText('数值 或 NULL')
                return editor
            if kind == 'json':
                editor = QLineEdit(parent)
                editor.setPlaceholderText('JSON（如 {"a": 1}）或 NULL')
                return editor
        except Exception:
            pass
        try:
            return super().createEditor(parent, option, index)
        except TypeError:
            # 测试/嵌入方可能传 option=None：退化到纯文本编辑器
            return QLineEdit(parent)

    def setEditorData(self, editor, index):
        text = index.data(Qt.DisplayRole)
        text = '' if text is None else str(text)
        kind = self._kind(index)
        try:
            if kind == 'bool' and isinstance(editor, QComboBox):
                low = text.strip().lower()
                if low in ('1', 'true', 'yes', '是'):
                    editor.setCurrentText('TRUE')
                elif low in ('0', 'false', 'no', '否'):
                    editor.setCurrentText('FALSE')
                else:
                    editor.setCurrentText('NULL')
                return
            if kind == 'datetime' and isinstance(editor, QDateTimeEdit):
                ok, value, _err = validate_typed_value('DATETIME', text)
                if ok and value:
                    dt = QDateTime.fromString(value, 'yyyy-MM-dd HH:mm:ss')
                    if dt.isValid():
                        editor.setDateTime(dt)
                return
            if kind == 'date' and isinstance(editor, QDateEdit):
                ok, value, _err = validate_typed_value('DATE', text)
                if ok and value:
                    d = QDate.fromString(value, 'yyyy-MM-dd')
                    if d.isValid():
                        editor.setDate(d)
                return
            if isinstance(editor, QLineEdit):
                editor.setText('' if text.upper() == 'NULL' else text)
        except Exception:
            pass

    def setModelData(self, editor, model, index):
        kind = self._kind(index)
        try:
            if kind == 'bool' and isinstance(editor, QComboBox):
                text = editor.currentText()
            elif kind == 'datetime' and isinstance(editor, QDateTimeEdit):
                text = editor.dateTime().toString('yyyy-MM-dd HH:mm:ss')
            elif kind == 'date' and isinstance(editor, QDateEdit):
                text = editor.date().toString('yyyy-MM-dd')
            elif isinstance(editor, QLineEdit):
                text = editor.text()
            else:
                text = editor.text() if hasattr(editor, 'text') else ''
            model.setData(index, text, Qt.EditRole)
        except Exception:
            pass


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
        self.invalid_cells = {}    # {(row, col): error_text}（P1-5 校验失败）

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
        # P1-5：按列类型提供编辑控件（日期/布尔/数值/JSON）
        try:
            self._delegate = TypedCellDelegate(self.columns_meta, self.table)
            self.table.setItemDelegate(self._delegate)
        except Exception:
            self._delegate = None
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
            self.invalid_cells.clear()
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
        col_meta = self.columns_meta[ci]
        ok, val, err = validate_typed_value(col_meta.get('type'), item.text())
        if not ok:
            # P1-5：非法值显式报错并阻止提交（不写入 row_data / modified）
            self.invalid_cells[(ri, ci)] = err
            self._set_item_error(item, err)
            self._update_pending()
            self._mark_row(ri)
            return
        self.invalid_cells.pop((ri, ci), None)
        self._clear_item_error(item)
        row_info['values'][ci] = val

        if row_info['is_new']:
            pass
        elif val != row_info['orig_values'][ci]:
            self.modified.setdefault(ri, {})[self.column_names[ci]] = val
        else:
            if ri in self.modified:
                self.modified[ri].pop(self.column_names[ci], None)
                if not self.modified[ri]:
                    del self.modified[ri]

        self._update_pending()
        self._mark_row(ri)

    def _set_item_error(self, item, error):
        self.table.blockSignals(True)
        try:
            item.setForeground(QColor("#f48771"))
            item.setToolTip(f'非法值: {error}')
        finally:
            self.table.blockSignals(False)

    def _clear_item_error(self, item):
        self.table.blockSignals(True)
        try:
            item.setForeground(QBrush())
            item.setToolTip('')
        finally:
            self.table.blockSignals(False)

    def _parse_value(self, text, col_type):
        """兼容旧调用：解析非法时回退原文（校验走 validate_typed_value）。"""
        ok, value, _err = validate_typed_value(col_type, text)
        return value if ok else text

    def set_cell_null(self, row, col):
        """显式把单元格设为 NULL（右键/工具栏"设为 NULL"路径）。"""
        item = self.table.item(row, col)
        if item is None:
            return False
        self.table.blockSignals(True)
        try:
            item.setText('NULL')
            item.setForeground(QColor("#808080"))
            f = item.font()
            f.setItalic(True)
            item.setFont(f)
        finally:
            self.table.blockSignals(False)
        self._on_item_changed(item)
        return True

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
                self.table.blockSignals(True)
                try:
                    item.setBackground(QBrush(color) if color else QBrush())
                finally:
                    self.table.blockSignals(False)

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
        n_bad = len(self.invalid_cells)
        parts = []
        if n_mod or n_new or n_del:
            parts.append(f"待提交: 修改 {n_mod} 行, 新增 {n_new} 行, 删除 {n_del} 行")
        if n_bad:
            parts.append(f"非法值 {n_bad} 处（修复后才能提交）")
        self.pending_label.setText('；'.join(parts))

    # ==================== 提交 ====================

    def _save(self):
        # P1-5：存在非法值（JSON/日期/数值）→ 先显式报错、不提交
        # （即使本次还没有其它待提交修改，也要把非法值暴露出来）
        if self.invalid_cells:
            lines = []
            for (ri, ci), err in list(self.invalid_cells.items())[:10]:
                col = self.column_names[ci] if ci < len(self.column_names) else ci
                lines.append(f"第 {ri + 1} 行 `{col}`：{err}")
            QMessageBox.critical(
                self, "存在非法值，未提交",
                f"有 {len(self.invalid_cells)} 处单元格校验失败，请先修复：\n"
                + '\n'.join(lines))
            return

        if not (self.modified or self.new_rows or self.deleted_rows):
            QMessageBox.information(self, "提示", "没有待提交的修改")
            return

        if not self.pk_cols:
            QMessageBox.warning(
                self, "错误",
                "该表没有主键，无法安全地修改/删除。\n请使用 SQL 编辑器手动操作。")
            return

        stmts = self.build_pending_statements()
        if not stmts:
            return
        self.commit_statements(stmts, ask=True)

    def build_pending_statements(self) -> list:
        """把待提交的删除/修改/新增转成 SQL 列表（不执行；供预览/测试）。"""
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
        return stmts

    def commit_statements(self, stmts, ask=True) -> dict:
        """P1-5：在**一个事务**内提交全部语句（单条修改也走同一路径）。

        BEGIN → 逐条执行 → COMMIT；任一失败 → ROLLBACK，显式提示并刷新数据。
        """
        stmts = [s for s in (stmts or []) if s]
        if not stmts:
            return {'ok': True, 'executed': 0, 'errors': [],
                    'rolled_back': False, 'cancelled': False}

        if ask:
            preview = '\n'.join(stmts[:15])
            if len(stmts) > 15:
                preview += f"\n... 还有 {len(stmts) - 15} 条"
            ret = QMessageBox.question(
                self, "确认提交",
                f"将在一个事务内执行 {len(stmts)} 条 SQL：\n\n{preview[:2000]}",
                QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                return {'ok': False, 'executed': 0, 'errors': [],
                        'rolled_back': False, 'cancelled': True}

        result = execute_atomic(self.conn, stmts)
        result['cancelled'] = False
        if result['ok']:
            QMessageBox.information(
                self, "成功",
                f"已在一个事务中提交 {result['executed']} 条 SQL")
            try:
                self.data_changed.emit()
            except Exception:
                pass
        else:
            err_text = '\n'.join(result['errors'][:5])
            QMessageBox.critical(
                self, "提交失败（已回滚）",
                f"事务已 ROLLBACK，未留下半提交数据。\n"
                f"已执行 {result['executed']} 条后失败：\n{err_text}")
        self.reload()
        return result

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