"""左侧对象浏览器：连接 / 数据库 / 表"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QMenu, QMessageBox, QInputDialog, QToolBar, QLineEdit,
    QPushButton, QHBoxLayout, QLabel
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
NODE_PROC_FOLDER = 'procedures'
NODE_PROC = 'procedure'
NODE_FUNC_FOLDER = 'functions'
NODE_FUNC = 'function'
NODE_TRIGGER_FOLDER = 'triggers'
NODE_TRIGGER = 'trigger'
NODE_EVENT_FOLDER = 'events'
NODE_EVENT = 'event'


class ObjectTree(QWidget):
    """对象浏览器"""

    table_double_clicked = Signal(str)       # table_name
    query_requested = Signal(str)            # sql
    connection_selected = Signal(object)     # DBConnection

    def __init__(self, parent=None):
        super().__init__(parent)
        self.connections = {}       # conn_name -> DBConnection
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
        """重建整棵树"""
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
                titem = QTreeWidgetItem(tables_folder)
                titem.setText(0, f"📋 {t}")
                titem.setData(0, Qt.UserRole, (NODE_TABLE, t))
                titem.setData(0, Qt.UserRole + 1,
                              {'conn': conn_name, 'db': db_name})
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

    # ==================== 事件 ====================

    def _on_expand(self, item):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == NODE_DB:
            # 检查是否需要加载
            if item.childCount() == 1 and \
               item.child(0).text(0) == "加载中...":
                self._load_db_children(item)

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

    def _show_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        node_type, name = data

        menu = QMenu(self)

        if node_type == NODE_CONN:
            act_disconnect = menu.addAction("断开连接")
            act_refresh = menu.addAction("刷新")
            action = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if action == act_disconnect:
                self.remove_connection(name)
            elif action == act_refresh:
                conn = self.connections.get(name)
                if conn:
                    item.takeChildren()
                    try:
                        loader = MetadataLoader(conn)
                        for db in loader.databases():
                            self._add_db_node(item, name, db)
                    except Exception:
                        pass

        elif node_type == NODE_DB:
            act_use = menu.addAction("切换到此库")
            act_refresh = menu.addAction("刷新")
            action = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if action == act_use:
                conn = self.get_current_connection()
                if conn:
                    try:
                        conn.switch_database(name)
                    except Exception as e:
                        QMessageBox.critical(self, "错误", str(e))
            elif action == act_refresh:
                self._load_db_children(item)
                item.setExpanded(True)

        elif node_type == NODE_TABLE:
            act_select = menu.addAction("查询数据 (SELECT)")
            act_count = menu.addAction("统计行数")
            menu.addSeparator()
            act_show = menu.addAction("查看建表语句")
            act_cols = menu.addAction("查看列信息")
            menu.addSeparator()
            act_drop = menu.addAction("删除表")
            act_truncate = menu.addAction("清空表")

            action = menu.exec(self.tree.viewport().mapToGlobal(pos))

            if action == act_select:
                self.query_requested.emit(f"SELECT * FROM `{name}` LIMIT 1000")
            elif action == act_count:
                self.query_requested.emit(f"SELECT COUNT(*) FROM `{name}`")
            elif action == act_show:
                self.query_requested.emit(f"SHOW CREATE TABLE `{name}`")
            elif action == act_cols:
                self.query_requested.emit(f"SHOW COLUMNS FROM `{name}`")
            elif action == act_drop:
                ret = QMessageBox.question(self, "确认",
                                           f"确定要删除表 {name} 吗？此操作不可恢复！")
                if ret == QMessageBox.Yes:
                    conn = self.get_current_connection()
                    if conn:
                        _, err = conn.execute(f"DROP TABLE `{name}`")
                        if err:
                            QMessageBox.critical(self, "错误", err)
                        else:
                            self._load_db_children(item.parent().parent())
            elif action == act_truncate:
                ret = QMessageBox.question(self, "确认",
                                           f"确定要清空表 {name} 吗？")
                if ret == QMessageBox.Yes:
                    conn = self.get_current_connection()
                    if conn:
                        _, err = conn.execute(f"TRUNCATE TABLE `{name}`")
                        if err:
                            QMessageBox.critical(self, "错误", err)

        elif node_type == NODE_PROC:
            act_show = menu.addAction("查看定义")
            act_drop = menu.addAction("删除存储过程")
            action = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if action == act_show:
                self.query_requested.emit(f"SHOW CREATE PROCEDURE `{name}`")
            elif action == act_drop:
                ret = QMessageBox.question(self, "确认", f"删除存储过程 {name}?")
                if ret == QMessageBox.Yes:
                    conn = self.get_current_connection()
                    if conn:
                        _, err = conn.execute(f"DROP PROCEDURE `{name}`")
                        if err:
                            QMessageBox.critical(self, "错误", err)

        elif node_type == NODE_FUNC:
            act_show = menu.addAction("查看定义")
            action = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if action == act_show:
                self.query_requested.emit(f"SHOW CREATE FUNCTION `{name}`")

        elif node_type == NODE_TRIGGER:
            act_drop = menu.addAction("删除触发器")
            action = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if action == act_drop:
                conn = self.get_current_connection()
                if conn:
                    _, err = conn.execute(f"DROP TRIGGER `{name}`")
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