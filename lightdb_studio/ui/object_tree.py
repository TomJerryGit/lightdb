"""左侧对象浏览器：连接 / 数据库 / 表（P1-6：懒加载 + 右键菜单）"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QMenu, QMessageBox, QInputDialog, QToolBar, QLineEdit,
    QPushButton, QHBoxLayout, QLabel, QApplication
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QIcon, QFont, QColor

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

# 表节点占位子项文本（说明"展开时才加载"）
_LOADING_TEXT = "加载中..."


class ObjectTree(QWidget):
    """对象浏览器"""

    table_double_clicked = Signal(str)       # table_name
    query_requested = Signal(str)            # sql（当前编辑器执行）
    new_query_requested = Signal(str)        # sql（新查询页，不自动执行）
    connection_selected = Signal(object)     # DBConnection

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

        layout.addWidget(bar)

        # 树
        self.tree = QTreeWidget()
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
        """重建整棵树（同时清空表级懒加载缓存）"""
        self._table_cache.clear()
        self.tree.clear()
        for name, conn in list(self.connections.items()):
            info = conn.config
            self.add_connection(info, conn)

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

        # 表文件夹
        tables_folder = QTreeWidgetItem(item)
        tables_folder.setText(0, "📁 表")
        tables_folder.setData(0, Qt.UserRole, (NODE_TABLE_FOLDER, 'tables'))
        tables_folder.setData(0, Qt.UserRole + 1, {'conn': conn_name, 'db': db_name})

        try:
            for t in loader.tables():
                self._add_table_node(tables_folder, conn_name, db_name, t)
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

    # ==================== 表级懒加载（P1-6） ====================

    def _add_table_node(self, parent: QTreeWidgetItem, conn_name: str,
                        db_name: str, table: str) -> QTreeWidgetItem:
        """表节点：只挂一个"加载中..."占位子项，展开时才查列/索引/触发器。"""
        titem = QTreeWidgetItem(parent)
        titem.setText(0, f"📋 {table}")
        titem.setData(0, Qt.UserRole, (NODE_TABLE, table))
        titem.setData(0, Qt.UserRole + 1,
                      {'conn': conn_name, 'db': db_name})
        QTreeWidgetItem(titem).setText(0, _LOADING_TEXT)
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
            # P1-6：展开表才加载列/索引/触发器（缓存命中不重复查）
            if item.childCount() == 1 and \
               item.child(0).text(0) == _LOADING_TEXT:
                self._load_table_children(item)

    def _on_double_click(self, item, col):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        node_type, name = data

        if node_type == NODE_TABLE:
            # 双击表 → 打开数据（Batch 2 实现数据编辑器；这里先弹查询）
            db = self.get_current_database()
            sql = f"SELECT * FROM `{name}` LIMIT 1000"
            self.query_requested.emit(sql)

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
            add("复制库名", lambda: copy_text(name))
            add("刷新", lambda: self._reload_db(item))
        elif node_type == NODE_TABLE_FOLDER:
            add("刷新", lambda: self._reload_table_folder(item))
            add("复制库名", lambda: copy_text(db_name))
        elif node_type == NODE_TABLE:
            add("复制表名", lambda: copy_text(name))
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
            add("删除表", lambda: self._drop_table(item, name))
            add("清空表", lambda: self._truncate_table(name))
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

    def _switch_database(self, db_name):
        conn = self.get_current_connection()
        if conn:
            try:
                conn.switch_database(db_name)
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
        self._filter_node(self.tree.invisibleRootItem())

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