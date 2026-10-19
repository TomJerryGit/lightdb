"""
LightDB Studio UI 层

分组:
- 主窗口 / 基础控件
- 查询相关
- 数据 / 表编辑
- 可视化
- 治理 / 质量
- 运维 / 监控
- AI / 协作
- 工具对话框
"""

__all__ = [
    # 主窗口
    'MainWindow',
    'ObjectTree',
    'SQLEditor',
    'ResultTable',
    'QueryHistoryPanel',
    'QueryBuilderDialog',
    'DataEditor',
    'TableDesigner',
    'ImportCSVDialog',
    'ExportCSVDialog',
    'ChartDialog',
    'DataMapView',
    'ERDiagramDialog',
    'CatalogPanel',
    'QualityV2Dialog',
    'DataQualityDialog',
    'AssetsDashboard',
    'ImpactDialog',
    'MaskingDialog',
    'LineageDialog',
    'LineageGraphView',
    'ProfilerView',
    'PerfMonitorPanel',
    'HealthDashboard',
    'AlertsPanel',
    'AuditViewer',
    'SchedulerPanel',
    'SnapshotDialog',
    'DBCompareDialog',
    'MigrationWizard',
    'AIChatPanel',
    'AgentChatPanel',
    'CollaborationPanel',
    'PluginPanel',
    'ReportBuilder',
    'ReviewPanel',
    'ReviewSubmitDialog',
    'TeamConfigDialog',
    'CloudSyncDialog',
    'ConnectionDialog',
    'SnippetPanel',
    'SettingsDialog',
    'EnvironmentBar',
    'apply_theme',
]


# ==================== 延迟导入表 ====================
# 名称 -> (子模块, 模块内属性)
_LAZY = {
    # 主窗口
    'MainWindow':              ('main_window', 'MainWindow'),
    'ObjectTree':              ('object_tree', 'ObjectTree'),
    'SQLEditor':               ('sql_editor', 'SQLEditor'),
    'ResultTable':             ('result_table', 'ResultTable'),
    'QueryHistoryPanel':       ('query_history', 'QueryHistoryPanel'),
    'QueryBuilderDialog':      ('query_builder', 'QueryBuilderDialog'),
    'DataEditor':              ('data_editor', 'DataEditor'),
    'TableDesigner':           ('table_designer', 'TableDesigner'),
    'ImportCSVDialog':         ('import_export', 'ImportCSVDialog'),
    'ExportCSVDialog':         ('import_export', 'ExportCSVDialog'),

    # 可视化
    'ChartDialog':             ('chart_view', 'ChartDialog'),
    'DataMapView':             ('data_map_view', 'DataMapView'),
    'ERDiagramDialog':         ('er_diagram', 'ERDiagramDialog'),

    # 治理 / 质量
    'CatalogPanel':            ('catalog_panel', 'CatalogPanel'),
    'QualityV2Dialog':         ('quality_v2_dialog', 'QualityV2Dialog'),
    'DataQualityDialog':       ('data_quality_dialog', 'DataQualityDialog'),
    'AssetsDashboard':         ('assets_dashboard', 'AssetsDashboard'),
    'ImpactDialog':            ('impact_dialog', 'ImpactDialog'),
    'MaskingDialog':           ('masking_dialog', 'MaskingDialog'),
    'LineageDialog':           ('lineage_view', 'LineageDialog'),
    'LineageGraphView':        ('lineage_view', 'LineageGraphView'),
    'ProfilerView':            ('profiler_view', 'ProfilerView'),

    # 运维 / 监控
    'PerfMonitorPanel':        ('perf_monitor', 'PerfMonitorPanel'),
    'HealthDashboard':         ('health_dashboard', 'HealthDashboard'),
    'AlertsPanel':             ('alerts_panel', 'AlertsPanel'),
    'AuditViewer':             ('audit_viewer', 'AuditViewer'),
    'SchedulerPanel':          ('scheduler_panel', 'SchedulerPanel'),
    'SnapshotDialog':          ('snapshot_dialog', 'SnapshotDialog'),
    'DBCompareDialog':         ('db_compare', 'DBCompareDialog'),
    'MigrationWizard':         ('migration_wizard', 'MigrationWizard'),

    # AI / 协作
    'AIChatPanel':             ('ai_chat', 'AIChatPanel'),
    'AgentChatPanel':          ('agent_chat', 'AgentChatPanel'),
    'CollaborationPanel':      ('collaboration_panel', 'CollaborationPanel'),
    'PluginPanel':             ('plugin_panel', 'PluginPanel'),

    # 工具对话框
    'ReportBuilder':           ('report_builder', 'ReportBuilder'),
    'ReviewPanel':             ('review_panel', 'ReviewPanel'),
    'ReviewSubmitDialog':      ('review_panel', 'ReviewSubmitDialog'),
    'TeamConfigDialog':        ('team_config_dialog', 'TeamConfigDialog'),
    'CloudSyncDialog':         ('cloud_sync_dialog', 'CloudSyncDialog'),
    'ConnectionDialog':        ('connection_dialog', 'ConnectionDialog'),
    'SnippetPanel':            ('snippet_panel', 'SnippetPanel'),
    'SettingsDialog':          ('settings_dialog', 'SettingsDialog'),
    'EnvironmentBar':          ('environment_bar', 'EnvironmentBar'),

    # 主题
    'apply_theme':             ('theme', 'apply_theme'),
}


def __getattr__(name):
    """延迟导入：只在第一次访问时加载对应 UI 模块"""
    if name in _LAZY:
        import importlib
        submodule, attr = _LAZY[name]
        mod = importlib.import_module(f'.{submodule}', __name__)
        value = getattr(mod, attr)
        # 缓存到模块命名空间，避免重复 import
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals().keys()) + __all__)