"""左侧对象浏览器：连接 / 数据库 / 表（P1-6：懒加载 + 右键菜单）"""
import json
import re

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QMenu, QMessageBox, QInputDialog, QToolBar, QLineEdit,
    QPushButton, QHBoxLayout, QLabel, QApplication, QAbstractItemView
)
from PySide6.QtCore import Qt, Signal, QMimeData, QTimer
from PySide6.QtGui import QAction, QIcon, QFont, QColor, QDrag

from ..db.connection import DBConnection
from ..db.metadata import MetadataLoader


# 节点类型
NODE_CONN = 'conn'
NODE_DB = 'db'
NODE_TABLE_FOLDER = 'tables'
NODE_TABLE = 'table'
NODE_COLUMN = 'column'
NODE_INDEX_FOLDER = 'indexes'
NODE_INDEX = 'index'
NODE_PROC_FOLDER = 'procedures'
NODE_PROC = 'procedure'
NODE_FUNC_FOLDER = 'functions'
NODE_FUNC = 'function'
NODE_TRIGGER_FOLDER = 'triggers'
NODE_TRIGGER = 'trigger'
NODE_EVENT_FOLDER = 'events'
NODE_EVENT = 'event'

# task-89：大树防护 —— 单库一次最多插入 500 张表，避免 1000+ 表瞬间建节点
TABLE_RENDER_LIMIT = 500
TABLE_FILTER_DEBOUNCE_MS = 220

# 表节点占位子项文本（说明"展开时才加载"）
_LOADING_TEXT = "加载中..."


class _DraggableTree(QTreeWidget):
    """G2：支持拖表/列到 SQL 编辑器（自定义 MIME + 文本兜底）。"""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setDefaultDropAction(Qt.CopyAction)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        payload = self.owner.drag_payload(item) if item is not None else None
        if not payload:
            return
        mime = QMimeData()
        mime.setData(self.owner.DRAG_MIME,
                     json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        mime.setText(payload['text'])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)


class ObjectTree(QWidget):
    """对象浏览器"""

    table_double_clicked = Signal(str)       # table_name
    query_requested = Signal(str)            # sql（当前编辑器执行）
    new_query_requested = Signal(str)        # sql（新查询页，不自动执行）
    connection_selected = Signal(object)     # DBConnection
    # G1：把对象管理动作接到 MainWindow（设计表/导入/导出）
    design_table_requested = Signal(str, str, str)   # conn, db, table
    import_data_requested = Signal(str, str, str)
    export_data_requested = Signal(str, str, str)
    DRAG_MIME = 'application/x-lightdb-object'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.connections = {}       # conn_name -> DBConnection
        # P1-6 表级缓存：key=(conn_name, db, table) -> {'columns','indexes','triggers'}
        self._table_cache = {}
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部工具栏
        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(4, 4, 4, 4)
        bar_layout.setSpacing(4)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("过滤表名...")
        self.filter_edit.textChanged.connect(self._apply_filter)
        bar_layout.addWidget(self.filter_edit)

        refresh_btn = QPushButton("🔄")
        refresh_btn.setFixedWidth(32)
        refresh_btn.clicked.connect(self.refresh_all)
        bar_layout.addWidget(refresh_btn)
        self.refresh_status_label = QLabel('')
        self.refresh_status_label.setStyleSheet('color: #8a8a8a;')
        self.refresh_status_label.setObjectName('tree_refresh_status')
        bar_layout.addWidget(self.refresh_status_label)

        layout.addWidget(bar)

        # 树
        self.tree = _DraggableTree(self)
        self.tree.setHeaderHidden(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_menu)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.itemExpanded.connect(self._on_expand)
        self.tree.setIndentation(14)
        self.tree.setAnimated(True)
        self.tree.setExpandsOnDoubleClick(False)
        layout.addWidget(self.tree)

        # 隐藏的刷新用
        self._filter_text = ""
        # task-89：过滤防抖（大树才启用；小树保持即时，兼容既有交互）
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(TABLE_FILTER_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self._apply_filter_now)
        self._table_count = 0

    # ==================== 对外接口 ====================

    def add_connection(self, info, conn: DBConnection):
        """添加一个连接节点"""
        self.connections[info.name] = conn

        root = QTreeWidgetItem(self.tree)
        root.setText(0, f"🔌 {info.name}")
        root.setData(0, Qt.UserRole, (NODE_CONN, info.name))
        root.setData(0, Qt.UserRole + 1, {'host': info.host,
                                          'port': info.port,
                                          'user': info.user})
        root.setExpanded(True)
        font = root.font(0)
        font.setBold(True)
        root.setFont(0, font)

        # 第一棵树节点自动成为「当前连接」：否则连上了也是 `未连接`——
        # `get_current_connection()` 只看 `tree.currentItem()`，而 Qt 插入
        # 节点时**不会**自动设置 current（实测：插入后 currentItem() 仍为
        # None，于是刚连上的连接用不了，查询还会弹「请先选择一个连接」）。
        # 已经有当前项时不动它（不抢用户的选择）。
        if self.tree.currentItem() is None:
            self.tree.setCurrentItem(root)

        # 加载数据库
        try:
            loader = MetadataLoader(conn)
            for db in loader.databases():
                self._add_db_node(root, info.name, db)
        except Exception as e:
            err_item = QTreeWidgetItem(root)
            err_item.setText(0, f"⚠ 加载失败: {e}")
            err_item.setForeground(0, QColor("#f48771"))

    def select_connection(self, name: str) -> bool:
        """把 `name` 这条连接设为「当前连接」（找不到返回 False）。

        用于自动连接后**主动选中** `last_connection`：这样
        `get_current_connection()` / `MainWindow._get_current_conn()` 立刻
        返回它，用户不必先去树里点一下。
        """
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            data = item.data(0, Qt.UserRole)
            if data and data[0] == NODE_CONN and data[1] == name:
                self.tree.setCurrentItem(item)
                return True
        return False

    def remove_connection(self, name: str):
        # 遍历顶层节点
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            data = item.data(0, Qt.UserRole)
            if data and data[0] == NODE_CONN and data[1] == name:
                self.tree.takeTopLevelItem(i)
                break
        if name in self.connections:
            try:
                self.connections[name].close()
            except Exception:
                pass
            del self.connections[name]

    def refresh_all(self):
        """重建整棵树；保留展开的库/当前选择，反馈刷新结果（task-87）。"""
        # 记录展开的 DB 节点与当前节点，刷新后恢复（多库切换状态保持）
        expanded = set()
        current = self.tree.currentItem()
        current_key = None
        if current is not None:
            d = current.data(0, Qt.UserRole)
            if d:
                current_key = (d[0], d[1])
        def scan_expanded(parent):
            for i in range(parent.childCount()):
                node = parent.child(i)
                data = node.data(0, Qt.UserRole)
                if data and data[0] == NODE_DB and node.isExpanded():
                    expanded.add((data[1],))
                scan_expanded(node)
        scan_expanded(self.tree.invisibleRootItem())
        self._table_cache.clear()
        self._table_count = 0
        self.tree.clear()
        errors = []
        for name, conn in list(self.connections.items()):
            info = conn.config
            try:
                self.add_connection(info, conn)
            except Exception as exc:                  # noqa: BLE001
                errors.append(f'{name}: {exc}')
        # 恢复展开的库 & 当前项
        try:
            def restore(parent):
                for i in range(parent.childCount()):
                    node = parent.child(i)
                    data = node.data(0, Qt.UserRole)
                    if data and data[0] == NODE_CONN:
                        for j in range(node.childCount()):
                            dbnode = node.child(j)
                            dd = dbnode.data(0, Qt.UserRole)
                            if dd and dd[0] == NODE_DB and (dd[1],) in expanded:
                                dbnode.setExpanded(True)
                    restore(node)
            restore(self.tree.invisibleRootItem())
            if current_key:
                def find(parent):
                    for i in range(parent.childCount()):
                        node = parent.child(i)
                        d = node.data(0, Qt.UserRole)
                        if d == current_key:
                            self.tree.setCurrentItem(node)
                            return True
                        if find(node):
                            return True
                    return False
                find(self.tree.invisibleRootItem())
        except Exception:
            pass
        if errors:
            self.refresh_status_label.setText('刷新失败: ' + '; '.join(errors))
            self.refresh_status_label.setStyleSheet('color: #f48771;')
        else:
            self.refresh_status_label.setText(
                f'已刷新 {len(self.connections)} 个连接')
            self.refresh_status_label.setStyleSheet('color: #89d185;')
        return errors

    def get_current_connection(self):
        item = self.tree.currentItem()
        if not item:
            return None
        # 向上找连接节点
        while item:
            data = item.data(0, Qt.UserRole)
            if data and data[0] == NODE_CONN:
                return self.connections.get(data[1])
            item = item.parent()
        return None

    def get_current_database(self):
        item = self.tree.currentItem()
        if not item:
            return None
        while item:
            data = item.data(0, Qt.UserRole)
            if data and data[0] == NODE_DB:
                return data[1]
            item = item.parent()
        return None

    # ==================== 节点构建 ====================

    def _add_db_node(self, parent: QTreeWidgetItem, conn_name: str, db_name: str):
        item = QTreeWidgetItem(parent)
        item.setText(0, f"🗄 {db_name}")
        item.setData(0, Qt.UserRole, (NODE_DB, db_name))
        item.setData(0, Qt.UserRole + 1, {'conn': conn_name})
        # 惰性加载：先放一个占位节点
        QTreeWidgetItem(item).setText(0, "加载中...")

    def _load_db_children(self, item: QTreeWidgetItem):
        """展开数据库节点时加载子节点"""
        item.takeChildren()
        data = item.data(0, Qt.UserRole)
        if not data or data[0] != NODE_DB:
            return
        db_name = data[1]
        conn_name = item.data(0, Qt.UserRole + 1).get('conn')
        conn = self.connections.get(conn_name)
        if not conn:
            return

        # 切换数据库
        try:
            conn.switch_database(db_name)
        except Exception as e:
            err = QTreeWidgetItem(item)
            err.setText(0, f"⚠ {e}")
            return

        loader = MetadataLoader(conn)
        # task-89：500+ 表批量插入期间关闭动画/重绘，避免展开卡死 UI
        self.tree.setUpdatesEnabled(False)
        self.tree.setAnimated(False)

        # 表文件夹
        tables_folder = QTreeWidgetItem(item)
        tables_folder.setText(0, "📁 表")
        tables_folder.setData(0, Qt.UserRole, (NODE_TABLE_FOLDER, 'tables'))
        tables_folder.setData(0, Qt.UserRole + 1, {'conn': conn_name, 'db': db_name})

        try:
            tables = list(loader.tables())
            shown = 0
            for t in tables:
                if shown >= TABLE_RENDER_LIMIT:
                    note = QTreeWidgetItem(tables_folder)
                    note.setText(0, f"… 仅显示前 {TABLE_RENDER_LIMIT} 个表"
                                     f"（共 {len(tables)}，请用过滤框缩小范围）")
                    note.setForeground(0, QColor('#d7ba7d'))
                    break
                self._add_table_node(tables_folder, conn_name, db_name, t)
                shown += 1
            self._table_count += shown
        except Exception:
            pass

        # 存储过程
        proc_folder = QTreeWidgetItem(item)
        proc_folder.setText(0, "📁 存储过程")
        proc_folder.setData(0, Qt.UserRole, (NODE_PROC_FOLDER, 'procedures'))
        proc_folder.setData(0, Qt.UserRole + 1, {'conn': conn_name, 'db': db_name})
        try:
            for p in loader.procedures():
                pitem = QTreeWidgetItem(proc_folder)
                pitem.setText(0, f"⚙ {p}")
                pitem.setData(0, Qt.UserRole, (NODE_PROC, p))
                pitem.setData(0, Qt.UserRole + 1,
                              {'conn': conn_name, 'db': db_name})
        except Exception:
            pass

        # 函数
        fn_folder = QTreeWidgetItem(item)
        fn_folder.setText(0, "📁 函数")
        fn_folder.setData(0, Qt.UserRole, (NODE_FUNC_FOLDER, 'functions'))
        fn_folder.setData(0, Qt.UserRole + 1, {'conn': conn_name, 'db': db_name})
        try:
            for f in loader.functions():
                fitem = QTreeWidgetItem(fn_folder)
                fitem.setText(0, f"ƒ {f}")
                fitem.setData(0, Qt.UserRole, (NODE_FUNC, f))
                fitem.setData(0, Qt.UserRole + 1,
                              {'conn': conn_name, 'db': db_name})
        except Exception:
            pass

        # 触发器
        trg_folder = QTreeWidgetItem(item)
        trg_folder.setText(0, "📁 触发器")
        trg_folder.setData(0, Qt.UserRole, (NODE_TRIGGER_FOLDER, 'triggers'))
        trg_folder.setData(0, Qt.UserRole + 1, {'conn': conn_name, 'db': db_name})
        try:
            for t in loader.triggers():
                titem = QTreeWidgetItem(trg_folder)
                titem.setText(0, f"⚡ {t}")
                titem.setData(0, Qt.UserRole, (NODE_TRIGGER, t))
                titem.setData(0, Qt.UserRole + 1,
                              {'conn': conn_name, 'db': db_name})
        except Exception:
            pass

        # 事件
        evt_folder = QTreeWidgetItem(item)
        evt_folder.setText(0, "📁 事件")
        evt_folder.setData(0, Qt.UserRole, (NODE_EVENT_FOLDER, 'events'))
        evt_folder.setData(0, Qt.UserRole + 1, {'conn': conn_name, 'db': db_name})
        try:
            for e in loader.events():
                eitem = QTreeWidgetItem(evt_folder)
                eitem.setText(0, f"⏰ {e}")
                eitem.setData(0, Qt.UserRole, (NODE_EVENT, e))
                eitem.setData(0, Qt.UserRole + 1,
                              {'conn': conn_name, 'db': db_name})
        except Exception:
            pass

        self.tree.setAnimated(True)
        self.tree.setUpdatesEnabled(True)

    # ==================== 表级懒加载（P1-6） ====================

    def _add_table_node(self, parent: QTreeWidgetItem, conn_name: str,
                        db_name: str, table: str) -> QTreeWidgetItem:
        """表节点：只挂一个"加载中..."占位子项，展开时才查列/索引/触发器。"""
        titem = QTreeWidgetItem(parent)
        titem.setText(0, f"📋 {table}")
        titem.setData(0, Qt.UserRole, (NODE_TABLE, table))
        titem.setData(0, Qt.UserRole + 1,
                      {'conn': conn_name, 'db': db_name})
        # task-89：不再为每个表建“加载中”子节点（500 表省 500 个 item）；
        # 用 setChildIndicatorPolicy 保留展开箭头，首次展开再加载。
        titem.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        return titem

    def _table_key(self, item: QTreeWidgetItem):
        data = item.data(0, Qt.UserRole)
        if not data:
            return None
        meta = item.data(0, Qt.UserRole + 1) or {}
        return (meta.get('conn'), meta.get('db'), data[1])

    def _fetch_table_children(self, conn_name, db_name, table) -> dict:
        """查一次表元数据（列/索引/触发器），结果进缓存。失败不抛（返回空）。"""
        conn = self.connections.get(conn_name)
        if conn is None:
            return {'columns': [], 'indexes': [], 'triggers': []}
        loader = MetadataLoader(conn)
        columns = loader.columns(table) or []
        indexes = []
        try:
            _cols, rows = conn.query(f"SHOW INDEX FROM `{table}`")
            for r in rows or []:
                # MySQL 形态: Table, Non_unique, Key_name, Seq_in_index,
                #             Column_name, ...
                try:
                    indexes.append({'name': str(r[2]), 'unique': int(r[1]) == 0,
                                    'column': str(r[4]) if len(r) > 4 else ''})
                except Exception:
                    continue
        except Exception:
            indexes = []
        triggers = []
        try:
            _cols, rows = conn.query("SHOW TRIGGERS")
            for r in rows or []:
                # (Trigger, Event, Table, Statement, Timing, ...)
                if len(r) >= 3 and str(r[2]) == str(table):
                    triggers.append({'name': str(r[0]), 'event': str(r[1])})
        except Exception:
            triggers = []
        return {'columns': columns, 'indexes': indexes, 'triggers': triggers}

    def _load_table_children(self, item: QTreeWidgetItem, force=False):
        """展开表节点时加载列/索引/触发器；命中缓存不重复查询。"""
        key = self._table_key(item)
        if key is None:
            return
        cached = None if force else self._table_cache.get(key)
        if cached is None:
            cached = self._fetch_table_children(*key)
            self._table_cache[key] = cached
        item.takeChildren()

        columns = cached.get('columns') or []
        if columns:
            for col in columns:
                name = str(col.get('name', ''))
                ctype = str(col.get('type', ''))
                key_mark = '🔑 ' if str(col.get('key', '')).upper() == 'PRI' else ''
                citem = QTreeWidgetItem(item)
                citem.setText(0, f"{key_mark}{name}  {ctype}".strip())
                citem.setData(0, Qt.UserRole, (NODE_COLUMN, name))
                citem.setData(0, Qt.UserRole + 1, {
                    'conn': key[0], 'db': key[1], 'table': key[2],
                    'type': ctype, 'null': col.get('null', 'YES'),
                    'key': col.get('key', ''), 'default': col.get('default'),
                    'extra': col.get('extra', ''),
                })
        else:
            empty = QTreeWidgetItem(item)
            empty.setText(0, "（无列信息）")

        index_folder = QTreeWidgetItem(item)
        index_folder.setText(0, "📁 索引")
        index_folder.setData(0, Qt.UserRole, (NODE_INDEX_FOLDER, 'indexes'))
        index_folder.setData(0, Qt.UserRole + 1,
                             {'conn': key[0], 'db': key[1], 'table': key[2]})
        for idx in cached.get('indexes') or []:
            iitem = QTreeWidgetItem(index_folder)
            mark = 'UNIQUE ' if idx.get('unique') else ''
            iitem.setText(0, f"{mark}{idx.get('name', '')}")
            iitem.setData(0, Qt.UserRole, (NODE_INDEX, str(idx.get('name', ''))))
            iitem.setData(0, Qt.UserRole + 1,
                          {'conn': key[0], 'db': key[1], 'table': key[2],
                           'column': idx.get('column', ''),
                           'unique': bool(idx.get('unique'))})

        trg_folder = QTreeWidgetItem(item)
        trg_folder.setText(0, "📁 触发器")
        trg_folder.setData(0, Qt.UserRole, (NODE_TRIGGER_FOLDER, 'triggers'))
        trg_folder.setData(0, Qt.UserRole + 1,
                           {'conn': key[0], 'db': key[1], 'table': key[2]})
        for trg in cached.get('triggers') or []:
            titem = QTreeWidgetItem(trg_folder)
            titem.setText(0, f"⚡ {trg.get('name', '')}")
            titem.setData(0, Qt.UserRole, (NODE_TRIGGER, str(trg.get('name', ''))))
            titem.setData(0, Qt.UserRole + 1,
                          {'conn': key[0], 'db': key[1], 'table': key[2],
                           'event': trg.get('event', '')})

    def table_cache_size(self) -> int:
        return len(self._table_cache)

    # ==================== 事件 ====================

    def _on_expand(self, item):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == NODE_DB:
            # 检查是否需要加载
            if item.childCount() == 1 and \
               item.child(0).text(0) == _LOADING_TEXT:
                self._load_db_children(item)
        elif data[0] == NODE_TABLE:
            # P1-6/task-89：展开表才加载列/索引/触发器（缓存命中不重复查）；
            # 无占位子节点时 childCount()==0 也要加载。
            if item.childCount() == 0 or (
                    item.childCount() == 1 and
                    item.child(0).text(0) == _LOADING_TEXT):
                self._load_table_children(item)

    def _on_double_click(self, item, col):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        node_type, name = data

        if node_type == NODE_TABLE:
            # task-87：双击表 → 打开数据编辑器（信号由主窗口接线）
            self.table_double_clicked.emit(name)

        elif node_type == NODE_PROC:
            db = self.get_current_database()
            sql = f"SHOW CREATE PROCEDURE `{name}`"
            self.query_requested.emit(sql)

        elif node_type == NODE_FUNC:
            sql = f"SHOW CREATE FUNCTION `{name}`"
            self.query_requested.emit(sql)

        elif node_type == NODE_CONN:
            item.setExpanded(not item.isExpanded())

    # ==================== 右键菜单（P1-6） ====================

    def build_context_menu(self, item):
        """构建右键菜单，返回 (QMenu, {QAction: handler})。

        与 `_show_menu()` 分离：测试可直接检查菜单项文本并触发 handler，
        不需要阻塞式 `menu.exec()`。
        """
        data = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        handlers = {}
        if not data:
            return menu, handlers
        node_type, name = data
        meta = item.data(0, Qt.UserRole + 1) or {}
        db_name = meta.get('db', '')
        conn_name = meta.get('conn', '')

        def add(text, handler):
            act = menu.addAction(text)
            handlers[act] = handler
            return act

        def copy_text(value):
            cb = QApplication.clipboard()
            if cb is not None:
                cb.setText(str(value))

        if node_type == NODE_CONN:
            add("断开连接", lambda: self.remove_connection(name))
            add("刷新", lambda: self._reload_connection(item, name))
        elif node_type == NODE_DB:
            add("切换到此库", lambda: self._switch_database(name))
            add("新建库…", lambda: self._ui_create_database(item))
            add("删除库…", lambda: self._ui_drop_database(item, name))
            add("复制库名", lambda: copy_text(name))
            add("刷新", lambda: self._reload_db(item))
        elif node_type == NODE_TABLE_FOLDER:
            add("刷新", lambda: self._reload_table_folder(item))
            add("复制库名", lambda: copy_text(db_name))
        elif node_type == NODE_TABLE:
            add("打开表", lambda: self.table_double_clicked.emit(name))
            add("复制表名", lambda: copy_text(name))
            add("设计表", lambda: self._ui_design_table(item))
            add("重命名表…", lambda: self._ui_rename_table(item))
            add("导入数据…", lambda: self._ui_import_data(item))
            add("导出数据…", lambda: self._ui_export_data(item))
            menu.addSeparator()
            add("生成 SELECT LIMIT 200（新查询页）",
                lambda: self.new_query_requested.emit(
                    f"SELECT * FROM `{name}` LIMIT 200"))
            add("查看建表语句",
                lambda: self.query_requested.emit(
                    f"SHOW CREATE TABLE `{name}`"))
            add("查看列信息",
                lambda: self.query_requested.emit(
                    f"SHOW COLUMNS FROM `{name}`"))
            add("刷新此表", lambda: self._reload_table(item))
            add("统计行数",
                lambda: self.query_requested.emit(
                    f"SELECT COUNT(*) FROM `{name}`"))
            menu.addSeparator()
            add("复制结构…", lambda: self._ui_copy_structure(item))
            add("删除表", lambda: self._drop_table(item, name))
            add("清空表", lambda: self._truncate_table(name))
        elif node_type == NODE_COLUMN:
            add("复制列名", lambda: copy_text(name))
            add("查看列信息", lambda: self._show_column_info(item))
        elif node_type == NODE_PROC:
            add("查看定义",
                lambda: self.query_requested.emit(
                    f"SHOW CREATE PROCEDURE `{name}`"))
            add("删除存储过程", lambda: self._drop_procedure(name))
        elif node_type == NODE_FUNC:
            add("查看定义",
                lambda: self.query_requested.emit(
                    f"SHOW CREATE FUNCTION `{name}`"))
        elif node_type == NODE_TRIGGER:
            add("复制触发器名", lambda: copy_text(name))
            add("删除触发器", lambda: self._drop_trigger(name))
        elif node_type == NODE_EVENT:
            add("复制事件名", lambda: copy_text(name))
        return menu, handlers

    def _show_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        menu, handlers = self.build_context_menu(item)
        if menu.isEmpty():
            return
        action = menu.exec(self.tree.viewport().mapToGlobal(pos))
        handler = handlers.get(action)
        if handler:
            try:
                handler()
            except Exception as e:                    # noqa: BLE001
                QMessageBox.critical(self, "错误", str(e))

    # ---------- 菜单动作实现 ----------

    def _reload_connection(self, item, name):
        conn = self.connections.get(name)
        if not conn:
            return
        item.takeChildren()
        try:
            for db in MetadataLoader(conn).databases():
                self._add_db_node(item, name, db)
        except Exception:
            pass

    def _reload_db(self, item):
        self._load_db_children(item)
        item.setExpanded(True)

    def _reload_table_folder(self, item):
        db_item = item.parent()
        if db_item is not None:
            self._load_db_children(db_item)
            db_item.setExpanded(True)

    def clear_table_cache(self):
        """task-89：切库/关闭时释放表级元数据缓存。"""
        self._table_cache.clear()
        self._table_count = 0

    def _switch_database(self, db_name):
        conn = self.get_current_connection()
        if conn:
            try:
                conn.switch_database(db_name)
                self.clear_table_cache()
            except Exception as e:                    # noqa: BLE001
                QMessageBox.critical(self, "错误", str(e))

    def _reload_table(self, item):
        """刷新此表：强制丢弃缓存并重新加载列/索引/触发器。"""
        self._load_table_children(item, force=True)
        item.setExpanded(True)

    def _drop_table(self, item, name):
        ret = QMessageBox.question(
            self, "确认", f"确定要删除表 {name} 吗？此操作不可恢复！")
        if ret != QMessageBox.Yes:
            return
        conn = self.get_current_connection()
        if not conn:
            return
        _n, err = conn.execute(f"DROP TABLE `{name}`")
        if err:
            QMessageBox.critical(self, "错误", err)
            return
        self._table_cache.pop(self._table_key(item), None)
        parent = item.parent()
        if parent is not None and parent.parent() is not None:
            self._load_db_children(parent.parent())

    def _truncate_table(self, name):
        ret = QMessageBox.question(self, "确认", f"确定要清空表 {name} 吗？")
        if ret != QMessageBox.Yes:
            return
        conn = self.get_current_connection()
        if not conn:
            return
        _n, err = conn.execute(f"TRUNCATE TABLE `{name}`")
        if err:
            QMessageBox.critical(self, "错误", err)

    def _drop_procedure(self, name):
        ret = QMessageBox.question(self, "确认", f"删除存储过程 {name}?")
        if ret != QMessageBox.Yes:
            return
        conn = self.get_current_connection()
        if conn:
            _n, err = conn.execute(f"DROP PROCEDURE `{name}`")
            if err:
                QMessageBox.critical(self, "错误", err)

    def _drop_trigger(self, name):
        conn = self.get_current_connection()
        if conn:
            _n, err = conn.execute(f"DROP TRIGGER `{name}`")
            if err:
                QMessageBox.critical(self, "错误", err)

    # ==================== 过滤 ====================

    def _apply_filter(self, text):
        self._filter_text = text.strip().lower()
        # task-89：大树输入防抖（>200 表），避免每次按键都遍历/懒加载
        if self._table_count > 200 and self._filter_text:
            self._filter_timer.start()
            return
        self._filter_timer.stop()
        self._apply_filter_now()

    def flush_filter(self):
        """立即执行待处理的过滤（测试/用户按 Enter 时使用）。"""
        self._filter_timer.stop()
        self._apply_filter_now()

    def _apply_filter_now(self):
        # 即时过滤：有搜索词时先把尚未展开的库懒加载出来，再匹配表名
        if self._filter_text:
            try:
                for i in range(self.tree.topLevelItemCount()):
                    conn_item = self.tree.topLevelItem(i)
                    data = conn_item.data(0, Qt.UserRole)
                    if not data or data[0] != NODE_CONN:
                        continue
                    for j in range(conn_item.childCount()):
                        db_item = conn_item.child(j)
                        if db_item.childCount() == 1 and \
                                db_item.child(0).text(0) == _LOADING_TEXT:
                            self._load_db_children(db_item)
            except Exception as exc:                  # noqa: BLE001
                if hasattr(self, 'refresh_status_label'):
                    self.refresh_status_label.setText(f'过滤加载失败: {exc}')
                    self.refresh_status_label.setStyleSheet(
                        'color: #f48771;')
        self._filter_node(self.tree.invisibleRootItem())
        # 搜索时自动展开库/表文件夹，否则匹配项藏在折叠节点里看不见
        if self._filter_text:
            try:
                def expand(parent):
                    for i in range(parent.childCount()):
                        node = parent.child(i)
                        data = node.data(0, Qt.UserRole)
                        if data and data[0] in (NODE_DB, NODE_TABLE_FOLDER):
                            node.setExpanded(True)
                        expand(node)
                expand(self.tree.invisibleRootItem())
            except Exception:
                pass

    def _filter_node(self, parent):
        for i in range(parent.childCount()):
            child = parent.child(i)
            data = child.data(0, Qt.UserRole)
            if not data:
                continue
            node_type, name = data
            if node_type == NODE_TABLE:
                visible = (not self._filter_text or
                           self._filter_text in name.lower())
                child.setHidden(not visible)
            else:
                self._filter_node(child)

    # ==================== G2：拖拽生成 SQL ====================

    def drag_payload(self, item):
        """返回拖拽 payload：表→SELECT 模板/列→列名；其它节点 None。"""
        if item is None:
            return None
        data = item.data(0, Qt.UserRole)
        if not data:
            return None
        node_type, name = data
        meta = item.data(0, Qt.UserRole + 1) or {}
        if node_type == NODE_TABLE:
            return {
                'kind': 'table', 'table': name,
                'db': meta.get('db', ''), 'conn': meta.get('conn', ''),
                'text': f'SELECT * FROM `{name}` LIMIT 200;',
            }
        if node_type == NODE_COLUMN:
            table = meta.get('table', '')
            return {
                'kind': 'column', 'column': name, 'table': table,
                'db': meta.get('db', ''), 'conn': meta.get('conn', ''),
                'text': name,
                'insert_sql': f'`{table}`.`{name}`' if table else f'`{name}`',
            }
        return None

    # ==================== G1：库/表/列对象管理 ====================

    @staticmethod
    def _valid_ident(name) -> bool:
        return bool(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]{0,63}',
                                 str(name or '')))

    def _conn_for_meta(self, meta):
        conn = self.connections.get((meta or {}).get('conn'))
        return conn or self.get_current_connection()

    def _conn_root_for(self, conn_name):
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            data = item.data(0, Qt.UserRole)
            if data and data[0] == NODE_CONN and data[1] == conn_name:
                return item
        return None

    def _refresh_conn_root(self, conn_name):
        root = self._conn_root_for(conn_name)
        if root is not None:
            self._reload_connection(root, conn_name)

    def create_database(self, name, conn_name=None):
        """新建库；名称非法/执行失败显式返回错误，不假成功。"""
        conn = self.connections.get(conn_name) if conn_name \
            else self.get_current_connection()
        if conn is None:
            return {'ok': False, 'error': '未选择连接'}
        if not self._valid_ident(name):
            return {'ok': False,
                    'error': f'非法库名: {name!r}（仅字母/数字/下划线/$，字母开头）'}
        try:
            _n, err = conn.execute(f"CREATE DATABASE `{name}`")
        except Exception as exc:                      # noqa: BLE001
            return {'ok': False, 'error': f'新建库失败: {exc}'}
        if err:
            return {'ok': False, 'error': f'新建库失败: {err}'}
        self._refresh_conn_root(conn_name or getattr(
            conn.config, 'name', ''))
        return {'ok': True, 'database': name, 'error': ''}

    def drop_database(self, name, item=None, ask=True, confirmer=None):
        """删除库（二次确认含库名；失败显式）。"""
        meta = item.data(0, Qt.UserRole + 1) or {} if item else {}
        conn = self._conn_for_meta(meta)
        if conn is None:
            return {'ok': False, 'error': '未选择连接'}
        if not self._valid_ident(name):
            return {'ok': False, 'error': f'非法库名: {name!r}'}
        if ask:
            question = confirmer or (lambda title, text:
                                     QMessageBox.question(self, title, text))
            ret = question('确认删除数据库',
                           f'确定删除数据库 {name}？其中所有表将丢失！')
            if ret != QMessageBox.Yes:
                return {'ok': False, 'error': '用户取消', 'cancelled': True}
        try:
            _n, err = conn.execute(f"DROP DATABASE `{name}`")
        except Exception as exc:                      # noqa: BLE001
            return {'ok': False, 'error': f'删除库失败: {exc}'}
        if err:
            return {'ok': False, 'error': f'删除库失败: {err}'}
        conn_name = meta.get('conn') or getattr(conn.config, 'name', '')
        self._refresh_conn_root(conn_name)
        return {'ok': True, 'database': name, 'error': ''}

    def rename_table(self, item, new_name, ask=True, confirmer=None):
        """重命名表：ALTER TABLE ... RENAME TO ...（失败显式）。"""
        data = item.data(0, Qt.UserRole) if item is not None else None
        meta = item.data(0, Qt.UserRole + 1) or {} if item is not None else {}
        if not data or data[0] != NODE_TABLE:
            return {'ok': False, 'error': '不是表节点'}
        old_name = data[1]
        if not self._valid_ident(new_name):
            return {'ok': False,
                    'error': f'非法表名: {new_name!r}（仅字母/数字/下划线/$）'}
        if str(new_name) == str(old_name):
            return {'ok': False, 'error': '新表名与原名相同'}
        if ask:
            question = confirmer or (lambda title, text:
                                     QMessageBox.question(self, title, text))
            ret = question('重命名表',
                           f'将表 {old_name} 重命名为 {new_name}？')
            if ret != QMessageBox.Yes:
                return {'ok': False, 'error': '用户取消', 'cancelled': True}
        conn = self._conn_for_meta(meta)
        if conn is None:
            return {'ok': False, 'error': '未选择连接'}
        try:
            _n, err = conn.execute(
                f"ALTER TABLE `{old_name}` RENAME TO `{new_name}`")
        except Exception as exc:                      # noqa: BLE001
            return {'ok': False, 'error': f'重命名失败: {exc}'}
        if err:
            return {'ok': False, 'error': f'重命名失败: {err}'}
        parent = item.parent()
        try:
            if parent is not None and parent.parent() is not None:
                self._load_db_children(parent.parent())
            elif parent is not None:
                self._reload_table_folder(parent)
        except Exception:
            pass
        return {'ok': True, 'old': old_name, 'new': new_name, 'error': ''}

    def copy_table_structure(self, item, new_name, ask=False, confirmer=None):
        """复制表结构（CREATE TABLE ... LIKE）；引擎不支持时显式提示。"""
        data = item.data(0, Qt.UserRole) if item is not None else None
        meta = item.data(0, Qt.UserRole + 1) or {} if item is not None else {}
        if not data or data[0] != NODE_TABLE:
            return {'ok': False, 'error': '不是表节点'}
        source = data[1]
        if not self._valid_ident(new_name):
            return {'ok': False, 'error': f'非法表名: {new_name!r}'}
        if str(new_name) == str(source):
            return {'ok': False, 'error': '新表名与原名相同'}
        if ask:
            question = confirmer or (lambda title, text:
                                     QMessageBox.question(self, title, text))
            ret = question('复制表结构',
                           f'按 {source} 的结构新建表 {new_name}？')
            if ret != QMessageBox.Yes:
                return {'ok': False, 'error': '用户取消', 'cancelled': True}
        conn = self._conn_for_meta(meta)
        if conn is None:
            return {'ok': False, 'error': '未选择连接'}
        try:
            _n, err = conn.execute(
                f"CREATE TABLE `{new_name}` LIKE `{source}`")
        except Exception as exc:                      # noqa: BLE001
            return {'ok': False, 'engine_unsupported': True,
                    'error': f'引擎不支持复制表结构（CREATE TABLE ... LIKE）: {exc}'}
        if err:
            low = str(err).lower()
            unsupported = any(k in low for k in (
                'syntax', 'not support', 'unsupported', 'unknown',
                'near', 'denied', '1146', '1064'))
            prefix = ('引擎不支持复制表结构（CREATE TABLE ... LIKE）'
                      if unsupported else '复制表结构失败')
            return {'ok': False, 'engine_unsupported': unsupported,
                    'error': f'{prefix}: {err}'}
        parent = item.parent()
        try:
            if parent is not None and parent.parent() is not None:
                self._load_db_children(parent.parent())
        except Exception:
            pass
        return {'ok': True, 'source': source, 'new': new_name, 'error': ''}

    def design_table(self, item):
        data = item.data(0, Qt.UserRole) if item is not None else None
        meta = item.data(0, Qt.UserRole + 1) or {} if item is not None else {}
        if not data or data[0] != NODE_TABLE:
            return {'ok': False, 'error': '不是表节点'}
        conn = meta.get('conn', '')
        self.design_table_requested.emit(
            conn, meta.get('db', ''), data[1])
        return {'ok': True, 'conn': conn, 'db': meta.get('db', ''),
                'table': data[1], 'error': ''}

    def import_data(self, item):
        data = item.data(0, Qt.UserRole) if item is not None else None
        meta = item.data(0, Qt.UserRole + 1) or {} if item is not None else {}
        if not data or data[0] != NODE_TABLE:
            return {'ok': False, 'error': '不是表节点'}
        self.import_data_requested.emit(
            meta.get('conn', ''), meta.get('db', ''), data[1])
        return {'ok': True, 'table': data[1], 'error': ''}

    def export_data(self, item):
        data = item.data(0, Qt.UserRole) if item is not None else None
        meta = item.data(0, Qt.UserRole + 1) or {} if item is not None else {}
        if not data or data[0] != NODE_TABLE:
            return {'ok': False, 'error': '不是表节点'}
        self.export_data_requested.emit(
            meta.get('conn', ''), meta.get('db', ''), data[1])
        return {'ok': True, 'table': data[1], 'error': ''}

    def _show_column_info(self, item):
        data = item.data(0, Qt.UserRole) if item is not None else None
        meta = item.data(0, Qt.UserRole + 1) or {} if item is not None else {}
        if not data or data[0] != NODE_COLUMN:
            return None
        text = '\n'.join([
            f'列名: {data[1]}',
            f"表: {meta.get('db', '')}.{meta.get('table', '')}",
            f"类型: {meta.get('type', '')}",
            f"可空: {meta.get('null', '')}",
            f"键: {meta.get('key', '')}",
            f"默认值: {meta.get('default')}",
            f"额外: {meta.get('extra', '')}",
        ])
        QMessageBox.information(self, '列信息', text)
        return {'name': data[1], **meta, 'text': text}

    # ---------- UI 包装（对话框/确认） ----------

    def _ui_create_database(self, item):
        meta = item.data(0, Qt.UserRole + 1) or {}
        name, ok = QInputDialog.getText(self, '新建数据库', '库名:')
        if not ok:
            return None
        return self.create_database(name.strip(), meta.get('conn'))

    def _ui_drop_database(self, item, name):
        return self.drop_database(name, item=item, ask=True)

    def _ui_design_table(self, item):
        return self.design_table(item)

    def _ui_rename_table(self, item):
        new_name, ok = QInputDialog.getText(self, '重命名表', '新表名:')
        if not ok:
            return None
        return self.rename_table(item, new_name.strip(), ask=False)

    def _ui_import_data(self, item):
        return self.import_data(item)

    def _ui_export_data(self, item):
        return self.export_data(item)

    def _ui_copy_structure(self, item):
        new_name, ok = QInputDialog.getText(self, '复制表结构', '新表名:')
        if not ok:
            return None
        return self.copy_table_structure(item, new_name.strip(), ask=False)