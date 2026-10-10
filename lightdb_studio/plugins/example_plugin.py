"""
示例插件 - 演示插件系统能力
"""
import os
import sys

# 使插件能找到 lightdb_studio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lightdb_studio.core.plugin_manager import PluginBase


name = "示例插件"
version = "1.0.0"
description = "演示插件系统：提供 SQL 大写格式化 + 生成随机 UUID 功能"
author = "LightDB Studio"


class ExamplePlugin(PluginBase):

    def on_load(self):
        print(f"[Plugin] {name} v{version} loaded")

    def on_unload(self):
        print(f"[Plugin] {name} unloaded")

    def get_menu_items(self):
        return [
            {
                'menu': '插件',
                'text': '🎨 格式化 SQL（大写关键字）',
                'callback': self.format_sql,
            },
            {
                'menu': '插件',
                'text': '🆔 生成 UUID',
                'callback': self.gen_uuid,
            },
            {
                'menu': '插件',
                'text': '📊 统计当前 SQL',
                'callback': self.sql_stats,
            },
        ]

    def get_toolbar_buttons(self):
        return [
            {
                'text': '🎨 格式化',
                'callback': self.format_sql,
            },
        ]

    # ==================== 功能 ====================

    def format_sql(self):
        """把 SQL 关键字大写"""
        mw = self.ctx.get('main_window')
        if not mw:
            return
        editor, _ = mw._current_widgets()
        if editor is None:
            self._msg("请先打开一个查询标签")
            return

        sql = editor.toPlainText()
        if not sql.strip():
            return

        formatted = self._uppercase_keywords(sql)
        editor.set_sql(formatted)
        self._msg("已格式化")

    def _uppercase_keywords(self, sql):
        keywords = ['SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'INSERT',
                    'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE', 'CREATE',
                    'TABLE', 'DROP', 'ALTER', 'INDEX', 'JOIN', 'INNER',
                    'LEFT', 'RIGHT', 'OUTER', 'ON', 'GROUP', 'BY',
                    'ORDER', 'HAVING', 'LIMIT', 'OFFSET', 'UNION',
                    'ALL', 'DISTINCT', 'AS', 'IN', 'LIKE', 'BETWEEN',
                    'IS', 'NULL', 'NOT', 'EXISTS', 'CASE', 'WHEN',
                    'THEN', 'ELSE', 'END']
        import re
        for kw in keywords:
            sql = re.sub(rf'\b{kw}\b', kw, sql, flags=re.IGNORECASE)
        return sql

    def gen_uuid(self):
        import uuid
        u = str(uuid.uuid4())
        mw = self.ctx.get('main_window')
        if not mw:
            return
        editor, _ = mw._current_widgets()
        if editor:
            cursor = editor.textCursor()
            cursor.insertText(f"'{u}'")
        self._msg(f"UUID: {u}")

    def sql_stats(self):
        mw = self.ctx.get('main_window')
        if not mw:
            return
        editor, _ = mw._current_widgets()
        if editor is None:
            return
        sql = editor.toPlainText()
        n_chars = len(sql)
        n_lines = sql.count('\n') + 1
        n_stmts = sql.count(';')
        self._msg(
            f"字符: {n_chars} | 行数: {n_lines} | 语句: {n_stmts}")

    def _msg(self, text):
        fn = self.ctx.get('show_message')
        if fn:
            fn(text)

    def process_sql(self, sql):
        """示例：自动去除末尾多余分号"""
        return sql.strip().rstrip(';')