"""数据库迁移向导"""
from PySide6.QtWidgets import (
    QWizard, QWizardPage, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QFileDialog, QComboBox, QTableWidget,
    QTableWidgetItem, QCheckBox, QSpinBox, QProgressBar, QMessageBox,
    QTextEdit, QGroupBox, QRadioButton, QButtonGroup, QFormLayout
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor

from ..core.migration import MySQLMigrator, SQLiteMigrator, TypeMapper


class MigrationWizard(QWizard):
    def __init__(self, target_conn_getter, parent=None):
        super().__init__(parent)
        self.target_conn_getter = target_conn_getter
        self.setWindowTitle("数据库迁移向导")
        self.resize(920, 680)

        self._migrator = None
        self._preview_data = None

        self._build_pages()
        self.currentIdChanged.connect(self._on_page)

    def _build_pages(self):
        # 页1：选择源
        p1 = QWizardPage()
        p1.setTitle("选择数据源")
        p1.setSubTitle("从 MySQL 或 SQLite 迁移到 LightDB")

        l1 = QVBoxLayout(p1)

        src_group = QGroupBox("数据源类型")
        sg = QHBoxLayout(src_group)
        self.src_group = QButtonGroup(p1)
        rb_mysql = QRadioButton("MySQL")
        rb_mysql.setChecked(True)
        rb_sqlite = QRadioButton("SQLite")
        self.src_group.addButton(rb_mysql, 0)
        self.src_group.addButton(rb_sqlite, 1)
        sg.addWidget(rb_mysql)
        sg.addWidget(rb_sqlite)
        sg.addStretch()
        l1.addWidget(src_group)

        # MySQL 配置
        self.mysql_group = QGroupBox("MySQL 连接")
        mg = QFormLayout(self.mysql_group)
        self.mysql_host = QLineEdit("127.0.0.1")
        mg.addRow("主机:", self.mysql_host)
        self.mysql_port = QSpinBox()
        self.mysql_port.setRange(1, 65535)
        self.mysql_port.setValue(3306)
        mg.addRow("端口:", self.mysql_port)
        self.mysql_user = QLineEdit("root")
        mg.addRow("用户名:", self.mysql_user)
        self.mysql_pass = QLineEdit()
        self.mysql_pass.setEchoMode(QLineEdit.Password)
        mg.addRow("密码:", self.mysql_pass)
        self.mysql_db = QLineEdit()
        mg.addRow("数据库:", self.mysql_db)
        l1.addWidget(self.mysql_group)

        # SQLite 配置
        self.sqlite_group = QGroupBox("SQLite 文件")
        sl = QHBoxLayout(self.sqlite_group)
        self.sqlite_path = QLineEdit()
        sl.addWidget(self.sqlite_path)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._browse_sqlite)
        sl.addWidget(browse_btn)
        self.sqlite_group.setVisible(False)
        l1.addWidget(self.sqlite_group)

        def on_src_change():
            idx = self.src_group.checkedId()
            self.mysql_group.setVisible(idx == 0)
            self.sqlite_group.setVisible(idx == 1)
        self.src_group.idClicked.connect(lambda _: on_src_change())

        l1.addStretch()
        self.addPage(p1)

        # 页2：预览
        p2 = QWizardPage()
        p2.setTitle("预览迁移")
        p2.setSubTitle("确认表结构与类型映射")

        l2 = QVBoxLayout(p2)
        self.preview_table = QTableWidget()
        self.preview_table.setColumnCount(3)
        self.preview_table.setHorizontalHeaderLabels(
            ['选择', '表名', '行数'])
        self.preview_table.setColumnWidth(0, 60)
        self.preview_table.setColumnWidth(1, 240)
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        l2.addWidget(self.preview_table)

        l2.addWidget(QLabel("类型映射预览:"))
        self.type_view = QTextEdit()
        self.type_view.setReadOnly(True)
        self.type_view.setFont(QFont("Consolas", 9))
        self.type_view.setMaximumHeight(120)
        l2.addWidget(self.type_view)

        self.addPage(p2)

        # 页3：选项
        p3 = QWizardPage()
        p3.setTitle("迁移选项")
        l3 = QVBoxLayout(p3)

        self.drop_check = QCheckBox("删除目标端已存在的同名表")
        self.drop_check.setChecked(False)
        l3.addWidget(self.drop_check)

        self.data_check = QCheckBox("复制数据（取消则只迁移结构）")
        self.data_check.setChecked(True)
        l3.addWidget(self.data_check)

        opt_form = QFormLayout()
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(50, 5000)
        self.batch_spin.setValue(500)
        self.batch_spin.setSingleStep(50)
        opt_form.addRow("批量大小:", self.batch_spin)
        l3.addLayout(opt_form)

        l3.addStretch()
        self.addPage(p3)

        # 页4：执行
        p4 = QWizardPage()
        p4.setTitle("执行迁移")
        l4 = QVBoxLayout(p4)

        self.progress_bar = QProgressBar()
        l4.addWidget(self.progress_bar)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 9))
        l4.addWidget(self.log_view)

        self.addPage(p4)

        # 完成时触发迁移
        self.finished.connect(self._on_finished)

    def _browse_sqlite(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 SQLite 文件", "", "SQLite (*.db *.sqlite *.sqlite3)")
        if path:
            self.sqlite_path.setText(path)

    def _on_page(self, page_id):
        if page_id == 1:  # 预览页
            self._build_preview()
        elif page_id == 3:  # 执行页
            self._execute_migration()

    def _build_preview(self):
        target = self.target_conn_getter()
        if target is None:
            QMessageBox.warning(self, "提示", "请先连接目标 LightDB")
            self.back()
            return

        src_idx = self.src_group.checkedId()

        try:
            if src_idx == 0:
                import pymysql
                src = pymysql.connect(
                    host=self.mysql_host.text(),
                    port=self.mysql_port.value(),
                    user=self.mysql_user.text(),
                    password=self.mysql_pass.text(),
                    database=self.mysql_db.text() or None,
                    charset='utf8mb4',
                )
                # 用类似的连接对象
                from ..db.connection import DBConnection
                from ..config import ConnectionInfo
                info = ConnectionInfo(
                    name='mysql_src',
                    host=self.mysql_host.text(),
                    port=self.mysql_port.value(),
                    user=self.mysql_user.text(),
                    password=self.mysql_pass.text(),
                    database=self.mysql_db.text())
                src_conn = DBConnection(info)
                src_conn.connect()
                self._migrator = MySQLMigrator(src_conn, target)
                preview = self._migrator.preview()
            else:
                self._migrator = SQLiteMigrator(
                    self.sqlite_path.text(), target)
                preview = self._migrator.preview()

            if not preview.get('ok'):
                QMessageBox.critical(
                    self, "错误", preview.get('error', '未知错误'))
                self.back()
                return

            self._preview_data = preview
            self._display_preview(preview)
        except Exception as e:
            QMessageBox.critical(self, "连接失败", str(e))
            self.back()

    def _display_preview(self, preview):
        tables = preview.get('tables', [])
        self.preview_table.setRowCount(len(tables))

        for r, t in enumerate(tables):
            chk = QCheckBox()
            chk.setChecked(True)
            self.preview_table.setCellWidget(r, 0, chk)
            self.preview_table.setItem(
                r, 1, QTableWidgetItem(t.get('table', '')))
            self.preview_table.setItem(
                r, 2, QTableWidgetItem(str(t.get('rows', 0))))

        # 类型映射示例
        lines = ["-- 类型映射 (前 3 张表):"]
        for t in tables[:3]:
            lines.append(f"\n[{t.get('table')}]")
            for c in t.get('columns', [])[:8]:
                old = c.get('old_type') or c.get('type', '')
                new = c.get('new_type', TypeMapper.map_type(old))
                lines.append(f"  {c['name']:20s} {old:20s} → {new}")

        self.type_view.setPlainText('\n'.join(lines))

    def _execute_migration(self):
        if self._migrator is None or self._preview_data is None:
            return

        # 收集选中的表
        tables = []
        for r in range(self.preview_table.rowCount()):
            w = self.preview_table.cellWidget(r, 0)
            if w and w.isChecked():
                name_item = self.preview_table.item(r, 1)
                if name_item:
                    tables.append(name_item.text())

        if not tables:
            QMessageBox.information(self, "提示", "没有选中任何表")
            return

        self.log_view.clear()
        self.progress_bar.setValue(0)

        def cb(stage, current, total, message):
            if total > 0:
                self.progress_bar.setValue(int(current / total * 100))
            self.log_view.append(
                f"[{stage}] {message}")
            self.log_view.verticalScrollBar().setValue(
                self.log_view.verticalScrollBar().maximum())

        try:
            if isinstance(self._migrator, MySQLMigrator):
                stats = self._migrator.migrate(
                    tables=tables,
                    create_tables=True,
                    copy_data=self.data_check.isChecked(),
                    drop_existing=self.drop_check.isChecked(),
                    batch_size=self.batch_spin.value(),
                    progress_cb=cb)
            else:
                stats = self._migrator.migrate(
                    tables=tables, progress_cb=cb)

            elapsed = stats.end_time - stats.start_time
            self.log_view.append(
                f"\n=== 迁移完成 ===\n"
                f"表: {stats.migrated_tables}/{stats.total_tables}\n"
                f"行: {stats.migrated_rows}\n"
                f"失败: {stats.failed_tables}\n"
                f"耗时: {elapsed:.2f}s")
            if stats.errors:
                self.log_view.append("\n错误:")
                for e in stats.errors[:10]:
                    self.log_view.append(f"  - {e}")

            QMessageBox.information(
                self, "完成",
                f"迁移完成！\n"
                f"成功 {stats.migrated_tables} 表，"
                f"{stats.migrated_rows} 行")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))
            self.log_view.append(f"❌ 迁移失败: {e}")

    def _on_finished(self, result):
        if result != QWizard.Accepted:
            return