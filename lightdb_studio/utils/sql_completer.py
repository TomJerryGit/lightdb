"""SQL 自动补全"""
from PySide6.QtCore import QStringListModel, Qt
from PySide6.QtWidgets import QCompleter
from PySide6.QtGui import QTextCursor


KEYWORDS = [
    'SELECT', 'FROM', 'WHERE', 'GROUP BY', 'ORDER BY', 'HAVING', 'LIMIT',
    'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE',
    'CREATE', 'TABLE', 'INDEX', 'VIEW', 'PROCEDURE', 'FUNCTION', 'TRIGGER',
    'DROP', 'ALTER', 'ADD', 'COLUMN', 'MODIFY',
    'PRIMARY KEY', 'FOREIGN KEY', 'REFERENCES', 'UNIQUE', 'NOT NULL',
    'DEFAULT', 'AUTO_INCREMENT', 'CHECK',
    'INNER JOIN', 'LEFT JOIN', 'RIGHT JOIN', 'CROSS JOIN', 'ON',
    'UNION', 'UNION ALL', 'DISTINCT', 'AS', 'AND', 'OR', 'NOT', 'IN',
    'LIKE', 'BETWEEN', 'IS NULL', 'IS NOT NULL', 'EXISTS',
    'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
    'WITH', 'RECURSIVE',
    'BEGIN', 'COMMIT', 'ROLLBACK', 'SAVEPOINT',
    'GRANT', 'REVOKE', 'TO', 'USER',
    'SHOW', 'DESCRIBE', 'USE', 'EXPLAIN', 'ANALYZE', 'OPTIMIZE', 'CHECK',
    'PARTITION BY', 'OVER',
    'MATCH', 'AGAINST',
    'ASC', 'DESC', 'LIMIT', 'OFFSET',
]

FUNCTIONS = [
    'COUNT', 'SUM', 'AVG', 'MIN', 'MAX',
    'NOW', 'CURDATE', 'CURTIME', 'DATE_FORMAT', 'DATE_ADD', 'DATE_SUB', 'DATEDIFF',
    'CONCAT', 'CONCAT_WS', 'SUBSTRING', 'SUBSTR', 'UPPER', 'LOWER',
    'LENGTH', 'CHAR_LENGTH', 'TRIM', 'LTRIM', 'RTRIM', 'REPLACE', 'REVERSE',
    'LPAD', 'RPAD', 'LOCATE', 'INSTR', 'FIELD',
    'IFNULL', 'NULLIF', 'IF', 'COALESCE', 'CAST', 'CONVERT',
    'ABS', 'CEIL', 'CEILING', 'FLOOR', 'ROUND', 'TRUNCATE', 'MOD', 'POW',
    'SQRT', 'RAND', 'UUID', 'MD5', 'SHA1', 'SHA2',
    'JSON_EXTRACT', 'JSON_SET', 'JSON_REMOVE', 'JSON_KEYS', 'JSON_LENGTH',
    'JSON_TYPE', 'JSON_VALID', 'JSON_OBJECT', 'JSON_ARRAY',
    'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'PERCENT_RANK', 'CUME_DIST', 'NTILE',
    'LEAD', 'LAG', 'FIRST_VALUE', 'LAST_VALUE', 'NTH_VALUE',
    'STDDEV', 'VARIANCE',
]


class SQLCompleter:
    def __init__(self, editor, schema_provider=None):
        """
        editor: QPlainTextEdit
        schema_provider: 返回 (tables, columns_by_table) 的函数
        """
        self.editor = editor
        self.schema_provider = schema_provider
        self.tables = []
        self.columns = {}  # table -> [cols]

        self.model = QStringListModel()
        self.completer = QCompleter(self.model, editor)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.setWrapAround(False)
        self.completer.setWidget(editor)
        self.completer.activated.connect(self._insert_completion)

        self._prefix = ""

        # 绑定按键
        editor.keyPressEvent = self._wrap_key_press(editor.keyPressEvent)
        editor.textUnderCursorChanged = None  # 忽略

    def refresh_schema(self):
        """刷新 schema 缓存"""
        if not self.schema_provider:
            return
        try:
            tables, cols = self.schema_provider()
            self.tables = tables
            self.columns = cols
        except Exception:
            pass

    def _wrap_key_press(self, original):
        def new_key_press(event):
            from PySide6.QtCore import QEvent
            if self.completer.popup().isVisible():
                # 补全弹窗显示时的按键
                if event.key() in (Qt.Key_Enter, Qt.Key_Return, Qt.Key_Tab):
                    event.ignore()
                    return
                if event.key() == Qt.Key_Escape:
                    self.completer.popup().hide()
                    return

            if event.key() == Qt.Key_Space and \
               event.modifiers() & Qt.ControlModifier:
                self._trigger_completion(force=True)
                return

            original(event)

            # 每次输入后检查是否触发补全
            self._maybe_trigger()

        return new_key_press

    def _maybe_trigger(self):
        cursor = self.editor.textCursor()
        text_before = cursor.block().text()[:cursor.positionInBlock()]

        # 找到当前 token 的前缀
        prefix = ''
        for ch in reversed(text_before):
            if ch.isalnum() or ch in ('_', '.'):
                prefix = ch + prefix
            else:
                break

        if len(prefix) >= 1:
            self._trigger_completion(prefix)
        else:
            self.completer.popup().hide()

    def _trigger_completion(self, prefix=None, force=False):
        if prefix is None:
            cursor = self.editor.textCursor()
            text_before = cursor.block().text()[:cursor.positionInBlock()]
            prefix = ''
            for ch in reversed(text_before):
                if ch.isalnum() or ch in ('_', '.'):
                    prefix = ch + prefix
                else:
                    break

        if not prefix and not force:
            self.completer.popup().hide()
            return

        candidates = self._get_candidates(prefix)
        if not candidates:
            self.completer.popup().hide()
            return

        self._prefix = prefix
        self.model.setStringList(candidates)
        self.completer.setCompletionPrefix(prefix)

        rect = self.editor.cursorRect()
        rect.setWidth(
            self.completer.popup().sizeHintForColumn(0) +
            self.completer.popup().verticalScrollBar().sizeHint().width() + 40)
        self.completer.complete(rect)

    def _get_candidates(self, prefix):
        """根据前缀筛选候选项"""
        up = prefix.upper()
        result = []

        # 若带点：table.column
        if '.' in prefix:
            tbl, col_prefix = prefix.rsplit('.', 1)
            tbl_cols = self.columns.get(tbl, [])
            result = [c for c in tbl_cols
                      if c.upper().startswith(col_prefix.upper())]
            return sorted(set(result))[:50]

        # 表名
        for t in self.tables:
            if t.upper().startswith(up):
                result.append(t)
        # 列名
        seen = set()
        for tbl, cols in self.columns.items():
            for c in cols:
                if c.upper().startswith(up) and c not in seen:
                    result.append(c)
                    seen.add(c)
        # 关键字
        for kw in KEYWORDS:
            if kw.upper().startswith(up):
                result.append(kw)
        # 函数
        for fn in FUNCTIONS:
            if fn.upper().startswith(up):
                result.append(fn)

        # 去重排序，优先关键字
        priority = []
        rest = []
        for r in result:
            if r.upper() in [k.upper() for k in KEYWORDS]:
                priority.append(r)
            else:
                rest.append(r)

        priority = sorted(set(priority))
        rest = sorted(set(rest))
        return (priority + rest)[:60]

    def _insert_completion(self, text):
        """插入补全文本"""
        cursor = self.editor.textCursor()
        text_before = cursor.block().text()[:cursor.positionInBlock()]
        prefix_len = 0
        for ch in reversed(text_before):
            if ch.isalnum() or ch in ('_', '.'):
                prefix_len += 1
            else:
                break
        cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, prefix_len)
        cursor.removeSelectedText()
        cursor.insertText(text)
        self.editor.setTextCursor(cursor)