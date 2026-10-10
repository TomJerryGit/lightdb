"""SQL 语法高亮"""
import re
from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor, QFont


class SQLHighlighter(QSyntaxHighlighter):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rules = []

        # 关键字
        kw_format = QTextCharFormat()
        kw_format.setForeground(QColor('#569cd6'))
        kw_format.setFontWeight(QFont.Bold)
        keywords = [
            'SELECT', 'FROM', 'WHERE', 'INSERT', 'INTO', 'VALUES', 'UPDATE',
            'SET', 'DELETE', 'CREATE', 'DROP', 'ALTER', 'TABLE', 'DATABASE',
            'INDEX', 'VIEW', 'PROCEDURE', 'FUNCTION', 'TRIGGER', 'EVENT',
            'PRIMARY', 'KEY', 'FOREIGN', 'REFERENCES', 'UNIQUE', 'NOT', 'NULL',
            'DEFAULT', 'AUTO_INCREMENT', 'AND', 'OR', 'IN', 'LIKE', 'BETWEEN',
            'IS', 'AS', 'ON', 'JOIN', 'INNER', 'LEFT', 'RIGHT', 'CROSS',
            'OUTER', 'GROUP', 'BY', 'ORDER', 'HAVING', 'LIMIT', 'OFFSET',
            'UNION', 'ALL', 'DISTINCT', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
            'IF', 'EXISTS', 'BEGIN', 'COMMIT', 'ROLLBACK', 'TRANSACTION',
            'GRANT', 'REVOKE', 'TO', 'USER', 'IDENTIFIED', 'WITH', 'RECURSIVE',
            'PARTITION', 'OVER', 'MATCH', 'AGAINST', 'FULLTEXT', 'JSON',
            'EXPLAIN', 'ANALYZE', 'SHOW', 'DESCRIBE', 'USE', 'CHECK', 'OPTIMIZE',
            'TRUNCATE', 'REPLACE', 'IGNORE', 'RETURNING', 'ASC', 'DESC',
        ]
        for kw in keywords:
            pattern = QRegularExpression(rf'\b{kw}\b',
                                         QRegularExpression.CaseInsensitiveOption)
            self.rules.append((pattern, kw_format))

        # 函数
        fn_format = QTextCharFormat()
        fn_format.setForeground(QColor('#dcdcaa'))
        functions = [
            'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'NOW', 'CURDATE', 'CURTIME',
            'CONCAT', 'SUBSTRING', 'UPPER', 'LOWER', 'LENGTH', 'TRIM',
            'COALESCE', 'IFNULL', 'NULLIF', 'CAST', 'CONVERT',
            'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'LEAD', 'LAG',
            'FIRST_VALUE', 'LAST_VALUE', 'NTILE', 'PERCENT_RANK',
            'JSON_EXTRACT', 'JSON_SET', 'JSON_REMOVE', 'JSON_KEYS',
            'JSON_LENGTH', 'JSON_TYPE', 'JSON_VALID', 'JSON_OBJECT', 'JSON_ARRAY',
            'DATE_FORMAT', 'DATE_ADD', 'DATE_SUB', 'DATEDIFF',
            'ABS', 'CEIL', 'FLOOR', 'ROUND', 'MOD', 'POW', 'SQRT',
            'MD5', 'SHA1', 'SHA2', 'UUID', 'RAND',
        ]
        for fn in functions:
            pattern = QRegularExpression(rf'\b{fn}\b(?=\s*\()',
                                         QRegularExpression.CaseInsensitiveOption)
            self.rules.append((pattern, fn_format))

        # 字符串
        self.string_format = QTextCharFormat()
        self.string_format.setForeground(QColor('#ce9178'))

        # 数字
        self.number_format = QTextCharFormat()
        self.number_format.setForeground(QColor('#b5cea8'))

        # 注释
        self.comment_format = QTextCharFormat()
        self.comment_format.setForeground(QColor('#6a9955'))
        self.comment_format.setFontItalic(True)

        # 单行注释 --
        self.rules.append((
            QRegularExpression(r'--[^\n]*'),
            self.comment_format
        ))
        # 块注释
        self.rules.append((
            QRegularExpression(r'/\*.*?\*/',
                               QRegularExpression.DotMatchesEverythingOption),
            self.comment_format
        ))

        # 数字
        self.rules.append((
            QRegularExpression(r'\b\d+(\.\d+)?\b'),
            self.number_format
        ))

    def highlightBlock(self, text):
        for pattern, fmt in self.rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)

        # 字符串（避免跨行的简单处理）
        self._highlight_strings(text)

    def _highlight_strings(self, text):
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "'" or ch == '"':
                start = i
                quote = ch
                i += 1
                while i < n:
                    if text[i] == '\\':
                        i += 2
                        continue
                    if text[i] == quote:
                        if i + 1 < n and text[i + 1] == quote:
                            i += 2
                            continue
                        i += 1
                        break
                    i += 1
                self.setFormat(start, i - start, self.string_format)
            else:
                i += 1