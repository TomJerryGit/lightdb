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
import json
import re

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPlainTextEdit, QToolBar, QPushButton,
    QLabel, QHBoxLayout, QMessageBox, QTextEdit, QCompleter, QLineEdit,
    QCheckBox, QGridLayout, QInputDialog,
)
from PySide6.QtCore import (
    Qt, Signal, QRect, QSize, QTimer, QStringListModel, QRegularExpression,
)
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

# G2：常用函数补全（可维护列表；补全只插入函数名）
SQL_FUNCTIONS = sorted({
    'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'COALESCE', 'IFNULL', 'NULLIF',
    'NOW', 'CURDATE', 'CURTIME', 'DATE_FORMAT', 'DATE_ADD', 'DATE_SUB',
    'DATEDIFF', 'TIMESTAMPDIFF', 'JSON_EXTRACT', 'JSON_UNQUOTE',
    'JSON_OBJECT', 'JSON_ARRAY', 'CONCAT', 'CONCAT_WS', 'SUBSTRING',
    'TRIM', 'UPPER', 'LOWER', 'LENGTH', 'CHAR_LENGTH', 'ROUND', 'FLOOR',
    'CEIL', 'ABS', 'MOD', 'POWER', 'GROUP_CONCAT', 'ROW_NUMBER',
    'RANK', 'DENSE_RANK', 'IF', 'CAST', 'CONVERT',
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
    # G1：执行光标所在语句（分号切分）
    execute_statement_requested = Signal(str)

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
        # ---- G1 查找/替换状态 ----
        self._find_matches = []             # [(start, length)]
        self._find_index = -1
        self._find_bar_visible = False
        self.find_error = ''
        self.last_action_error = ''
        # G2：接受对象树拖拽（表 → SELECT；列 → 列名）
        self.setAcceptDrops(True)

        self.line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_line_number_area_width(0)
        self._highlight_current_line()

        # ---- P1-3 自动补全 ----
        self._setup_completer()

        # 快捷键（保持原有执行快捷键不变；G3：保存引用供自定义应用）
        # task-75：全部 editor QShortcut 使用 WidgetWithChildrenShortcut，
        # 避免焦点在对象树/主窗口控件时仍触发编辑器动作；F5/Ctrl+R 也登记
        # 到 _shortcut_refs（纳入默认表/冲突检测/恢复默认）。
        self._shortcut_refs = {
            'run': QShortcut(QKeySequence("Ctrl+Return"), self, self._exec_all),
            'run_selection': QShortcut(
                QKeySequence("Ctrl+Shift+Return"), self, self._exec_selection),
            'completion': QShortcut(
                QKeySequence("Ctrl+Space"), self,
                lambda: self.show_completion(force=True)),
            'find': QShortcut(
                QKeySequence("Ctrl+F"), self,
                lambda: self.open_find_bar(replace=False)),
            'replace': QShortcut(
                QKeySequence("Ctrl+H"), self,
                lambda: self.open_find_bar(replace=True)),
            'goto_line': QShortcut(
                QKeySequence("Ctrl+L"), self, self.goto_line_dialog),
            'toggle_comment': QShortcut(
                QKeySequence("Ctrl+/"), self, self.toggle_comment),
            'duplicate_line': QShortcut(
                QKeySequence("Ctrl+D"), self, self.duplicate_line),
            'move_line_up': QShortcut(
                QKeySequence("Alt+Up"), self, lambda: self.move_line(-1)),
            'move_line_down': QShortcut(
                QKeySequence("Alt+Down"), self, lambda: self.move_line(1)),
            'execute_statement': QShortcut(
                QKeySequence("Ctrl+Shift+E"), self,
                self.execute_current_statement),
            'run_f5': QShortcut(QKeySequence("F5"), self, self._exec_all),
            'replace_alt': QShortcut(
                QKeySequence("Ctrl+R"), self,
                lambda: self.open_find_bar(replace=True)),
        }
        for _ref in self._shortcut_refs.values():
            try:
                _ref.setContext(Qt.WidgetWithChildrenShortcut)
            except Exception:
                pass
        self._build_find_bar()

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
        for idx, (start, length) in enumerate(getattr(self, '_find_matches', [])):
            if length <= 0:
                continue
            sel = QTextEdit.ExtraSelection()
            if idx == getattr(self, '_find_index', -1):
                sel.format.setBackground(QColor('#d7ba7d'))
                sel.format.setForeground(QColor('#1e1e1e'))
            else:
                sel.format.setBackground(QColor('#264f78'))
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.KeepAnchor)
            sel.cursor = cursor
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
        """光标前 `表.` / 最近 FROM/JOIN 的表名；G2：别名解析回真实表。

        单表简单别名：`FROM users AS u` 后输入 `u.` → 返回 `users`。
        """
        text = self.toPlainText()[:self.textCursor().position()]
        m = re.search(r'([A-Za-z_][\w]*)\.\s*$', text)
        if m:
            name = m.group(1)
            return self.schema_aliases().get(name.lower(), name)
        matches = re.findall(
            r'(?i)\b(?:FROM|JOIN|INTO|UPDATE)\s+[`"\[]?([A-Za-z_][\w]*)',
            text)
        return matches[-1] if matches else ''

    def schema_aliases(self) -> dict:
        """从 SQL 文本推断单表简单别名：`FROM t [AS] a` → {'a': 't'}。"""
        text = self.toPlainText()
        aliases = {}
        pattern = re.compile(
            r'(?i)\b(?:FROM|JOIN)\s+[`"\[]?([A-Za-z_]\w*)[`"\]]?'
            r'\s+(?:AS\s+)?([A-Za-z_]\w*)')
        keywords = {'where', 'group', 'order', 'limit', 'having', 'on',
                    'join', 'left', 'right', 'inner', 'outer', 'union',
                    'set', 'values', 'as', 'using'}
        for table, alias in pattern.findall(text or ''):
            if alias.lower() in keywords or alias.lower() == table.lower():
                continue
            aliases[alias.lower()] = table
        return aliases

    def completion_functions(self) -> list:
        return list(SQL_FUNCTIONS)

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
        candidates = list(SQL_KEYWORDS) + list(SQL_FUNCTIONS)
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

    # ==================== G1 查找 / 替换 / 行操作 ====================

    def _build_find_bar(self):
        self.find_bar = QWidget(self)
        self.find_bar.setObjectName('find_bar')
        lay = QGridLayout(self.find_bar)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText('查找...')
        self.find_edit.setAccessibleName('查找内容')
        self.find_edit.returnPressed.connect(self.find_next)
        lay.addWidget(self.find_edit, 0, 0, 1, 3)
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText('替换为...')
        self.replace_edit.setAccessibleName('替换内容')
        lay.addWidget(self.replace_edit, 1, 0, 1, 3)
        self.find_case = QCheckBox('区分大小写')
        self.find_whole = QCheckBox('全词')
        self.find_regex = QCheckBox('正则')
        for i, cb in enumerate((self.find_case, self.find_whole,
                                self.find_regex)):
            lay.addWidget(cb, 2, i)
        self.find_prev_btn = QPushButton('上一个')
        self.find_next_btn = QPushButton('下一个')
        self.replace_btn = QPushButton('替换')
        self.replace_all_btn = QPushButton('全部替换')
        self.find_close_btn = QPushButton('✕')
        for i, b in enumerate((self.find_prev_btn, self.find_next_btn,
                               self.replace_btn, self.replace_all_btn,
                               self.find_close_btn)):
            lay.addWidget(b, 3, i)
        self.find_count_label = QLabel('0/0')
        lay.addWidget(self.find_count_label, 4, 0)
        self.find_error_label = QLabel('')
        self.find_error_label.setStyleSheet('color: #f48771;')
        lay.addWidget(self.find_error_label, 4, 1, 1, 2)
        self.find_prev_btn.clicked.connect(self.find_prev)
        self.find_next_btn.clicked.connect(self.find_next)
        self.replace_btn.clicked.connect(self.replace_current)
        self.replace_all_btn.clicked.connect(self.replace_all)
        self.find_close_btn.clicked.connect(self.close_find_bar)
        self.find_edit.textChanged.connect(self._refresh_find_matches)
        for cb in (self.find_case, self.find_whole, self.find_regex):
            cb.stateChanged.connect(self._refresh_find_matches)
        self.find_bar.resize(380, 118)
        self.find_bar.hide()
        self._find_bar_visible = False

    def _position_find_bar(self):
        if not getattr(self, 'find_bar', None):
            return
        try:
            self.find_bar.move(max(0, self.width() - self.find_bar.width() - 18),
                               8)
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(
            QRect(cr.left(), cr.top(),
                  self.line_number_area_width(), cr.height()))
        self._position_find_bar()

    def open_find_bar(self, replace=False):
        """Ctrl+F / Ctrl+H：打开查找/替换条。"""
        self.find_bar.setVisible(True)
        self._find_bar_visible = True
        self.replace_edit.setVisible(bool(replace))
        self.replace_btn.setVisible(bool(replace))
        self.replace_all_btn.setVisible(bool(replace))
        self._position_find_bar()
        selected = self.textCursor().selectedText()
        if selected and '\u2029' not in selected:
            self.find_edit.setText(selected)
        self.find_edit.setFocus()
        self.find_edit.selectAll()
        self._refresh_find_matches()
        return True

    def close_find_bar(self):
        self.find_bar.setVisible(False)
        self._find_bar_visible = False
        self._find_matches = []
        self._find_index = -1
        self._highlight_current_line()
        return True

    def _search_regex(self):
        pattern = self.find_edit.text()
        if not pattern:
            return None
        flags = 0 if self.find_case.isChecked() else re.IGNORECASE
        try:
            if self.find_regex.isChecked():
                expr = pattern
            else:
                expr = re.escape(pattern)
            if self.find_whole.isChecked():
                expr = r'\b(?:' + expr + r')\b'
            return re.compile(expr, flags)
        except re.error as exc:
            self.find_error = f'非法正则: {exc}'
            self.find_error_label.setText(self.find_error)
            return None

    def _refresh_find_matches(self, *_args):
        pattern = self.find_edit.text()
        self.find_error = ''
        if self.find_error_label.text():
            self.find_error_label.setText('')
        self._find_matches = []
        self._find_index = -1
        if pattern:
            regex = self._search_regex()
            if regex is not None:
                text = self.toPlainText()
                self._find_matches = [(m.start(), m.end() - m.start())
                                      for m in regex.finditer(text)]
                if self._find_matches:
                    self._find_index = 0
        self._update_find_count()
        self._highlight_current_line()
        return len(self._find_matches)

    def _update_find_count(self):
        total = len(self._find_matches)
        current = self._find_index + 1 if total and self._find_index >= 0 else 0
        self.find_count_label.setText(f'{current}/{total}')
        return total

    def find_count(self):
        return len(self._find_matches)

    def find_next(self):
        if not self._find_matches:
            self._refresh_find_matches()
        if not self._find_matches:
            self.find_count_label.setText('0/0')
            return False
        self._find_index = (self._find_index + 1) % len(self._find_matches)
        self._select_current_match()
        return True

    def find_prev(self):
        if not self._find_matches:
            self._refresh_find_matches()
        if not self._find_matches:
            return False
        self._find_index = (self._find_index - 1) % len(self._find_matches)
        self._select_current_match()
        return True

    def _select_current_match(self):
        if not self._find_matches:
            return
        start, length = self._find_matches[self._find_index]
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(start + length, QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        self._update_find_count()
        self._highlight_current_line()

    def replace_current(self):
        """替换当前匹配；返回是否替换。"""
        if not self._find_matches:
            self._refresh_find_matches()
        if not self._find_matches:
            return False
        start, length = self._find_matches[self._find_index]
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(start + length, QTextCursor.KeepAnchor)
        cursor.insertText(self.replace_edit.text())
        self._refresh_find_matches()
        return True

    def replace_all(self):
        """全部替换；返回替换次数（非法正则返回 -1 并显式记录错误）。"""
        if self._search_regex() is None and self.find_edit.text():
            return -1
        if not self._find_matches:
            self._refresh_find_matches()
        count = len(self._find_matches)
        if not count:
            return 0
        text = self.toPlainText()
        regex = self._search_regex()
        if regex is None:
            return 0
        try:
            new_text = regex.sub(self.replace_edit.text(), text)
        except re.error as exc:
            self.find_error = f'替换失败: {exc}'
            self.find_error_label.setText(self.find_error)
            return -1
        self.setPlainText(new_text)
        self._refresh_find_matches()
        return count

    def goto_line(self, line: int):
        """跳转到第 line 行（1-based）并选中该行。"""
        block = self.document().findBlockByNumber(max(0, int(line) - 1))
        if not block.isValid():
            self.last_action_error = f'行号超出范围: {line}'
            return False
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                            QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        self.centerCursor()
        return True

    def goto_line_dialog(self):
        line, ok = QInputDialog.getInt(
            self, '跳转行', '行号:', self.textCursor().blockNumber() + 1,
            1, max(1, self.blockCount()))
        if ok:
            return self.goto_line(line)
        return False

    def _selected_or_current_lines(self):
        cursor = self.textCursor()
        if cursor.hasSelection():
            start = self.document().findBlock(cursor.selectionStart())
            end = self.document().findBlock(cursor.selectionEnd())
        else:
            start = end = cursor.block()
        lines = []
        block = start
        while block.isValid():
            lines.append(block)
            if block == end:
                break
            block = block.next()
        return lines

    def toggle_comment(self):
        """Ctrl+/：注释/取消注释当前行或选中行（`-- ` 前缀）。"""
        lines = self._selected_or_current_lines()
        if not lines:
            return False
        all_commented = all(block.text().lstrip().startswith('--')
                            for block in lines)
        cursor = self.textCursor()
        cursor.beginEditBlock()
        try:
            for block in lines:
                text = block.text()
                if all_commented:
                    idx = text.find('--')
                    new_text = text[:idx] + text[idx + 2:]
                    if new_text.startswith(' '):
                        new_text = new_text[1:]
                else:
                    indent = len(text) - len(text.lstrip())
                    new_text = text[:indent] + '-- ' + text[indent:]
                bcursor = QTextCursor(block)
                bcursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                     QTextCursor.MoveMode.KeepAnchor)
                bcursor.insertText(new_text)
        finally:
            cursor.endEditBlock()
        return True

    def duplicate_line(self):
        """Ctrl+D：复制当前行（选中多行时复制整段选中文本）。"""
        cursor = self.textCursor()
        if cursor.hasSelection():
            text = cursor.selectedText().replace('\u2029', '\n')
            end = QTextCursor(self.document())
            end.setPosition(cursor.selectionEnd())
            end.insertText('\n' + text)
            return True
        block = cursor.block()
        text = block.text()
        end = QTextCursor(block)
        end.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        end.insertText('\n' + text)
        return True

    def move_line(self, direction: int):
        """Alt+↑/↓：与相邻行交换（direction=-1 上移，1 下移）。"""
        if direction not in (-1, 1):
            return False
        block = self.textCursor().block()
        lines = self.toPlainText().split('\n')
        idx = block.blockNumber()
        target = idx + direction
        if target < 0 or target >= len(lines):
            return False
        lines[idx], lines[target] = lines[target], lines[idx]
        self.setPlainText('\n'.join(lines))
        new_block = self.document().findBlockByNumber(target)
        cursor = QTextCursor(new_block)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        self.setTextCursor(cursor)
        return True

    def _sql_statements_with_offsets(self):
        """按分号切分 SQL（跳过字符串/注释），返回 [(start,end,text)]。"""
        text = self.toPlainText()
        out = []
        start = 0
        i = 0
        quote = None
        n = len(text)
        while i < n:
            ch = text[i]
            if quote:
                if ch == '\\' and i + 1 < n:
                    i += 2
                    continue
                if ch == quote:
                    if i + 1 < n and text[i + 1] == quote:
                        i += 2
                        continue
                    quote = None
                i += 1
                continue
            if ch in ("'", '"'):
                quote = ch
                i += 1
                continue
            if ch == '-' and i + 1 < n and text[i + 1] == '-':
                j = text.find('\n', i)
                i = n if j < 0 else j
                continue
            if ch == '/' and i + 1 < n and text[i + 1] == '*':
                j = text.find('*/', i + 2)
                i = n if j < 0 else j + 2
                continue
            if ch == ';':
                stmt = text[start:i]
                if stmt.strip():
                    out.append((start, i + 1, stmt.strip()))
                start = i + 1
            i += 1
        tail = text[start:]
        if tail.strip():
            out.append((start, n, tail.strip()))
        return out

    def current_statement_text(self):
        """光标所在语句（按分号切分）；纯空白返回空串。"""
        pos = self.textCursor().position()
        statements = self._sql_statements_with_offsets()
        for start, end, text in statements:
            if start <= pos <= end:
                return text
        return ''

    def execute_current_statement(self):
        sql = self.current_statement_text()
        if not sql:
            self.last_action_error = '当前光标处没有可执行语句'
            self.setToolTip(self.last_action_error)
            return False
        self.execute_statement_requested.emit(sql)
        return True

    # ==================== G2：对象树拖拽插入 ====================

    DRAG_MIME = 'application/x-lightdb-object'

    def drop_payload(self, payload):
        """处理对象树拖拽数据（表→SELECT 模板；列→列名）；返回插入文本。"""
        if not isinstance(payload, dict):
            return ''
        kind = payload.get('kind')
        if kind == 'table':
            table = str(payload.get('table') or '').strip()
            if not table:
                return ''
            text = f'SELECT * FROM `{table}` LIMIT 200;'
        elif kind == 'column':
            text = str(payload.get('column') or payload.get('name') or '').strip()
            if not text:
                return ''
        else:
            return ''
        cursor = self.textCursor()
        before = self.toPlainText()[:cursor.position()]
        prefix = '\n' if before and not before.endswith('\n') else ''
        cursor.insertText(prefix + text)
        if kind == 'table':
            cursor.insertText('\n')
        self.setTextCursor(cursor)
        self.setFocus()
        return text

    def _drop_has_payload(self, event) -> bool:
        try:
            mime = event.mimeData()
            return mime.hasFormat(self.DRAG_MIME)
        except Exception:
            return False

    def dragEnterEvent(self, event):
        if self._drop_has_payload(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._drop_has_payload(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if not self._drop_has_payload(event):
            super().dropEvent(event)
            return
        try:
            raw = bytes(event.mimeData().data(self.DRAG_MIME)).decode('utf-8')
            payload = json.loads(raw)
        except Exception as exc:                      # noqa: BLE001
            self.last_action_error = f'拖拽数据解析失败: {exc}'
            event.ignore()
            return
        inserted = self.drop_payload(payload)
        if inserted:
            event.acceptProposedAction()
        else:
            self.last_action_error = '拖拽对象不是表/列节点'
            event.ignore()

    # ==================== 执行 ====================

    def apply_shortcut_map(self, mapping):
        """G3：按设置里的 id→键序列应用快捷键；返回应用数量。"""
        from PySide6.QtGui import QKeySequence
        applied = 0
        for action_id, ref in getattr(self, '_shortcut_refs', {}).items():
            if action_id not in (mapping or {}):
                continue
            key = str(mapping.get(action_id) or '').strip()
            if not key:
                continue
            try:
                ref.setKey(QKeySequence(key))
                applied += 1
            except Exception:
                pass
        return applied

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
