"""数据库结构对比对话框"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QComboBox, QPushButton,
    QLabel, QTreeWidget, QTreeWidgetItem, QTextEdit, QSplitter,
    QMessageBox, QDialogButtonBox, QWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont

from ..db.schema_diff import diff_databases


class DBCompareDialog(QDialog):
    def __init__(self, connections: dict, parent=None):
        """
        connections: {conn_name: DBConnection}
        """
        super().__init__(parent)
        self.connections = connections
        self.diffs = {}
        self.setWindowTitle("数据库结构对比")
        self.resize(1100, 720)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 顶部选择
        top = QHBoxLayout()

        top.addWidget(QLabel("源连接:"))
        self.conn_a_combo = QComboBox()
        self.conn_a_combo.addItems(list(self.connections.keys()))
        self.conn_a_combo.currentTextChanged.connect(self._on_conn_a_changed)
        top.addWidget(self.conn_a_combo)

        top.addWidget(QLabel("源库:"))
        self.db_a_combo = QComboBox()
        top.addWidget(self.db_a_combo)

        top.addSpacing(20)

        top.addWidget(QLabel("目标连接:"))
        self.conn_b_combo = QComboBox()
        self.conn_b_combo.addItems(list(self.connections.keys()))
        self.conn_b_combo.currentTextChanged.connect(self._on_conn_b_changed)
        top.addWidget(self.conn_b_combo)

        top.addWidget(QLabel("目标库:"))
        self.db_b_combo = QComboBox()
        top.addWidget(self.db_b_combo)

        top.addStretch()

        self.compare_btn = QPushButton("🔍 开始对比")
        self.compare_btn.clicked.connect(self._do_compare)
        top.addWidget(self.compare_btn)

        layout.addLayout(top)

        # 主体：左树 + 右 SQL
        splitter = QSplitter(Qt.Horizontal)

        # 差异树
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['对象', '差异类型', '详情'])
        self.tree.setColumnWidth(0, 240)
        self.tree.setColumnWidth(1, 100)
        self.tree.itemClicked.connect(self._on_item_clicked)
        splitter.addWidget(self.tree)

        # SQL 预览
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        self.sql_view = QTextEdit()
        self.sql_view.setFont(QFont("Consolas", 10))
        self.sql_view.setStyleSheet(
            "background: #1e1e1e; color: #ce9178;")
        rl.addWidget(QLabel("差异 SQL:"))
        rl.addWidget(self.sql_view)

        # 汇总
        self.summary = QLabel("")
        self.summary.setStyleSheet(
            "background: #252526; color: #dcdcaa; padding: 6px;")
        rl.addWidget(self.summary)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        # 底部按钮
        bottom = QHBoxLayout()
        bottom.addStretch()

        self.sync_btn = QPushButton("⚡ 同步到目标")
        self.sync_btn.setEnabled(False)
        self.sync_btn.clicked.connect(self._sync)
        bottom.addWidget(self.sync_btn)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)

        # 初始化
        self._on_conn_a_changed(self.conn_a_combo.currentText())
        self._on_conn_b_changed(self.conn_b_combo.currentText())

    def _on_conn_a_changed(self, name):
        conn = self.connections.get(name)
        self.db_a_combo.clear()
        if conn:
            try:
                self.db_a_combo.addItems(conn.get_databases())
            except Exception as e:
                self.db_a_combo.addItem(f"<错误: {e}>")

    def _on_conn_b_changed(self, name):
        conn = self.connections.get(name)
        self.db_b_combo.clear()
        if conn:
            try:
                self.db_b_combo.addItems(conn.get_databases())
            except Exception as e:
                self.db_b_combo.addItem(f"<错误: {e}>")

    def _do_compare(self):
        conn_a = self.connections.get(self.conn_a_combo.currentText())
        conn_b = self.connections.get(self.conn_b_combo.currentText())
        db_a = self.db_a_combo.currentText()
        db_b = self.db_b_combo.currentText()

        if not conn_a or not conn_b or not db_a or not db_b:
            QMessageBox.warning(self, "提示", "请选择源和目标")
            return

        self.tree.clear()
        self.sql_view.clear()
        self.sync_btn.setEnabled(False)

        try:
            # 切换数据库
            conn_a.switch_database(db_a)
            conn_b.switch_database(db_b)

            tables_a = conn_a.get_tables()
            tables_b = conn_b.get_tables()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return

        try:
            self.diffs = diff_databases(conn_a, conn_b, tables_a, tables_b)
        except Exception as e:
            QMessageBox.critical(self, "对比失败", str(e))
            return

        n_added = 0
        n_removed = 0
        n_changed = 0

        for table, td in self.diffs.items():
            if td.columns_removed == ['__TABLE_NOT_IN_A__']:
                item = QTreeWidgetItem(self.tree)
                item.setText(0, f"表: {table}")
                item.setText(1, "DROP")
                item.setText(2, "目标中存在，源中不存在 → 删除")
                item.setForeground(1, QColor("#f48771"))
                item.setData(0, Qt.UserRole, (table, 'drop'))
                n_removed += 1
                continue

            if not td.is_empty:
                if td.columns_added and not td.columns_removed \
                        and not td.columns_changed:
                    # 新建表
                    item = QTreeWidgetItem(self.tree)
                    item.setText(0, f"表: {table}")
                    item.setText(1, "CREATE")
                    item.setText(2, f"新增 {len(td.columns_added)} 列")
                    item.setForeground(1, QColor("#89d185"))
                    item.setData(0, Qt.UserRole, (table, 'create'))
                    n_added += 1
                    continue

                # 修改
                parent = QTreeWidgetItem(self.tree)
                parent.setText(0, f"表: {table}")
                parent.setText(1, "ALTER")
                parent.setText(2, f"+{len(td.columns_added)} "
                                  f"-{len(td.columns_removed)} "
                                  f"~{len(td.columns_changed)}")
                parent.setForeground(1, QColor("#dcdcaa"))
                parent.setData(0, Qt.UserRole, (table, 'alter'))
                n_changed += 1

                for c in td.columns_added:
                    child = QTreeWidgetItem(parent)
                    child.setText(0, f"  + {c.name}")
                    child.setText(1, "ADD")
                    child.setText(2, c.sql_type())

                for name in td.columns_removed:
                    child = QTreeWidgetItem(parent)
                    child.setText(0, f"  - {name}")
                    child.setText(1, "DROP")

                for old, new in td.columns_changed:
                    child = QTreeWidgetItem(parent)
                    child.setText(0, f"  ~ {new.name}")
                    child.setText(1, "MODIFY")
                    child.setText(2, f"{old.sql_type()} → {new.sql_type()}")

        self.tree.expandAll()

        total = n_added + n_removed + n_changed
        self.summary.setText(
            f"差异汇总: {total} 张表 | 新增 {n_added} | 删除 {n_removed} | 修改 {n_changed}")

        if total == 0:
            self.sql_view.setPlainText("-- 两库结构一致，无差异")
        else:
            self.sync_btn.setEnabled(True)
            self._show_all_sql()

    def _show_all_sql(self):
        lines = ["-- 数据库结构同步 SQL",
                 f"-- 源: {self.conn_a_combo.currentText()}."
                 f"{self.db_a_combo.currentText()}",
                 f"-- 目标: {self.conn_b_combo.currentText()}."
                 f"{self.db_b_combo.currentText()}",
                 ""]
        for table, td in self.diffs.items():
            if td.columns_removed == ['__TABLE_NOT_IN_A__']:
                lines.append(f"DROP TABLE IF EXISTS `{table}`;")
                lines.append("")
                continue

            if td.is_empty and td.columns_added:
                # 新建表
                from ..db.table_ops import build_create_table
                cols = []
                for c in td.columns_added:
                    cols.append({
                        'name': c.name, 'type': c.type,
                        'length': c.length, 'primary': c.primary,
                        'notnull': c.notnull, 'unique': c.unique,
                        'auto_increment': c.auto_increment,
                        'default': c.default,
                    })
                lines.append(build_create_table(table, cols) + ';')
                lines.append("")
                continue

            for stmt in td.to_sql(table):
                lines.append(stmt)
            lines.append("")

        self.sql_view.setPlainText('\n'.join(lines))

    def _on_item_clicked(self, item, col):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        table, kind = data
        td = self.diffs.get(table)
        if not td:
            return

        if kind == 'drop':
            self.sql_view.setPlainText(f"DROP TABLE IF EXISTS `{table}`;")
        elif kind == 'create':
            from ..db.table_ops import build_create_table
            cols = [{
                'name': c.name, 'type': c.type, 'length': c.length,
                'primary': c.primary, 'notnull': c.notnull,
                'unique': c.unique, 'auto_increment': c.auto_increment,
                'default': c.default,
            } for c in td.columns_added]
            self.sql_view.setPlainText(build_create_table(table, cols) + ';')
        else:
            stmts = td.to_sql(table)
            self.sql_view.setPlainText('\n'.join(stmts))

    def _sync(self):
        ret = QMessageBox.question(
            self, "确认同步",
            "将会在**目标库**执行差异 SQL，此操作可能修改数据！\n"
            "请确保已备份。是否继续？")
        if ret != QMessageBox.Yes:
            return

        conn_b = self.connections.get(self.conn_b_combo.currentText())
        if not conn_b:
            return

        sql = self.sql_view.toPlainText().strip()
        if not sql or sql.startswith('-- 两库'):
            QMessageBox.information(self, "提示", "没有需要执行的 SQL")
            return

        stmts = [s.strip() for s in sql.split(';') if s.strip()
                 and not s.strip().startswith('--')]
        ok = 0
        errs = []
        for stmt in stmts:
            _, err = conn_b.execute(stmt)
            if err:
                errs.append(f"{stmt[:80]} → {err}")
            else:
                ok += 1

        if errs:
            QMessageBox.critical(
                self, "部分失败",
                f"成功 {ok}, 失败 {len(errs)}:\n" + '\n'.join(errs[:5]))
        else:
            QMessageBox.information(self, "同步成功", f"执行 {ok} 条 SQL")
            self.accept()