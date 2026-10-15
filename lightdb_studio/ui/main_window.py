"""主窗口"""
import os
import re
import time
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QTabWidget, QWidget, QVBoxLayout,
    QHBoxLayout, QToolBar, QStatusBar, QMessageBox, QFileDialog,
    QSplitter, QLabel, QInputDialog, QDialog, QLineEdit,
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QAction, QKeySequence

# ---------- 核心管理器 ----------
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
from ..utils.i18n import i18n
from ..utils.history import QueryHistory

# ---------- DB 错误字段（errno / sqlstate / message，绝不丢） ----------
from ..db.connection import error_code, error_sqlstate

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
from .connection_dialog import ConnectionDialog
from .object_tree import ObjectTree
from .sql_editor import SQLEditor
from .result_table import ResultTable, parse_error_fields
from .data_editor import DataEditor
from .table_designer import TableDesigner
from .query_history import QueryHistoryPanel
from .import_export import ImportCSVDialog, ExportCSVDialog
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
from .dashboard import DashboardWidget
from .data_map_view import DataMapView

# ---------- DB ----------
from ..config import ConnectionInfo
from ..db.connection import DBConnection


class MainWindow(QMainWindow):
    """LightDB Studio 主窗口"""

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.settings = config.settings
        self.history = QueryHistory()

        # ---------- 核心管理器 ----------
        self.env_mgr = EnvironmentManager()
        self.lineage_store = LineageStore()
        self.lineage_enhanced = EnhancedLineageStore()
        self.review_workflow = ReviewWorkflow()
        self.snapshot_mgr = SnapshotManager()
        self.sql_formatter = SQLFormatter()
        self.snippet_mgr = SnippetManager()
        self.masking_engine = MaskingEngine()
        self.team_mgr = TeamConfigManager()
        self.cloud_sync = CloudSync()
        self.report_engine = ReportEngine()
        self.collab_store = CollaborationStore()
        self.ai_store = AIConfigStore()
        self.catalog = CatalogEngine()
        self.alert_engine = AlertEngine()

        # 调度
        self.scheduler = Scheduler(
            connection_provider=self._get_conn_by_name,
            report_engine=self.report_engine)
        self.workflow_engine = WorkflowEngine(
            connection_provider=self._get_conn_by_name)

        # AI Agent
        self.ai_agent = AIAgent(
            conn_getter=self._get_current_conn,
            ai_config=self.ai_store.load(),
            catalog=self.catalog,
            lineage=self.lineage_store)

        # 插件系统
        plugins_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            '..', 'plugins')
        self.plugin_mgr = PluginManager(plugins_dir, self._plugin_context())
        self.plugin_mgr.load_all()

        # i18n
        i18n.set_lang(self.settings.language)
        i18n.on_change(self._on_lang_changed)

        self.setWindowTitle(f"{i18n.t('app.title')} v1.0")
        self.resize(1480, 940)

        # 构建 UI
        # 注意顺序：_build_ui() 里新增查询标签页会触发 _on_tab_changed()，
        # 而它要访问 self.conn_label；因此状态栏必须先建好（原实现顺序相反，
        # 导致 MainWindow 构造时直接 AttributeError）。
        self._build_menu()
        self._build_statusbar()
        self._build_ui()
        self._build_toolbar()
        self._apply_settings()

        # 启动后台服务
        self.scheduler.start()

        # 自动连接
        self._auto_restore()

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

        self.tree_dock = QDockWidget("对象浏览器", self)
        self.tree_dock.setWidget(self.object_tree)
        self.tree_dock.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.tree_dock)
        self.tree_dock.setMinimumWidth(260)

        # 右侧：多功能 Tab
        self.right_tabs = QTabWidget()
        self.right_tabs.setTabPosition(QTabWidget.North)

        # 查询历史
        self.history_panel = QueryHistoryPanel(self.history)
        self.history_panel.sql_selected.connect(self._insert_history_sql)
        self.right_tabs.addTab(self.history_panel, "历史")

        # SQL 片段
        self.snippet_panel = SnippetPanel(self.snippet_mgr)
        self.snippet_panel.snippet_activated.connect(self._insert_sql_to_editor)
        self.right_tabs.addTab(self.snippet_panel, "片段")

        # AI 对话
        self.ai_chat = AIChatPanel(
            schema_provider=self._schema_provider,
            conn_getter=self._get_current_conn)
        self.ai_chat.sql_generated.connect(self._insert_sql_to_editor)
        self.right_tabs.addTab(self.ai_chat, "AI")

        # Agent 对话
        self.agent_chat = AgentChatPanel(self.ai_agent)
        self.agent_chat.sql_generated.connect(self._insert_sql_to_editor)
        self.right_tabs.addTab(self.agent_chat, "Agent")

        # 协作
        self.collab_panel = CollaborationPanel(self.collab_store)
        self.collab_panel.session_selected.connect(self._on_session_selected)
        self.right_tabs.addTab(self.collab_panel, "协作")

        # 数据目录
        self.catalog_panel = CatalogPanel(self.catalog)
        self.catalog_panel.table_selected.connect(self._on_catalog_selected)
        self.right_tabs.addTab(self.catalog_panel, "目录")

        self.right_dock = QDockWidget("工具面板", self)
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

    def _build_menu(self):
        mb = self.menuBar()

        # ---------- 文件 ----------
        m_file = mb.addMenu("文件(&F)")

        a = QAction("新建连接...", self)
        a.triggered.connect(self._new_connection)
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

        a = QAction("🚚 数据库迁移...", self)
        a.triggered.connect(self._open_migration)
        m_tools.addAction(a)

        m_tools.addSeparator()

        a = QAction("🤖 AI 对话", self)
        a.setShortcut(QKeySequence("Ctrl+Shift+A"))
        a.triggered.connect(
            lambda: self._show_right_tab_by_widget(self.ai_chat))
        m_tools.addAction(a)

        a = QAction("💬 协作会话", self)
        a.triggered.connect(
            lambda: self._show_right_tab_by_widget(self.collab_panel))
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

        # ---------- 帮助 ----------
        m_help = mb.addMenu("帮助(&H)")

        a = QAction("关于", self)
        a.triggered.connect(self._show_about)
        m_help.addAction(a)

    def _build_toolbar(self):
        tb = QToolBar("主工具栏")
        tb.setIconSize(QSize(18, 18))
        tb.setMovable(False)
        self.addToolBar(tb)

        items = [
            ("🔌 连接", self._new_connection),
            ("📝 查询", lambda: self._add_new_query_tab(True)),
            None,
            ("▶ 执行", self._exec_current),
            ("📊 EXPLAIN", self._exec_explain),
            None,
            ("➕ 新建表", self._new_table_designer),
            ("✏ 编辑数据", self._edit_current_table),
            None,
            ("📥 导入", self._import_current_table),
            ("📤 导出", self._export_current_result),
            None,
            ("🔄 刷新", self._refresh_tree),
        ]

        for item in items:
            if item is None:
                tb.addSeparator()
                continue
            text, slot = item
            a = QAction(text, self)
            a.triggered.connect(slot)
            tb.addAction(a)

        # 环境条
        tb.addSeparator()
        tb.addWidget(self.env_bar)

    def _build_statusbar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        # 用只读 QLineEdit 而不是 QLabel：完整错误文本可以在状态栏里
        # 直接选中复制（以前是 `any_error[:80]` 静默截断，后面的诊断信息
        # 用户永远看不到）。用 QLineEdit 仍然保持单行高度与横向滚动。
        self.status_label = QLineEdit("就绪")
        self.status_label.setReadOnly(True)
        self.status_label.setFrame(False)
        self.status_label.setStyleSheet(
            "QLineEdit { background: transparent; border: none; color: #cccccc; }")
        sb.addWidget(self.status_label)
        self.conn_label = QLabel("未连接")
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

        idx = self.tabs.addTab(splitter, "查询")
        editor.execute_requested.connect(
            lambda sql, e=editor, r=result: self._execute_sql(sql, e, r))
        editor.execute_selection_requested.connect(
            lambda sql, e=editor, r=result: self._execute_sql(sql, e, r))

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

    def _new_connection(self):
        dlg = ConnectionDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        info = dlg.get_connection()
        try:
            conn = DBConnection(info)
            conn.connect()
        except Exception as e:
            QMessageBox.critical(self, "连接失败", f"无法连接:\n{e}")
            return

        self.config.add_connection(info)
        self.config.last_connection = info.name
        self.config.save()

        self.object_tree.add_connection(info, conn)
        self.status_label.setText(f"已连接: {info.name}")
        self._on_tab_changed(self.tabs.currentIndex())

    def _auto_restore(self):
        for info in self.config.connections:
            try:
                conn = DBConnection(info)
                conn.connect()
                self.object_tree.add_connection(info, conn)
                self.status_label.setText(f"自动连接: {info.name}")
                self._on_tab_changed(self.tabs.currentIndex())
            except Exception:
                self.status_label.setText(f"自动连接失败: {info.name}")

    def _refresh_tree(self):
        self.object_tree.refresh_all()

    # ==================== 查询执行 ====================

    def _on_query_requested(self, sql: str):
        editor, result = self._current_widgets()
        if editor is None:
            self._add_new_query_tab(auto_switch=False)
            editor, result = self._current_widgets()
        editor.set_sql(sql)
        self._execute_sql(sql, editor, result)

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
            self._execute_sql(sql, editor, result)

    def _exec_explain(self):
        editor, result = self._current_widgets()
        if editor is None:
            return
        sql = editor.toPlainText().strip().rstrip(';')
        if not sql:
            return
        if sql.upper().startswith('EXPLAIN'):
            self._execute_sql(sql, editor, result)
        else:
            self._execute_sql("EXPLAIN " + sql, editor, result)

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
            self._set_status(f"错误: {any_error}", error=True)
            self.history.add(sql, conn.config.name, False, elapsed)
        else:
            if last_cols:
                result.show_result(last_cols, last_rows, elapsed)
            else:
                result.show_message(last_msg)
            self._set_status(f"完成 ({elapsed:.1f} ms)")
            self.history.add(sql, conn.config.name, True, elapsed)

        self.history_panel.refresh()

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

    def _show_right_tab_by_widget(self, widget):
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

    def _open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.Accepted:
            self._apply_settings()

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

    def _on_lang_changed(self):
        self.setWindowTitle(f"{i18n.t('app.title')} v1.0")

    # ==================== 关于 / 关闭 ====================

    def _show_about(self):
        QMessageBox.about(
            self, "关于 LightDB Studio",
            "<h3>LightDB Studio v1.0</h3>"
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

        try:
            self.alert_engine.stop_monitoring()
        except Exception:
            pass

        try:
            self.plugin_mgr.unload_all()
        except Exception:
            pass

        event.accept()