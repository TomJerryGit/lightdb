"""简单多语言支持（P2 i18n 覆盖率 + 可访问性）

- zh_CN / en_US 两套文案保持**相同 key 集合**（测试断言）；
- `t(key)` 未知 key → 原样返回 key，并记录到 `missing()`（不崩溃、不假装有翻译）；
- `t_strict(key)` 严格查询：缺失抛 KeyError；
- `set_lang()` 立刻通知监听器（主窗口热切换菜单/工具栏/状态栏）；
- `check_coverage()` 报告两语言 key 集合差异。
"""
import threading

_LOCK = threading.RLock()

_ZH = {
    'app.title': 'LightDB Studio',
    # ---------- 菜单 ----------
    'menu.file': '文件(&F)',
    'menu.edit': '编辑(&E)',
    'menu.query': '查询(&Q)',
    'menu.table': '表(&T)',
    'menu.tools': '工具(&T)',
    'menu.view': '视图(&V)',
    'menu.help': '帮助(&H)',
    # ---------- 菜单项 / 动作 ----------
    'action.new_connection': '新建连接...',
    'action.new_query': '新建查询',
    'action.open_sql': '打开 SQL 文件...',
    'action.save_sql': '保存 SQL...',
    'action.exit': '退出',
    'action.close_tab': '关闭当前标签',
    'action.format_sql': '格式化 SQL',
    'action.execute': '执行',
    'action.explain': 'EXPLAIN',
    'action.cancel_query': '取消当前查询',
    'action.cancel_query_tip': ('取消正在执行的后台查询（KILL 连接）'),
    'action.profiler': '性能分析',
    'action.query_builder': '查询构建器...',
    'action.chart': '数据图表...',
    'action.export_result_csv': '导出当前结果为 CSV...',
    'action.new_table': '新建表...',
    'action.edit_data': '编辑当前表数据',
    'action.db_compare': '数据库对比...',
    'action.er_diagram': 'ER 图...',
    'action.refresh_tree': '刷新对象浏览器',
    'action.toggle_tools': '显示/隐藏工具面板',
    'action.review': '📊 SQL 审查...',
    'action.submit_review': '📤 提交审查',
    'action.lineage': '🔗 数据血缘...',
    'action.snapshot': '📸 快照 / 克隆...',
    'action.migration': '🚚 数据库迁移...',
    'action.ai_chat': '🤖 AI 对话',
    'action.collab': '💬 协作会话',
    'action.perf_monitor': '📊 性能监控',
    'action.data_quality': '🔍 数据质量检查',
    'action.quality_v2': '📈 数据质量 2.0',
    'action.health': '💚 健康度检查',
    'action.alerts': '🔔 智能告警',
    'action.scheduler': '⏰ 任务调度',
    'action.assets': '📊 数据资产',
    'action.data_map': '🗺 数据地图',
    'action.reports': '📋 报表管理',
    'action.masking': '🔒 脱敏规则',
    'action.team_config': '👥 团队配置',
    'action.cloud_sync': '☁️ 云同步',
    'action.plugins': '🧩 插件管理',
    'action.audit': '📜 审计日志',
    'action.settings': '⚙️ 设置...',
    'action.about': '关于',
    'action.import': '导入',
    'action.export': '导出',
    # ---------- 工具栏 ----------
    'toolbar.title': '主工具栏',
    'toolbar.connect': '🔌 连接',
    'toolbar.query': '📝 查询',
    'toolbar.execute': '▶ 执行',
    'toolbar.explain': '📊 EXPLAIN',
    'toolbar.new_table': '➕ 新建表',
    'toolbar.edit_data': '✏ 编辑数据',
    'toolbar.import': '📥 导入',
    'toolbar.export': '📤 导出',
    'toolbar.refresh': '🔄 刷新',
    # ---------- 标签页 ----------
    'tab.query': '查询',
    'tab.history': '历史',
    'tab.snippets': '片段',
    'tab.ai': 'AI',
    'tab.agent': 'Agent',
    'tab.collab': '协作',
    'tab.catalog': '目录',
    'tab.tools': '工具面板',
    'tab.object_browser': '对象浏览器',
    # ---------- 状态栏 ----------
    'status.ready': '就绪',
    'status.executing': '执行中...',
    'status.connected': '已连接',
    'status.disconnected': '未连接',
    # ---------- 编辑器/结果空态 ----------
    'editor.empty_hint': '-- 输入 SQL 语句，Ctrl+Enter 执行',
    'result.rows': '返回 {n} 行',
    'result.affected': '影响 {n} 行',
    'result.empty': '执行查询后，结果显示在这里',
    # ---------- 设置对话框 ----------
    'settings.title': '设置',
    'settings.tab.general': '常规',
    'settings.tab.editor': '编辑器',
    'settings.tab.shortcuts': '快捷键',
    'settings.tab.advanced': '高级',
    'settings.theme': '主题:',
    'settings.language': '语言:',
    'settings.page_size': '默认每页行数:',
    'settings.auto_connect': '启动时自动连接',
    'settings.restore_tabs': '恢复上次打开的标签',
    'settings.editor_font': '编辑器字体:',
    'settings.font_size': '字号:',
    'settings.tab_size': 'Tab 缩进:',
    'settings.auto_complete': '启用 SQL 自动补全',
    'settings.auto_pair': '自动配对括号/引号',
    'settings.highlight_line': '高亮当前行',
    'settings.shortcuts_hint': '双击快捷键可修改:',
    'settings.shortcut_feature': '功能',
    'settings.shortcut_key': '快捷键',
    'settings.reset_shortcuts': '恢复默认',
    'settings.log': '日志',
    'settings.log_level': '日志级别:',
    'settings.performance': '性能',
    'settings.query_timeout': '查询超时:',
    'settings.max_rows': '结果集最大行数:',
    'settings.timeout_suffix': ' 秒 (0=无限制)',
    'settings.cache': '缓存',
    'settings.clear_history': '清除查询历史缓存',
    'settings.reset_all': '重置所有设置',
    'settings.high_dpi': '界面缩放:',
    'settings.high_dpi_auto': '自动（由系统 / Qt6 High-DPI 控制）',
    'settings.high_dpi_note': ('Qt6 默认启用 High-DPI 自动缩放：界面缩放由操作系统'
                               '显示设置控制；本版本不提供应用内覆盖，避免设置'
                               '看似生效实际无效。'),
    'settings.ok': '确定',
    'settings.cancel': '取消',
    'settings.apply': '应用',
    'settings.reset_confirm': '重置所有设置为默认值？',
    'settings.reset_done': '已重置',
    'settings.clear_confirm': '清除所有查询历史？',
    'settings.clear_done': '已清除',
    # ---------- 数据编辑器（已有 key 保留） ----------
    'data_editor.pending': '待提交: {m} 修改, {n} 新增, {d} 删除',
    'data_editor.commit': '提交',
    'data_editor.revert': '撤销',
    'data_editor.add_row': '新增行',
    'data_editor.del_row': '删除行',
    'data_editor.page_size': '每页',
    # ---------- 可访问性 ----------
    'a11y.main_window': 'LightDB Studio 主窗口',
    'a11y.sql_editor': 'SQL 查询编辑器（Ctrl+Enter 执行）',
    'a11y.result_table': '查询结果表格',
    'a11y.object_tree': '数据库对象浏览器',
    'a11y.toolbar': '主工具栏（导入/导出/执行等快捷操作）',
    'a11y.status': '状态栏（显示执行状态与错误信息）',
    'a11y.tabs': '查询标签页',
    'a11y.settings': '设置对话框',
}

_EN = {
    'app.title': 'LightDB Studio',
    # ---------- menus ----------
    'menu.file': '&File',
    'menu.edit': '&Edit',
    'menu.query': '&Query',
    'menu.table': '&Table',
    'menu.tools': '&Tools',
    'menu.view': '&View',
    'menu.help': '&Help',
    # ---------- actions ----------
    'action.new_connection': 'New Connection...',
    'action.new_query': 'New Query',
    'action.open_sql': 'Open SQL File...',
    'action.save_sql': 'Save SQL...',
    'action.exit': 'Exit',
    'action.close_tab': 'Close Current Tab',
    'action.format_sql': 'Format SQL',
    'action.execute': 'Execute',
    'action.explain': 'EXPLAIN',
    'action.cancel_query': 'Cancel Current Query',
    'action.cancel_query_tip': ('Cancel the running background query '
                                '(KILL the connection)'),
    'action.profiler': 'Profile Query',
    'action.query_builder': 'Query Builder...',
    'action.chart': 'Chart...',
    'action.export_result_csv': 'Export Current Result as CSV...',
    'action.new_table': 'New Table...',
    'action.edit_data': 'Edit Current Table Data',
    'action.db_compare': 'Compare Databases...',
    'action.er_diagram': 'ER Diagram...',
    'action.refresh_tree': 'Refresh Object Browser',
    'action.toggle_tools': 'Show/Hide Tools Panel',
    'action.review': '📊 SQL Review...',
    'action.submit_review': '📤 Submit Review',
    'action.lineage': '🔗 Data Lineage...',
    'action.snapshot': '📸 Snapshots / Clone...',
    'action.migration': '🚚 Database Migration...',
    'action.ai_chat': '🤖 AI Chat',
    'action.collab': '💬 Collaboration',
    'action.perf_monitor': '📊 Performance Monitor',
    'action.data_quality': '🔍 Data Quality Check',
    'action.quality_v2': '📈 Data Quality 2.0',
    'action.health': '💚 Health Check',
    'action.alerts': '🔔 Smart Alerts',
    'action.scheduler': '⏰ Task Scheduler',
    'action.assets': '📊 Data Assets',
    'action.data_map': '🗺 Data Map',
    'action.reports': '📋 Reports',
    'action.masking': '🔒 Masking Rules',
    'action.team_config': '👥 Team Config',
    'action.cloud_sync': '☁️ Cloud Sync',
    'action.plugins': '🧩 Plugins',
    'action.audit': '📜 Audit Log',
    'action.settings': '⚙️ Settings...',
    'action.about': 'About',
    'action.import': 'Import',
    'action.export': 'Export',
    # ---------- toolbar ----------
    'toolbar.title': 'Main Toolbar',
    'toolbar.connect': '🔌 Connect',
    'toolbar.query': '📝 Query',
    'toolbar.execute': '▶ Execute',
    'toolbar.explain': '📊 EXPLAIN',
    'toolbar.new_table': '➕ New Table',
    'toolbar.edit_data': '✏ Edit Data',
    'toolbar.import': '📥 Import',
    'toolbar.export': '📤 Export',
    'toolbar.refresh': '🔄 Refresh',
    # ---------- tabs ----------
    'tab.query': 'Query',
    'tab.history': 'History',
    'tab.snippets': 'Snippets',
    'tab.ai': 'AI',
    'tab.agent': 'Agent',
    'tab.collab': 'Collaboration',
    'tab.catalog': 'Catalog',
    'tab.tools': 'Tools',
    'tab.object_browser': 'Object Browser',
    # ---------- status ----------
    'status.ready': 'Ready',
    'status.executing': 'Executing...',
    'status.connected': 'Connected',
    'status.disconnected': 'Disconnected',
    # ---------- editor/result empty ----------
    'editor.empty_hint': '-- Type SQL here, Ctrl+Enter to execute',
    'result.rows': '{n} row(s) returned',
    'result.affected': '{n} row(s) affected',
    'result.empty': 'Query results will appear here',
    # ---------- settings ----------
    'settings.title': 'Settings',
    'settings.tab.general': 'General',
    'settings.tab.editor': 'Editor',
    'settings.tab.shortcuts': 'Shortcuts',
    'settings.tab.advanced': 'Advanced',
    'settings.theme': 'Theme:',
    'settings.language': 'Language:',
    'settings.page_size': 'Default page size:',
    'settings.auto_connect': 'Auto-connect on startup',
    'settings.restore_tabs': 'Restore tabs from last session',
    'settings.editor_font': 'Editor font:',
    'settings.font_size': 'Font size:',
    'settings.tab_size': 'Tab size:',
    'settings.auto_complete': 'Enable SQL auto-completion',
    'settings.auto_pair': 'Auto-pair brackets/quotes',
    'settings.highlight_line': 'Highlight current line',
    'settings.shortcuts_hint': 'Double-click a shortcut to edit:',
    'settings.shortcut_feature': 'Feature',
    'settings.shortcut_key': 'Shortcut',
    'settings.reset_shortcuts': 'Reset Defaults',
    'settings.log': 'Logging',
    'settings.log_level': 'Log level:',
    'settings.performance': 'Performance',
    'settings.query_timeout': 'Query timeout:',
    'settings.max_rows': 'Max result rows:',
    'settings.timeout_suffix': ' s (0 = unlimited)',
    'settings.cache': 'Cache',
    'settings.clear_history': 'Clear query history cache',
    'settings.reset_all': 'Reset all settings',
    'settings.high_dpi': 'UI scale:',
    'settings.high_dpi_auto': 'Auto (controlled by system / Qt6 High-DPI)',
    'settings.high_dpi_note': ('Qt6 enables High-DPI scaling automatically; the '
                               'scale is controlled by the OS display settings. '
                               'This build does not override it in-app, so the '
                               'option is never silently ineffective.'),
    'settings.ok': 'OK',
    'settings.cancel': 'Cancel',
    'settings.apply': 'Apply',
    'settings.reset_confirm': 'Reset all settings to defaults?',
    'settings.reset_done': 'Settings reset',
    'settings.clear_confirm': 'Clear all query history?',
    'settings.clear_done': 'History cleared',
    # ---------- data editor (existing keys kept) ----------
    'data_editor.pending': 'Pending: {m} modified, {n} new, {d} deleted',
    'data_editor.commit': 'Commit',
    'data_editor.revert': 'Revert',
    'data_editor.add_row': 'Add Row',
    'data_editor.del_row': 'Delete Row',
    'data_editor.page_size': 'Page',
    # ---------- accessibility ----------
    'a11y.main_window': 'LightDB Studio main window',
    'a11y.sql_editor': 'SQL query editor (Ctrl+Enter to execute)',
    'a11y.result_table': 'Query result table',
    'a11y.object_tree': 'Database object browser',
    'a11y.toolbar': 'Main toolbar (import/export/execute shortcuts)',
    'a11y.status': 'Status bar (execution state and errors)',
    'a11y.tabs': 'Query tabs',
    'a11y.settings': 'Settings dialog',
}

STRINGS = {'zh_CN': _ZH, 'en_US': _EN}


class I18N:
    def __init__(self, lang='zh_CN'):
        self.lang = lang if lang in STRINGS else 'zh_CN'
        self.listeners = []
        self._missing = []
        self._format_errors = []
        self._lang_warnings = []

    # ---------- 语言 ----------

    def set_lang(self, lang) -> bool:
        with _LOCK:
            if lang not in STRINGS:
                self._lang_warnings.append(f'未知语言: {lang!r}')
                return False
            if lang == self.lang:
                return True
            self.lang = lang
            callbacks = list(self.listeners)
        for cb in callbacks:
            try:
                cb()
            except Exception as exc:                  # noqa: BLE001
                self._lang_warnings.append(f'语言变更回调失败: {exc}')
        return True

    def on_change(self, callback):
        if callable(callback):
            self.listeners.append(callback)

    def off_change(self, callback):
        try:
            self.listeners.remove(callback)
        except ValueError:
            pass

    # ---------- 查询 ----------

    def _strings(self, lang=None):
        return STRINGS.get(lang or self.lang, STRINGS['zh_CN'])

    def key_set(self, lang=None) -> set:
        return set(self._strings(lang).keys())

    def check_coverage(self) -> dict:
        base = self.key_set('zh_CN')
        report = {'base': 'zh_CN', 'languages': {}, 'ok': True}
        for lang in STRINGS:
            keys = self.key_set(lang)
            missing = sorted(base - keys)
            extra = sorted(keys - base)
            report['languages'][lang] = {'missing': missing, 'extra': extra}
            if lang != 'zh_CN' and (missing or extra):
                report['ok'] = False
        return report

    def _record_missing(self, key):
        if key not in self._missing:
            self._missing.append(key)

    def missing(self, key=None):
        """无参数：当前语言缺失 key 列表；传 key：该 key 当前语言是否缺失。"""
        if key is not None:
            return key not in self._strings()
        return sorted(self._strings().keys() ^
                      self._strings('zh_CN').keys()) or []

    def missing_keys(self):
        return self.missing()

    def reset_missing(self):
        self._missing = []
        self._format_errors = []

    def t(self, key, **kwargs):
        strings = self._strings()
        if key not in strings:
            self._record_missing(key)
            return key
        text = strings[key]
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError) as exc:
                self._format_errors.append((key, str(exc)))
                return text
        return text

    def t_strict(self, key, **kwargs):
        strings = self._strings()
        if key not in strings:
            self._record_missing(key)
            raise KeyError(f'缺少翻译 key: {key} ({self.lang})')
        return self.t(key, **kwargs)

    def has(self, key) -> bool:
        return key in self._strings()

    def missing_report(self) -> dict:
        return {'lang': self.lang, 'missing': list(self._missing),
                'format_errors': list(self._format_errors),
                'lang_warnings': list(self._lang_warnings)}


i18n = I18N()
