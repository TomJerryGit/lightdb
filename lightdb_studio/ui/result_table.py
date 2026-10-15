"""查询结果表格"""
import re

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QLabel, QHBoxLayout, QHeaderView, QAbstractItemView,
    QPlainTextEdit, QPushButton, QApplication
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont

# 从 `format_db_error` 的文本里回捞字段（如 `错误码  : 1064` / `SQLSTATE: 42000`）。
# 目的：错误以字符串形式到达界面时（DBConnection.execute 的返回值就是这种
# 形态），标题行不能退化成「(未提供)」，否则用户以为诊断字段丢了。
_CODE_RE = re.compile(r'错误码\s*[:：]\s*(\d+)')
_STATE_RE = re.compile(r'SQLSTATE\s*[:：]\s*([0-9A-Za-z]{5})')


def parse_error_fields(text):
    """从错误文本里解析 (errno, sqlstate)；解析不到返回 None（不编造）。"""
    if not text:
        return None, None
    m = _CODE_RE.search(text)
    errno = int(m.group(1)) if m else None
    m2 = _STATE_RE.search(text)
    return errno, (m2.group(1) if m2 else None)


class ResultTable(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw_error_text = ""
        self._build_ui()

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
        layout.addWidget(info_bar)

        # 表格
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionsMovable(True)

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

    def show_result(self, columns, rows, elapsed_ms=0.0):
        self._hide_error_view()
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setRowCount(len(rows))
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)

        for ri, row in enumerate(rows):
            for ci, val in enumerate(row):
                item = QTableWidgetItem()
                if val is None:
                    item.setText("NULL")
                    item.setForeground(QColor("#808080"))
                    f = item.font()
                    f.setItalic(True)
                    item.setFont(f)
                elif isinstance(val, bool):
                    item.setText('1' if val else '0')
                    item.setTextAlignment(Qt.AlignCenter)
                elif isinstance(val, (int, float)):
                    item.setText(str(val))
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                elif isinstance(val, bytes):
                    item.setText(f"<binary {len(val)} bytes>")
                else:
                    s = str(val)
                    if len(s) > 500:
                        s = s[:500] + "..."
                    item.setText(s)
                    if '\n' in s:
                        item.setText(s.replace('\n', ' '))
                self.table.setItem(ri, ci, item)

        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()

        # 限制列宽
        for ci in range(self.table.columnCount()):
            w = self.table.columnWidth(ci)
            if w > 400:
                self.table.setColumnWidth(ci, 400)
            if w < 60:
                self.table.setColumnWidth(ci, 60)

        self.info_label.setText(f"返回 {len(rows)} 行")
        self.time_label.setText(f"{elapsed_ms:.2f} ms")

    def show_message(self, message: str):
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.table.setSortingEnabled(True)
        self.info_label.setText(message)

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
          * `self._raw_error_text` 保存原始文本，供「复制错误」与测试断言。
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

        blocks = [header, text]
        if sql:
            blocks.append(f"SQL: {sql}")
        full = '\n'.join(b for b in blocks if b != '')

        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.table.setSortingEnabled(True)

        self.error_text.setPlainText(full)
        self.error_view.setVisible(True)
        self.table.setVisible(False)

        # 表格区域被错误视图取代，状态栏给出「有完整诊断」的指引
        summary = text.splitlines()[0] if text.strip() else '(无报文)'
        self.info_label.setText(f"错误 [{header}] {summary}")
        self.info_label.setToolTip(full)
        self.info_label.setStyleSheet("color: #f48771;")
        self.error_label.setText(
            f"错误  （错误码 {code_str}，SQLSTATE {state_str}）")
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
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.info_label.setText("就绪")
        self.info_label.setToolTip("")
        self.info_label.setStyleSheet("color: #969696;")
        self.time_label.setText("")
        self._raw_error_text = ""