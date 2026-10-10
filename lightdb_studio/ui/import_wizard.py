"""
导入向导 - 支持 Excel / JSON / SQL / CSV
- 字段映射
- 预览
- 批量导入
"""
import csv
import json
import os
from PySide6.QtWidgets import (
    QWizard, QWizardPage, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QFileDialog, QComboBox, QTableWidget,
    QTableWidgetItem, QCheckBox, QProgressDialog, QMessageBox,
    QGroupBox, QFormLayout, QRadioButton, QButtonGroup, QTextEdit
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont


class ImportWizard(QWizard):
    """导入向导"""

    def __init__(self, conn, table_name=None, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.table_name = table_name
        self.setWindowTitle("导入向导")
        self.resize(900, 660)

        self._file_path = ""
        self._headers = []
        self._rows = []
        self._file_type = ""

        self._build_pages()

    def _build_pages(self):
        # 页1：选择文件
        p1 = QWizardPage()
        p1.setTitle("选择数据文件")
        p1.setSubTitle("支持 CSV / Excel(*.xlsx) / JSON / SQL")
        l1 = QVBoxLayout(p1)

        row = QHBoxLayout()
        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        row.addWidget(self.file_edit)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._browse)
        row.addWidget(browse_btn)
        l1.addLayout(row)

        l1.addWidget(QLabel("选择格式:"))
        self.format_group = QButtonGroup(p1)
        fmt_row = QHBoxLayout()
        for text, ftype in [('CSV', 'csv'), ('Excel', 'excel'),
                            ('JSON', 'json'), ('SQL', 'sql')]:
            rb = QRadioButton(text)
            rb.setProperty('ftype', ftype)
            self.format_group.addButton(rb)
            fmt_row.addWidget(rb)
            if ftype == 'csv':
                rb.setChecked(True)
        fmt_row.addStretch()
        l1.addLayout(fmt_row)
        l1.addStretch()

        l1.addWidget(QLabel(
            "<b>说明:</b><br>"
            "• CSV: 逗号分隔，首行为表头<br>"
            "• Excel: 第一个工作表的表头+数据<br>"
            "• JSON: 数组格式 [{{...}}, {{...}}]<br>"
            "• SQL: INSERT 语句批量导入"))
        self.addPage(p1)

        # 页2：目标表
        p2 = QWizardPage()
        p2.setTitle("选择目标表")
        l2 = QVBoxLayout(p2)

        form = QFormLayout()
        self.table_combo = QComboBox()
        self.table_combo.setEditable(True)
        try:
            tables = conn.get_tables()
            self.table_combo.addItems(tables)
            if table_name and table_name in tables:
                self.table_combo.setCurrentText(table_name)
        except Exception:
            pass
        form.addRow("目标表:", self.table_combo)

        self.new_table_check = QCheckBox("若表不存在则创建")
        self.new_table_check.setChecked(True)
        form.addRow("", self.new_table_check)

        self.truncate_check = QCheckBox("导入前清空表")
        self.truncate_check.setChecked(False)
        form.addRow("", self.truncate_check)

        self.skip_header_check = QCheckBox("首行为表头（CSV）")
        self.skip_header_check.setChecked(True)
        form.addRow("", self.skip_header_check)

        l2.addLayout(form)
        l2.addStretch()
        self.addPage(p2)

        # 页3：预览
        p3 = QWizardPage()
        p3.setTitle("预览数据")
        l3 = QVBoxLayout(p3)

        self.preview_info = QLabel("")
        l3.addWidget(self.preview_info)

        self.preview_table = QTableWidget()
        l3.addWidget(self.preview_table)

        self.sql_preview = QTextEdit()
        self.sql_preview.setReadOnly(True)
        self.sql_preview.setFont(QFont("Consolas", 9))
        self.sql_preview.setMaximumHeight(100)
        self.sql_preview.setStyleSheet("background: #1e1e1e; color: #ce9178;")
        l3.addWidget(QLabel("将执行的 SQL 预览:"))
        l3.addWidget(self.sql_preview)

        self.addPage(p3)

        # 在向导完成时触发导入
        self.currentIdChanged.connect(self._on_page_changed)
        self.finished.connect(self._on_finished)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择文件", "",
            "所有支持 (*.csv *.xlsx *.json *.sql);;"
            "CSV (*.csv);;Excel (*.xlsx);;JSON (*.json);;SQL (*.sql)")
        if not path:
            return
        self._file_path = path
        self.file_edit.setText(path)

        # 自动选择格式
        ext = os.path.splitext(path)[1].lower()
        mapping = {'.csv': 'csv', '.xlsx': 'excel',
                   '.json': 'json', '.sql': 'sql'}
        ftype = mapping.get(ext)
        if ftype:
            for rb in self.format_group.buttons():
                if rb.property('ftype') == ftype:
                    rb.setChecked(True)
                    break

    def _load_file(self):
        """根据选择的格式读取文件"""
        # 找当前选中的格式
        ftype = 'csv'
        for rb in self.format_group.buttons():
            if rb.isChecked():
                ftype = rb.property('ftype')
                break

        self._file_type = ftype
        self._headers = []
        self._rows = []

        try:
            if ftype == 'csv':
                self._load_csv()
            elif ftype == 'excel':
                self._load_excel()
            elif ftype == 'json':
                self._load_json()
            elif ftype == 'sql':
                self._load_sql()
        except Exception as e:
            QMessageBox.critical(self, "加载失败", str(e))

    def _load_csv(self):
        try:
            with open(self._file_path, 'r', encoding='utf-8', newline='') as f:
                all_rows = list(csv.reader(f))
        except UnicodeDecodeError:
            with open(self._file_path, 'r', encoding='gbk', newline='') as f:
                all_rows = list(csv.reader(f))

        if not all_rows:
            return

        if self.skip_header_check.isChecked():
            self._headers = all_rows[0]
            self._rows = all_rows[1:]
        else:
            self._headers = [f"col{i+1}" for i in range(len(all_rows[0]))]
            self._rows = all_rows

    def _load_excel(self):
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise RuntimeError(
                "需要安装 openpyxl: pip install openpyxl")

        wb = load_workbook(self._file_path, read_only=True, data_only=True)
        ws = wb.active

        all_rows = []
        for row in ws.iter_rows(values_only=True):
            all_rows.append(list(row))
        wb.close()

        if not all_rows:
            return

        if self.skip_header_check.isChecked():
            self._headers = [str(c) if c is not None else f"col{i+1}"
                             for i, c in enumerate(all_rows[0])]
            self._rows = all_rows[1:]
        else:
            self._headers = [f"col{i+1}" for i in range(len(all_rows[0]))]
            self._rows = all_rows

    def _load_json(self):
        with open(self._file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not data:
            raise ValueError("JSON 必须是对象数组")

        # 收集所有键
        keys = []
        for item in data:
            if isinstance(item, dict):
                for k in item.keys():
                    if k not in keys:
                        keys.append(k)

        self._headers = keys
        self._rows = []
        for item in data:
            if not isinstance(item, dict):
                continue
            row = []
            for k in keys:
                v = item.get(k)
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                row.append(v)
            self._rows.append(row)

    def _load_sql(self):
        with open(self._file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # 直接使用 SQL（后续解析）
        self._headers = ['-- SQL 文件 --']
        self._rows = [[content]]
        return

    def _on_page_changed(self, page_id):
        if page_id == 2:  # 第三页
            # 加载文件 + 显示预览
            if not self._file_path:
                QMessageBox.warning(self, "提示", "请先选择文件")
                self.back()
                return
            self._load_file()
            self._update_preview()
        elif page_id == 3:  # 第四页（如果有）
            self._update_preview()

    def _update_preview(self):
        if not self._rows:
            self.preview_table.clear()
            self.preview_table.setRowCount(0)
            self.preview_info.setText("无数据")
            return

        # SQL 文件特殊处理
        if self._file_type == 'sql':
            content = self._rows[0][0]
            self.preview_info.setText(
                f"SQL 文件，{len(content)} 字符，"
                f"{content.count(';')} 条语句")
            self.preview_table.setRowCount(1)
            self.preview_table.setColumnCount(1)
            self.preview_table.setHorizontalHeaderLabels(['内容预览'])
            self.preview_table.setItem(
                0, 0, QTableWidgetItem(content[:500]))
            self.sql_preview.setPlainText(
                "-- 将直接执行文件中的所有 SQL 语句")
            return

        n = min(20, len(self._rows))
        cols = len(self._headers) if self._headers else 0
        self.preview_table.setColumnCount(cols)
        self.preview_table.setHorizontalHeaderLabels(
            [str(h) for h in self._headers])
        self.preview_table.setRowCount(n)

        for r in range(n):
            for c in range(cols):
                v = self._rows[r][c] if c < len(self._rows[r]) else ''
                self.preview_table.setItem(r, c, QTableWidgetItem(
                    '' if v is None else str(v)))
        self.preview_table.resizeColumnsToContents()

        table = self.table_combo.currentText()
        self.preview_info.setText(
            f"文件: {len(self._rows)} 行 × {cols} 列 | "
            f"目标表: {table} | 预览前 {n} 行")

        # SQL 预览
        if self._headers and len(self._headers) > 1:
            cols_str = ', '.join(f"`{h}`" for h in self._headers)
            self.sql_preview.setPlainText(
                f"INSERT INTO `{table}` ({cols_str}) VALUES (...);\n"
                f"* 共 {len(self._rows)} 条 INSERT")

    def _on_finished(self, result):
        if result != QWizard.Accepted:
            return
        # 执行导入
        self._execute_import()

    def _execute_import(self):
        table_name = self.table_combo.currentText().strip()
        if not table_name:
            QMessageBox.warning(self, "提示", "请填写目标表名")
            return

        # SQL 文件模式
        if self._file_type == 'sql':
            self._execute_sql_file()
            return

        if not self._headers or not self._rows:
            QMessageBox.warning(self, "提示", "没有可导入的数据")
            return

        # 检查表是否存在
        try:
            existing_tables = self.conn.get_tables()
        except Exception:
            existing_tables = []

        if table_name not in existing_tables:
            if self.new_table_check.isChecked():
                if not self._create_table_from_headers(table_name):
                    return
            else:
                QMessageBox.warning(self, "提示",
                                    f"表 {table_name} 不存在")
                return

        if self.truncate_check.isChecked():
            ret = QMessageBox.question(
                self, "确认", f"确定要清空表 {table_name} 吗？")
            if ret != QMessageBox.Yes:
                return
            _, err = self.conn.execute(f"TRUNCATE TABLE `{table_name}`")
            if err:
                QMessageBox.critical(self, "错误", err)
                return

        # 导入
        cols_str = ', '.join(f"`{h}`" for h in self._headers)
        placeholders = ', '.join(['%s'] * len(self._headers))
        sql = f"INSERT INTO `{table_name}` ({cols_str}) VALUES ({placeholders})"

        params_list = []
        for row in self._rows:
            params = []
            for i in range(len(self._headers)):
                v = row[i] if i < len(row) else None
                if v == '':
                    v = None
                params.append(v)
            params_list.append(tuple(params))

        progress = QProgressDialog("导入中...", "取消",
                                   0, len(params_list), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        ok = 0
        errors = []
        batch = 500
        try:
            for i in range(0, len(params_list), batch):
                if progress.wasCanceled():
                    break
                chunk = params_list[i:i + batch]
                try:
                    self.conn.execute_many(sql, chunk)
                    ok += len(chunk)
                except Exception as e:
                    errors.append(str(e))
                progress.setValue(min(i + batch, len(params_list)))
        finally:
            progress.close()

        QMessageBox.information(
            self, "完成",
            f"成功 {ok} 行，失败 {len(params_list) - ok} 行"
            + (f"\n首个错误: {errors[0]}" if errors else ""))

    def _create_table_from_headers(self, table_name):
        """根据表头自动创建表"""
        cols = []
        for i, h in enumerate(self._headers):
            # 猜测类型
            col_type = 'VARCHAR'
            length = '255'
            for r in self._rows[:20]:
                v = r[i] if i < len(r) else None
                if v is None or v == '':
                    continue
                if isinstance(v, bool):
                    col_type, length = 'BOOLEAN', ''
                    break
                if isinstance(v, int):
                    col_type, length = 'INT', ''
                    break
                if isinstance(v, float):
                    col_type, length = 'DOUBLE', ''
                    break
                # 字符串
                col_type, length = 'VARCHAR', str(min(max(len(str(v)) * 2, 50), 4000))
                break

            cols.append({
                'name': h,
                'type': col_type,
                'length': length,
                'primary': (i == 0),
                'notnull': False,
                'unique': False,
                'auto_increment': False,
                'default': '',
            })

        from ..db.table_ops import build_create_table
        sql = build_create_table(table_name, cols)
        _, err = self.conn.execute(sql)
        if err:
            QMessageBox.critical(self, "建表失败", err)
            return False
        return True

    def _execute_sql_file(self):
        if not self._rows:
            return
        content = self._rows[0][0]
        stmts = [s.strip() for s in content.split(';') if s.strip()]
        ok = 0
        errors = []
        for s in stmts:
            _, err = self.conn.execute(s)
            if err:
                errors.append(f"{s[:60]}... → {err}")
            else:
                ok += 1
        QMessageBox.information(
            self, "完成",
            f"执行 {ok} 条 SQL，失败 {len(errors)} 条"
            + (f"\n首个错误: {errors[0]}" if errors else ""))