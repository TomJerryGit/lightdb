# -*- coding: utf-8 -*-
"""示例插件（P2-8：演示菜单 / 工具栏 / process_sql 三条扩展点）。

安装方式：把本文件（或任意插件）放进插件目录（默认仓库根 `plugins/`），
在「工具 → 插件管理」中启用即可。插件不会修改数据库结构，过程安全：
- 菜单项：把 `SELECT 1` 插入当前 SQL 编辑器（无窗口时用 show_message 提示）；
- 工具栏按钮：同上；
- `process_sql`：只做去掉末尾多余空白的安全格式化，不改变 SQL 语义。
"""
import logging

try:                                   # 正常安装环境
    from lightdb_studio.core.plugin_manager import PluginBase
except ImportError:                    # 源码目录直接运行（仓库根在 sys.path）
    from core.plugin_manager import PluginBase

log = logging.getLogger(__name__)

name = "示例插件"
version = "1.0.0"
description = "示例插件：演示菜单/工具栏/SQL 处理（安全、不改数据）"
author = "LightDB"


class ExamplePlugin(PluginBase):
    name = name
    version = version
    description = description
    author = author

    def __init__(self, app_context):
        super().__init__(app_context)
        self.loaded = False

    def on_load(self):
        self.loaded = True
        log.info("示例插件已加载（%s v%s）", name, version)

    def on_unload(self):
        self.loaded = False
        log.info("示例插件已卸载：%s", name)

    # ---------- 扩展点 ----------

    def get_menu_items(self):
        return [{
            'menu': '工具',
            'text': '示例：插入 SELECT 1',
            'callback': self.insert_select,
        }]

    def get_toolbar_buttons(self):
        return [{
            'text': '✨ 示例插件',
            'callback': self.insert_select,
        }]

    def process_sql(self, sql: str) -> str:
        # 演示：只做安全的尾部空白清理，不改变语句语义
        return (sql or '').rstrip()

    # ---------- 动作 ----------

    def insert_select(self):
        sql = 'SELECT 1'
        main_window = (self.ctx or {}).get('main_window')
        try:
            if main_window is not None:
                editor = None
                getter = getattr(main_window, '_current_widgets', None)
                if callable(getter):
                    editor, _ = getter()
                if editor is not None and hasattr(editor, 'append_sql'):
                    editor.append_sql(sql)
                    return
            show_message = (self.ctx or {}).get('show_message')
            if callable(show_message):
                show_message(f'示例插件：{sql}')
        except Exception as exc:                      # noqa: BLE001
            log.warning("示例插件插入 SQL 失败: %s", exc)
