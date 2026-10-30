"""主窗口"""
import os
import re
import sys
import threading
import time
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QTabWidget, QWidget, QVBoxLayout,
    QHBoxLayout, QToolBar, QStatusBar, QMessageBox, QFileDialog,
    QSplitter, QLabel, QInputDialog, QDialog, QLineEdit, QMenu,
)
from PySide6.QtCore import Qt, QSize, QTimer, QThreadPool
from PySide6.QtGui import QAction, QKeySequence

# ---------- 核心管理器 ----------
from .. import __version__ as _STUDIO_VERSION
from ..core.environments import EnvironmentManager
from ..core.lineage import LineageStore
from ..core.lineage_enhanced import EnhancedLineageStore
from ..core.review_workflow import ReviewWorkflow
from ..core.snapshot import SnapshotManager
from ..core.sql_formatter import SQLFormatter
from ..core.ai_integration import AIClient, AIConfigStore
from ..core.ai_agent import AIAgent
from ..core.collaboration import CollaborationStore
from ..core.performance_monitor import PerformanceMonitor
from ..core.data_quality import DataQualityChecker
from ..core.migration import MySQLMigrator, SQLiteMigrator
from ..core.snippets import SnippetManager
from ..core.masking import MaskingEngine
from ..core.team_config import TeamConfigManager
from ..core.cloud_sync import CloudSync
from ..core.reports import ReportEngine
from ..core.scheduler import Scheduler
from ..core.workflow import WorkflowEngine
from ..core.plugin_manager import PluginManager
from ..core.alerts import AlertEngine
from ..core.catalog import CatalogEngine
from ..core.metrics import get_registry

# ---------- i18n ----------
from ..utils.i18n import i18n, STRINGS
from ..utils.history import QueryHistory

# ---------- DB 错误字段（errno / sqlstate / message，绝不丢） ----------
from ..db.connection import error_code, error_sqlstate
from ..core.db_errors import connection_error_summary

# ---------- UI 面板 ----------
from .ai_chat import AIChatPanel
from .agent_chat import AgentChatPanel
from .collaboration_panel import CollaborationPanel
from .perf_monitor import PerfMonitorPanel
from .data_quality_dialog import DataQualityDialog
from .quality_v2_dialog import QualityV2Dialog
from .migration_wizard import MigrationWizard
from .environment_bar import EnvironmentBar
from .lineage_view import LineageGraphView, LineageDialog
from .review_panel import ReviewPanel, ReviewSubmitDialog
from .snapshot_dialog import SnapshotDialog
from .connection_dialog import (
    ConnectionDialog, delete_connection, duplicate_connection,
    edit_connection, export_connections_json, import_connections_json)
from .error_prompts import (show_connection_error,
                            show_connection_error_nonblocking)
from .object_tree import ObjectTree
from .sql_editor import SQLEditor
from .result_table import ResultTable, parse_error_fields
from .workers import QueryWorker
from .data_editor import DataEditor
from .table_designer import TableDesigner
from .query_history import QueryHistoryPanel
from .import_export import (
    ImportCSVDialog, ExportCSVDialog, write_export_chunked)
from .snippet_panel import SnippetPanel
from .catalog_panel import CatalogPanel
from .profiler_view import ProfilerView
from .db_compare import DBCompareDialog
from .er_diagram import ERDiagramDialog
from .query_builder import QueryBuilderDialog
from .chart_view import ChartDialog
from .settings_dialog import SettingsDialog
from .assets_dashboard import AssetsDashboard
from .health_dashboard import HealthDashboard
from .alerts_panel import AlertsPanel
from .scheduler_panel import SchedulerPanel
from .masking_dialog import MaskingDialog
from .team_config_dialog import TeamConfigDialog
from .cloud_sync_dialog import CloudSyncDialog
from .report_builder import ReportBuilder
from .plugin_panel import PluginPanel
from .audit_viewer import AuditViewer
from .data_map_view import DataMapView

# ---------- DB ----------
from ..config import ConnectionInfo
from ..db.connection import DBConnection

# 仅供性能对照测量：置 1 时回到"全部管理器立即创建 / 右侧面板立即创建 /
# 启动期恢复立即执行"的老行为，用于同一代码状态下的 before/after 计时。
_LEGACY_STARTUP = os.environ.get('LIGHTDB_STUDIO_LEGACY_STARTUP', '') \
    .strip().lower() not in ('', '0', 'false', 'no', 'off')
# 只回退"启动期恢复延迟"一项（其余优化保留），用于分项计时。
_LEGACY_RESTORE = _LEGACY_STARTUP or (
    os.environ.get('LIGHTDB_STUDIO_LEGACY_RESTORE', '')
    .strip().lower() not in ('', '0', 'false', 'no', 'off'))


class MainWindow(QMainWindow):
    """LightDB Studio 主窗口"""

    def __init__(self, config):
        # 兜底：如果调用方没走 `lightdb_studio.app`（测试/嵌入方直接建窗口），
        # 字体目录变量还没设，Qt 会报 "Cannot find font directory"。
        # 在 QApplication 已存在时设置无效但无害；正常入口在 app.py 里已经设过。
        # 注意：必须从**中立模块** `core.fonts` 导入。此前这里写的是
        # `from ..app import ensure_font_dir`，会把入口模块 app.py 当普通模块
        # **第二次执行**（模块级代码与启动计时标记重跑一遍），并且构成
        # `app -> main_window -> app` 循环导入。
        try:
            from ..core.fonts import ensure_font_dir
            ensure_font_dir()
        except Exception as e:                        # noqa: BLE001
            print('字体目录设置失败（不影响功能）: %s' % e, file=sys.stderr)

        super().__init__()
        self.config = config
        self.settings = config.settings
        # task-75：多窗口生命周期跟踪（子窗口关闭/主窗口关闭都清理）
        self._extra_windows = []
        self._shortcut_apply_result = {}
        self.history = QueryHistory()

        # ---------- 后台查询状态（P1-1 响应性） ----------
        # 真实 UI 查询走 `_execute_sql_async()`：DB 调用在 `ui.workers.QueryWorker`
        # 的线程池线程执行，信号回主线程更新 UI；`_execute_sql()` 同步版本保留给
        # 测试/嵌入调用方（旧 API 兼容，不改变既有行为）。
        self._query_pool = QThreadPool(self)
        self._query_pool.setMaxThreadCount(2)
        self._active_worker = None
        self._active_signals = None
        self._active_ctx = None
        self._query_busy = False
        self._abort_reason = ''
        self._cancel_action = None
        self._load_more_ctx = {}
        self._query_timeout_timer = QTimer(self)
        self._query_timeout_timer.setSingleShot(True)
        self._query_timeout_timer.timeout.connect(self._on_query_timeout)

        # ---------- P1-3 schema 缓存（自动补全用） ----------
        # 后台线程刷新（DB 查询不占 UI 线程）；编辑器补全只读这份内存缓存。
        self._schema_cache = {'tables': [], 'columns': {}, 'ts': 0.0}
        self._schema_lock = threading.Lock()
        self._schema_thread = None

        # ---------- 核心管理器 ----------
        # 说明（实测）：30+ 个管理器**全部构造只占 ~9ms**（MainWindow 构造的
        # 1% 不到），所以这里只把「首屏完全用不到」的 workflow_engine /
        # ai_agent / alert_engine 做成惰性属性（见下方 `__getattr__` 与
        # `_LAZY_MANAGERS`）：真正省时间的是 UI 面板的延迟构建与主题重复
        # 应用的去重。
        self.env_mgr = EnvironmentManager()
        self.lineage_store = LineageStore()
        self.lineage_enhanced = EnhancedLineageStore()
        self.review_workflow = ReviewWorkflow()
        self.snapshot_mgr = SnapshotManager()
        # P2-5：定时快照不在后台常驻线程里跑；主窗口用 60s QTimer 调
        # `snapshot_mgr.run_due(conn)`（到点才创建；未启用/无连接直接跳过）。
        # 这样既保留"定时"能力，又避免常驻线程在 GUI 关闭时泄漏/崩溃。
        self._snapshot_timer = QTimer(self)
        self._snapshot_timer.setInterval(60_000)
        self._snapshot_timer.timeout.connect(self._tick_snapshot_schedule)
        self._snapshot_timer.start()
        self.sql_formatter = SQLFormatter()
        self.snippet_mgr = SnippetManager()
        self.masking_engine = MaskingEngine()
        self.team_mgr = TeamConfigManager()
        self.cloud_sync = CloudSync()
        self.report_engine = ReportEngine()
        self.collab_store = CollaborationStore()
        self.ai_store = AIConfigStore()
        self.catalog = CatalogEngine()

        # 调度
        # scheduler 保持即时创建：构造函数末尾要 start()（后台线程/定时器），
        # 延迟创建会把"调度器是否在跑"变成不确定状态。
        self.scheduler = Scheduler(
            connection_provider=self._get_conn_by_name,
            report_engine=self.report_engine)

        if _LEGACY_STARTUP:                 # 对照测量用：立即建（老行为）
            self.workflow_engine = WorkflowEngine(
                connection_provider=self._get_conn_by_name)
            self.ai_agent = AIAgent(
                conn_getter=self._get_current_conn,
                ai_config=self.ai_store.load(),
                catalog=self.catalog,
                lineage=self.lineage_store)
            self.alert_engine = AlertEngine()

        # 插件系统：延迟到首屏之后 load_all()（见 __init__ 末尾的
        # `_defer_startup_work`），插件目录为空时几乎零成本，但插件多/慢时
        # 它会把窗口卡在"白屏"。`plugin_mgr` 本身仍是即时创建的同名属性，
        # 首屏之后由 load_all() 填充。
        plugins_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            '..', 'plugins')
        self.plugin_mgr = PluginManager(plugins_dir, self._plugin_context())

        # i18n
        i18n.set_lang(self.settings.language)
        i18n.on_change(self._on_lang_changed)

        self.setWindowTitle(f"{i18n.t('app.title')} v{_STUDIO_VERSION}")
        self.resize(1480, 940)

        # 构建 UI
        # 注意顺序：_build_ui() 里新增查询标签页会触发 _on_tab_changed()，
        # 而它要访问 self.conn_label；因此状态栏必须先建好（原实现顺序相反，
        # 导致 MainWindow 构造时直接 AttributeError）。
        self._build_menu()
        self._build_statusbar()
        self._build_ui()
        self._build_toolbar()
        self._setup_accessibility()
        self._apply_settings()
        self._apply_shortcut_settings()

        # 启动后台服务
        self.scheduler.start()

        if _LEGACY_RESTORE:                 # 对照测量用：立即恢复（老行为）
            try:
                self.plugin_mgr.load_all()
            except Exception as e:            # noqa: BLE001
                print('插件加载失败: %s' % e, file=sys.stderr)
            self._install_plugin_actions()
            self._auto_restore()
        else:
            # 启动期恢复（插件扫描 / 自动连接 / 恢复上次打开的查询页）
            # 推迟到首屏显示之后，见 `_defer_startup_work`。
            self._defer_startup_work()

    # ==================== 惰性管理器 / 惰性面板 ====================

    # 名字 → 构造函数（在 `__getattr__` 里首次访问时才执行，主线程）。
    #   * 管理器：首屏完全用不到的 workflow_engine / ai_agent / alert_engine；
    #   * 右侧面板：ai_chat / agent_chat / collab_panel / catalog_panel
    #     （它们对应的标签页首屏看不见，构造是纯浪费，实测 7~27ms/个）。
    # `hasattr(win, 'catalog_panel')` 会照常构造 —— 所以对既有代码/测试来说
    # 这些属性依然"存在"，只是构造被推迟到第一次真正访问。
    _LAZY_MANAGERS = {
        'workflow_engine': lambda self: WorkflowEngine(
            connection_provider=self._get_conn_by_name),
        'ai_agent': lambda self: AIAgent(
            conn_getter=self._get_current_conn,
            ai_config=self.ai_store.load(),
            catalog=self.catalog,
            lineage=self.lineage_store),
        'alert_engine': lambda self: AlertEngine(),
        'ai_chat': lambda self: self._make_ai_chat(),
        'agent_chat': lambda self: self._make_agent_chat(),
        'collab_panel': lambda self: self._make_collab_panel(),
        'catalog_panel': lambda self: self._make_catalog_panel(),
    }

    def __getattr__(self, name):
        """惰性管理器：首次访问才构造，构造后写回 `__dict__`。

        写回后就是普通实例属性，`vars(win)` / `hasattr` / 赋值覆盖的行为与
        惰性化之前完全一致。构造失败**不吞异常**（不会留下半初始化状态）。
        """
        if name in MainWindow._LAZY_MANAGERS:
            obj = MainWindow._LAZY_MANAGERS[name](self)   # 抛错就抛错
            self.__dict__[name] = obj
            return obj
        raise AttributeError('%s 对象没有属性 %r'
                             % (type(self).__name__, name))

    def _lazy_eager(self, name):
        """若该惰性管理器**已经**被创建过就返回它，否则返回 None（不创建）。"""
        return self.__dict__.get(name)

    def _defer_startup_work(self):
        """把「首屏不需要」的启动期恢复动作推迟到窗口显示之后。

        这些动作最终**一定会发生**（`QTimer.singleShot(0, ...)` 在事件循环
        第一个事件里跑，即窗口 show() 之后），只是不再挡在首屏前面：
          * 插件扫描 load_all()（插件目录为空时 ~0.2ms，插件多时会白屏）；
          * `_auto_restore()`：逐个 DBConnection.connect()（真实 TCP 握手，
            慢网/不可达时每个连接都要等到超时）。`auto_connect=False` 时
            仍按老逻辑不连。
        """
        QTimer.singleShot(0, self.do_startup_restore)

    def do_startup_restore(self):
        """首屏之后要做的启动期恢复（可被测试直接调用，不必等事件循环）。"""
        if getattr(self, '_startup_restore_done', False):
            return
        self._startup_restore_done = True
        try:
            self.plugin_mgr.load_all()
        except Exception as e:                        # noqa: BLE001
            print('插件加载失败: %s' % e, file=sys.stderr)
        self._install_plugin_actions()
        self._auto_restore()

    def _install_plugin_actions(self):
        """P2-8：把已加载插件的菜单/工具栏 action 安装进主窗口。

        插件加载是延迟启动动作；安装只在这里做一次（面板启停后不会自动
        重装，避免菜单/工具栏重复堆积）。失败显式写 stderr，不静默。
        """
        installer = getattr(self.plugin_mgr, 'install_actions', None)
        if not callable(installer):
            return
        try:
            result = installer(self) or {}
            errors = result.get('errors') or []
            if errors:
                print('插件 action 安装失败: %s' % '; '.join(
                    str(e) for e in errors), file=sys.stderr)
        except Exception as e:                        # noqa: BLE001
            print('插件 action 安装异常: %s' % e, file=sys.stderr)


    # ==================== 上下文 ====================

    def _plugin_context(self) -> dict:
        return {
            'main_window': self,
            'get_connection': self._get_current_conn,
            'show_message': lambda m: self.status_label.setText(m),
        }

    # ==================== UI 构建 ====================

    def _build_ui(self):
        # 左侧：对象浏览器
        self.object_tree = ObjectTree()
        self.object_tree.query_requested.connect(self._on_query_requested)
        self.object_tree.new_query_requested.connect(self._open_sql_in_new_tab)
        # G1：对象树里的设计表/导入/导出动作接线到现有实现
        self.object_tree.design_table_requested.connect(
            self._on_tree_design_table)
        self.object_tree.import_data_requested.connect(
            self._on_tree_import_data)
        self.object_tree.export_data_requested.connect(
            self._on_tree_export_data)
        # task-87：双击表/右键“打开表”→ 数据编辑器
        self.object_tree.table_double_clicked.connect(self._open_data_editor)

        self.tree_dock = QDockWidget(i18n.t('tab.object_browser'), self)
        self.tree_dock.setWidget(self.object_tree)
        self.tree_dock.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.tree_dock)
        self.tree_dock.setMinimumWidth(260)

        # 右侧：多功能 Tab
        self.right_tabs = QTabWidget()
        self.right_tabs.setTabPosition(QTabWidget.North)
        self._right_tab_title_keys = {}

        # 首屏可见的右侧标签（右侧工具面板的第一个标签是"历史"）：
        # 这两个面板很小（历史 0.7ms / 片段 ~1ms）且首屏就在前台，直接建。
        self.history_panel = QueryHistoryPanel(self.history)
        self.history_panel.sql_selected.connect(self._insert_history_sql)
        idx = self.right_tabs.addTab(self.history_panel, i18n.t('tab.history'))
        self._right_tab_title_keys[idx] = 'tab.history'

        self.snippet_panel = SnippetPanel(self.snippet_mgr)
        self.snippet_panel.snippet_activated.connect(self._insert_sql_to_editor)
        idx = self.right_tabs.addTab(self.snippet_panel, i18n.t('tab.snippets'))
        self._right_tab_title_keys[idx] = 'tab.snippets'

        # 其余四个面板首屏看不到（右侧面板当前停在"历史"），构造它们是纯浪费：
        # 实测 AI 11ms + Agent(含 AIAgent) 19ms + 协作 7ms + 目录 27ms。
        # 这里先占位，**首次切到该标签（或首次访问 `win.ai_chat` 等属性）时**
        # 才真正构造，见 `_ensure_right_tab` / `__getattr__`。
        self._right_tab_index = {}
        for attr, title_key in (('ai_chat', 'tab.ai'),
                                ('agent_chat', 'tab.agent'),
                                ('collab_panel', 'tab.collab'),
                                ('catalog_panel', 'tab.catalog')):
            ph = QWidget()
            idx = self.right_tabs.addTab(ph, i18n.t(title_key))
            self._right_tab_index[attr] = idx
            self._right_tab_title_keys[idx] = title_key
        self.right_tabs.currentChanged.connect(self._on_right_tab_changed)
        if _LEGACY_STARTUP:                 # 对照测量用：立即建（老行为）
            for _attr in list(self._right_tab_index):
                self._ensure_right_tab(_attr)
            self.right_tabs.setCurrentIndex(0)

        self.right_dock = QDockWidget(i18n.t('tab.tools'), self)
        self.right_dock.setWidget(self.right_tabs)
        self.right_dock.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.RightDockWidgetArea, self.right_dock)
        self.right_dock.setMinimumWidth(320)

        # 中间：Tab 容器
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

        # 环境条
        self.env_bar = EnvironmentBar(self.env_mgr)
        self.env_bar.environment_changed.connect(self._on_env_changed)

        # 添加一个空白查询标签
        self._add_new_query_tab(auto_switch=True)

    # ---------- 右侧面板的延迟构建 ----------

    def _make_ai_chat(self):
        panel = AIChatPanel(schema_provider=self._schema_provider,
                            conn_getter=self._get_current_conn)
        panel.sql_generated.connect(self._insert_sql_to_editor)
        return panel

    def _make_agent_chat(self):
        # self.ai_agent 是惰性管理器：真正需要 Agent 面板时才构造
        panel = AgentChatPanel(self.ai_agent)
        panel.sql_generated.connect(self._insert_sql_to_editor)
        return panel

    def _make_collab_panel(self):
        panel = CollaborationPanel(self.collab_store)
        panel.session_selected.connect(self._on_session_selected)
        return panel

    def _make_catalog_panel(self):
        panel = CatalogPanel(self.catalog)
        panel.table_selected.connect(self._on_catalog_selected)
        return panel

    def _ensure_right_tab(self, attr):
        """保证 `attr` 对应的右侧标签页上挂着**真面板**（幂等）。

        注意两个坑：
          1. `removeTab`/`insertTab`/`setCurrentIndex` 都会再次发出
             `currentChanged`，所以用 `_right_tab_building` 做重入保护，
             否则无限递归；
          2. 面板可能已经被 `win.catalog_panel` 这样的**属性访问**提前
             构造了（建好但还没挂到标签页上），所以"已在 __dict__ 里"
             也必须继续走挂载流程。
        """
        panel = self.__dict__.get(attr)
        if panel is None:
            panel = getattr(self, attr)             # 走 __getattr__ 惰性构造
        if panel is None:
            return None
        idx = self._right_tab_index[attr]
        if self.right_tabs.widget(idx) is panel:
            return panel                            # 已经挂好了
        if getattr(self, '_right_tab_building', False):
            # 重入（信号是同步发的）：外层正在挂，这里不要动标签页，
            # 直接把真面板交回去即可。
            return panel
        self._right_tab_building = True
        try:
            old = self.right_tabs.widget(idx)
            self.right_tabs.removeTab(idx)
            if old is not None and old is not panel:
                old.setParent(None)
                old.deleteLater()
            self.right_tabs.insertTab(
                idx, panel, i18n.t(self._right_tab_titles[attr]))
            if self.right_tabs.widget(idx) is not panel:
                for i in range(self.right_tabs.count()):
                    if self.right_tabs.widget(i) is panel:
                        idx = i
                        break
                else:
                    raise RuntimeError('右侧面板 %s 未能挂到标签页上' % attr)
            self._right_tab_index[attr] = idx
            self.right_tabs.setCurrentIndex(idx)
            return panel
        finally:
            self._right_tab_building = False

    _right_tab_titles = {'ai_chat': 'tab.ai', 'agent_chat': 'tab.agent',
                         'collab_panel': 'tab.collab',
                         'catalog_panel': 'tab.catalog'}

    def _on_right_tab_changed(self, idx):
        for attr, i in self._right_tab_index.items():
            if i != idx:
                continue
            panel = self.__dict__.get(attr)
            if panel is None or self.right_tabs.widget(i) is not panel:
                # 用户真的要看这个面板了 —— 现在才值得花那 7~27ms
                self._ensure_right_tab(attr)
            return

    def _build_menu(self):
        mb = self.menuBar()

        # ---------- 文件 ----------
        m_file = mb.addMenu("文件(&F)")

        a = QAction("新建连接...", self)
        a.triggered.connect(self._new_connection)
        m_file.addAction(a)

        # G1：编辑 / 复制 / 删除连接（作用于对象树当前选中的连接）
        a = QAction("编辑当前连接...", self)
        a.triggered.connect(self._edit_current_connection)
        m_file.addAction(a)
        a = QAction("复制当前连接...", self)
        a.triggered.connect(self._duplicate_current_connection)
        m_file.addAction(a)
        a = QAction("删除当前连接...", self)
        a.triggered.connect(self._delete_current_connection)
        m_file.addAction(a)
        a = QAction("导出连接...", self)
        a.triggered.connect(self._export_connections)
        m_file.addAction(a)
        a = QAction("导入连接...", self)
        a.triggered.connect(self._import_connections)
        m_file.addAction(a)

        a = QAction("新建查询", self)
        a.setShortcut(QKeySequence("Ctrl+N"))
        a.triggered.connect(lambda: self._add_new_query_tab(True))
        m_file.addAction(a)

        m_file.addSeparator()

        a = QAction("打开 SQL 文件...", self)
        a.setShortcut(QKeySequence("Ctrl+O"))
        a.triggered.connect(self._open_sql_file)
        m_file.addAction(a)

        a = QAction("保存 SQL...", self)
        a.setShortcut(QKeySequence("Ctrl+S"))
        a.triggered.connect(self._save_sql_file)
        m_file.addAction(a)

        # G2：导出 JSON / SQL INSERT（当前结果集）
        a = QAction("导出当前结果为 JSON...", self)
        a.triggered.connect(lambda: self._export_current_result_as('json'))
        m_file.addAction(a)
        a = QAction("导出当前结果为 SQL INSERT...", self)
        a.triggered.connect(
            lambda: self._export_current_result_as('sql_insert'))
        m_file.addAction(a)

        m_file.addSeparator()

        a = QAction("退出", self)
        a.setShortcut(QKeySequence("Ctrl+Q"))
        a.triggered.connect(self.close)
        m_file.addAction(a)

        # ---------- 编辑 ----------
        m_edit = mb.addMenu("编辑(&E)")

        a = QAction("关闭当前标签", self)
        a.setShortcut(QKeySequence("Ctrl+W"))
        a.triggered.connect(
            lambda: self._close_tab(self.tabs.currentIndex()))
        m_edit.addAction(a)

        a = QAction("格式化 SQL", self)
        a.setShortcut(QKeySequence("Ctrl+Shift+F"))
        a.triggered.connect(self._format_sql)
        m_edit.addAction(a)

        # ---------- 视图（N1：现有动作不丢，快捷切主题/面板） ----------
        m_view = mb.addMenu(i18n.t('menu.view'))
        a = QAction("显示/隐藏工具面板", self)
        a.triggered.connect(self._toggle_right_dock)
        m_view.addAction(a)
        a = QAction("刷新对象浏览器", self)
        a.triggered.connect(self._refresh_tree)
        m_view.addAction(a)
        a = QAction("显示/隐藏对象浏览器", self)
        a.triggered.connect(
            lambda: self.tree_dock.setVisible(not self.tree_dock.isVisible()))
        m_view.addAction(a)
        m_theme = m_view.addMenu("主题")
        for theme_id, label in (('dark', '深色（Navicat）'),
                                ('light', '浅色（Navicat）'),
                                ('highcontrast', '高对比度')):
            act = QAction(label, self)
            act.triggered.connect(
                lambda _checked=False, t=theme_id:
                self._set_theme_from_menu(t))
            m_theme.addAction(act)

        # ---------- 查询 ----------
        m_query = mb.addMenu("查询(&Q)")

        a = QAction("执行", self)
        a.setShortcut(QKeySequence("F5"))
        a.triggered.connect(self._exec_current)
        m_query.addAction(a)

        a = QAction("EXPLAIN", self)
        a.setShortcut(QKeySequence("Ctrl+E"))
        a.triggered.connect(self._exec_explain)
        m_query.addAction(a)

        a = QAction("📊 可视化 EXPLAIN...", self)
        a.triggered.connect(self._open_visual_explain)
        m_query.addAction(a)

        self._cancel_action = QAction("取消当前查询", self)
        self._cancel_action.setToolTip("取消正在执行的后台查询（KILL 连接）")
        self._cancel_action.setEnabled(False)
        self._cancel_action.triggered.connect(lambda: self.cancel_running_query())
        m_query.addAction(self._cancel_action)

        a = QAction("性能分析", self)
        a.triggered.connect(self._open_profiler)
        m_query.addAction(a)

        m_query.addSeparator()

        a = QAction("查询构建器...", self)
        a.triggered.connect(self._open_query_builder)
        m_query.addAction(a)

        a = QAction("数据图表...", self)
        a.setShortcut(QKeySequence("Ctrl+G"))
        a.triggered.connect(self._open_chart)
        m_query.addAction(a)

        m_query.addSeparator()

        a = QAction("导出当前结果为 CSV...", self)
        a.triggered.connect(self._export_current_result)
        m_query.addAction(a)

        # ---------- 表 ----------
        m_table = mb.addMenu("表(&T)")

        a = QAction("新建表...", self)
        a.setShortcut(QKeySequence("Ctrl+T"))
        a.triggered.connect(self._new_table_designer)
        m_table.addAction(a)

        a = QAction("编辑当前表数据", self)
        a.triggered.connect(self._edit_current_table)
        m_table.addAction(a)

        a = QAction("数据库对比...", self)
        a.triggered.connect(self._open_db_compare)
        m_table.addAction(a)

        a = QAction("ER 图...", self)
        a.triggered.connect(self._open_er_diagram)
        m_table.addAction(a)

        # ---------- 工具 ----------
        m_tools = mb.addMenu("工具(&T)")

        a = QAction("刷新对象浏览器", self)
        a.setShortcut(QKeySequence("F9"))
        a.triggered.connect(self._refresh_tree)
        m_tools.addAction(a)

        a = QAction("显示/隐藏工具面板", self)
        a.setShortcut(QKeySequence("Ctrl+H"))
        a.triggered.connect(self._toggle_right_dock)
        m_tools.addAction(a)

        m_tools.addSeparator()

        a = QAction("📊 SQL 审查...", self)
        a.triggered.connect(self._open_review)
        m_tools.addAction(a)

        a = QAction("📤 提交审查", self)
        a.triggered.connect(self._submit_review)
        m_tools.addAction(a)

        a = QAction("🔗 数据血缘...", self)
        a.triggered.connect(self._open_lineage)
        m_tools.addAction(a)

        a = QAction("📸 快照 / 克隆...", self)
        a.triggered.connect(self._open_snapshot)
        m_tools.addAction(a)

        a = QAction("🩺 生产面板：复制 / 指标 / 二进制快照...", self)
        a.setToolTip('读取真实复制状态、Prometheus 指标摘要、调用快照 CLI（恢复需确认）')
        a.triggered.connect(self._open_production_panel)
        m_tools.addAction(a)

        a = QAction("🚚 数据库迁移...", self)
        a.triggered.connect(self._open_migration)
        m_tools.addAction(a)

        m_tools.addSeparator()

        a = QAction("🤖 AI 对话", self)
        a.setShortcut(QKeySequence("Ctrl+Shift+A"))
        # 传属性名而不是 `self.ai_chat`：后者会在 `_build_menu()` 里就把
        # 面板建出来，延迟构建就白做了。
        a.triggered.connect(lambda: self._show_right_tab('ai_chat'))
        m_tools.addAction(a)

        # task-43：AI SQL 助手（建表/存储过程生成）+ 离线示例库
        a = QAction("🪄 AI SQL 助手...", self)
        a.setToolTip('自然语言生成 CREATE TABLE/CREATE PROCEDURE，'
                     '预览 + parser 校验 + 二次确认执行')
        a.triggered.connect(self._open_ai_sql_dialog)
        m_tools.addAction(a)

        m_examples = m_tools.addMenu("📚 SQL 示例库")
        try:
            from .ai_examples import example_menu_entries
            for example_id, title in example_menu_entries():
                act = QAction(title, self)
                act.triggered.connect(
                    lambda _checked=False, eid=example_id:
                    self._insert_sql_example(eid))
                m_examples.addAction(act)
        except Exception:
            pass

        a = QAction("💬 协作会话", self)
        a.triggered.connect(lambda: self._show_right_tab('collab_panel'))
        m_tools.addAction(a)

        a = QAction("📊 性能监控", self)
        a.triggered.connect(self._open_perf_monitor)
        m_tools.addAction(a)

        a = QAction("🔍 数据质量检查", self)
        a.triggered.connect(self._open_data_quality)
        m_tools.addAction(a)

        a = QAction("📈 数据质量 2.0", self)
        a.triggered.connect(self._open_quality_v2)
        m_tools.addAction(a)

        a = QAction("💚 健康度检查", self)
        a.triggered.connect(self._open_health)
        m_tools.addAction(a)

        a = QAction("🔔 智能告警", self)
        a.triggered.connect(self._open_alerts)
        m_tools.addAction(a)

        a = QAction("⏰ 任务调度", self)
        a.triggered.connect(self._open_scheduler)
        m_tools.addAction(a)

        a = QAction("📊 数据资产", self)
        a.triggered.connect(self._open_assets)
        m_tools.addAction(a)

        a = QAction("🗺 数据地图", self)
        a.triggered.connect(self._open_data_map)
        m_tools.addAction(a)

        a = QAction("📋 报表管理", self)
        a.triggered.connect(self._open_reports)
        m_tools.addAction(a)

        a = QAction("🔒 脱敏规则", self)
        a.triggered.connect(self._open_masking)
        m_tools.addAction(a)

        a = QAction("👥 团队配置", self)
        a.triggered.connect(self._open_team_config)
        m_tools.addAction(a)

        a = QAction("☁️ 云同步", self)
        a.triggered.connect(self._open_cloud_sync)
        m_tools.addAction(a)

        a = QAction("🧩 插件管理", self)
        a.triggered.connect(self._open_plugins)
        m_tools.addAction(a)

        a = QAction("📜 审计日志", self)
        a.triggered.connect(self._open_audit)
        m_tools.addAction(a)

        m_tools.addSeparator()

        a = QAction("⚙️ 设置...", self)
        a.setShortcut(QKeySequence("Ctrl+,"))
        a.triggered.connect(self._open_settings)
        m_tools.addAction(a)

        m_tools.addSeparator()
        a = QAction("👥 用户管理...", self)
        a.triggered.connect(self._open_user_management)
        m_tools.addAction(a)
        a = QAction("🪟 新建窗口", self)
        a.setShortcut(QKeySequence("Ctrl+Shift+N"))
        a.triggered.connect(self._new_window)
        m_tools.addAction(a)

        # ---------- 窗口（N1：菜单结构 文件/编辑/视图/查询/工具/窗口/帮助） ----------
        m_window = mb.addMenu("窗口(&W)")
        a = QAction("新建窗口", self)
        a.setShortcut(QKeySequence("Ctrl+Shift+N"))
        a.triggered.connect(self._new_window)
        m_window.addAction(a)
        a = QAction("关闭当前标签", self)
        a.setShortcut(QKeySequence("Ctrl+W"))
        a.triggered.connect(
            lambda: self._close_tab(self.tabs.currentIndex()))
        m_window.addAction(a)
        a = QAction("关闭窗口", self)
        a.triggered.connect(self.close)
        m_window.addAction(a)

        # ---------- 帮助 ----------
        m_help = mb.addMenu("帮助(&H)")

        a = QAction(i18n.t('action.check_updates'), self)
        a.triggered.connect(self._check_updates)
        m_help.addAction(a)

        m_help.addSeparator()

        a = QAction("关于", self)
        a.triggered.connect(self._show_about)
        m_help.addAction(a)

        self._decorate_menu_icons()

    def _decorate_menu_icons(self):
        """task-52：主要菜单动作也使用内置 SVG 图标（本地优先）。"""
        try:
            from PySide6.QtWidgets import QMenu
            from .toolbar import make_icon
            from .theme import icon_color
            mapping = [
                ('新建连接', 'new_connection'),
                ('设计表', 'design_table'),
                ('新建表', 'new_table'),
                ('新建查询', 'new_query'),
                ('执行', 'execute'),
                ('EXPLAIN', 'explain'),
                ('导入', 'import'),
                ('导出', 'export'),
                ('备份', 'backup'),
                ('AI SQL', 'ai'),
                ('设置', 'settings'),
                ('刷新对象', 'refresh'),
            ]
            color = icon_color()
            menus = [self.menuBar()] + [
                m for m in self.menuBar().findChildren(QMenu)]
            for menu in menus:
                for act in menu.actions():
                    if act.isSeparator():
                        continue
                    text = act.text()
                    for needle, local_name in mapping:
                        if needle in text:
                            act.setIcon(make_icon(
                                '', 'new', color=color,
                                local_name=local_name))
                            break
        except Exception:
            pass

    def _build_toolbar(self):
        """N1：Ribbon 分组大图标工具栏（保留现有动作 + 显式禁用态）。"""
        from .toolbar import RibbonActionSpec, RibbonBar
        specs = [
            RibbonActionSpec(
                'new_connection', i18n.t('action.new_connection'), '新建连接',
                self._new_connection, 'mdi.plug', 'connect', '连接',
                local_icon='new_connection'),
            RibbonActionSpec(
                'edit_connection', '编辑连接', '编辑连接',
                self._edit_current_connection, 'mdi.connection', 'edit',
                '连接', needs_connection=True, local_icon='edit_connection'),
            RibbonActionSpec(
                'refresh_tree', i18n.t('toolbar.refresh'), '刷新对象树',
                self._refresh_tree, 'mdi.refresh', 'refresh', '连接',
                local_icon='refresh'),
            RibbonActionSpec(
                'new_table', i18n.t('toolbar.new_table'), '新建表',
                self._new_table_designer, 'mdi.table-plus', 'table', '表',
                local_icon='new_table'),
            RibbonActionSpec(
                'edit_data', i18n.t('toolbar.edit_data'), '编辑数据',
                self._edit_current_table, 'mdi.table-edit', 'edit', '表',
                needs_connection=True, local_icon='edit_data'),
            RibbonActionSpec(
                'design_table', '设计表', '设计表',
                self._design_current_table, 'mdi.table-cog', 'design', '表',
                needs_connection=True, local_icon='design_table'),
            RibbonActionSpec(
                'import_data', i18n.t('toolbar.import'), '导入数据',
                self._import_current_table, 'mdi.import', 'import', '表',
                needs_connection=True, local_icon='import'),
            RibbonActionSpec(
                'export_data', i18n.t('toolbar.export'), '导出结果',
                self._export_current_result, 'mdi.export', 'export', '表',
                needs_connection=True, local_icon='export'),
            RibbonActionSpec(
                'backup', '备份/快照', '备份',
                self._open_snapshot, 'mdi.backup-restore', 'backup', '表',
                needs_connection=True, local_icon='backup'),
            RibbonActionSpec(
                'toggle_panels', '显示/隐藏工具面板', '面板',
                self._toggle_right_dock, 'mdi.view-dashboard', 'view', '视图',
                local_icon='panel'),
            RibbonActionSpec(
                'new_query', i18n.t('toolbar.query'), '新建查询',
                lambda: self._add_new_query_tab(True), 'mdi.file-document-plus',
                'query', '查询', local_icon='new_query'),
            RibbonActionSpec(
                'execute', i18n.t('toolbar.execute'), '执行',
                self._exec_current, 'mdi.play', 'run', '查询',
                needs_connection=True, local_icon='execute'),
            RibbonActionSpec(
                'explain', i18n.t('toolbar.explain'), 'EXPLAIN',
                self._exec_explain, 'mdi.chart-timeline', 'explain', '查询',
                needs_connection=True, local_icon='explain'),
            RibbonActionSpec(
                'ai_assistant', 'AI SQL 助手', 'AI 助手',
                self._open_ai_sql_dialog, 'mdi.robot', 'ai', '工具',
                local_icon='ai'),
            RibbonActionSpec(
                'settings', '设置', '设置',
                self._open_settings, 'mdi.cog', 'tool', '工具',
                local_icon='settings'),
        ]
        tb = RibbonBar(specs, self)
        self.addToolBar(tb)
        self._main_toolbar = tb
        tb.setAccessibleName(i18n.t('a11y.toolbar'))
        self.ribbon = tb
        if getattr(self, 'env_bar', None) is not None:
            tb.addWidget(self.env_bar)
        self._update_ribbon_state()

    def _update_ribbon_state(self):
        ribbon = getattr(self, 'ribbon', None)
        if ribbon is None:
            return
        try:
            connected = self._get_current_conn() is not None
        except Exception:
            connected = False
        ribbon.update_availability(connected)

    def _build_statusbar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        # 用只读 QLineEdit 而不是 QLabel：完整错误文本可以在状态栏里
        # 直接选中复制（以前是 `any_error[:80]` 静默截断，后面的诊断信息
        # 用户永远看不到）。用 QLineEdit 仍然保持单行高度与横向滚动。
        self.status_label = QLineEdit(i18n.t('status.ready'))
        self.status_label.setReadOnly(True)
        self.status_label.setFrame(False)
        self.status_label.setStyleSheet(
            "QLineEdit { background: transparent; border: none; color: #cccccc; }")
        sb.addWidget(self.status_label)
        self.conn_label = QLabel(i18n.t('status.disconnected'))
        sb.addPermanentWidget(self.conn_label)

    def _set_status(self, text: str, error: bool = False, tooltip: str = None):
        """设置状态栏文本；**不截断**，全文同时放进 tooltip 方便悬停查看。"""
        text = '' if text is None else str(text)
        self.status_label.setText(text)
        self.status_label.setCursorPosition(0)   # 从头显示，不看到尾巴
        self.status_label.setToolTip(text if tooltip is None else tooltip)
        self.status_label.setStyleSheet(
            "QLineEdit { background: transparent; border: none; color: %s; }"
            % ('#f48771' if error else '#cccccc'))

    # ==================== 标签页 ====================

    def _add_new_query_tab(self, auto_switch=True):
        editor = SQLEditor()
        result = ResultTable()

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(editor)
        splitter.addWidget(result)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([220, 460])
        # task-88：分栏比例可拖动并持久化
        try:
            from .layout_store import (connect_splitter_persistence,
                                       restore_splitter_ratios)
            restore_splitter_ratios('query_editor_result', splitter,
                                    default_sizes=[220, 460])
            connect_splitter_persistence('query_editor_result', splitter)
        except Exception:
            pass

        idx = self.tabs.addTab(splitter, i18n.t('tab.query'))
        # P2 i18n / 可访问性：编辑器空态提示 + accessibleName
        try:
            editor.setPlaceholderText(i18n.t('editor.empty_hint'))
            editor.setAccessibleName(i18n.t('a11y.sql_editor'))
            editor.setToolTip(i18n.t('a11y.sql_editor'))
            result.setAccessibleName(i18n.t('a11y.result_table'))
            result.setToolTip(i18n.t('a11y.result_table'))
            if hasattr(result, 'show_message'):
                result.show_message(i18n.t('result.empty'))
        except Exception:
            pass
        # P1-1：真实 UI 执行走后台 worker（不冻结主线程）
        editor.execute_requested.connect(
            lambda sql, e=editor, r=result: self._execute_sql_async(sql, e, r))
        editor.execute_selection_requested.connect(
            lambda sql, e=editor, r=result: self._execute_sql_async(sql, e, r))
        # G1：执行光标所在语句（分号切分）
        editor.execute_statement_requested.connect(
            lambda sql, e=editor, r=result: self._execute_sql_async(sql, e, r))
        # P1-2：结果被 max_result_rows 截断时，结果区「加载更多」继续取下一批
        result.load_more_requested.connect(
            lambda r=result: self._on_load_more(r))
        # P1-3：schema 自动补全（只读 MainWindow 的后台刷新缓存，不查库）
        try:
            editor.set_schema_provider(self._schema_snapshot)
            editor.set_auto_complete(
                bool(getattr(self.settings, 'editor_auto_complete', True)))
        except Exception:
            pass
        # G3：快捷键自定义（持久化在 settings.shortcuts）
        try:
            from .shortcuts import apply_to_editor
            apply_to_editor(editor, getattr(self.settings, 'shortcuts', {}))
        except Exception:
            pass

        if auto_switch:
            self.tabs.setCurrentIndex(idx)
        return idx

    def _open_data_editor(self, table_name):
        conn = self._get_current_conn()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择一个连接")
            return
        try:
            cols_meta = conn.get_columns(table_name)
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return

        editor = DataEditor(conn, table_name, cols_meta)
        idx = self.tabs.addTab(editor, f"📋 {table_name}")
        self.tabs.setTabToolTip(idx, f"数据编辑器: {table_name}")
        self.tabs.setCurrentIndex(idx)

    def _close_tab(self, idx):
        if self.tabs.count() <= 1:
            return
        self.tabs.removeTab(idx)

    def _on_tab_changed(self, idx):
        conn = self._get_current_conn()
        if conn and conn.is_connected():
            info = conn.config
            self.conn_label.setText(
                f"已连接: {info.name} @ {info.host}:{info.port}")
        else:
            self.conn_label.setText("未连接")
        # N1：Ribbon 依赖连接的动作显式禁用/启用
        self._update_ribbon_state()

    def _current_widgets(self):
        w = self.tabs.currentWidget()
        if not isinstance(w, QSplitter):
            return None, None
        return w.widget(0), w.widget(1)

    def _current_table(self):
        w = self.tabs.currentWidget()
        if isinstance(w, DataEditor):
            return w.table_name
        return None

    # ==================== 连接 ====================

    def _get_conn_by_name(self, name):
        """供 Scheduler 等使用"""
        return self.object_tree.connections.get(name)

    def _get_current_conn(self):
        return self.object_tree.get_current_connection()

    # ---------- P1-3：schema 缓存（自动补全） ----------

    def _schema_snapshot(self):
        """给编辑器补全用的只读快照（内存读，绝不查库、不阻塞 UI）。"""
        with self._schema_lock:
            tables = list(self._schema_cache.get('tables') or [])
            columns = {k: list(v) for k, v in
                       (self._schema_cache.get('columns') or {}).items()}
        return tables, columns

    def _refresh_schema_cache_async(self, conn=None, max_tables: int = 200):
        """后台线程刷新库/表/列缓存（供自动补全）；失败静默保留旧缓存。"""
        conn = conn or self._get_current_conn()
        if conn is None or not conn.is_connected():
            return False
        if self._schema_thread is not None and self._schema_thread.is_alive():
            return False

        def _worker():
            tables, columns = [], {}
            try:
                tables = list(conn.get_tables() or [])[:max_tables]
                for table in tables:
                    try:
                        cols = conn.get_columns(table) or []
                        columns[table] = [str(c.get('name', '')) for c in cols]
                    except Exception:
                        columns[table] = []
            except Exception:
                return
            with self._schema_lock:
                self._schema_cache = {'tables': tables, 'columns': columns,
                                      'ts': time.time()}

        self._schema_thread = threading.Thread(
            target=_worker, name='studio-schema-cache', daemon=True)
        self._schema_thread.start()
        return True

    def _open_sql_in_new_tab(self, sql: str):
        """对象树「生成 SELECT ...」：新建查询页并填入 SQL（不自动执行）。"""
        if not sql:
            return
        self._add_new_query_tab(auto_switch=True)
        editor, _ = self._current_widgets()
        if editor is not None:
            editor.set_sql(sql)
            editor.setFocus()
            self._set_status(f'已在新建查询页填入 SQL（{len(sql)} 字符）')

    def _schema_provider(self):
        """供 AI Chat 使用"""
        conn = self._get_current_conn()
        if conn is None:
            return [], {}
        try:
            tables = conn.get_tables()
            cols = {}
            for t in tables[:30]:
                try:
                    col_infos = conn.get_columns(t)
                    cols[t] = [c['name'] for c in col_infos]
                except Exception:
                    cols[t] = []
            return tables, cols
        except Exception:
            return [], {}

    def _suggest_connection_port(self):
        """task-87：新建连接默认端口 = 上次连接端口；否则 8898（LightDB）。"""
        try:
            last = getattr(self.config, 'last_connection', '') or ''
            if last:
                info = self.config.find(last)
                if info is not None and 1 <= int(info.port) <= 65535:
                    return int(info.port)
            if getattr(self.config, 'connections', None):
                port = int(self.config.connections[-1].port)
                if 1 <= port <= 65535:
                    return port
        except Exception:
            pass
        return 8898

    def _new_connection(self):
        try:
            dlg = ConnectionDialog(
                self, default_port=self._suggest_connection_port())
        except TypeError:
            dlg = ConnectionDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        info = dlg.get_connection()
        auto_connect = True
        try:
            auto_connect = bool(dlg.auto_connect_requested())
        except Exception:
            auto_connect = True
        if not auto_connect:
            # 只保存配置，不拨号（用户可稍后从对象树/连接菜单连接）
            self.config.add_connection(info)
            self.config.last_connection = info.name
            self.config.save()
            self.status_label.setText(
                f"已保存连接: {info.name}（未自动连接）")
            return
        try:
            conn = DBConnection(info)
            conn.connect()
        except Exception as e:
            self._report_connection_failure(info, e)
            return

        self.config.add_connection(info)
        self.config.last_connection = info.name
        self.config.save()

        self.object_tree.add_connection(info, conn)
        self.object_tree.select_connection(info.name)
        self.status_label.setText(f"已连接: {info.name}")
        self._on_tab_changed(self.tabs.currentIndex())
        # P1-3：连接建立后后台刷新 schema 缓存（首次补全前就有候选）
        self._refresh_schema_cache_async(conn)

    # ==================== G2：连接导入/导出 + JSON/SQL 导出 ====================

    def _export_connections(self):
        name = self._current_connection_name()
        conns = [c for c in self.config.connections if c.name == name] \
            if name else list(self.config.connections)
        if not conns:
            QMessageBox.information(self, '提示', '没有可导出的连接')
            return None
        path, _ = QFileDialog.getSaveFileName(
            self, '导出连接', 'lightdb_connections.json',
            'JSON 文件 (*.json)')
        if not path:
            return None
        include = QMessageBox.question(
            self, '导出选项',
            '是否包含密码？\n密码以 DPAPI 加密串写入，文件仍属敏感信息。\n'
            '选择「否」则导入后需重新填写密码。',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        result = export_connections_json(
            conns, path, include_passwords=(include == QMessageBox.Yes))
        if result.get('ok'):
            self.status_label.setText(
                f"已导出 {result['count']} 个连接"
                f"（含密码={result['passwords_included']}）")
        else:
            QMessageBox.warning(self, '导出连接失败', result.get('error', ''))
        return result

    def _import_connections(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '导入连接', '', 'JSON 文件 (*.json);;所有文件 (*.*)')
        if not path:
            return None
        result = import_connections_json(
            self.config, path, conflict='ask',
            confirm=lambda question: QMessageBox.question(
                self, '连接冲突', question,
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes)
        if not result.get('ok'):
            if not result.get('cancelled'):
                QMessageBox.warning(self, '导入连接失败',
                                    result.get('error', ''))
            return result
        self.status_label.setText(
            f"已导入 {result.get('imported', 0)} 个连接配置"
            f"（需重新连接；自动重命名 {len(result.get('renamed') or [])}）")
        return result

    def _export_current_result_as(self, fmt):
        result = self._current_widgets()[1]
        rows = [list(r) for r in getattr(result, '_orig_rows', [])] \
            if result is not None else []
        if not rows:
            QMessageBox.information(self, '提示', '当前没有可导出的结果')
            return None
        columns = list(getattr(result, '_all_columns', []) or [])
        ext = {'json': '.json', 'sql_insert': '.sql'}[fmt]
        path, _ = QFileDialog.getSaveFileName(
            self, '导出查询结果', 'query_result' + ext,
            f"{fmt.upper()} (*{ext})")
        if not path:
            return None
        if not path.lower().endswith(ext):
            path += ext
        try:
            count = write_export_chunked(
                path, columns, rows, fmt=fmt, table_name='query_result')
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.critical(self, '导出失败', str(exc))
            return {'ok': False, 'error': str(exc)}
        QMessageBox.information(self, '完成', f'已导出 {count} 行到:\n{path}')
        return {'ok': True, 'rows': count, 'path': path}

    # ==================== G1：连接编辑/复制/删除 ====================

    def _current_connection_name(self):
        conn = self.object_tree.get_current_connection()
        return getattr(getattr(conn, 'config', None), 'name', '') if conn else ''

    def _edit_current_connection(self):
        name = self._current_connection_name()
        if not name:
            QMessageBox.information(self, '提示', '请先在左侧选择一个连接')
            return None
        result = edit_connection(self, self.config, name)
        if not result.get('ok'):
            if not result.get('cancelled'):
                QMessageBox.warning(self, '编辑连接失败', result.get('error', ''))
            return result
        info = result['connection']
        # 连接参数变化：断开旧连接并按新配置重连（失败显式，不假装成功）
        try:
            old = self.object_tree.connections.get(name)
            if old is not None:
                old.close()
            self.object_tree.remove_connection(name)
            conn = DBConnection(info)
            conn.connect()
            self.object_tree.add_connection(info, conn)
            self.object_tree.select_connection(info.name)
            self.status_label.setText(f"已连接: {info.name}")
            self._refresh_schema_cache_async(conn)
        except Exception as exc:                      # noqa: BLE001
            self._report_connection_failure(info, exc)
            return {'ok': False, 'error': str(exc), 'connection': info}
        return result

    def _duplicate_current_connection(self):
        name = self._current_connection_name()
        if not name:
            QMessageBox.information(self, '提示', '请先在左侧选择一个连接')
            return None
        new_name, ok = QInputDialog.getText(
            self, '复制连接', '新连接名称:', text=name + ' - 副本')
        if not ok:
            return None
        result = duplicate_connection(self.config, name, new_name.strip())
        if not result.get('ok'):
            QMessageBox.warning(self, '复制连接失败', result.get('error', ''))
            return result
        self.status_label.setText(
            f"已复制连接配置: {result['name']}（未自动连接）")
        return result

    def _delete_current_connection(self):
        name = self._current_connection_name()
        if not name:
            QMessageBox.information(self, '提示', '请先在左侧选择一个连接')
            return None
        result = delete_connection(self.config, name, ask=True,
                                   confirmer=lambda t, m: QMessageBox.question(
                                       self, t, m))
        if result.get('cancelled'):
            return result
        if not result.get('ok'):
            QMessageBox.warning(self, '删除连接失败', result.get('error', ''))
            return result
        self.object_tree.remove_connection(name)
        self.status_label.setText(f"已删除连接: {name}")
        return result

    # ==================== G1：对象树设计/导入/导出接线 ====================

    def _on_tree_design_table(self, conn_name, db, table):
        conn = self.object_tree.connections.get(conn_name) or \
            self._get_current_conn()
        if conn is None:
            QMessageBox.warning(self, '提示', '请先选择一个连接')
            return None
        dlg = TableDesigner(conn, self, table_name=table)
        if dlg.exec() == TableDesigner.Accepted:
            self.object_tree.refresh_all()
        return dlg

    def _on_tree_import_data(self, conn_name, db, table):
        conn = self.object_tree.connections.get(conn_name) or \
            self._get_current_conn()
        if conn is None:
            QMessageBox.warning(self, '提示', '请先选择一个连接')
            return None
        self._import_csv(table)
        return True

    def _on_tree_export_data(self, conn_name, db, table):
        conn = self.object_tree.connections.get(conn_name) or \
            self._get_current_conn()
        if conn is None:
            QMessageBox.warning(self, '提示', '请先选择一个连接')
            return None
        self._export_table(table)
        return True

    def _report_connection_failure(self, info, exc, title='连接失败'):
        """连接失败 → 弹「可行动」的框（原始报文一字不删）+ 状态栏一行结论。

        文本由 `core/db_errors.explain_connection_error` 生成（错误码 →
        结论 → 可照抄的启动命令 → 原始报文），对话框里文本可选中复制。
        """
        text = show_connection_error(self, exc, host=getattr(info, 'host', None),
                                     port=getattr(info, 'port', None),
                                     title=title)
        self._set_status(connection_error_summary(
            exc, host=getattr(info, 'host', None),
            port=getattr(info, 'port', None)), error=True)
        return text

    def _auto_restore(self):
        """启动时恢复保存的连接（首帧之后由 `do_startup_restore()` 调用）。

        触发条件（读代码 + 实测确认的语义，schema 见 `config.py`）：

          * `settings.json` 的 `auto_connect`（设置里「启动时自动连接」，默认
            True）为 False 时**完全不拨号** —— 这一项此前根本没被读，
            关掉也没用；
          * `connections.json` 顶层 `last_connection` 就是「上次连接」标记
            （`_new_connection()` 成功后写入），它被**最先**连接、并且连上后
            被**选中**（`select_connection`），所以 `_get_current_conn()`
            立刻可用（此前 Qt 不会自动设 currentItem，连上了也是「未连接」）；
          * 其余保存的连接照旧连接并挂进对象树；
          * 连不上的：状态栏写一行结论；`last_connection` 那条再弹一个
            **非阻塞**对话框（含原始报文 + 可照抄的启动命令），不挡首屏。
        """
        if not getattr(self.settings, 'auto_connect', True):
            # 只在状态栏还停在初始「就绪」时写这条提示：它是**纯信息**，
            # 绝不能盖掉这次启动里更重要的东西（比如首屏之后用户已经跑出
            # 来的一条错误信息 —— 启动期恢复是 `QTimer.singleShot(0)`，
            # 完全可能排在用户的第一次查询之后）。
            if self.status_label.text().strip() in ('', '就绪'):
                self._set_status('已跳过自动连接（「启动时自动连接」已关闭）')
            return

        infos = list(self.config.connections)
        if not infos:
            return
        last = getattr(self.config, 'last_connection', '') or ''
        # 「上次连接」排最前：先把它连上，其余保持文件里的顺序（sorted 稳定）
        infos.sort(key=lambda c: 0 if c.name == last else 1)

        last_text = ''
        last_failed = False
        first_failure = ''
        ok_count = 0
        for info in infos:
            try:
                conn = DBConnection(info)
                conn.connect()
            except Exception as e:
                text = '自动连接失败: %s — %s' % (
                    info.name,
                    connection_error_summary(e, info.host, info.port))
                if not first_failure:
                    first_failure = text
                if info.name == last:
                    last_text = text
                    last_failed = True
                    show_connection_error_nonblocking(
                        self, e, host=info.host, port=info.port,
                        title='自动连接失败: %s' % info.name)
                continue
            ok_count += 1
            self.object_tree.add_connection(info, conn)
            if info.name == last:
                last_text = '自动连接: %s' % info.name
                last_failed = False
                self.object_tree.select_connection(info.name)
            self._on_tab_changed(self.tabs.currentIndex())

        if last_text:
            self._set_status(last_text, error=last_failed)
        elif first_failure:
            self._set_status(first_failure, error=True)
        elif not ok_count:
            self._set_status('自动连接: 没有连上的连接')
        # P1-3：只要有连接成功，后台刷新一次 schema 缓存（自动补全用）
        if ok_count:
            self._refresh_schema_cache_async()

    def _refresh_tree(self):
        errors = self.object_tree.refresh_all()
        if errors:
            self.status_label.setText('刷新失败: ' + '; '.join(errors))
        else:
            self.status_label.setText(
                f'已刷新对象树（{len(self.object_tree.connections)} 个连接）')

    # ==================== 查询执行 ====================

    def _on_query_requested(self, sql: str):
        editor, result = self._current_widgets()
        if editor is None:
            self._add_new_query_tab(auto_switch=False)
            editor, result = self._current_widgets()
        editor.set_sql(sql)
        self._execute_sql_async(sql, editor, result)

    def _insert_history_sql(self, sql):
        self._insert_sql_to_editor(sql)

    def _insert_sql_to_editor(self, sql):
        editor, _ = self._current_widgets()
        if editor is None:
            self._add_new_query_tab(auto_switch=False)
            editor, _ = self._current_widgets()
        if editor:
            editor.set_sql(sql)

    def _on_catalog_selected(self, db, table):
        self._open_data_editor(table)

    def _on_session_selected(self, session):
        if session and session.sql:
            self._insert_sql_to_editor(session.sql)

    def _exec_current(self):
        editor, result = self._current_widgets()
        if editor is None:
            return
        sql = editor.toPlainText().strip().rstrip(';')
        if sql:
            self._execute_sql_async(sql, editor, result)

    def _exec_explain(self):
        editor, result = self._current_widgets()
        if editor is None:
            return
        sql = editor.toPlainText().strip().rstrip(';')
        if not sql:
            return
        if sql.upper().startswith('EXPLAIN'):
            self._execute_sql_async(sql, editor, result)
        else:
            self._execute_sql_async("EXPLAIN " + sql, editor, result)

    def _execute_sql(self, sql: str, editor: SQLEditor, result: ResultTable):
        conn = self._get_current_conn()
        if conn is None or not conn.is_connected():
            QMessageBox.warning(self, "提示", "请先选择一个连接")
            return

        self.status_label.setText("执行中...")
        statements = self._split_statements(sql)
        if not statements:
            return

        t0 = time.time()
        last_cols, last_rows = [], []
        last_msg = ""
        any_error = None
        err_code = None
        err_state = None
        failed_stmt = None

        # 记录血缘
        try:
            self.lineage_store.record_sql(sql, conn.config.name)
        except Exception:
            pass

        for stmt in statements:
            stmt = stmt.strip()
            if not stmt:
                continue
            up = stmt.upper()
            try:
                if up.startswith(('SELECT', 'SHOW', 'DESCRIBE', 'DESC',
                                  'EXPLAIN', 'WITH')):
                    cols, rows = conn.query(stmt)
                    last_cols, last_rows = cols, rows
                    last_msg = f"查询返回 {len(rows)} 行"
                else:
                    rowcount, err = conn.execute(stmt)
                    if err:
                        any_error = err
                        failed_stmt = stmt
                        break
                    last_cols, last_rows = [], []
                    last_msg = f"影响 {rowcount} 行"
                    if up.startswith(('CREATE', 'DROP', 'ALTER',
                                       'TRUNCATE')):
                        self.object_tree.refresh_all()
            except Exception as e:
                any_error = str(e)
                err_code = error_code(e)
                err_state = error_sqlstate(e)
                failed_stmt = stmt
                break

        elapsed = (time.time() - t0) * 1000

        if any_error:
            # 错误码 / SQLSTATE / 完整报文一起交给结果区；状态栏不截断。
            # `any_error` 已经是 format_db_error 的完整文本（含 SQL 行），
            # 这里再解析出 errno/sqlstate 用于标题行，避免重复展示。
            if err_code is None:
                _c, _s = parse_error_fields(any_error)
                err_code = _c if _c is not None else err_code
                if not err_state:
                    err_state = _s
            result.show_error(any_error, errno=err_code, sqlstate=err_state)
            # P1-3：编辑器错误位置高亮（定位不到就不高亮，不伪造位置）
            try:
                if editor is not None and hasattr(editor,
                                                  'highlight_error_token'):
                    editor.highlight_error_token(err_code, any_error, sql=sql)
            except Exception:
                pass
            self._set_status(f"错误: {any_error}", error=True)
            self.history.add(sql, conn.config.name, False, elapsed)
        else:
            if last_cols:
                result.show_result(last_cols, last_rows, elapsed)
            else:
                result.show_message(last_msg)
            self._set_status(f"完成 ({elapsed:.1f} ms)")
            self.history.add(sql, conn.config.name, True, elapsed)
            # P1-3：成功执行清除上一次的错误高亮
            try:
                if editor is not None and hasattr(editor,
                                                  'clear_error_highlight'):
                    editor.clear_error_highlight()
            except Exception:
                pass
            # DDL 后刷新对象树 + schema 缓存（自动补全候选随之更新）
            try:
                if any(s.strip().upper().startswith(
                        ('CREATE', 'DROP', 'ALTER', 'TRUNCATE'))
                       for s in statements):
                    self.object_tree.refresh_all()
                    self._refresh_schema_cache_async()
            except Exception:
                pass

        self.history_panel.refresh()

    # ==================== 后台查询（P1-1 响应性） ====================
    # 说明：`_execute_sql()`（同步）保留给既有测试/嵌入调用方；所有真实 UI
    # 入口（F5/菜单执行、编辑器 Ctrl+Return、对象树双击、EXPLAIN）统一走
    # `_execute_sql_async()`，DB 调用在 QThreadPool worker 线程，UI 不冻结。

    def _effective_max_rows(self) -> int:
        """settings.max_result_rows（0/非法 → 100000 默认上限）。"""
        try:
            value = int(getattr(self.settings, 'max_result_rows', 0) or 0)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else 100000

    def _query_chunk_size(self) -> int:
        """「加载更多」每批追加的行数：取当前单次上限（50~1000）。"""
        return max(50, min(self._effective_max_rows(), 1000))

    def _query_action_widgets(self):
        """执行类 QAction（执行/EXPLAIN）——执行期间禁用，完成后恢复。"""
        out = []
        for action in self.findChildren(QAction):
            text = action.text().replace('&', '')
            if text in ('执行', 'EXPLAIN') or text.endswith('执行'):
                out.append(action)
        return out

    def _set_busy_ui(self, busy: bool):
        self._query_busy = bool(busy)
        if self._cancel_action is not None:
            self._cancel_action.setEnabled(bool(busy))
        for action in self._query_action_widgets():
            action.setEnabled(not busy)

    def _start_worker(self, worker, ctx):
        """登记 worker/上下文、接信号、启动线程池任务（公共路径）。"""
        self._active_worker = worker
        self._active_signals = worker.signals   # 保活：排队信号投递前不能被回收
        self._active_ctx = ctx
        self._abort_reason = ''
        self._set_busy_ui(True)
        worker.signals.result.connect(self._on_async_result)
        worker.signals.failed.connect(self._on_async_failed)
        worker.signals.cancelled.connect(self._on_async_cancelled)
        worker.signals.finished.connect(self._on_async_finished)
        self._query_pool.start(worker)

    def _execute_sql_async(self, sql: str, editor: SQLEditor,
                           result: ResultTable, load_more: bool = False):
        """真实 UI 查询入口：后台线程执行，主线程只更新 UI。"""
        if result is None:
            return
        statements = self._split_statements(sql)
        if not statements:
            return
        conn = self._get_current_conn()
        if conn is None or not conn.is_connected():
            QMessageBox.warning(self, "提示", "请先选择一个连接")
            return
        if self._query_busy:
            QMessageBox.information(self, "提示",
                                    "已有查询正在执行，请等待完成或先取消。")
            return

        max_rows = self._effective_max_rows()
        worker = QueryWorker(conn, statements, max_rows=max_rows,
                             chunk_size=self._query_chunk_size())
        ctx = {
            'editor': editor, 'result': result, 'sql': sql,
            'statements': statements, 'max_rows': max_rows,
            'rows_loaded': 0, 'load_more': bool(load_more),
            'conn_name': getattr(conn.config, 'name', ''),
            'started': time.time(),
        }
        if load_more:
            result.set_loading_more(True)
        else:
            self._set_status('执行中…（可点 查询 → 取消当前查询 取消）')
        try:
            timeout = int(getattr(self.settings, 'query_timeout', 0) or 0)
        except (TypeError, ValueError):
            timeout = 0
        if timeout > 0 and not load_more:
            ctx['timeout'] = timeout
            self._query_timeout_timer.start(timeout * 1000)
        self._start_worker(worker, ctx)

    # ---------- worker 信号回调（Qt 自动投递回主线程） ----------

    def _on_async_result(self, payload):
        ctx = self._active_ctx
        if ctx is None:
            return
        result = ctx['result']
        elapsed = float(payload.get('elapsed_ms') or 0.0)
        if ctx.get('load_more'):
            rows = list(payload.get('rows') or [])
            old = int(ctx.get('rows_loaded') or 0)
            new_rows = rows[old:] if len(rows) > old else []
            result.append_rows(new_rows,
                               truncated=bool(payload.get('truncated')))
            ctx['rows_loaded'] = result.rows_loaded
            if payload.get('truncated'):
                self._set_status(f"已加载 {result.rows_loaded} 行"
                                 f"（还有更多，可继续『加载更多』）")
            else:
                self._set_status(f"已加载全部 {result.rows_loaded} 行")
            return

        cols = list(payload.get('columns') or [])
        rows = list(payload.get('rows') or [])
        truncated = bool(payload.get('truncated'))
        ctx['rows_loaded'] = len(rows)
        if cols:
            result.show_result(
                cols, rows, elapsed, truncated=truncated,
                limit=self._effective_max_rows() if truncated else None)
        else:
            result.show_message(payload.get('message') or '执行完成')
        # P1-3：成功执行清除编辑器错误高亮
        try:
            editor = ctx.get('editor')
            if editor is not None and hasattr(editor,
                                              'clear_error_highlight'):
                editor.clear_error_highlight()
        except Exception:
            pass
        status = f"完成 ({elapsed:.1f} ms) · {len(rows)} 行"
        message = payload.get('message') or ''
        if message and not cols:
            status += f" - {message}"
        if truncated:
            status += (f"；结果已截断：已取 {len(rows)} 行 / 限制 "
                       f"{self._effective_max_rows()} 行（可点『加载更多』继续）")
        self._set_status(status)
        try:
            self.history.add(ctx['sql'], ctx['conn_name'], True, elapsed)
            self.history_panel.refresh()
        except Exception:
            pass
        # DDL 后刷新对象树 + schema 缓存（自动补全候选随之更新）
        try:
            if any(s.strip().upper().startswith(
                    ('CREATE', 'DROP', 'ALTER', 'TRUNCATE'))
                   for s in ctx['statements']):
                self.object_tree.refresh_all()
                self._refresh_schema_cache_async()
        except Exception:
            pass
        # 「加载更多」上下文：仅单语句 SELECT 且有截断时提供
        try:
            if truncated and len(ctx['statements']) == 1:
                self._load_more_ctx[id(result)] = {
                    'sql': ctx['statements'][0],
                    'max_rows': ctx['max_rows'],
                    'conn_name': ctx['conn_name'],
                }
            else:
                self._load_more_ctx.pop(id(result), None)
        except Exception:
            pass

    def _on_async_failed(self, exc):
        ctx = self._active_ctx
        if ctx is None:
            return
        text = str(exc) or repr(exc)
        errno = error_code(exc)
        sqlstate = error_sqlstate(exc)
        if errno is None:
            parsed_code, parsed_state = parse_error_fields(text)
            errno = parsed_code
            if not sqlstate:
                sqlstate = parsed_state
        if ctx.get('load_more'):
            # 加载更多失败：保留已加载的行，只提示错误
            try:
                ctx['result'].set_loading_more(False)
                ctx['result'].show_load_more(True)
            except Exception:
                pass
            self._set_status(f"加载更多失败: {text}", error=True)
            return
        try:
            ctx['result'].show_error(text, errno=errno, sqlstate=sqlstate)
        except Exception:
            pass
        # P1-3：错误位置高亮（token/行号定位不到就不高亮）
        try:
            editor = ctx.get('editor')
            if editor is not None and hasattr(editor,
                                              'highlight_error_token'):
                editor.highlight_error_token(
                    errno, text,
                    sql=';\n'.join(ctx.get('statements') or []))
        except Exception:
            pass
        self._set_status(f"错误: {text}", error=True)
        try:
            elapsed = (time.time() - ctx.get('started', time.time())) * 1000
            self.history.add(ctx['sql'], ctx['conn_name'], False, elapsed)
            self.history_panel.refresh()
        except Exception:
            pass

    def _on_async_cancelled(self, reason):
        ctx = self._active_ctx
        if ctx is None:
            return
        reason = str(reason or '已取消')
        result = ctx['result']
        try:
            if ctx.get('load_more'):
                result.set_loading_more(False)
            else:
                result.show_error(
                    f"查询已取消：{reason}\n"
                    f"（本引擎取消 = KILL 连接；连接会尝试自动重连，"
                    f"服务器端语句可能仍在收尾）")
        except Exception:
            pass
        # 取消：清除错误高亮（本次执行没有可定位的 SQL 错误位置）
        try:
            editor = ctx.get('editor')
            if editor is not None and hasattr(editor, 'clear_error_highlight'):
                editor.clear_error_highlight()
        except Exception:
            pass
        self._set_status(f"已取消（{reason}）", error=True)

    def _on_async_finished(self):
        ctx = self._active_ctx
        if not self._query_busy and ctx is None:
            return
        self._query_timeout_timer.stop()
        if self._abort_reason:
            self._reconnect_after_abort()
        elif ctx is not None and ctx.get('load_more'):
            try:
                ctx['result'].set_loading_more(False)
            except Exception:
                pass
        self._active_worker = None
        self._active_signals = None
        self._active_ctx = None
        self._abort_reason = ''
        self._set_busy_ui(False)

    def _reconnect_after_abort(self):
        """取消/超时后连接可能已被 KILL：自动重连并在状态栏提示。"""
        conn = self._get_current_conn()
        if conn is None:
            return
        if conn.is_connected():
            return
        try:
            conn.connect()
            suffix = '；连接已自动重连'
        except Exception as exc:                      # noqa: BLE001
            suffix = f'；自动重连失败: {exc}'
        current = self.status_label.text()
        if '连接已自动重连' in current or '自动重连失败' in current:
            return
        self._set_status(current + suffix,
                         error='自动重连失败' in suffix)

    def _on_query_timeout(self):
        if not self._query_busy:
            return
        ctx = self._active_ctx or {}
        timeout = ctx.get('timeout') or 0
        reason = f"查询超时（>{timeout}s）" if timeout else '查询超时'
        self.cancel_running_query(reason)

    def cancel_running_query(self, reason: str = '用户取消') -> bool:
        """取消当前后台查询：取消标志 + 另一条连接 KILL 目标 thread_id。"""
        if not self._query_busy or self._active_worker is None:
            self._set_status('当前没有正在执行的查询')
            return False
        self._abort_reason = str(reason or '用户取消')
        worker = self._active_worker
        try:
            worker.cancel(self._abort_reason)
        except Exception:
            pass
        conn = self._get_current_conn()
        thread_id = getattr(worker, 'thread_id', None)
        if thread_id is None and conn is not None:
            try:
                thread_id = conn.thread_id()
            except Exception:
                thread_id = None
        ok, error = False, '没有可用连接'
        try:
            if conn is not None:
                ok, error = conn.kill_connection(thread_id)
        except Exception as exc:                      # noqa: BLE001
            ok, error = False, f'KILL 异常: {exc}'
        if ok:
            self._set_status(f"正在取消（{self._abort_reason}）："
                             f"已发送 KILL {thread_id}")
        else:
            self._set_status(
                f"取消失败（{self._abort_reason}）：{error}；将忽略该查询结果",
                error=True)
        # 兜底：worker 3s 内不结束（引擎语句无中断点）→ 强制释放 UI
        QTimer.singleShot(3000, self._force_abort_watchdog)
        return ok

    def _force_abort_watchdog(self):
        """取消后 worker 迟迟不结束：强制释放 UI（避免"执行中"永久卡住）。"""
        if not self._query_busy or not self._abort_reason:
            return
        ctx = self._active_ctx
        self._abort_reason = ''
        conn = self._get_current_conn()
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if ctx is not None:
            try:
                if not ctx.get('load_more'):
                    ctx['result'].show_error(
                        '查询已取消：worker 未在 3s 内结束（服务器端语句可能'
                        '仍在收尾），已关闭该连接，请刷新/重连后继续')
            except Exception:
                pass
        self._set_status('已取消（强制）：连接已关闭，请重连', error=True)
        self._active_worker = None
        self._active_signals = None
        self._active_ctx = None
        self._set_busy_ui(False)

    def _on_load_more(self, result):
        """结果区「加载更多」：重新执行同一 SELECT（更大上限）并追加新行。"""
        ctx = self._load_more_ctx.get(id(result))
        if ctx is None:
            self._set_status('没有可继续加载的结果')
            return
        if self._query_busy:
            self._set_status('已有查询正在执行，请稍后再加载更多')
            return
        conn = self._get_current_conn()
        if conn is None or not conn.is_connected():
            self._set_status('连接不可用，无法加载更多', error=True)
            return
        old_rows = int(result.rows_loaded or 0)
        chunk = self._query_chunk_size()
        new_max = old_rows + chunk
        statements = [ctx['sql']]
        worker = QueryWorker(conn, statements, max_rows=new_max,
                             chunk_size=chunk)
        self._active_ctx = {
            'editor': None, 'result': result, 'sql': ctx['sql'],
            'statements': statements, 'max_rows': new_max,
            'rows_loaded': old_rows, 'load_more': True,
            'conn_name': ctx.get('conn_name', ''),
            'started': time.time(),
        }
        result.set_loading_more(True)
        self._set_status(f"正在加载更多（当前 {old_rows} 行，"
                         f"目标 {new_max} 行）…")
        self._start_worker(worker, self._active_ctx)

    def _split_statements(self, sql: str):
        stmts, buf, in_str = [], '', None
        i, n = 0, len(sql)
        while i < n:
            ch = sql[i]
            if in_str:
                buf += ch
                if ch == '\\' and i + 1 < n:
                    buf += sql[i + 1]
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
                i += 1
            elif ch in ("'", '"'):
                in_str = ch
                buf += ch
                i += 1
            elif ch == '-' and i + 1 < n and sql[i + 1] == '-':
                j = sql.find('\n', i)
                i = j if j >= 0 else n
                buf += '\n'
            elif ch == '/' and i + 1 < n and sql[i + 1] == '*':
                j = sql.find('*/', i + 2)
                i = (j + 2) if j >= 0 else n
                buf += ' '
            elif ch == ';':
                if buf.strip():
                    stmts.append(buf.strip())
                buf = ''
                i += 1
            else:
                buf += ch
                i += 1
        if buf.strip():
            stmts.append(buf.strip())
        return stmts

    # ==================== N1：主题 / Ribbon 辅助 ====================

    def _set_theme_from_menu(self, theme_name):
        """运行时切换主题并持久化到 settings（浅/深/高对比）。"""
        try:
            from PySide6.QtWidgets import QApplication
            from .theme import apply_theme
            self.settings.theme = str(theme_name)
            try:
                self.settings.save()
            except Exception:
                pass
            apply_theme(QApplication.instance(), self.settings.theme)
            ribbon = getattr(self, 'ribbon', None)
            if ribbon is not None:
                ribbon.refresh_icons()
            self._update_ribbon_state()
            self.status_label.setText(f'主题已切换: {theme_name}')
            return True
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.warning(self, '主题切换失败', str(exc))
            return False

    def _design_current_table(self):
        """打开当前对象树选中表的设计器（AUTO_INCREMENT 回显/编辑）。"""
        item = self.object_tree.tree.currentItem()
        meta = {}
        table = ''
        while item is not None:
            data = item.data(0, Qt.UserRole)
            if data and data[0] == 'table':
                table = data[1]
                meta = item.data(0, Qt.UserRole + 1) or {}
                break
            item = item.parent()
        if not table:
            QMessageBox.information(self, '提示', '请先在左侧选择一个表')
            return None
        conn = self.object_tree.connections.get(meta.get('conn')) or \
            self._get_current_conn()
        if conn is None:
            QMessageBox.warning(self, '提示', '请先选择一个连接')
            return None
        dlg = TableDesigner(conn, self, table_name=table)
        if dlg.exec() == TableDesigner.Accepted:
            self.object_tree.refresh_all()
            self._update_ribbon_state()
        return dlg

    # ==================== 表设计器 ====================

    def _new_table_designer(self):
        conn = self._get_current_conn()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择一个连接")
            return
        dlg = TableDesigner(conn, self)
        if dlg.exec() == TableDesigner.Accepted:
            self.object_tree.refresh_all()

    def _edit_current_table(self):
        item = self.object_tree.tree.currentItem()
        if item is None:
            QMessageBox.information(self, "提示", "请先在左侧选择一个表")
            return
        data = item.data(0, Qt.UserRole)
        if not data or data[0] != 'table':
            QMessageBox.information(self, "提示", "请先在左侧选择一个表")
            return
        self._open_data_editor(data[1])

    # ==================== 导入导出 ====================

    def _import_csv(self, table_name):
        conn = self._get_current_conn()
        if conn is None:
            return
        try:
            cols = conn.get_columns(table_name)
            col_names = [c['name'] for c in cols]
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return

        dlg = ImportCSVDialog(conn, table_name, col_names, self)
        if dlg.exec() == ImportCSVDialog.Accepted:
            for i in range(self.tabs.count()):
                w = self.tabs.widget(i)
                if isinstance(w, DataEditor) and w.table_name == table_name:
                    w.reload()

    def _import_current_table(self):
        t = self._current_table()
        if t is None:
            QMessageBox.information(self, "提示", "请先打开一个表")
            return
        self._import_csv(t)

    def _export_table(self, table_name):
        conn = self._get_current_conn()
        if conn is None:
            return
        try:
            cols, rows = conn.query(
                f"SELECT * FROM `{table_name}` LIMIT 100000")
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return
        dlg = ExportCSVDialog(cols, rows,
                              default_name=f"{table_name}.csv", parent=self)
        dlg.exec()

    def _export_current_result(self):
        w = self.tabs.currentWidget()
        if isinstance(w, DataEditor):
            cols, rows = w.get_current_data()
            dlg = ExportCSVDialog(cols, rows,
                                  default_name=f"{w.table_name}.csv",
                                  parent=self)
            dlg.exec()
            return

        _, result = self._current_widgets()
        if result is None:
            QMessageBox.information(self, "提示", "当前没有可导出的结果")
            return
        try:
            cols = [result.table.horizontalHeaderItem(i).text()
                    for i in range(result.table.columnCount())]
            rows = []
            for r in range(result.table.rowCount()):
                row = []
                for c in range(result.table.columnCount()):
                    item = result.table.item(r, c)
                    row.append(item.text() if item else '')
                rows.append(row)
        except Exception:
            cols, rows = [], []

        if not cols:
            QMessageBox.information(self, "提示", "没有数据可导出")
            return
        dlg = ExportCSVDialog(cols, rows, default_name="result.csv",
                              parent=self)
        dlg.exec()

    # ==================== 历史 ====================

    def _toggle_right_dock(self):
        self.right_dock.setVisible(not self.right_dock.isVisible())

    # ==================== 文件操作 ====================

    def _open_sql_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 SQL 文件", "",
            "SQL 文件 (*.sql);;所有文件 (*.*)")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(path, 'r', encoding='gbk') as f:
                content = f.read()
        idx = self._add_new_query_tab(auto_switch=True)
        w = self.tabs.widget(idx)
        w.widget(0).set_sql(content)
        self.tabs.setTabText(idx, os.path.basename(path))
        self.status_label.setText(f"已打开: {path}")

    def _save_sql_file(self):
        editor, _ = self._current_widgets()
        if editor is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存 SQL 文件", "",
            "SQL 文件 (*.sql);;所有文件 (*.*)")
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as f:
            f.write(editor.toPlainText())
        self.status_label.setText(f"已保存: {path}")

    # ==================== 环境 ====================

    def _on_env_changed(self, env):
        if env is None:
            return
        if env.connection and env.connection in self.object_tree.connections:
            conn = self.object_tree.connections[env.connection]
            if env.database:
                try:
                    conn.switch_database(env.database)
                except Exception:
                    pass
        self.status_label.setText(f"当前环境: {env.icon} {env.label}")

    # ==================== 右键 Tab 切换 ====================

    def _show_right_tab(self, attr):
        """把右侧工具面板切到 `attr` 对应的标签（需要时才真正构造该面板）。"""
        if not hasattr(self, 'right_dock') or self.right_dock is None:
            return
        widget = self._ensure_right_tab(attr)
        self.right_dock.setVisible(True)
        self.right_tabs.setCurrentWidget(widget)

    def _show_right_tab_by_widget(self, widget):
        """兼容旧调用点：按控件反查右侧标签（面板没建过就返回，不隐式构造）。"""
        if not hasattr(self, 'right_dock') or self.right_dock is None:
            return
        self.right_dock.setVisible(True)
        for i in range(self.right_tabs.count()):
            if self.right_tabs.widget(i) is widget:
                self.right_tabs.setCurrentIndex(i)
                return

    # ==================== 各功能对话框 ====================

    def _open_lineage(self):
        editor, _ = self._current_widgets()
        table = ''
        if editor:
            m = re.search(r'\b(?:FROM|JOIN|UPDATE|INTO)\s+[`"]?(\w+)',
                          editor.toPlainText(), re.IGNORECASE)
            if m:
                table = m.group(1)
        dlg = LineageDialog(self.lineage_store, table, self)
        dlg.exec()

    def _open_review(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("SQL 审查")
        dlg.resize(960, 680)
        layout = QVBoxLayout(dlg)
        panel = ReviewPanel(
            self.review_workflow,
            conn_getter=self._get_current_conn)
        layout.addWidget(panel)
        dlg.exec()

    def _open_snapshot(self):
        dlg = SnapshotDialog(
            conn_getter=self._get_current_conn, parent=self)
        dlg.exec()

    def _open_production_panel(self):
        """task-83：复制状态 / metrics 摘要 / 二进制快照 CLI 面板。"""
        from .production_panel import ProductionPanel
        dlg = ProductionPanel(conn_getter=self._get_current_conn, parent=self)
        dlg.exec()
        return dlg

    def _tick_snapshot_schedule(self):
        """P2-5：主窗口定时器回调——到点才创建定时快照。

        - schedule 未启用 / 没有已连接连接 / manager 不支持 run_due → 直接返回；
        - 创建成功只更新状态栏（不弹窗打断用户）；失败显式打印 stderr，
          不静默谎报"已创建"。
        """
        mgr = getattr(self, 'snapshot_mgr', None)
        run_due = getattr(mgr, 'run_due', None)
        if not callable(run_due):
            return
        try:
            status = (mgr.schedule_status() if hasattr(mgr, 'schedule_status')
                      else {}) or {}
            if not (status.get('config') or {}).get('enabled'):
                return
            conn = self._get_current_conn()
            if conn is None or not conn.is_connected():
                return
            report = run_due(conn)
            if report.get('created'):
                snap = report.get('snapshot')
                name = getattr(snap, 'name', None) or report.get('file') or '?'
                self._set_status(f'定时快照已创建: {name}')
            elif report.get('error'):
                print(f"定时快照失败: {report['error']}", file=sys.stderr)
        except Exception as exc:                      # noqa: BLE001
            # 定时后台动作失败不得打断用户、也不得静默：写 stderr 供日志排查
            print(f"定时快照检查失败: {exc}", file=sys.stderr)

    def _open_migration(self):
        dlg = MigrationWizard(
            target_conn_getter=self._get_current_conn, parent=self)
        dlg.exec()

    def _open_perf_monitor(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("性能监控")
        dlg.resize(900, 660)
        layout = QVBoxLayout(dlg)
        panel = PerfMonitorPanel(
            conn_getter=self._get_current_conn)
        layout.addWidget(panel)
        dlg.exec()
        try:
            panel.monitor.stop()
        except Exception:
            pass

    def _open_data_quality(self):
        dlg = DataQualityDialog(
            conn_getter=self._get_current_conn, parent=self)
        dlg.exec()

    def _open_quality_v2(self):
        dlg = QualityV2Dialog(
            conn_getter=self._get_current_conn, parent=self)
        dlg.exec()

    def _open_health(self):
        dlg = HealthDashboard(
            conn_getter=self._get_current_conn, parent=self)
        dlg.exec()

    def _open_alerts(self):
        dlg = AlertsPanel(self.alert_engine, parent=self)
        dlg.exec()

    def _open_scheduler(self):
        conn_names = list(self.object_tree.connections.keys())
        report_names = [t.name for t in self.report_engine.templates]
        dlg = SchedulerPanel(
            self.scheduler, conn_names, report_names, parent=self)
        dlg.exec()

    def _open_assets(self):
        dlg = AssetsDashboard(self.catalog, parent=self)
        dlg.exec()

    def _open_data_map(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("数据地图")
        dlg.resize(1100, 720)
        layout = QVBoxLayout(dlg)
        view = DataMapView(self.catalog, self.lineage_store)
        layout.addWidget(view)
        dlg.exec()

    def _open_reports(self):
        editor, _ = self._current_widgets()
        current_sql = editor.toPlainText() if editor else ''
        dlg = ReportBuilder(
            self.report_engine,
            conn_getter=self._get_current_conn,
            current_sql=current_sql, parent=self)
        dlg.exec()

    def _open_masking(self):
        dlg = MaskingDialog(self.masking_engine, parent=self)
        dlg.exec()

    def _open_team_config(self):
        dlg = TeamConfigDialog(
            list(self.object_tree.connections.values()), parent=self)
        dlg.exec()

    def _open_cloud_sync(self):
        dlg = CloudSyncDialog(parent=self)
        dlg.exec()

    def _open_plugins(self):
        dlg = PluginPanel(self.plugin_mgr, parent=self)
        dlg.exec()

    def _open_audit(self):
        dlg = AuditViewer(self._get_current_conn, parent=self)
        dlg.exec()

    # ==================== G2-b：可视化 EXPLAIN / 用户管理 / 多窗口 ====================

    def _current_sql_text(self):
        editor = self._current_widgets()[0]
        if editor is None:
            return ''
        return editor.toPlainText().strip()

    def _open_visual_explain(self):
        conn = self._get_current_conn()
        if conn is None:
            QMessageBox.warning(self, '提示', '请先选择一个连接')
            return None
        sql = self._current_sql_text()
        if not sql:
            QMessageBox.information(self, '提示', '当前编辑器没有 SQL')
            return None
        from .explain_view import ExplainViewDialog
        dlg = ExplainViewDialog(conn, sql, self)
        dlg.exec()
        return dlg

    def _open_user_management(self):
        conn = self._get_current_conn()
        if conn is None:
            QMessageBox.warning(self, '提示', '请先选择一个连接')
            return None
        dlg = UserManagementDialog(conn, self)
        dlg.exec()
        return dlg

    def _qt_alive(self, widget):
        try:
            from shiboken6 import isValid
            return isValid(widget)
        except Exception:
            return widget is not None

    def _prune_extra_windows(self):
        """移除已销毁子窗口，避免多窗口列表泄漏（task-75）。"""
        try:
            self._extra_windows = [
                w for w in getattr(self, '_extra_windows', [])
                if w is not None and self._qt_alive(w)]
        except Exception:
            self._extra_windows = []

    def _new_window(self):
        """G2-b：独立 MainWindow（关闭互不影响；task-75 生命周期清理）。"""
        try:
            from ..config import AppConfig
            cfg = AppConfig()
            win = MainWindow(cfg)
            # 共享同一 settings 对象：语言/主题/快捷键在窗口间一致
            win.settings = self.settings
            if hasattr(win, 'setAttribute'):
                win.setAttribute(Qt.WA_DeleteOnClose, True)
            if hasattr(win, 'destroyed'):
                win.destroyed.connect(
                    lambda *_args: self._prune_extra_windows())
            win.show()
            if hasattr(win, '_apply_settings'):
                win._apply_settings()
            if hasattr(win, '_apply_shortcut_settings'):
                win._apply_shortcut_settings()
            self._extra_windows = [
                w for w in getattr(self, '_extra_windows', [])
                if w is not None and self._qt_alive(w)]
            self._extra_windows.append(win)
            self.status_label.setText('已打开新窗口')
            return win
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.warning(self, '新建窗口失败', str(exc))
            return None

    def _apply_shortcut_settings(self):
        """G3：应用自定义快捷键到菜单/编辑器（冲突显式，不弹模态）。"""
        try:
            from .shortcuts import apply_to_window
            result = apply_to_window(self, getattr(self.settings, 'shortcuts', {}))
            self._shortcut_apply_result = result
            # task-75：共享 settings 的子窗口同步快捷键（避免窗口间不一致）
            for win in list(getattr(self, '_extra_windows', [])):
                try:
                    if win is self or not self._qt_alive(win):
                        continue
                    win.settings = self.settings
                    win._apply_shortcut_settings()
                except Exception:
                    pass
            return result
        except Exception as exc:                      # noqa: BLE001
            self._shortcut_apply_result = {'ok': False, 'error': str(exc)}
            return self._shortcut_apply_result

    # ==================== task-43：AI SQL 助手 / 示例库 ====================

    def _schema_snapshot_safe(self):
        try:
            return self._schema_snapshot()
        except Exception:
            return [], {}

    def _open_ai_sql_dialog(self):
        from .ai_sql_dialog import AISQLDialog
        dlg = AISQLDialog(schema_provider=self._schema_snapshot_safe,
                          parent=self)
        dlg.insert_requested.connect(self._insert_sql_to_editor)

        def _execute_generated(sql):
            editor, result = self._current_widgets()
            if editor is None:
                self._add_new_query_tab(auto_switch=True)
                editor, result = self._current_widgets()
            self._execute_sql_async(sql, editor, result)

        dlg.execute_requested.connect(_execute_generated)
        dlg.exec()
        return dlg

    def _insert_sql_example(self, example_id):
        from .ai_examples import get_example
        example = get_example(example_id)
        if not example:
            return False
        self._insert_sql_to_editor(example['sql'])
        self.status_label.setText(f"已插入示例：{example['title']}")
        return True

    def _open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.Accepted:
            # 语言可能已变：重新应用主题/字体并热重译主窗口
            i18n.set_lang(self.settings.language)
            self._apply_settings()
            self._retranslate_ui()
            self._apply_shortcut_settings()

    def _open_profiler(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("SQL 性能分析")
        dlg.resize(900, 660)
        layout = QVBoxLayout(dlg)
        view = ProfilerView(self._get_current_conn)
        layout.addWidget(view)
        dlg.exec()

    def _open_db_compare(self):
        dlg = DBCompareDialog(self.object_tree.connections, self)
        dlg.exec()

    def _open_er_diagram(self):
        conn = self._get_current_conn()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择一个连接")
            return
        db = conn.config.database or 'default'
        dlg = ERDiagramDialog(conn, db, self)
        dlg.exec()

    def _open_query_builder(self):
        conn = self._get_current_conn()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择一个连接")
            return
        table = self._current_table()
        if not table:
            item = self.object_tree.tree.currentItem()
            if item:
                data = item.data(0, Qt.UserRole)
                if data and data[0] == 'table':
                    table = data[1]
        if not table:
            QMessageBox.information(self, "提示", "请先在左侧选择一个表")
            return
        try:
            cols = conn.get_columns(table)
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return
        dlg = QueryBuilderDialog(conn, table, cols, self)
        if dlg.exec() == QDialog.Accepted:
            sql = dlg.get_sql()
            if sql:
                self._insert_sql_to_editor(sql)

    def _open_chart(self):
        _, result = self._current_widgets()
        if result is None:
            QMessageBox.information(self, "提示", "没有可绘制的数据")
            return
        try:
            cols = [result.table.horizontalHeaderItem(i).text()
                    for i in range(result.table.columnCount())]
            rows = []
            for r in range(result.table.rowCount()):
                row = []
                for c in range(result.table.columnCount()):
                    item = result.table.item(r, c)
                    txt = item.text() if item else ''
                    if txt == 'NULL':
                        row.append(None)
                    else:
                        try:
                            row.append(int(txt))
                        except ValueError:
                            try:
                                row.append(float(txt))
                            except ValueError:
                                row.append(txt)
                rows.append(row)
        except Exception:
            cols, rows = [], []

        if not cols:
            QMessageBox.information(self, "提示", "没有可绘制的数据")
            return
        dlg = ChartDialog(cols, rows, self)
        dlg.exec()

    def _format_sql(self):
        editor, _ = self._current_widgets()
        if editor is None:
            return
        sql = editor.toPlainText().strip()
        if not sql:
            return
        try:
            formatted = self.sql_formatter.format(sql)
            editor.set_sql(formatted)
            self.status_label.setText("已格式化 SQL")
        except Exception as e:
            QMessageBox.critical(self, "格式化失败", str(e))

    def _submit_review(self):
        editor, _ = self._current_widgets()
        if editor is None:
            return
        sql = editor.toPlainText().strip()
        if not sql:
            QMessageBox.warning(self, "提示", "SQL 为空")
            return

        conn = self._get_current_conn()
        conn_name = conn.config.name if conn else ''
        db_name = conn.config.database if conn else ''

        dlg = ReviewSubmitDialog(sql, conn_name, db_name, self)
        if dlg.exec() == QDialog.Accepted:
            data = dlg.get_data()
            req = self.review_workflow.submit(
                title=data['title'],
                sql=data['sql'],
                connection=data['connection'],
                database=data['database'],
                submitter='me',
                reason=data['reason'],
            )
            QMessageBox.information(
                self, "已提交",
                f"审查请求已提交\nID: {req.id}\n"
                "打开 SQL 审查面板可查看")

    # ==================== 设置 / i18n ====================

    def _apply_settings(self):
        # 主题
        try:
            from PySide6.QtWidgets import QApplication
            from .theme import apply_theme
            apply_theme(QApplication.instance(), self.settings.theme)
            ribbon = getattr(self, 'ribbon', None)
            if ribbon is not None:
                ribbon.refresh_icons()
        except Exception:
            pass

        # 编辑器字体
        editor, _ = self._current_widgets()
        if editor is not None:
            try:
                from PySide6.QtGui import QFont
                font = QFont(
                    self.settings.editor_font_family,
                    self.settings.editor_font_size)
                editor.setFont(font)
            except Exception:
                pass
        # P1-3：自动补全开关跟随设置（对所有已打开查询页生效）
        try:
            enabled = bool(getattr(self.settings, 'editor_auto_complete', True))
            for i in range(self.tabs.count()):
                widget = self.tabs.widget(i)
                if isinstance(widget, QSplitter):
                    ed = widget.widget(0)
                    if hasattr(ed, 'set_auto_complete'):
                        ed.set_auto_complete(enabled)
        except Exception:
            pass

        # task-75：共享 settings 的子窗口同步快捷键/字体设置
        for win in list(getattr(self, '_extra_windows', [])):
            try:
                if win is self or not self._qt_alive(win):
                    continue
                win.settings = self.settings
                win._apply_shortcut_settings()
            except Exception:
                pass

    # ==================== 设置 / i18n / 可访问性 ====================

    def _setup_i18n_refs(self):
        """收集菜单/工具栏中文本与 i18n 字典值相同的 key（只存 key，不存 QWidget，
        避免 PySide 包装对象跨函数生命周期问题）。"""
        self._i18n_key_of_text = {}
        for lang in STRINGS:
            for key, text in STRINGS[lang].items():
                self._i18n_key_of_text.setdefault(text, key)
        self._i18n_menus = []
        self._i18n_actions = []
        self._i18n_toolbar = []
        try:
            for menu in self.menuBar().findChildren(QMenu):
                key = self._i18n_key_of_text.get(menu.title())
                if key:
                    self._i18n_menus.append(key)
                for act in menu.actions():
                    if act.isSeparator():
                        continue
                    key = self._i18n_key_of_text.get(act.text())
                    if key:
                        self._i18n_actions.append(key)
        except Exception:
            pass
        tb = getattr(self, '_main_toolbar', None)
        if tb is not None:
            for act in tb.actions():
                if act.isSeparator():
                    continue
                key = self._i18n_key_of_text.get(act.text())
                if key:
                    self._i18n_toolbar.append(key)
        return {'menus': list(self._i18n_menus),
                'actions': list(self._i18n_actions),
                'toolbar': list(self._i18n_toolbar)}

    def _translate_state_text(self, text, keys):
        for key in keys:
            old_values = {STRINGS[lang].get(key) for lang in STRINGS
                          if key in STRINGS[lang]}
            if text in old_values:
                return i18n.t(key)
        return None

    def _retranslate_ui(self):
        """语言热切换：立即重译菜单/工具栏/状态栏/标签页/空态/accessibleName。"""
        # 先按当前（可能是旧语言）文本建立反向映射，再逐个 setText
        key_of_text = {}
        for lang in STRINGS:
            for key, text in STRINGS[lang].items():
                key_of_text.setdefault(text, key)
        try:
            for menu in self.menuBar().findChildren(QMenu):
                key = key_of_text.get(menu.title())
                if key:
                    menu.setTitle(i18n.t(key))
                for act in menu.actions():
                    if act.isSeparator():
                        continue
                    key = key_of_text.get(act.text())
                    if key:
                        act.setText(i18n.t(key))
        except Exception:
            pass
        tb = getattr(self, '_main_toolbar', None)
        if tb is not None:
            tb.setWindowTitle(i18n.t('toolbar.title'))
            tb.setAccessibleName(i18n.t('a11y.toolbar'))
            for act in tb.actions():
                if act.isSeparator():
                    continue
                key = key_of_text.get(act.text())
                if key:
                    act.setText(i18n.t(key))
                    act.setToolTip(i18n.t(key))
        cancel = getattr(self, '_cancel_action', None)
        if cancel is not None:
            cancel.setToolTip(i18n.t('action.cancel_query_tip'))
        # 状态栏（仅翻译已知的通用状态，业务错误文本保持原样不篡改）
        translated = self._translate_state_text(
            self.status_label.text(), ('status.ready', 'status.executing'))
        if translated is not None:
            self.status_label.setText(translated)
        connected_old = {STRINGS[lang]['status.connected'] for lang in STRINGS}
        conn_text = self.conn_label.text()
        if conn_text in connected_old:
            self.conn_label.setText(i18n.t('status.connected'))
        elif self._translate_state_text(
                conn_text, ('status.disconnected',)) is not None:
            self.conn_label.setText(i18n.t('status.disconnected'))
        else:
            # "已连接: name @ host:port" 这类前缀也随语言切换
            for lang in STRINGS:
                prefix = STRINGS[lang].get('status.connected') or ''
                if prefix and conn_text.startswith(prefix):
                    self.conn_label.setText(
                        i18n.t('status.connected') + conn_text[len(prefix):])
                    break
        # dock / 标签页标题
        try:
            self.tree_dock.setWindowTitle(i18n.t('tab.object_browser'))
            self.right_dock.setWindowTitle(i18n.t('tab.tools'))
        except Exception:
            pass
        for idx, key in getattr(self, '_right_tab_title_keys', {}).items():
            if 0 <= idx < self.right_tabs.count():
                self.right_tabs.setTabText(idx, i18n.t(key))
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if isinstance(widget, QSplitter) and widget.widget(0) is not None:
                self.tabs.setTabText(i, i18n.t('tab.query'))
                try:
                    editor = widget.widget(0)
                    result = widget.widget(1)
                    editor.setPlaceholderText(i18n.t('editor.empty_hint'))
                    editor.setAccessibleName(i18n.t('a11y.sql_editor'))
                    editor.setToolTip(i18n.t('a11y.sql_editor'))
                    result.setAccessibleName(i18n.t('a11y.result_table'))
                    result.setToolTip(i18n.t('a11y.result_table'))
                    if getattr(result, 'table', None) is not None and \
                            result.table.rowCount() == 0:
                        result.show_message(i18n.t('result.empty'))
                except Exception:
                    pass
        # 可访问性名称
        self.setAccessibleName(i18n.t('a11y.main_window'))
        self.tabs.setAccessibleName(i18n.t('a11y.tabs'))
        self.status_label.setAccessibleName(i18n.t('a11y.status'))
        self.conn_label.setAccessibleName(i18n.t('a11y.status'))
        try:
            self.menuBar().setAccessibleName(i18n.t('a11y.main_window'))
            self.object_tree.setAccessibleName(i18n.t('a11y.object_tree'))
            self.object_tree.setToolTip(i18n.t('a11y.object_tree'))
            self.object_tree.tree.setAccessibleName(i18n.t('a11y.object_tree'))
        except Exception:
            pass
        self.setWindowTitle(f"{i18n.t('app.title')} v{_STUDIO_VERSION}")
        # 刷新 key 列表（供测试/下一次热切换使用；只存字符串 key，不存 QWidget）
        try:
            self._setup_i18n_refs()
        except Exception:
            pass

    def _setup_tab_order(self):
        """合理 Tab 顺序：对象树 → SQL 编辑器 → 结果表 → 中央标签 → 右侧面板。"""
        widgets = []
        try:
            widgets.append(self.object_tree.tree)
            editor, result = self._current_widgets()
            if editor is not None:
                widgets.append(editor)
            if result is not None:
                widgets.append(result)
            widgets.append(self.tabs)
            widgets.append(self.right_tabs)
            widgets = [w for w in widgets if isinstance(w, QWidget)]
            for first, second in zip(widgets, widgets[1:]):
                QWidget.setTabOrder(first, second)
            self.tab_order_widgets = widgets
            self._tab_order_configured = len(widgets) >= 2
        except Exception:
            self.tab_order_widgets = []
            self._tab_order_configured = False

    def _setup_accessibility(self):
        """初始化可访问性：accessibleName/toolTip + Tab 顺序 + 语言热切换初译。"""
        self._setup_i18n_refs()
        self._retranslate_ui()
        self._setup_tab_order()
        try:
            self.setAccessibleName(i18n.t('a11y.main_window'))
            self.object_tree.setAccessibleName(i18n.t('a11y.object_tree'))
            self.object_tree.tree.setAccessibleName(i18n.t('a11y.object_tree'))
            self.tabs.setAccessibleName(i18n.t('a11y.tabs'))
            self.status_label.setAccessibleName(i18n.t('a11y.status'))
        except Exception:
            pass

    def _on_lang_changed(self):
        self._retranslate_ui()

    # ==================== 关于 / 更新 / 关闭 ====================

    def _check_updates(self):
        """检查更新（P3）：默认关闭，需显式配置更新通道。

        - 通道地址来自 `LIGHTDB_UPDATE_MANIFEST_URL`（http(s)/file/本地路径）；
          未配置时明确提示"未配置"，不静默假装已是最新；
        - 通道名来自 `LIGHTDB_UPDATE_CHANNEL`（默认 stable）；
        - 只展示新版本/下载地址/SHA256，**不自动下载或安装**；
        - 所有失败（网络/JSON/schema）显式弹 warning，不吞异常。
        """
        import os as _os
        from ..utils.updater import check_for_update, format_update_notice, UpdateError
        url = (_os.environ.get('LIGHTDB_UPDATE_MANIFEST_URL') or '').strip()
        if not url:
            QMessageBox.information(
                self, "检查更新",
                "未配置更新通道。\n\n"
                "请设置环境变量 LIGHTDB_UPDATE_MANIFEST_URL 指向 "
                "release_manifest.json（支持 http(s):// / file:// / 本地路径）。")
            return
        channel = (_os.environ.get('LIGHTDB_UPDATE_CHANNEL') or 'stable').strip()
        try:
            info = check_for_update(url, _STUDIO_VERSION, channel=channel)
        except UpdateError as exc:
            QMessageBox.warning(self, "检查更新失败", str(exc))
            return
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.warning(self, "检查更新失败", f'未知错误: {exc}')
            return
        text = format_update_notice(info)
        if info.available:
            QMessageBox.information(self, "发现新版本", text)
        else:
            QMessageBox.information(self, "检查更新", text)

    def _show_about(self):
        QMessageBox.about(
            self, "关于 LightDB Studio",
            f"<h3>LightDB Studio v{_STUDIO_VERSION}</h3>"
            "<p>LightDB 图形化管理工具</p>"
            "<p>类似 Navicat 的数据库管理体验</p>"
            "<hr>"
            "<p><b>核心能力：</b></p>"
            "<ul>"
            "<li>SQL 编辑 / 执行 / 格式化</li>"
            "<li>数据编辑 / 表设计</li>"
            "<li>AI 助手 / Agent</li>"
            "<li>数据血缘 / 治理</li>"
            "<li>数据质量 / SLO</li>"
            "<li>任务调度 / 报表</li>"
            "<li>团队协作 / 审查</li>"
            "</ul>"
        )

    def closeEvent(self, event):
        # task-75：先关闭子窗口（子窗口独立但主窗口关闭时不留下不可达窗口）
        for win in list(getattr(self, '_extra_windows', [])):
            try:
                if win is not None and self._qt_alive(win):
                    win.close()
            except Exception:
                pass
        self._extra_windows = []
        # P1-1：关闭窗口前取消后台查询并等待 worker 退出，避免 worker
        # 回调访问已销毁的控件（Qt 崩溃）。最多等 3s，超时不阻塞关闭。
        if self._query_busy:
            try:
                self.cancel_running_query('窗口关闭')
            except Exception:
                pass
        try:
            self._query_pool.waitForDone(3000)
        except Exception:
            pass

        # 关闭所有连接
        for name, conn in list(self.object_tree.connections.items()):
            try:
                conn.close()
            except Exception:
                pass

        # 停止后台服务
        try:
            self.scheduler.stop()
        except Exception:
            pass

        # 只在"真的被用过"时才去停它：alert_engine 是惰性管理器，
        # 直接 `self.alert_engine` 会在关闭窗口时白白构造一个引擎。
        _alerts = self._lazy_eager('alert_engine')
        if _alerts is not None:
            try:
                _alerts.stop_monitoring()
            except Exception:
                pass

        try:
            self.plugin_mgr.unload_all()
        except Exception:
            pass

        event.accept()


class UserManagementDialog(QDialog):
    """G2-b：用户管理（引擎能列用户则展示；不能则显式提示，不造假数据）。"""

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.users_error = ''
        self.users = []
        self.last_result = None
        self.setWindowTitle('用户管理')
        self.resize(640, 520)
        self._build_ui()
        self.load_users()

    def _build_ui(self):
        from PySide6.QtWidgets import (
            QFormLayout, QLineEdit, QComboBox, QTextEdit, QPushButton,
            QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit)
        layout = QVBoxLayout(self)
        self.engine_status = QLabel('')
        self.engine_status.setWordWrap(True)
        self.engine_status.setStyleSheet('color: #d7ba7d;')
        layout.addWidget(self.engine_status)
        self.user_list = QPlainTextEdit()
        self.user_list.setReadOnly(True)
        self.user_list.setMaximumHeight(120)
        layout.addWidget(QLabel('已有用户（真实查询结果；不支持时为空并显示原因）:'))
        layout.addWidget(self.user_list)

        form = QFormLayout()
        self.name_edit = QLineEdit('app_user')
        form.addRow('用户名:', self.name_edit)
        self.host_edit = QLineEdit('localhost')
        form.addRow('Host:', self.host_edit)
        self.pwd_edit = QLineEdit()
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        form.addRow('密码:', self.pwd_edit)
        self.db_edit = QLineEdit()
        form.addRow('授权库:', self.db_edit)
        self.priv_combo = QComboBox()
        self.priv_combo.addItems(['SELECT', 'INSERT', 'UPDATE', 'DELETE',
                                  'SELECT, INSERT, UPDATE, DELETE', 'ALL'])
        form.addRow('权限:', self.priv_combo)
        layout.addLayout(form)
        self.sql_preview = QTextEdit()
        self.sql_preview.setReadOnly(True)
        self.sql_preview.setMaximumHeight(110)
        layout.addWidget(QLabel('将执行的 SQL（预演）:'))
        layout.addWidget(self.sql_preview)

        row = QHBoxLayout()
        gen = QPushButton('生成 SQL')
        gen.clicked.connect(self.generate_sql)
        row.addWidget(gen)
        exec_btn = QPushButton('执行（显式结果）')
        exec_btn.clicked.connect(self.execute)
        row.addWidget(exec_btn)
        row.addStretch()
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
        self.result_label = QLabel('')
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        self.generate_sql()

    def load_users(self):
        self.users = []
        self.users_error = ''
        try:
            _cols, rows = self.conn.query('SELECT user, host FROM mysql.user')
            self.users = [tuple(r) for r in (rows or [])]
            self.user_list.setPlainText('\n'.join(
                f'{r[0]}@{r[1]}' for r in self.users) or '（无用户）')
            self.engine_status.setText(
                f'引擎返回 {len(self.users)} 个用户（真实数据）')
            self.engine_status.setStyleSheet('color: #89d185;')
        except Exception as exc:                      # noqa: BLE001
            self.users_error = str(exc)
            self.user_list.setPlainText('')
            self.engine_status.setText(
                '引擎暂不支持列出用户（显式提示，不造假数据）: ' + str(exc))
            self.engine_status.setStyleSheet('color: #f48771;')
        return self.users

    def generate_sql(self):
        import re
        name = self.name_edit.text().strip() or 'app_user'
        host = self.host_edit.text().strip() or 'localhost'
        pwd = self.pwd_edit.text()
        db = self.db_edit.text().strip()
        priv = self.priv_combo.currentText()
        if not re.fullmatch(r'[A-Za-z0-9_.$@-]{1,64}', name) or \
                not re.fullmatch(r'[A-Za-z0-9_.$@%:-]{1,128}', host):
            self.result_label.setText('用户名/Host 含非法字符（已拒绝生成 SQL）')
            self.result_label.setStyleSheet('color: #f48771;')
            if hasattr(self, 'sql_preview'):
                self.sql_preview.setPlainText('')
            return ''
        if re.search(r'[`\x00]', db or ''):
            self.result_label.setText('授权库名非法（已拒绝生成 SQL）')
            self.result_label.setStyleSheet('color: #f48771;')
            return ''
        pwd_sql = pwd.replace('\\', '\\\\').replace("'", "\\'")
        create = (f"CREATE USER '{name}'@'{host}' "
                  f"IDENTIFIED BY '{pwd_sql}';")
        grant = (f"GRANT {priv} ON `{db or '*'}`.* TO '{name}'@'{host}';")
        text = create + '\n' + grant
        if hasattr(self, 'sql_preview'):
            self.sql_preview.setPlainText(text)
        return text

    def execute(self):
        statements = [s for s in self.generate_sql().splitlines() if s.strip()]
        results = []
        for sql in statements:
            try:
                n, err = self.conn.execute(sql)
                results.append({'sql': sql, 'ok': not err,
                                 'error': err or '', 'affected': n})
            except Exception as exc:                  # noqa: BLE001
                results.append({'sql': sql, 'ok': False,
                                 'error': str(exc), 'affected': 0})
        self.last_result = {'ok': all(r['ok'] for r in results),
                            'results': results}
        lines = [f"{'OK' if r['ok'] else 'FAIL'}: {r['sql']}"
                 + (f" -> {r['error']}" if r['error'] else '')
                 for r in results]
        self.result_label.setText('\n'.join(lines))
        self.result_label.setStyleSheet(
            'color: #89d185;' if self.last_result['ok'] else 'color: #f48771;')
        return self.last_result
