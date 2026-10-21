"""查询结果表格

P0（2026-10-20 GUI 路线图）：
1. **数值/日期感知排序**：`QTableWidgetItem` 的默认排序是纯文本字典序
   （`88.5` 会排在 `270.5` 前面）。这里给每个单元格保存原始值
   （`Qt.UserRole`），表头点击后用类型化 key 重排：数字按数值、日期时间按
   时间、其余按大小写不敏感文本；**NULL（含未设置的单元格）无论升/降序
   都固定在最后**，不依赖 Qt 默认行为。
2. **结果过滤条**：输入框 + 列下拉 + 大小写开关 + 清空按钮。只对已加载
   行做 `setRowHidden`（不重新查库），无匹配时显示"无匹配行"空态；与排序
   叠加时排序后自动重新应用过滤。
3. **错误面板**：保留（错误码 + SQLSTATE + 完整报文 + SQL 原文），新增
   错误分类（查询/连接/权限）标签与颜色，并保留"复制错误"按钮。
"""
import datetime as _dt
import re

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QLabel, QHBoxLayout, QHeaderView, QAbstractItemView,
    QPlainTextEdit, QPushButton, QApplication, QLineEdit, QComboBox,
    QCheckBox, QMenu, QDialog, QDialogButtonBox, QListWidget,
    QListWidgetItem, QFileDialog,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QAction

# 从 `format_db_error` 的文本里回捞字段（如 `错误码  : 1064` / `SQLSTATE: 42000`）。
# 目的：错误以字符串形式到达界面时（DBConnection.execute 的返回值就是这种
# 形态），标题行不能退化成「(未提供)」，否则用户以为诊断字段丢了。
_CODE_RE = re.compile(r'错误码\s*[:：]\s*(\d+)')
_STATE_RE = re.compile(r'SQLSTATE\s*[:：]\s*([0-9A-Za-z]{5})')

# 数值文本：整数/小数/正负号/科学计数法
_NUM_RE = re.compile(r'^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$')
# 日期/时间文本：2026-10-20 / 2026/10/20 / 2026-10-20 12:30(:45)(.123456)
_DATE_RE = re.compile(
    r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})'
    r'(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?$')

# NULL 哨兵：QTableWidgetItem.data() 未设置时也返回 None，无法区分
# 「真 NULL」与「没存值」。用模块级对象保存真 NULL（PySide6 原样保留对象身份）。
NULL_SENTINEL = object()

# 错误分类：key -> (显示名, 颜色)
ERROR_CATEGORIES = {
    'query': ('查询错误', '#f48771'),
    'connection': ('连接错误', '#d7ba7d'),
    'permission': ('权限错误', '#f14c4c'),
}

_PERMISSION_ERRNOS = {1044, 1045, 1142, 1143, 1227, 1370, 3118}
_CONNECTION_ERRNOS = {1040, 1129, 1130, 1203, 2002, 2003, 2005, 2006,
                      2013, 2026, 2055}


def parse_error_fields(text):
    """从错误文本里解析 (errno, sqlstate)；解析不到返回 None（不编造）。"""
    if not text:
        return None, None
    m = _CODE_RE.search(text)
    errno = int(m.group(1)) if m else None
    m2 = _STATE_RE.search(text)
    return errno, (m2.group(1) if m2 else None)


def classify_error(errno=None, sqlstate=None, text='') -> dict:
    """把查询/连接/权限错误粗分类（错误面板标签用）。

    返回 ``{'key', 'category', 'color'}``；无法判断时归为「查询错误」。
    仅用于 UI 提示与配色，不影响 SQL/错误码语义。
    """
    low = (text or '').lower()
    state = str(sqlstate or '').upper()
    if (errno in _PERMISSION_ERRNOS
            or 'access denied' in low or 'permission denied' in low
            or '权限' in (text or '') or 'denied' in low
            or state in ('42000', '28000') and 'denied' in low):
        key = 'permission'
    elif (errno in _CONNECTION_ERRNOS
          or state.startswith('08')
          or any(k in low for k in (
              'connect', 'refused', 'lost connection', 'timed out',
              'timeout', 'connection', 'server has gone away',
              'no route to host', 'unknown host'))
          or any(k in (text or '') for k in (
              '连接被拒绝', '连接失败', '连接超时', '无法连接', '断开'))):
        key = 'connection'
    else:
        key = 'query'
    label, color = ERROR_CATEGORIES[key]
    return {'key': key, 'category': label, 'color': color}


def _parse_datetime(text):
    """把日期/时间文本解析成 datetime；不是日期文本返回 None。"""
    if not text:
        return None
    m = _DATE_RE.match(text.strip())
    if not m:
        return None
    year, mon, day, hh, mm, ss, frac = m.groups()
    try:
        micro = int((frac or '0').ljust(6, '0')[:6])
        return _dt.datetime(int(year), int(mon), int(day),
                            int(hh or 0), int(mm or 0), int(ss or 0), micro)
    except ValueError:
        return None


def typed_sort_key(value, text=None):
    """返回可比较的排序 key：``(kind_rank, key)``。

    kind_rank：0=数值，1=日期时间，2=普通文本，3=未知/二进制。
    NULL 不在这里处理（由调用方固定排到最后）。
    """
    if isinstance(value, bool):
        return (0, float(value))
    if isinstance(value, (int, float)):
        return (0, float(value))
    if isinstance(value, _dt.datetime):
        return (1, value.timestamp())
    if isinstance(value, _dt.date):
        return (1, _dt.datetime(value.year, value.month,
                                value.day).timestamp())
    s = text if text is not None else ('' if value is None else str(value))
    dt = _parse_datetime(s)
    if dt is not None:
        return (1, dt.timestamp())
    s2 = s.strip()
    if _NUM_RE.match(s2):
        try:
            return (0, float(s2))
        except ValueError:
            pass
    return (2, s2.casefold())


class ResultTable(QWidget):
    # P1-2：结果被 max_result_rows 截断后点「加载更多」→ 主窗口继续取下一批
    load_more_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw_error_text = ""
        self._sort_column = -1
        self._sort_order = Qt.AscendingOrder
        self._filter_text = ""
        self._filter_column = -1
        self._filter_case = False
        self._result_row_count = 0
        # 有界结果状态（P1-2）
        self._columns = []
        self.rows_loaded = 0
        self._truncated = False
        self.error_category_key = 'query'
        self.error_category = ERROR_CATEGORIES['query'][0]
        self._build_ui()

    # ==================== UI ====================

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 状态栏
        info_bar = QWidget()
        info_bar.setStyleSheet("background: #252526; border-top: 1px solid #3c3c3c;")
        info_layout = QHBoxLayout(info_bar)
        info_layout.setContentsMargins(8, 3, 8, 3)
        self.info_label = QLabel("就绪")
        self.info_label.setStyleSheet("color: #969696;")
        self.info_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        info_layout.addWidget(self.info_label)
        info_layout.addStretch()
        self.time_label = QLabel("")
        self.time_label.setStyleSheet("color: #969696;")
        info_layout.addWidget(self.time_label)
        # P1-2：结果被截断时可见的「加载更多」按钮
        self.load_more_btn = QPushButton("加载更多")
        self.load_more_btn.setToolTip("继续取下一批结果（不重新执行完整查询）")
        self.load_more_btn.clicked.connect(self._on_load_more_clicked)
        self.load_more_btn.setVisible(False)
        info_layout.addWidget(self.load_more_btn)
        layout.addWidget(info_bar)

        # 过滤条（P0-2）
        self.filter_bar = QWidget()
        self.filter_bar.setStyleSheet(
            "background: #2d2d30; border-bottom: 1px solid #3c3c3c;")
        fl = QHBoxLayout(self.filter_bar)
        fl.setContentsMargins(8, 3, 8, 3)
        fl.setSpacing(6)
        fl.addWidget(QLabel("过滤:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("输入关键字过滤当前结果（不重新查库）")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_filter_changed)
        fl.addWidget(self.filter_edit, 1)
        self.filter_col = QComboBox()
        self.filter_col.setMinimumWidth(110)
        self.filter_col.addItem("全部列")
        self.filter_col.currentIndexChanged.connect(self._on_filter_changed)
        fl.addWidget(self.filter_col)
        self.filter_case = QCheckBox("区分大小写")
        self.filter_case.stateChanged.connect(self._on_filter_changed)
        fl.addWidget(self.filter_case)
        self.filter_clear = QPushButton("清空")
        self.filter_clear.setToolTip("清空过滤条件，恢复全部行")
        self.filter_clear.clicked.connect(self.clear_filter)
        fl.addWidget(self.filter_clear)
        self.filter_count = QLabel("")
        self.filter_count.setStyleSheet("color: #969696;")
        fl.addWidget(self.filter_count)
        layout.addWidget(self.filter_bar)

        # 空态提示
        self.empty_label = QLabel("无匹配行")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet(
            "color: #969696; font-size: 14px; background: #1e1e1e;")
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)

        # 表格
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # 不用 Qt 内置的文本排序（它按字典序排 88.5 vs 270.5 是错的）：
        # 表头点击 → Qt 的事件先到这里，由 sort_by() 用类型化 key 重排。
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionsMovable(True)
        self.table.horizontalHeader().sectionClicked.connect(
            self._on_header_clicked)
        # ---- G1：表头/单元格右键菜单、双击值详情 ----
        self._all_columns = []
        self._hidden_columns = set()
        self._value_filters = {}
        self.last_detail_dialog = None
        self._header = self.table.horizontalHeader()
        self._header.setContextMenuPolicy(Qt.CustomContextMenu)
        self._header.customContextMenuRequested.connect(
            self._on_header_context_menu)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(
            self._on_cell_context_menu)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        layout.addWidget(self.table)

        # 错误视图
        # 背景：以前 show_error() 完全不用入参，界面只剩「错误」两个字，
        # SQLSTATE / 错误码 / 报文全部丢失。这里用只读 QPlainTextEdit 承载
        # 完整报文：可换行、可滚动、可选中复制（Ctrl+A / Ctrl+C / 右键复制）。
        self.error_view = QWidget()
        ev = QVBoxLayout(self.error_view)
        ev.setContentsMargins(8, 6, 8, 6)
        ev.setSpacing(4)

        bar = QHBoxLayout()
        self.error_label = QLabel("错误")
        self.error_label.setStyleSheet("color: #f48771; font-weight: bold;")
        bar.addWidget(self.error_label)
        bar.addStretch()
        self.copy_btn = QPushButton("复制错误")
        self.copy_btn.setToolTip("把完整错误文本复制到剪贴板")
        self.copy_btn.clicked.connect(self.copy_error)
        bar.addWidget(self.copy_btn)
        ev.addLayout(bar)

        self.error_text = QPlainTextEdit()
        self.error_text.setReadOnly(True)          # 只读
        self.error_text.setLineWrapMode(QPlainTextEdit.WidgetWidth)  # 自动换行
        self.error_text.setFont(QFont("Consolas", 10))
        self.error_text.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)  # 可选中复制
        self.error_text.setStyleSheet(
            "QPlainTextEdit { background: #1e1e1e; color: #f48771; "
            "border: 1px solid #3c3c3c; }")
        ev.addWidget(self.error_text)

        self.error_view.setVisible(False)
        layout.addWidget(self.error_view)

    # ==================== 结果渲染 ====================

    @staticmethod
    def _make_item(val):
        """把原始值转成表格单元格（原始值存 Qt.UserRole 供类型化排序）。"""
        item = QTableWidgetItem()
        if val is None:
            item.setText("NULL")
            item.setForeground(QColor("#808080"))
            f = item.font()
            f.setItalic(True)
            item.setFont(f)
            item.setData(Qt.UserRole, NULL_SENTINEL)
        elif isinstance(val, bool):
            item.setText('1' if val else '0')
            item.setTextAlignment(Qt.AlignCenter)
            item.setData(Qt.UserRole, val)
        elif isinstance(val, (int, float)):
            item.setText(str(val))
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            item.setData(Qt.UserRole, val)
        elif isinstance(val, bytes):
            item.setText(f"<binary {len(val)} bytes>")
            item.setData(Qt.UserRole, val)
        else:
            s = str(val)
            if len(s) > 500:
                s = s[:500] + "..."
            item.setText(s)
            if '\n' in s:
                item.setText(s.replace('\n', ' '))
            item.setData(Qt.UserRole, val)
        return item

    def _update_info_label(self, elapsed_ms=None):
        """结果行数/截断提示（P1-2）。"""
        suffix = ''
        if self._truncated:
            suffix = (f"（已截断：显示前 {self.rows_loaded} 行，"
                      f"可能还有更多，可点『加载更多』）")
        self.info_label.setText(f"返回 {self.rows_loaded} 行" + suffix)
        if elapsed_ms is not None:
            self.time_label.setText(f"{elapsed_ms:.2f} ms")

    def show_result(self, columns, rows, elapsed_ms=0.0, truncated=False):
        self._hide_error_view()
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setRowCount(len(rows))
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self._columns = list(columns)
        self._orig_rows = [list(r) for r in rows]
        # G1 列可见性：列集合变化时重置（结果刷新/换查询）；同一列集合保留用户选择
        if list(columns) != list(getattr(self, '_all_columns', [])):
            self._all_columns = list(columns)
            self._hidden_columns = set()
            self._value_filters = {}
        self._apply_column_visibility()
        self.rows_loaded = len(rows)
        self._truncated = bool(truncated)
        self._result_row_count = len(rows)
        self._sort_column = -1
        self._sort_order = Qt.AscendingOrder
        try:
            self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        except Exception:
            pass

        for ri, row in enumerate(rows):
            for ci, val in enumerate(row):
                self.table.setItem(ri, ci, self._make_item(val))

        self.table.setSortingEnabled(False)   # 保持禁用：类型化排序自己管
        self.table.resizeColumnsToContents()

        # 限制列宽
        for ci in range(self.table.columnCount()):
            w = self.table.columnWidth(ci)
            if w > 400:
                self.table.setColumnWidth(ci, 400)
            if w < 60:
                self.table.setColumnWidth(ci, 60)

        # 过滤条随结果重建（新的列集合 / 新数据）
        self._reset_filter(columns)
        self._update_info_label(elapsed_ms)
        self.show_load_more(self._truncated)

    def append_rows(self, rows, truncated=None):
        """把「加载更多」取到的下一批行追加到表格（P1-2）。

        - 保持既有过滤条件并重新应用；若当前有排序则重新按同一列排序；
        - `truncated` 为 None 表示沿用当前状态，否则更新"是否还有更多"。
        返回实际追加的行数。
        """
        rows = list(rows or [])
        if truncated is not None:
            self._truncated = bool(truncated)
        if not rows:
            self._update_info_label()
            self.show_load_more(self._truncated)
            return 0
        n_cols = self.table.columnCount()
        old_n = self.table.rowCount()
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(old_n + len(rows))
            for i, row in enumerate(rows):
                for ci in range(n_cols):
                    val = row[ci] if ci < len(row) else None
                    self.table.setItem(old_n + i, ci, self._make_item(val))
        finally:
            self.table.setUpdatesEnabled(True)
        self.rows_loaded = old_n + len(rows)
        self._result_row_count = self.rows_loaded
        try:
            self._orig_rows.extend([list(r) for r in rows])
        except AttributeError:
            self._orig_rows = [list(r) for r in rows]
        if self._sort_column >= 0:
            self.sort_by(self._sort_column, self._sort_order)
        else:
            self._apply_filter()
        self._update_info_label()
        self.show_load_more(self._truncated)
        return len(rows)

    def show_load_more(self, visible: bool, loading: bool = False):
        self.load_more_btn.setVisible(bool(visible))
        self.load_more_btn.setEnabled(not loading)
        self.load_more_btn.setText("加载中…" if loading else "加载更多")

    def set_loading_more(self, loading: bool):
        if loading:
            self.load_more_btn.setVisible(True)
        self.load_more_btn.setEnabled(not loading)
        self.load_more_btn.setText("加载中…" if loading else "加载更多")

    def _on_load_more_clicked(self):
        if not self.load_more_btn.isEnabled():
            return
        self.set_loading_more(True)
        self.load_more_requested.emit()

    def _reset_load_more(self):
        self._truncated = False
        self._columns = []
        self.rows_loaded = self.table.rowCount()
        self.load_more_btn.setVisible(False)
        self.load_more_btn.setEnabled(True)
        self.load_more_btn.setText("加载更多")

    def show_message(self, message: str):
        # 修复已知缺陷：无结果集的语句（INSERT/UPDATE…）必须收起上一次错误视图，
        # 否则用户看到的报错是过期的（tests/test_studio_smoke.py 的 xfail 记录）。
        self._hide_error_view()
        self._hide_filter(True)
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self._result_row_count = 0
        self._reset_load_more()
        self.info_label.setText(message)

    # ==================== 类型化排序（P0-1） ====================

    def _on_header_clicked(self, section: int):
        """表头点击：同列再点切换升/降序，用类型化 key 排序。"""
        if self.table.isSortingEnabled():
            # 外部代码打开了 Qt 内置排序：内置逻辑已经处理过点击并更新指示器，
            # 这里直接沿用它的新方向，再按类型化 key 重排（纠正文本序）。
            order = self.table.horizontalHeader().sortIndicatorOrder()
        elif (self._sort_column == section
              and self._sort_order == Qt.AscendingOrder):
            order = Qt.DescendingOrder
        else:
            order = Qt.AscendingOrder
        self.sort_by(section, order)

    def sort_by(self, section: int, order: Qt.SortOrder = Qt.AscendingOrder):
        """按列 `section` 的类型化值排序；NULL 恒排最后（升/降序都一样）。"""
        n_rows = self.table.rowCount()
        n_cols = self.table.columnCount()
        if n_rows <= 1 or not (0 <= section < n_cols):
            return 0

        # QTableWidget 开启 sorting 时 setItem 会立即按内置文本序重排，
        # 把手工顺序打乱；因此重排期间必须禁用内置排序，且不恢复。
        if self.table.isSortingEnabled():
            self.table.setSortingEnabled(False)

        # 把整行 item 取出来（takeItem 不销毁对象，原始值仍在 item 里）
        items = [[self.table.takeItem(r, c) for c in range(n_cols)]
                 for r in range(n_rows)]

        nulls, valid = [], []
        for r, row in enumerate(items):
            it = row[section]
            if it is None or it.data(Qt.UserRole) is NULL_SENTINEL:
                nulls.append(r)
            else:
                valid.append(r)

        reverse = (order == Qt.DescendingOrder)

        def _key(row_idx):
            it = items[row_idx][section]
            raw = it.data(Qt.UserRole)
            return typed_sort_key(raw, it.text())

        try:
            valid.sort(key=_key, reverse=reverse)
        except TypeError:
            # 混合列兜底：全部按显示文本
            valid.sort(key=lambda i: items[i][section].text().casefold(),
                       reverse=reverse)

        new_order = valid + nulls          # NULL 固定最后，不参与方向反转
        self.table.setUpdatesEnabled(False)
        try:
            for new_r, old_r in enumerate(new_order):
                for c in range(n_cols):
                    self.table.setItem(new_r, c, items[old_r][c])
        finally:
            self.table.setUpdatesEnabled(True)

        self._sort_column = section
        self._sort_order = order
        try:
            self.table.horizontalHeader().setSortIndicator(section, order)
        except Exception:
            pass

        # 排序会按行号重排 item，但 setRowHidden 是行号状态 → 重新应用过滤
        self._apply_filter()
        return len(new_order)

    # ==================== 过滤（P0-2） ====================

    def _reset_filter(self, columns):
        """新结果到达：重建列下拉并清空过滤条件。"""
        self._filter_text = ""
        self._filter_column = -1
        self._filter_case = False
        for w in (self.filter_edit, self.filter_col, self.filter_case):
            w.blockSignals(True)
        try:
            self.filter_edit.setText("")
            self.filter_col.clear()
            self.filter_col.addItem("全部列")
            for c in columns:
                self.filter_col.addItem(str(c))
            self.filter_col.setCurrentIndex(0)
            self.filter_case.setChecked(False)
        finally:
            for w in (self.filter_edit, self.filter_col, self.filter_case):
                w.blockSignals(False)
        self.empty_label.setVisible(False)
        for r in range(self.table.rowCount()):
            self.table.setRowHidden(r, False)
        self.filter_count.setText(
            f"{self.table.rowCount()} 行" if self.table.rowCount() else "")
        self.filter_bar.setVisible(self.table.rowCount() > 0)
        self.table.setVisible(True)

    def _on_filter_changed(self, *args):
        self._apply_filter()

    def _row_matches(self, row: int, needle: str, column: int, case: bool) -> bool:
        n_cols = self.table.columnCount()
        if column >= 0:
            cols = [column]
        else:
            cols = range(n_cols)
        for c in cols:
            it = self.table.item(row, c)
            if it is None:
                continue
            text = it.text()
            if not case:
                text = text.casefold()
            if needle in text:
                return True
        return False

    def apply_filter(self, text: str = None, column: int = None) -> int:
        """过滤当前已加载行（不查库），返回可见行数。

        `text` 为空/None 时读取输入框；`column` 为 -1/None 表示全部列。
        """
        if text is not None:
            self.filter_edit.blockSignals(True)
            self.filter_edit.setText(text)
            self.filter_edit.blockSignals(False)
            self._filter_text = text
        if column is not None:
            self.filter_col.blockSignals(True)
            self.filter_col.setCurrentIndex(int(column) + 1)
            self.filter_col.blockSignals(False)
            self._filter_column = int(column)
        return self._apply_filter()

    def _apply_filter(self) -> int:
        text = self.filter_edit.text()
        column = self.filter_col.currentIndex() - 1   # -1 = 全部列
        case = self.filter_case.isChecked()
        needle = text if case else text.casefold()
        self._filter_text, self._filter_column, self._filter_case = \
            text, column, case

        n_rows = self.table.rowCount()
        has_text = bool(text.strip())
        if not has_text and not self._value_filters:
            for r in range(n_rows):
                self.table.setRowHidden(r, False)
            visible = n_rows
        else:
            visible = 0
            for r in range(n_rows):
                match = (not has_text or
                         self._row_matches(r, needle, column, case))
                if match:
                    for fcol, values in self._value_filters.items():
                        it = self.table.item(r, fcol)
                        if it is None or it.text() not in values:
                            match = False
                            break
                self.table.setRowHidden(r, not match)
                if match:
                    visible += 1
            if visible == 0:
                self.empty_label.setText(f"无匹配行（共 {n_rows} 行）")
        self.empty_label.setVisible(has_text and visible == 0 and n_rows > 0)
        no_match = not self.empty_label.isHidden()
        self.table.setVisible(not no_match)
        self.filter_bar.setVisible(n_rows > 0)
        if n_rows:
            if has_text or self._value_filters:
                self.filter_count.setText(f"显示 {visible}/{n_rows} 行")
            else:
                self.filter_count.setText(f"{n_rows} 行")
        else:
            self.filter_count.setText("")
        return visible

    def clear_filter(self):
        """清空过滤条件并恢复全部行。"""
        for w in (self.filter_edit, self.filter_col):
            w.blockSignals(True)
        try:
            self.filter_edit.setText("")
            self.filter_col.setCurrentIndex(0)
        finally:
            for w in (self.filter_edit, self.filter_col):
                w.blockSignals(False)
        self.empty_label.setVisible(False)
        self.table.setVisible(True)
        self._apply_filter()

    def visible_row_count(self) -> int:
        """当前未被过滤隐藏的行数（测试/辅助用）。"""
        return sum(1 for r in range(self.table.rowCount())
                   if not self.table.isRowHidden(r))

    def _hide_filter(self, hide=True):
        self.filter_bar.setVisible(not hide)
        self.empty_label.setVisible(False)
        if hide:
            for w in (self.filter_edit, self.filter_col, self.filter_case):
                w.blockSignals(True)
            self.filter_edit.setText("")
            self.filter_col.setCurrentIndex(0)
            self.filter_case.setChecked(False)
            for w in (self.filter_edit, self.filter_col, self.filter_case):
                w.blockSignals(False)
            self.filter_count.setText("")

    # ==================== 错误呈现 ====================

    def _hide_error_view(self):
        self.error_view.setVisible(False)
        self.table.setVisible(True)
        self.info_label.setStyleSheet("color: #969696;")

    def show_error(self, error: str, errno=None, sqlstate=None, sql: str = None):
        """展示完整错误：错误码 + SQLSTATE + 完整 message（可滚动/换行/选中复制）。

        以前这里 `setText("错误")` 完全不用入参，界面看不到任何诊断信息。
        现在：
          * `error_text`（只读 QPlainTextEdit）承载**完整**报文，不截断；
          * 第一行给出「错误码 / SQLSTATE」（缺失时写「(未提供)」，不编造）；
          * `self._raw_error_text` 保存原始文本，供「复制错误」与测试断言；
          * 新增错误分类（查询/连接/权限）标签与颜色（P0-6）。
        """
        text = '' if error is None else str(error)
        self._raw_error_text = text

        # 入参没带诊断字段时，从文本里回捞（不编造：捞不到仍写「(未提供)」）
        if errno is None or not sqlstate:
            p_code, p_state = parse_error_fields(text)
            if errno is None:
                errno = p_code
            if not sqlstate:
                sqlstate = p_state

        code_str = str(errno) if errno is not None else '(未提供)'
        state_str = str(sqlstate) if sqlstate else '(未提供)'
        header = f"错误码: {code_str}    SQLSTATE: {state_str}"

        # 错误分类（查询/连接/权限）——只做 UI 标签与配色
        cat = classify_error(errno=errno, sqlstate=sqlstate, text=text)
        self.error_category_key = cat['key']
        self.error_category = cat['category']

        blocks = [header, text]
        if sql:
            blocks.append(f"SQL: {sql}")
        full = '\n'.join(b for b in blocks if b != '')

        self._hide_filter(True)
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self._result_row_count = 0
        self._reset_load_more()

        self.error_text.setPlainText(full)
        self.error_view.setVisible(True)
        self.table.setVisible(False)

        # 表格区域被错误视图取代，状态栏给出「有完整诊断」的指引
        summary = text.splitlines()[0] if text.strip() else '(无报文)'
        self.info_label.setText(f"错误 [{header}] {summary}")
        self.info_label.setToolTip(full)
        self.info_label.setStyleSheet("color: #f48771;")
        self.error_label.setText(
            f"{cat['category']}  （错误码 {code_str}，SQLSTATE {state_str}）")
        self.error_label.setStyleSheet(
            f"color: {cat['color']}; font-weight: bold;")
        self.time_label.setText("")

    def error_display_text(self) -> str:
        """当前错误视图里**实际可见**的完整文本（测试与复制共用）。"""
        return self.error_text.toPlainText()

    def error_display_is_selectable(self) -> bool:
        return bool(self.error_text.textInteractionFlags()
                    & Qt.TextSelectableByMouse)

    def copy_error(self) -> str:
        """把完整错误文本复制到剪贴板，返回被复制的文本。"""
        text = self.error_display_text() or self._raw_error_text
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        return text

    def clear(self):
        self._hide_error_view()
        self._hide_filter(True)
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self._result_row_count = 0
        self._reset_load_more()
        self.info_label.setText("就绪")
        self.info_label.setToolTip("")
        self.info_label.setStyleSheet("color: #969696;")
        self.time_label.setText("")
        self._raw_error_text = ""

    # ==================== G1：结果网格细节 ====================

    def _raw_value(self, row, col):
        item = self.table.item(row, col)
        if item is None:
            return None
        val = item.data(Qt.UserRole)
        return None if val is NULL_SENTINEL else val

    def _column_name(self, section):
        item = self.table.horizontalHeaderItem(section)
        return item.text() if item is not None else f'列{section + 1}'

    def sort_indicator(self):
        """返回 (列, 顺序)；未排序返回 (-1, None)（表头箭头状态）。"""
        if self._sort_column < 0:
            return -1, None
        return self._sort_column, self._sort_order

    def sort_ascending(self, section):
        self.sort_by(section, Qt.AscendingOrder)
        return True

    def sort_descending(self, section):
        self.sort_by(section, Qt.DescendingOrder)
        return True

    def clear_sort(self):
        """取消排序：恢复原始结果顺序，并隐藏表头箭头。"""
        self._rebuild_from_original()
        self._sort_column = -1
        self._sort_order = Qt.AscendingOrder
        try:
            self.table.horizontalHeader().setSortIndicator(-1,
                                                           Qt.AscendingOrder)
        except Exception:
            pass
        self._apply_filter()
        return True

    def _rebuild_from_original(self):
        """按 show_result/append_rows 的原始行重建表格（保留列/过滤/可见性）。"""
        cols = self._all_columns or self._columns
        rows = getattr(self, '_orig_rows', [])
        if not cols:
            return
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(rows))
            for ri, row in enumerate(rows):
                for ci in range(len(cols)):
                    val = row[ci] if ci < len(row) else None
                    self.table.setItem(ri, ci, self._make_item(val))
        finally:
            self.table.setUpdatesEnabled(True)
        self.rows_loaded = len(rows)
        self._result_row_count = len(rows)
        self._apply_column_visibility()

    def _apply_column_visibility(self):
        n_cols = self.table.columnCount()
        for c in range(n_cols):
            self.table.setColumnHidden(c, c in self._hidden_columns)

    def visible_columns(self):
        return [c for c in range(self.table.columnCount())
                if not self.table.isColumnHidden(c)]

    def hide_column(self, section):
        if 0 <= section < self.table.columnCount():
            self._hidden_columns.add(section)
            self.table.setColumnHidden(section, True)
            return True
        return False

    def show_column(self, section):
        self._hidden_columns.discard(section)
        if 0 <= section < self.table.columnCount():
            self.table.setColumnHidden(section, False)
            return True
        return False

    def show_all_columns(self):
        self._hidden_columns.clear()
        self._apply_column_visibility()
        return True

    def reset_column_visibility(self):
        """恢复默认：全部列可见（并清除列值筛选）。"""
        self.show_all_columns()
        return True

    def set_visible_columns(self, sections):
        n = self.table.columnCount()
        keep = {int(c) for c in sections if 0 <= int(c) < n}
        self._hidden_columns = set(range(n)) - keep
        self._apply_column_visibility()
        return True

    def build_column_visibility_dialog(self, parent=None):
        dlg = QDialog(parent or self)
        dlg.setWindowTitle('列可见性')
        dlg.resize(320, 360)
        layout = QVBoxLayout(dlg)
        listw = QListWidget()
        for c in range(self.table.columnCount()):
            item = QListWidgetItem(self._column_name(c))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(Qt.UserRole, c)
            item.setCheckState(Qt.Unchecked if c in self._hidden_columns
                               else Qt.Checked)
            listw.addItem(item)
        layout.addWidget(listw)
        row = QHBoxLayout()
        show_all = QPushButton('显示全部')
        default_btn = QPushButton('恢复默认')
        box = QDialogButtonBox(QDialogButtonBox.Ok |
                               QDialogButtonBox.Cancel)
        row.addWidget(show_all)
        row.addWidget(default_btn)
        row.addStretch()
        row.addWidget(box)
        layout.addLayout(row)

        def _all():
            for i in range(listw.count()):
                listw.item(i).setCheckState(Qt.Checked)
        show_all.clicked.connect(_all)
        default_btn.clicked.connect(_all)

        def _accept():
            cols = [listw.item(i).data(Qt.UserRole)
                    for i in range(listw.count())
                    if listw.item(i).checkState() == Qt.Checked]
            self.set_visible_columns(cols)
            dlg.accept()
        box.accepted.connect(_accept)
        box.rejected.connect(dlg.reject)
        dlg.column_list = listw
        dlg.show_all_button = show_all
        dlg.reset_button = default_btn
        return dlg

    def column_statistics(self, section):
        """列统计（明确基于当前结果集；数值列才有 min/max/sum/avg）。"""
        n = self.table.rowCount()
        if not (0 <= section < self.table.columnCount()):
            return None
        values, nulls = [], 0
        for r in range(n):
            raw = self._raw_value(r, section)
            if raw is None:
                nulls += 1
                continue
            values.append(raw)
        numeric = []
        for v in values:
            if isinstance(v, bool):
                numeric.append(int(v))
            elif isinstance(v, (int, float)):
                numeric.append(float(v))
            elif isinstance(v, str) and _NUM_RE.match(v.strip()):
                try:
                    numeric.append(float(v.strip()))
                except ValueError:
                    pass
        stats = {
            'column': self._column_name(section),
            'count': n,
            'non_null': len(values),
            'nulls': nulls,
            'unique': len({self._raw_key(v) for v in values}),
            'numeric_count': len(numeric),
            'min': None, 'max': None, 'sum': None, 'avg': None,
            'based_on': '当前已加载结果集（非全表）',
        }
        if numeric:
            stats['min'] = min(numeric)
            stats['max'] = max(numeric)
            stats['sum'] = sum(numeric)
            stats['avg'] = sum(numeric) / len(numeric)
        return stats

    @staticmethod
    def _raw_key(value):
        try:
            hash(value)
            return ('v', value)
        except TypeError:
            return ('r', repr(value))

    def column_stats_text(self, section):
        stats = self.column_statistics(section)
        if not stats:
            return ''
        lines = [
            f"列: {stats['column']}（基于{stats['based_on']}）",
            f"总行数: {stats['count']}  非空: {stats['non_null']}  "
            f"空值: {stats['nulls']}  唯一值: {stats['unique']}",
        ]
        if stats['numeric_count']:
            lines.append(
                f"数值({stats['numeric_count']}): min={stats['min']:g} "
                f"max={stats['max']:g} sum={stats['sum']:g} "
                f"avg={stats['avg']:.4f}")
        else:
            lines.append('数值统计: 无（该列没有可解析的数值）')
        return '\n'.join(lines)

    def distinct_values(self, section, limit=100):
        """当前结果集该列的去重值（最多 limit 项，排序后返回）。"""
        if not (0 <= section < self.table.columnCount()):
            return []
        seen, out = set(), []
        for r in range(self.table.rowCount()):
            raw = self._raw_value(r, section)
            text = 'NULL' if raw is None else str(raw)
            if text not in seen:
                seen.add(text)
                out.append(text)
        out.sort(key=str.casefold)
        return out[:max(1, int(limit))]

    def apply_value_filter(self, section, values):
        """按列值列表筛选（基于当前结果集，可与文本过滤叠加）。"""
        if not (0 <= section < self.table.columnCount()):
            return 0
        vals = [str(v) for v in (values or [])]
        if vals:
            self._value_filters[section] = set(vals)
        else:
            self._value_filters.pop(section, None)
        return self._apply_filter()

    def clear_value_filters(self):
        self._value_filters.clear()
        return self._apply_filter()

    def filter_by_column(self, section, text=''):
        """表头菜单「按本列过滤」：只对该列应用文本过滤。"""
        if not (0 <= section < self.table.columnCount()):
            return 0
        return self.apply_filter(text=str(text), column=section)

    def build_value_filter_menu(self, section, limit=100, parent=None):
        """值列表筛选菜单（基于当前结果集；超出上限显式提示）。"""
        from PySide6.QtWidgets import QMenu as _QMenu
        menu = _QMenu(parent or self)
        all_values = self.distinct_values(section, limit=10 ** 9)
        shown = all_values[:limit]
        for value in shown:
            act = menu.addAction(value)
            act.triggered.connect(
                lambda _checked=False, v=value: self.apply_value_filter(
                    section, [v]))
        if len(all_values) > limit:
            act = menu.addAction(
                f"…共 {len(all_values)} 个值，仅显示前 {limit} 个（可加文本过滤）")
            act.setEnabled(False)
        menu.addSeparator()
        clear = menu.addAction('清除本列值筛选')
        clear.triggered.connect(lambda: self.apply_value_filter(section, []))
        return menu

    def build_header_menu(self, section, parent=None):
        """表头右键菜单：排序/取消、按本列过滤、值列表、隐藏列、列统计。"""
        menu = QMenu(parent or self)
        act = menu.addAction('升序排序')
        act.triggered.connect(lambda: self.sort_ascending(section))
        act = menu.addAction('降序排序')
        act.triggered.connect(lambda: self.sort_descending(section))
        act = menu.addAction('取消排序')
        act.triggered.connect(self.clear_sort)
        menu.addSeparator()
        act = menu.addAction('按本列过滤…')
        act.triggered.connect(
            lambda: self.filter_by_column(
                section, self.filter_edit.text()))
        menu.addMenu(self.build_value_filter_menu(section))
        act = menu.addAction('隐藏此列')
        act.triggered.connect(lambda: self.hide_column(section))
        act = menu.addAction('列可见性…')
        act.triggered.connect(self._open_column_visibility_dialog)
        menu.addSeparator()
        act = menu.addAction('列统计')
        act.triggered.connect(
            lambda: self._show_column_stats(section))
        return menu

    def _on_header_context_menu(self, pos):
        section = self._header.logicalIndexAt(pos)
        if section < 0:
            return
        self.build_header_menu(section).exec(
            self._header.mapToGlobal(pos))

    def _open_column_visibility_dialog(self):
        dlg = self.build_column_visibility_dialog()
        dlg.exec()
        return dlg

    def _show_column_stats(self, section):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(self, '列统计', self.column_stats_text(section))
        return self.column_statistics(section)

    def cell_detail_text(self, row, col):
        """单元格「值详情」文本（完整值；列类型未知时显式说明）。"""
        if not (0 <= row < self.table.rowCount()
                and 0 <= col < self.table.columnCount()):
            return ''
        raw = self._raw_value(row, col)
        if raw is None:
            value = 'NULL'
        elif isinstance(raw, bytes):
            value = '0x' + raw.hex()
        else:
            value = str(raw)
        return '\n'.join([
            f'列名: {self._column_name(col)}',
            f'行号: {row + 1} / {self.table.rowCount()}',
            '类型: 未知（查询结果未携带列类型信息）',
            f'长度: {len(value)} 字符',
            '',
            '完整值:',
            value,
        ])

    def build_cell_detail_dialog(self, row, col, parent=None):
        dlg = QDialog(parent or self)
        dlg.setWindowTitle(f'值详情 - {self._column_name(col)}')
        dlg.resize(560, 320)
        layout = QVBoxLayout(dlg)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(self.cell_detail_text(row, col))
        text.setFont(QFont('Consolas', 10))
        layout.addWidget(text)
        rowbar = QHBoxLayout()
        copy_btn = QPushButton('复制值')
        value = self._raw_value(row, col)
        value_text = 'NULL' if value is None else str(value)
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(value_text))
        close_btn = QPushButton('关闭')
        close_btn.clicked.connect(dlg.accept)
        rowbar.addWidget(copy_btn)
        rowbar.addStretch()
        rowbar.addWidget(close_btn)
        layout.addLayout(rowbar)
        dlg.value_text = text
        dlg.copy_button = copy_btn
        self.last_detail_dialog = dlg
        return dlg

    def show_cell_detail(self, row, col):
        dlg = self.build_cell_detail_dialog(row, col)
        dlg.exec()
        return dlg

    def _on_cell_double_clicked(self, row, col):
        self.show_cell_detail(row, col)

    # ---------- 复制 / 导出选中 ----------

    def _selected_matrix(self):
        """返回 (rows, cols) 选中的矩形单元格原始值；无选择返回 ([], [])。"""
        ranges = self.table.selectedRanges()
        if not ranges:
            return [], [], []
        rng = ranges[0]
        rows = list(range(rng.topRow(), rng.bottomRow() + 1))
        cols = list(range(rng.leftColumn(), rng.rightColumn() + 1))
        matrix = [[self._raw_value(r, c) for c in cols] for r in rows]
        return rows, cols, matrix

    def selected_block_text(self, include_headers=False):
        rows, cols, matrix = self._selected_matrix()
        if not rows:
            return ''
        lines = []
        if include_headers:
            lines.append('\t'.join(self._column_name(c) for c in cols))
        for row in matrix:
            lines.append('\t'.join(
                'NULL' if v is None else str(v) for v in row))
        return '\n'.join(lines)

    def copy_selection(self, include_headers=False):
        text = self.selected_block_text(include_headers=include_headers)
        if text:
            QApplication.clipboard().setText(text)
        return text

    def copy_with_headers(self):
        return self.copy_selection(include_headers=True)

    def copy_without_headers(self):
        return self.copy_selection(include_headers=False)

    @staticmethod
    def _sql_literal(value):
        if value is None:
            return 'NULL'
        if isinstance(value, bool):
            return '1' if value else '0'
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, bytes):
            return "0x" + value.hex()
        s = str(value).replace('\\', '\\\\').replace("'", "\\'")
        return f"'{s}'"

    def copy_as_insert(self, table_name='result'):
        rows, cols, matrix = self._selected_matrix()
        if not rows:
            return ''
        names = ', '.join('`%s`' % self._column_name(c).replace('`', '``')
                          for c in cols)
        values = []
        for row in matrix:
            literals = ', '.join(self._sql_literal(v) for v in row)
            values.append(f'({literals})')
        sql = (f'INSERT INTO `{table_name}` ({names}) VALUES\n'
               + ',\n'.join(values) + ';')
        QApplication.clipboard().setText(sql)
        return sql

    def copy_as_csv(self):
        import csv as _csv
        import io
        rows, cols, matrix = self._selected_matrix()
        if not rows:
            return ''
        buf = io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow([self._column_name(c) for c in cols])
        for row in matrix:
            writer.writerow(['' if v is None else v for v in row])
        text = buf.getvalue()
        QApplication.clipboard().setText(text)
        return text

    def build_copy_menu(self, parent=None):
        menu = QMenu(parent or self)
        act = menu.addAction('复制单元格/选中块（无表头）')
        act.triggered.connect(self.copy_without_headers)
        act = menu.addAction('复制选中块（含表头）')
        act.triggered.connect(self.copy_with_headers)
        menu.addSeparator()
        act = menu.addAction('复制为 INSERT 语句')
        act.triggered.connect(self.copy_as_insert)
        act = menu.addAction('复制为 CSV')
        act.triggered.connect(self.copy_as_csv)
        return menu

    def build_context_menu(self, parent=None):
        menu = QMenu(parent or self)
        menu.addMenu(self.build_copy_menu(menu))
        menu.addSeparator()
        act = menu.addAction('值详情…')
        act.triggered.connect(self._show_current_cell_detail)
        act = menu.addAction('导出选中行 CSV…')
        act.triggered.connect(lambda: self._export_selected_ui('csv'))
        act = menu.addAction('导出选中行 XLSX…')
        act.triggered.connect(lambda: self._export_selected_ui('xlsx'))
        menu.addSeparator()
        act = menu.addAction('列可见性…')
        act.triggered.connect(self._open_column_visibility_dialog)
        return menu

    def _on_cell_context_menu(self, pos):
        self.build_context_menu().exec(self.table.viewport().mapToGlobal(pos))

    def _show_current_cell_detail(self):
        cell = self.table.currentItem()
        if cell is None:
            return
        self.show_cell_detail(cell.row(), cell.column())

    def selected_rows_data(self):
        rows, cols, matrix = self._selected_matrix()
        if not rows:
            return [], []
        return [self._column_name(c) for c in cols], matrix

    def export_selected(self, filepath, fmt=None):
        """仅导出选中行 CSV/XLSX（复用 chunked 导出，真实写文件）。"""
        columns, rows = self.selected_rows_data()
        if not rows:
            return {'ok': False, 'error': '未选择行', 'rows': 0,
                    'path': str(filepath) if filepath else ''}
        fmt = fmt or (str(filepath).rsplit('.', 1)[-1].lower()
                      if filepath and '.' in str(filepath) else 'csv')
        if fmt not in ('csv', 'xlsx'):
            return {'ok': False, 'error': f'不支持的导出格式: {fmt}',
                    'rows': 0}
        try:
            from .import_export import write_export_chunked
            write_export_chunked(str(filepath), columns, rows, fmt=fmt)
            return {'ok': True, 'path': str(filepath), 'rows': len(rows),
                    'columns': len(columns), 'format': fmt, 'error': ''}
        except Exception as exc:                      # noqa: BLE001
            return {'ok': False, 'error': f'导出失败: {exc}',
                    'rows': 0, 'path': str(filepath)}

    def _export_selected_ui(self, fmt):
        from PySide6.QtWidgets import QMessageBox, QFileDialog
        columns, rows = self.selected_rows_data()
        if not rows:
            QMessageBox.information(self, '导出选中行', '未选择行')
            return {'ok': False, 'error': '未选择行'}
        path, _ = QFileDialog.getSaveFileName(
            self, '导出选中行', f'selection.{fmt}',
            f'{fmt.upper()} (*.{fmt})')
        if not path:
            return {'ok': False, 'error': '用户取消'}
        result = self.export_selected(path, fmt=fmt)
        if not result.get('ok'):
            QMessageBox.warning(self, '导出失败', result.get('error', ''))
        return result
