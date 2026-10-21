# -*- coding: utf-8 -*-
"""SQL 编辑器（语法高亮 + 自动补全 + 错误位置高亮 + 执行快捷键）

P1-3（GUI 路线图）：
1. **自动补全**：QCompleter + QStringListModel，候选 = SQL 关键字 + 当前库
   表名 + 列名（来自 MainWindow 维护的 schema 缓存；缓存由后台线程刷新，
   补全本身**不查库、不阻塞 UI**）。`set_auto_complete(False)`（对应
   `settings.editor_auto_complete=False`）时自动弹出被关闭；Ctrl+Space 可
   手动触发（只在编辑器有焦点时自动弹出）。
2. **错误位置高亮**：`highlight_error_token()` 从错误文本 `near '<token>'`
   / `at line N` / `Unknown column '<c>'` 等模式提取定位信息；token 能在当前
   SQL 里找到就选中并标红该片段（找不到就只报错、**不伪造位置**）。
   下次成功执行调用 `clear_error_highlight()` 清除。
3. 保留原有 Ctrl+Return / Ctrl+Shift+Return / F5 快捷键与行号区域行为。
"""
import re

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPlainTextEdit, QToolBar, QPushButton,
    QLabel, QHBoxLayout, QMessageBox, QTextEdit, QCompleter,
)
from PySide6.QtCore import Qt, Signal, QRect, QSize, QTimer, QStringListModel
from PySide6.QtGui import (
    QFont, QPainter, QColor, QTextFormat, QKeySequence, QShortcut,
    QTextCursor, QTextCharFormat,
)

from ..utils.sql_highlighter import SQLHighlighter

# 常用 SQL 关键字（补全候选的一部分；纯 UI 提示，不改变语法校验）
SQL_KEYWORDS = sorted({
    'SELECT', 'FROM', 'WHERE', 'GROUP BY', 'ORDER BY', 'HAVING', 'LIMIT',
    'OFFSET', 'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE', 'JOIN',
    'INNER JOIN', 'LEFT JOIN', 'RIGHT JOIN', 'ON', 'AS', 'AND', 'OR', 'NOT',
    'NULL', 'IS', 'IN', 'LIKE', 'BETWEEN', 'EXISTS', 'UNION', 'ALL',
    'DISTINCT', 'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'CREATE', 'TABLE',
    'ALTER', 'DROP', 'INDEX', 'VIEW', 'TRIGGER', 'PROCEDURE', 'FUNCTION',
    'DATABASE', 'SHOW', 'DESCRIBE', 'EXPLAIN', 'USE', 'BEGIN', 'COMMIT',
    'ROLLBACK', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'ASC', 'DESC',
    'PRIMARY KEY', 'FOREIGN KEY', 'UNIQUE', 'NOT NULL', 'DEFAULT',
})

# 错误文本里的定位信息（引擎会回 "near '<出错处起的 SQL 尾巴>'"）
_NEAR_SINGLE = re.compile(r"near\s+'([^']+)'", re.IGNORECASE)
_NEAR_DOUBLE = re.compile(r'near\s+"([^"]+)"', re.IGNORECASE)
_AT_LINE = re.compile(r'\bat line\s+(\d+)', re.IGNORECASE)
_NAMED_TOKENS = [
    re.compile(r"Unknown column '([^']+)'", re.IGNORECASE),
    re.compile(r"Unknown table '([^']+)'", re.IGNORECASE),
    re.compile(r"Table '([^']+)' doesn't exist", re.IGNORECASE),
    re.compile(r"Duplicate entry '([^']+)'", re.IGNORECASE),
    re.compile(r"Unknown database '([^']+)'", re.IGNORECASE),
]
_CTRL_KEYS = (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab)


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)


class SQLEditor(QPlainTextEdit):
    execute_requested = Signal(str)             # 执行全文
    execute_selection_requested = Signal(str)   # 执行选中

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont("Consolas", 11)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        self.setTabStopDistance(4 * 10)

        self.highlighter = SQLHighlighter(self.document())

        # ---- 状态初始化（必须在 _highlight_current_line 之前） ----
        self._auto_complete_enabled = True
        self._schema_provider = None       # callable() -> (tables, {table: [columns]})
        self._schema_cache = ([], {})
        self._error_span = None            # (start, length)
        self._error_info = None            # {'token','line','column','errno'}

        self.line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_line_number_area_width(0)
        self._highlight_current_line()

        # ---- P1-3 自动补全 ----
        self._setup_completer()

        # 快捷键（保持原有执行快捷键不变）
        QShortcut(QKeySequence("Ctrl+Return"), self, self._exec_all)
        QShortcut(QKeySequence("Ctrl+Shift+Return"), self, self._exec_selection)
        QShortcut(QKeySequence("F5"), self, self._exec_all)
        # 手动补全
        QShortcut(QKeySequence("Ctrl+Space"), self,
                  lambda: self.show_completion(force=True))

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
        painter.end()

    # ==================== 当前行 / 错误高亮 ====================

    def _extra_selections(self):
        extra = []
        if not self.isReadOnly():
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor("#2a2d2e"))
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            extra.append(sel)
        if self._error_span:
            start, length = self._error_span
            doc_len = self.document().characterCount() - 1
            start = max(0, min(start, doc_len))
            length = max(1, min(length, max(1, doc_len - start)))
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(120, 40, 40))
            sel.format.setUnderlineColor(QColor("#f48771"))
            sel.format.setUnderlineStyle(
                QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.KeepAnchor)
            sel.cursor = cursor
            extra.append(sel)
        return extra

    def _highlight_current_line(self):
        self.setExtraSelections(self._extra_selections())

    # ==================== 自动补全（P1-3） ====================

    def _setup_completer(self):
        self._completion_model = QStringListModel(self)
        self.completer = QCompleter(self._completion_model, self)
        self.completer.setWidget(self)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchContains)
        self.completer.activated[str].connect(self._insert_completion)
        self.completer.setMaxVisibleItems(12)

    def set_auto_complete(self, enabled: bool):
        """对应 `settings.editor_auto_complete`；False 时不再自动弹补全。"""
        self._auto_complete_enabled = bool(enabled)
        if not self._auto_complete_enabled:
            try:
                self.completer.popup().hide()
            except Exception:
                pass

    def auto_complete_enabled(self) -> bool:
        return bool(self._auto_complete_enabled)

    def set_schema_provider(self, provider):
        """provider: callable() -> (tables:list[str], columns:dict[str,list])。

        约定 provider **只读内存缓存、不查库**（MainWindow 在后台线程刷新缓存）。
        """
        self._schema_provider = provider
        self._refresh_schema_snapshot()

    def refresh_schema_cache(self, tables=None, columns=None):
        """直接注入 schema（测试/外部已缓存时使用）。"""
        self._schema_cache = (list(tables or []), dict(columns or {}))
        self._schema_provider = None

    def _refresh_schema_snapshot(self):
        if not callable(self._schema_provider):
            return
        try:
            snapshot = self._schema_provider()
        except Exception:
            snapshot = None
        if not snapshot:
            return
        try:
            tables = list(snapshot[0] or [])
            columns = dict(snapshot[1] or {})
        except Exception:
            return
        self._schema_cache = (tables, columns)

    def _schema(self):
        if callable(self._schema_provider):
            self._refresh_schema_snapshot()
        return self._schema_cache

    def _context_table(self) -> str:
        """当前光标前的 `表.` / 最近 FROM/JOIN 的表名（用于列补全）。"""
        text = self.toPlainText()[:self.textCursor().position()]
        m = re.search(r'([A-Za-z_][\w]*)\.\s*$', text)
        if m:
            return m.group(1)
        matches = re.findall(
            r'(?i)\b(?:FROM|JOIN|INTO|UPDATE)\s+[`"\[]?([A-Za-z_][\w]*)',
            text)
        return matches[-1] if matches else ''

    def _current_word_prefix(self) -> str:
        text = self.toPlainText()[:self.textCursor().position()]
        m = re.search(r'([A-Za-z_][\w]*)$', text)
        return m.group(1) if m else ''

    def completion_candidates(self, prefix: str = '',
                              context_table: str = None) -> list:
        """返回补全候选（关键字 + 表名 + 列名；带上下文表时优先该表列）。"""
        tables, columns = self._schema()
        context = context_table if context_table is not None \
            else self._context_table()
        candidates = list(SQL_KEYWORDS)
        lowered = {t.lower() for t in tables}
        if context and context.lower() in lowered:
            for tname, cols in columns.items():
                if tname.lower() == context.lower():
                    candidates.extend([str(c) for c in cols])
                    break
        else:
            candidates.extend([str(t) for t in tables])
            for cols in columns.values():
                candidates.extend([str(c) for c in cols])
        seen = set()
        out = []
        p = (prefix or '').lower()
        for cand in candidates:
            key = cand.lower()
            if key in seen:
                continue
            if p and p not in key:
                continue
            seen.add(key)
            out.append(cand)
        out.sort(key=lambda s: (not s.lower().startswith(p), s.lower()))
        return out[:500]

    def visible_completion_count(self) -> int:
        if not self.completer:
            return 0
        return int(self.completer.completionCount())

    def show_completion(self, force: bool = False) -> bool:
        """显示补全弹窗。`force=True` 用于 Ctrl+Space/测试（绕过 auto 开关）。"""
        if not force and not self._auto_complete_enabled:
            return False
        if not self.isVisible():
            self.show()
        prefix = self._current_word_prefix()
        candidates = self.completion_candidates(prefix)
        if not candidates:
            return False
        self._completion_model.setStringList(candidates)
        self.completer.setCompletionPrefix(prefix)
        if self.completer.completionCount() <= 0:
            return False
        self.completer.complete(self.cursorRect())
        return True

    def _insert_completion(self, text: str):
        if not text:
            return
        cursor = self.textCursor()
        text_before = self.toPlainText()[:cursor.position()]
        m = re.search(r'([A-Za-z_][\w]*)$', text_before)
        if m:
            cursor.setPosition(cursor.position() - len(m.group(1)))
            cursor.setPosition(cursor.position() + len(m.group(1)),
                               QTextCursor.KeepAnchor)
        cursor.insertText(text)
        self.setTextCursor(cursor)

    def _maybe_show_completion(self) -> bool:
        if not self._auto_complete_enabled:
            return False
        if not self.hasFocus():
            return False
        return self.show_completion(force=False)

    def keyPressEvent(self, event):
        popup = None
        try:
            popup = self.completer.popup()
        except Exception:
            popup = None
        popup_visible = bool(popup is not None and popup.isVisible())
        if popup_visible and event.key() in _CTRL_KEYS and \
                not (event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
            current = self.completer.currentCompletion()
            if current:
                self._insert_completion(current)
                popup.hide()
                return
        if popup_visible and event.key() == Qt.Key_Escape:
            popup.hide()
            return
        super().keyPressEvent(event)
        text = event.text()
        if text and (text[-1].isalnum() or text[-1] in '_.'):
            self._maybe_show_completion()

    # ==================== 错误位置高亮（P1-3） ====================

    @staticmethod
    def extract_error_token(error_text: str) -> dict:
        """从错误文本提取定位信息：`near '<token>'` / `at line N` / 列名等。"""
        text = '' if error_text is None else str(error_text)
        m = _NEAR_SINGLE.search(text) or _NEAR_DOUBLE.search(text)
        if m:
            return {'kind': 'near', 'token': m.group(1).strip(),
                    'line': None, 'column': None}
        for pat in _NAMED_TOKENS:
            m = pat.search(text)
            if m:
                return {'kind': 'named', 'token': m.group(1).strip(),
                        'line': None, 'column': None}
        m = _AT_LINE.search(text)
        if m:
            try:
                return {'kind': 'line', 'token': '',
                        'line': int(m.group(1)), 'column': None}
            except ValueError:
                pass
        return {'kind': None, 'token': '', 'line': None, 'column': None}

    @staticmethod
    def _locate_token(sql: str, token: str):
        """在 SQL 中定位 token：先整段，再逐词（不区分大小写）。返回 (pos,len)。"""
        if not sql or not token:
            return None
        pos = sql.find(token)
        if pos >= 0:
            return pos, len(token)
        low_sql = sql.lower()
        low_tok = token.lower()
        pos = low_sql.find(low_tok)
        if pos >= 0:
            return pos, len(token)
        for word in re.findall(r'[A-Za-z_][\w]*', token):
            if len(word) < 2:
                continue
            pos = low_sql.find(word.lower())
            if pos >= 0:
                return pos, len(word)
        return None

    def highlight_error_token(self, errno=None, text: str = '',
                              sql: str = None) -> dict:
        """执行失败时定位并高亮错误 token/行；定位不到返回 None（不伪造位置）。"""
        source = self.toPlainText() if sql is None else str(sql)
        info = self.extract_error_token(text)
        if info['kind'] == 'line' and info.get('line'):
            lines = source.split('\n')
            idx = info['line'] - 1
            if 0 <= idx < len(lines):
                start = sum(len(ln) + 1 for ln in lines[:idx])
                span = (start, max(1, len(lines[idx])))
                self._set_error_span(span, {
                    'token': '', 'line': info['line'], 'column': 0,
                    'errno': errno})
                return self._error_info
            info = {'kind': None, 'token': '', 'line': None, 'column': None}
        token = info.get('token') or ''
        located = self._locate_token(source, token) if token else None
        if not located:
            self.clear_error_highlight()
            return None
        start, length = located
        line_no = source.count('\n', 0, start) + 1
        line_start = source.rfind('\n', 0, start)
        column = start - (line_start + 1)
        self._set_error_span((start, length), {
            'token': token, 'line': line_no, 'column': column,
            'errno': errno})
        return self._error_info

    def _set_error_span(self, span, info):
        self._error_span = span
        self._error_info = info
        cursor = QTextCursor(self.document())
        cursor.setPosition(span[0])
        cursor.setPosition(span[0] + span[1], QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        self._highlight_current_line()
        self.ensureCursorVisible()

    def clear_error_highlight(self):
        """清除错误高亮（成功执行 / 新 SQL 时调用）。"""
        self._error_span = None
        self._error_info = None
        self._highlight_current_line()

    def has_error_highlight(self) -> bool:
        return self._error_span is not None

    def error_highlight_info(self) -> dict:
        return dict(self._error_info) if self._error_info else None

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
        self.clear_error_highlight()

    def append_sql(self, sql: str):
        cur = self.textCursor()
        # PySide6 6.x 的枚举是作用域限定的：旧的 `cur.End` 会抛
        # AttributeError（现场 GUI 点“示例插件”暴露）；用 MoveOperation.End。
        cur.movePosition(QTextCursor.MoveOperation.End)
        if self.toPlainText().strip():
            self.insertPlainText("\n\n" + sql)
        else:
            self.insertPlainText(sql)
