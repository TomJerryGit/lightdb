"""导入导出对话框"""
import csv
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QFileDialog, QMessageBox, QProgressDialog, QDialogButtonBox,
    QCheckBox, QTableWidget, QTableWidgetItem
)
from PySide6.QtCore import Qt


class ImportCSVDialog(QDialog):
    """CSV 导入到表"""

    def __init__(self, conn, table_name, columns, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.table_name = table_name
        self.columns = columns
        self.csv_path = ""
        self.headers = []
        self.rows = []
        self.setWindowTitle(f"导入 CSV → {table_name}")
        self.resize(760, 560)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 文件行
        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("CSV 文件:"))
        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        file_row.addWidget(self.file_edit)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._browse)
        file_row.addWidget(browse_btn)
        layout.addLayout(file_row)

        # 选项
        opt_row = QHBoxLayout()
        self.skip_header = QCheckBox("首行是表头")
        self.skip_header.setChecked(True)
        self.skip_header.stateChanged.connect(self._reload_preview)
        opt_row.addWidget(self.skip_header)

        self.truncate = QCheckBox("导入前清空表")
        opt_row.addWidget(self.truncate)
        opt_row.addStretch()
        layout.addLayout(opt_row)

        # 列映射信息
        self.map_label = QLabel("")
        self.map_label.setStyleSheet("color: #969696;")
        layout.addWidget(self.map_label)

        # 预览
        layout.addWidget(QLabel("预览（前 20 行）:"))
        self.preview = QTableWidget()
        layout.addWidget(self.preview)

        # 按钮
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("开始导入")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._do_import)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 CSV 文件", "",
            "CSV 文件 (*.csv);;所有文件 (*.*)")
        if not path:
            return
        self.csv_path = path
        self.file_edit.setText(path)
        self._load_csv()
        self._reload_preview()

    def _load_csv(self):
        self.headers = []
        self.rows = []
        if not self.csv_path:
            return
        try:
            try:
                with open(self.csv_path, 'r', encoding='utf-8', newline='') as f:
                    all_rows = list(csv.reader(f))
            except UnicodeDecodeError:
                with open(self.csv_path, 'r', encoding='gbk', newline='') as f:
                    all_rows = list(csv.reader(f))

            if not all_rows:
                return

            if self.skip_header.isChecked():
                self.headers = all_rows[0]
                self.rows = all_rows[1:]
            else:
                self.headers = [f"col{i+1}" for i in range(len(all_rows[0]))]
                self.rows = all_rows
        except Exception as e:
            QMessageBox.critical(self, "读取失败", str(e))

    def _reload_preview(self):
        if not self.rows:
            self.preview.clear()
            self.map_label.setText("")
            return

        n = min(20, len(self.rows))
        cols = len(self.rows[0]) if self.rows else 0
        self.preview.setColumnCount(cols)
        self.preview.setHorizontalHeaderLabels(
            self.headers or [f"col{i+1}" for i in range(cols)])
        self.preview.setRowCount(n)
        for ri in range(n):
            for ci in range(cols):
                v = self.rows[ri][ci] if ci < len(self.rows[ri]) else ''
                self.preview.setItem(ri, ci, QTableWidgetItem(str(v)))
        self.preview.resizeColumnsToContents()

        self.map_label.setText(
            f"CSV 有 {cols} 列 | 表有 {len(self.columns)} 列 | "
            f"共 {len(self.rows)} 行待导入")

    def _do_import(self):
        if not self.rows:
            QMessageBox.warning(self, "提示", "没有可导入的数据")
            return

        n_cols = len(self.columns)
        csv_cols = len(self.rows[0]) if self.rows else 0

        if csv_cols != n_cols:
            ret = QMessageBox.question(
                self, "列数不符",
                f"CSV 有 {csv_cols} 列，表有 {n_cols} 列。\n"
                f"将按顺序映射，多余列忽略，缺失列填 NULL。是否继续？")
            if ret != QMessageBox.Yes:
                return

        if self.truncate.isChecked():
            ret = QMessageBox.question(
                self, "确认", f"确定要清空表 {self.table_name} 吗？")
            if ret != QMessageBox.Yes:
                return
            _, err = self.conn.execute(f"TRUNCATE TABLE `{self.table_name}`")
            if err:
                QMessageBox.critical(self, "错误", err)
                return

        cols_str = ', '.join(f"`{c}`" for c in self.columns)
        placeholders = ', '.join(['%s'] * n_cols)
        sql = (f"INSERT INTO `{self.table_name}` ({cols_str}) "
               f"VALUES ({placeholders})")

        params_list = []
        for row in self.rows:
            params = []
            for i in range(n_cols):
                v = row[i] if i < len(row) else None
                params.append(v if v != '' else None)
            params_list.append(tuple(params))

        progress = QProgressDialog("导入中...", "取消",
                                   0, len(params_list), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        ok = 0
        errors = []
        batch_size = 500
        try:
            for i in range(0, len(params_list), batch_size):
                if progress.wasCanceled():
                    break
                batch = params_list[i:i + batch_size]
                try:
                    self.conn.execute_many(sql, batch)
                    ok += len(batch)
                except Exception as e:
                    errors.append(str(e))
                progress.setValue(min(i + batch_size, len(params_list)))
        finally:
            progress.close()

        QMessageBox.information(
            self, "完成",
            f"成功 {ok} 行，失败 {len(params_list) - ok} 行"
            + (f"\n首个错误: {errors[0]}" if errors else ""))
        self.accept()


class ExportCSVDialog(QDialog):
    """导出结果/表到 CSV"""

    def __init__(self, columns, rows, default_name="export.csv", parent=None):
        super().__init__(parent)
        self.columns = columns
        self.rows = rows
        self.setWindowTitle("导出 CSV")
        self.resize(560, 240)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"共 {len(rows)} 行，{len(columns)} 列"))

        row = QHBoxLayout()
        row.addWidget(QLabel("保存到:"))
        self.path_edit = QLineEdit(default_name)
        row.addWidget(self.path_edit)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._browse)
        row.addWidget(browse_btn)
        layout.addLayout(row)

        self.with_header = QCheckBox("包含表头")
        self.with_header.setChecked(True)
        layout.addWidget(self.with_header)

        self.null_as_empty = QCheckBox("NULL 导出为空字符串")
        self.null_as_empty.setChecked(True)
        layout.addWidget(self.null_as_empty)

        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("导出")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._do_export)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存 CSV", self.path_edit.text(),
            "CSV 文件 (*.csv)")
        if path:
            self.path_edit.setText(path)

    def _do_export(self):
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "提示", "请填写保存路径")
            return
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                if self.with_header.isChecked():
                    writer.writerow(self.columns)
                null_as = '' if self.null_as_empty.isChecked() else 'NULL'
                for row in self.rows:
                    writer.writerow([null_as if v is None else v for v in row])
            QMessageBox.information(
                self, "完成", f"已导出 {len(self.rows)} 行到:\n{path}")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))