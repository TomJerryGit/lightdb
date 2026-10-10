"""简单多语言支持"""


STRINGS = {
    'zh_CN': {
        'app.title': 'LightDB Studio',
        'menu.file': '文件(&F)',
        'menu.edit': '编辑(&E)',
        'menu.query': '查询(&Q)',
        'menu.table': '表(&T)',
        'menu.tools': '工具(&T)',
        'menu.view': '视图(&V)',
        'menu.help': '帮助(&H)',
        'action.new_connection': '新建连接...',
        'action.new_query': '新建查询',
        'action.execute': '执行',
        'action.explain': 'EXPLAIN',
        'action.refresh': '刷新',
        'action.save': '保存',
        'action.open': '打开',
        'action.close': '关闭',
        'action.exit': '退出',
        'action.settings': '设置...',
        'action.chart': '数据图表...',
        'action.import_wizard': '导入向导...',
        'action.export': '导出...',
        'action.toggle_history': '查询历史',
        'action.toggle_perf': '性能面板',
        'action.db_compare': '数据库对比...',
        'action.er_diagram': 'ER 图...',
        'action.query_builder': '查询构建器...',
        'status.ready': '就绪',
        'status.executing': '执行中...',
        'status.connected': '已连接',
        'status.disconnected': '未连接',
        'dialog.confirm': '确认',
        'dialog.cancel': '取消',
        'dialog.ok': '确定',
        'dialog.save': '保存',
        'dialog.error': '错误',
        'dialog.success': '成功',
        'dialog.warning': '警告',
        'dialog.info': '提示',
        'editor.empty_hint': '-- 输入 SQL 语句，Ctrl+Enter 执行',
        'result.rows': '返回 {n} 行',
        'result.affected': '影响 {n} 行',
        'data_editor.pending': '待提交: {m} 修改, {n} 新增, {d} 删除',
        'data_editor.commit': '提交',
        'data_editor.revert': '撤销',
        'data_editor.add_row': '新增行',
        'data_editor.del_row': '删除行',
        'data_editor.page_size': '每页',
    },
    'en_US': {
        'app.title': 'LightDB Studio',
        'menu.file': '&File',
        'menu.edit': '&Edit',
        'menu.query': '&Query',
        'menu.table': '&Table',
        'menu.tools': '&Tools',
        'menu.view': '&View',
        'menu.help': '&Help',
        'action.new_connection': 'New Connection...',
        'action.new_query': 'New Query',
        'action.execute': 'Execute',
        'action.explain': 'EXPLAIN',
        'action.refresh': 'Refresh',
        'action.save': 'Save',
        'action.open': 'Open',
        'action.close': 'Close',
        'action.exit': 'Exit',
        'action.settings': 'Settings...',
        'action.chart': 'Chart...',
        'action.import_wizard': 'Import Wizard...',
        'action.export': 'Export...',
        'action.toggle_history': 'Query History',
        'action.toggle_perf': 'Performance',
        'action.db_compare': 'Compare Databases...',
        'action.er_diagram': 'ER Diagram...',
        'action.query_builder': 'Query Builder...',
        'status.ready': 'Ready',
        'status.executing': 'Executing...',
        'status.connected': 'Connected',
        'status.disconnected': 'Disconnected',
        'dialog.confirm': 'Confirm',
        'dialog.cancel': 'Cancel',
        'dialog.ok': 'OK',
        'dialog.save': 'Save',
        'dialog.error': 'Error',
        'dialog.success': 'Success',
        'dialog.warning': 'Warning',
        'dialog.info': 'Info',
        'editor.empty_hint': '-- Type SQL here, Ctrl+Enter to execute',
        'result.rows': '{n} row(s) returned',
        'result.affected': '{n} row(s) affected',
        'data_editor.pending': 'Pending: {m} modified, {n} new, {d} deleted',
        'data_editor.commit': 'Commit',
        'data_editor.revert': 'Revert',
        'data_editor.add_row': 'Add Row',
        'data_editor.del_row': 'Delete Row',
        'data_editor.page_size': 'Page',
    },
}


class I18N:
    def __init__(self, lang='zh_CN'):
        self.lang = lang
        self.listeners = []

    def set_lang(self, lang):
        if lang in STRINGS:
            self.lang = lang
            for cb in self.listeners:
                try:
                    cb()
                except Exception:
                    pass

    def t(self, key, **kwargs):
        text = STRINGS.get(self.lang, STRINGS['zh_CN']).get(key, key)
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError):
                return text
        return text

    def on_change(self, callback):
        self.listeners.append(callback)


i18n = I18N()