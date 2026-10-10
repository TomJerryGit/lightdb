"""SQL 编辑器（带语法高亮 + 执行快捷键）"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPlainTextEdit, QToolBar, QPushButton,
    QLabel, QHBoxLayout, QMessageBox
)
from PySide6.QtCore import Qt, Signal, QRect, QSize
from PySide6.QtGui import (
    QFont, QPainter, QColor, QTextFormat, QKeySequence, QShortcut
)

from ..utils.sql_highlighter import SQLHighlighter


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)


class SQLEditor(QPlainTextEdit):
    execute_requested = Signal(str)        # 执行全文
    execute_selection_requested = Signal(str)   # 执行选中

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont("Consolas", 11)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        self.setTabStopDistance(4 * 10)

        self.highlighter = SQLHighlighter(self.document())

        self.line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_line_number_area_width(0)
        self._highlight_current_line()

        # 快捷键
        QShortcut(QKeySequence("Ctrl+Return"), self, self._exec_all)
        QShortcut(QKeySequence("Ctrl+Shift+Return"), self, self._exec_selection)
        QShortcut(QKeySequence("F5"), self, self._exec_all)

    # ==================== 行号区域 ====================

    def line_number_area_width(self):
        digits = 1
        max_num = max(1, self.blockCount())
        while max_num >= 10:
            max_num //= 10
            digits += 1
        return 12 + self.fontMetrics().horizontalAdvance('9') * digits

    def _update_line_number_area_width(self, _):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(
                0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(
            QRect(cr.left(), cr.top(),
                  self.line_number_area_width(), cr.height()))

    def line_number_area_paint_event(self, event):
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#252526"))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(
            self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        height = self.fontMetrics().height()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#858585"))
                painter.drawText(
                    0, int(top), self.line_number_area.width() - 6,
                    height, Qt.AlignRight, str(block_number + 1))
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            block_number += 1

    def _highlight_current_line(self):
        extra = []
        if not self.isReadOnly():
            sel = QPlainTextEdit.ExtraSelection()
            line_color = QColor("#2a2d2e")
            sel.format.setBackground(line_color)
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            extra.append(sel)
        self.setExtraSelections(extra)

    # ==================== 执行 ====================

    def _exec_all(self):
        sql = self.toPlainText().strip()
        if sql:
            # 去掉末尾分号
            if sql.endswith(';'):
                sql = sql[:-1]
            self.execute_requested.emit(sql)

    def _exec_selection(self):
        cur = self.textCursor()
        if cur.hasSelection():
            sql = cur.selectedText().strip()
            if sql:
                self.execute_selection_requested.emit(sql)
        else:
            self._exec_all()

    def set_sql(self, sql: str):
        self.setPlainText(sql)

    def append_sql(self, sql: str):
        cur = self.textCursor()
        cur.movePosition(cur.End)
        if self.toPlainText().strip():
            self.insertPlainText("\n\n" + sql)
        else:
            self.insertPlainText(sql)