"""报表构建器"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QComboBox, QPushButton, QFormLayout, QGroupBox, QTableWidget,
    QTableWidgetItem, QDialogButtonBox, QMessageBox, QTabWidget,
    QWidget, QFileDialog, QListWidget, QListWidgetItem, QSplitter
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from ..core.reports import ReportTemplate, ReportEngine


class ReportBuilder(QDialog):
    def __init__(self, engine: ReportEngine, conn_getter=None,
                 current_sql: str = '', parent=None):
        super().__init__(parent)
        self.engine = engine
        self.conn_getter = conn_getter
        self.setWindowTitle("报表管理")
        self.resize(920, 680)
        self._build_ui()
        self._refresh_list()
        self.sql_edit.setPlainText(current_sql)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)

        # 左：模板列表
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        ll.addWidget(QLabel("📋 报表模板:"))
        self.list_widget = QListWidget()
        self.list_widget.itemSelectionChanged.connect(self._on_select)
        ll.addWidget(self.list_widget)

        row = QHBoxLayout()
        new_btn = QPushButton("➕ 新建")
        new_btn.clicked.connect(self._new_template)
        row.addWidget(new_btn)
        del_btn = QPushButton("🗑 删除")
        del_btn.clicked.connect(self._delete_template)
        row.addWidget(del_btn)
        ll.addLayout(row)

        splitter.addWidget(left)

        # 右：编辑器
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("报表名称:", self.name_edit)
        self.desc_edit = QLineEdit()
        form.addRow("描述:", self.desc_edit)
        self.conn_edit = QLineEdit()
        form.addRow("连接:", self.conn_edit)
        self.db_edit = QLineEdit()
        form.addRow("数据库:", self.db_edit)
        rl.addLayout(form)

        rl.addWidget(QLabel("SQL 查询:"))
        self.sql_edit = QTextEdit()
        self.sql_edit.setFont(QFont("Consolas", 10))
        self.sql_edit.setMinimumHeight(180)
        rl.addWidget(self.sql_edit)

        # 操作行
        btn_row = QHBoxLayout()
        save_btn = QPushButton("💾 保存")
        save_btn.clicked.connect(self._save_current)
        btn_row.addWidget(save_btn)

        preview_btn = QPushButton("👁 预览")
        preview_btn.clicked.connect(self._preview)
        btn_row.addWidget(preview_btn)

        run_btn = QPushButton("▶ 立即执行")
        run_btn.clicked.connect(self._run_now)
        btn_row.addWidget(run_btn)
        btn_row.addStretch()
        rl.addLayout(btn_row)

        # 导出区
        exp_group = QGroupBox("导出")
        eg = QHBoxLayout(exp_group)

        eg.addWidget(QLabel("格式:"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(['CSV', 'Excel', 'HTML'])
        eg.addWidget(self.format_combo)

        exp_btn = QPushButton("📤 立即导出")
        exp_btn.clicked.connect(self._export)
        eg.addWidget(exp_btn)
        eg.addStretch()
        rl.addWidget(exp_group)

        # 历史输出
        hist_group = QGroupBox("历史输出")
        hg = QVBoxLayout(hist_group)
        self.output_list = QListWidget()
        hg.addWidget(self.output_list)
        hg_row = QHBoxLayout()
        refresh_out_btn = QPushButton("🔄 刷新")
        refresh_out_btn.clicked.connect(self._refresh_outputs)
        hg_row.addWidget(refresh_out_btn)
        open_btn = QPushButton("📂 打开目录")
        open_btn.clicked.connect(self._open_output_dir)
        hg_row.addWidget(open_btn)
        hg_row.addStretch()
        hg.addLayout(hg_row)
        rl.addWidget(hist_group)

        splitter.addWidget(right)
        splitter.setSizes([280, 640])
        layout.addWidget(splitter)

        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.reject)
        layout.addWidget(close_btn)

        self._refresh_outputs()

    def _refresh_list(self):
        self.list_widget.clear()
        for t in self.engine.templates:
            item = QListWidgetItem(f"📊 {t.name}")
            item.setData(Qt.UserRole, t.name)
            if t.schedule:
                item.setText(f"📊 {t.name}  ⏰")
            self.list_widget.addItem(item)

    def _refresh_outputs(self):
        self.output_list.clear()
        for o in self.engine.list_outputs()[:50]:
            import time
            ts = time.strftime('%Y-%m-%d %H:%M:%S',
                               time.localtime(o['mtime']))
            size_kb = o['size'] / 1024
            item = QListWidgetItem(
                f"[{ts}] {o['name']} ({size_kb:.1f} KB)")
            item.setData(Qt.UserRole, o['path'])
            self.output_list.addItem(item)

    def _on_select(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        t = self.engine.get(name)
        if not t:
            return
        self.name_edit.setText(t.name)
        self.desc_edit.setText(t.description)
        self.conn_edit.setText(t.connection)
        self.db_edit.setText(t.database)
        self.sql_edit.setPlainText(t.sql)

    def _new_template(self):
        self.list_widget.clearSelection()
        self.name_edit.clear()
        self.desc_edit.clear()
        if self.conn_getter:
            conn = self.conn_getter()
            if conn:
                self.conn_edit.setText(conn.config.name)
                self.db_edit.setText(conn.config.database or '')
        self.sql_edit.clear()

    def _save_current(self):
        name = self.name_edit.text().strip()
        sql = self.sql_edit.toPlainText().strip()
        if not name or not sql:
            QMessageBox.warning(self, "提示", "请填写名称和 SQL")
            return

        # 找到现有或新建
        existing = self.engine.get(name)
        tmpl = ReportTemplate(
            name=name,
            description=self.desc_edit.text().strip(),
            sql=sql,
            connection=self.conn_edit.text().strip(),
            database=self.db_edit.text().strip(),
        )
        if existing:
            idx = self.engine.templates.index(existing)
            self.engine.update(idx, tmpl)
        else:
            self.engine.add(tmpl)
        QMessageBox.information(self, "完成", "报表已保存")
        self._refresh_list()

    def _delete_template(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        ret = QMessageBox.question(
            self, "确认", f"删除报表 '{name}'？")
        if ret == QMessageBox.Yes:
            existing = self.engine.get(name)
            if existing:
                idx = self.engine.templates.index(existing)
                self.engine.remove(idx)
                self._refresh_list()

    def _get_conn_for(self, name):
        if self.conn_getter is None:
            return None
        # 尝试按名字找
        conn = self.conn_getter()
        if conn and (not name or conn.config.name == name):
            return conn
        return conn

    def _preview(self):
        sql = self.sql_edit.toPlainText().strip()
        if not sql:
            return
        conn = self._get_conn_for(self.conn_edit.text().strip())
        if conn is None:
            QMessageBox.warning(self, "提示", "无可用连接")
            return
        try:
            cols, rows = conn.query(sql)
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("预览")
        dlg.resize(900, 520)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"共 {len(rows)} 行"))
        table = QTableWidget()
        table.setRowCount(min(len(rows), 200))
        table.setColumnCount(len(cols))
        table.setHorizontalHeaderLabels(cols)
        for r in range(min(len(rows), 200)):
            for c in range(len(cols)):
                v = rows[r][c] if c < len(rows[r]) else None
                table.setItem(r, c, QTableWidgetItem(
                    'NULL' if v is None else str(v)))
        table.resizeColumnsToContents()
        layout.addWidget(table)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)
        dlg.exec()

    def _run_now(self):
        self._export()

    def _export(self):
        sql = self.sql_edit.toPlainText().strip()
        if not sql:
            return
        conn = self._get_conn_for(self.conn_edit.text().strip())
        if conn is None:
            QMessageBox.warning(self, "提示", "无可用连接")
            return

        try:
            cols, rows = conn.query(sql)
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return

        fmt = self.format_combo.currentText()
        name = self.name_edit.text().strip() or 'report'
        import time
        ts = time.strftime('%Y%m%d_%H%M%S')

        if fmt == 'CSV':
            ext = 'csv'
        elif fmt == 'Excel':
            ext = 'xlsx'
        else:
            ext = 'html'

        path, _ = QFileDialog.getSaveFileName(
            self, "保存报表",
            str(self.engine.out_dir / f"{name}_{ts}.{ext}"),
            f"{fmt} (*.{ext})")
        if not path:
            return

        try:
            if fmt == 'CSV':
                self.engine.export_csv(cols, rows, path)
            elif fmt == 'Excel':
                self.engine.export_excel(cols, rows, path, title=name)
            else:
                self.engine.export_html(cols, rows, path, title=name)
            QMessageBox.information(self, "成功", f"已导出:\n{path}")
            self._refresh_outputs()
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))

    def _open_output_dir(self):
        import subprocess, os
        path = str(self.engine.out_dir)
        try:
            if os.name == 'nt':
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception:
            pass