"""表设计器（task-47：AUTO_INCREMENT UI 对齐真实引擎能力）。

规则（与引擎行为一致）：
- AUTO_INCREMENT 只允许整数列（INT/BIGINT/SMALLINT/TINYINT/MEDIUMINT），
  且该列必须是主键或唯一键；否则复选框禁用 + tooltip 说明；
- 合法勾选 → `build_create_table` 生成带 `AUTO_INCREMENT` 的 CREATE TABLE；
- 编辑已有表：读取列元数据回显（含自增勾选），并展示
  `SHOW CREATE TABLE` / `SHOW TABLE STATUS.Auto_increment` 信息（只读回显，
  不静默假装保存了 ALTER）。
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QComboBox, QPushButton, QLineEdit, QCheckBox, QLabel, QMessageBox,
    QDialogButtonBox, QTextEdit, QAbstractItemView, QWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from ..db.table_ops import build_create_table
from ..db.metadata import MetadataLoader


TYPE_OPTIONS = ['INT', 'BIGINT', 'SMALLINT', 'TINYINT', 'MEDIUMINT',
                'VARCHAR', 'TEXT', 'DATETIME', 'DATE', 'DECIMAL', 'FLOAT',
                'DOUBLE', 'BOOLEAN', 'JSON']
INTEGER_TYPES = {'INT', 'BIGINT', 'SMALLINT', 'TINYINT', 'MEDIUMINT'}

COL_NAME, COL_TYPE, COL_LENGTH, COL_PK, COL_NN, COL_UNIQUE, COL_AUTO, \
    COL_DEFAULT = 0, 1, 2, 3, 4, 5, 6, 7


def _split_type(full_type):
    full = str(full_type or '').strip()
    if '(' in full:
        base, _, rest = full.partition('(')
        return base.strip().upper(), rest.rstrip(')').strip()
    return full.upper(), ''


def is_integer_type(ctype):
    return (ctype or '').strip().upper() in INTEGER_TYPES


class TableDesigner(QDialog):
    """新建/编辑表设计器（AUTO_INCREMENT 复选框仅合法时可用）。"""

    def __init__(self, conn, parent=None, table_name=None):
        super().__init__(parent)
        self.conn = conn
        self.edit_table = str(table_name or '').strip()
        self.is_edit = bool(self.edit_table)
        self.last_error = ''
        self.last_validation_error = ''
        self.setWindowTitle(
            f"编辑表 - {self.edit_table}" if self.is_edit else "新建表 - 表设计器")
        self.resize(1000, 780)
        self._build_ui()
        if self.is_edit:
            self.load_existing_table(self.edit_table)
        else:
            self._add_column(default_id=True)

    # ---------- UI ----------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("表名:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如: users")
        top.addWidget(self.name_edit)
        layout.addLayout(top)

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            '列名', '类型', '长度', '主键', '非空', '唯一', '自增', '默认值'])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        widths = [150, 110, 70, 55, 55, 55, 55, 140]
        for i, width in enumerate(widths):
            self.table.setColumnWidth(i, width)
        layout.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        for text, slot in [
            ("➕ 添加列", self._add_column),
            ("➖ 删除列", self._del_column),
            ("⬆ 上移", lambda: self._move_column(-1)),
            ("⬇ 下移", lambda: self._move_column(1)),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            btn_row.addWidget(btn)
        btn_row.addStretch()
        self.preview_btn = QPushButton("👁 预览 SQL")
        self.preview_btn.clicked.connect(self._preview_sql)
        btn_row.addWidget(self.preview_btn)
        layout.addLayout(btn_row)

        layout.addWidget(QLabel("SQL 预览:"))
        self.sql_preview = QTextEdit()
        self.sql_preview.setReadOnly(True)
        self.sql_preview.setFont(QFont("Consolas", 10))
        self.sql_preview.setMaximumHeight(120)
        self.sql_preview.setStyleSheet('background: #1e1e1e; color: #ce9178;')
        layout.addWidget(self.sql_preview)

        self.info_label = QLabel('')
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet('color: #d7ba7d;')
        layout.addWidget(self.info_label)
        self.show_create_view = QTextEdit()
        self.show_create_view.setReadOnly(True)
        self.show_create_view.setFont(QFont("Consolas", 9))
        self.show_create_view.setMaximumHeight(90)
        self.show_create_view.setVisible(False)
        layout.addWidget(self.show_create_view)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(
            "创建表" if not self.is_edit else "关闭（仅回显）")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---------- 行操作 ----------

    def _make_checkbox_widget(self, checked=False, tooltip=''):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignCenter)
        cb = QCheckBox()
        cb.setChecked(checked)
        if tooltip:
            cb.setToolTip(tooltip)
        lay.addWidget(cb)
        w.checkbox = cb
        return w, cb

    def _add_column(self, default_id=False):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, COL_NAME, QTableWidgetItem(
            "id" if default_id else ""))
        type_combo = QComboBox()
        type_combo.addItems(TYPE_OPTIONS)
        type_combo.setCurrentText('INT' if default_id else 'VARCHAR')
        type_combo.currentTextChanged.connect(self._refresh_auto_states)
        self.table.setCellWidget(r, COL_TYPE, type_combo)
        self.table.setItem(r, COL_LENGTH, QTableWidgetItem(
            "" if default_id else "255"))
        for col, checked in ((COL_PK, default_id),
                             (COL_NN, default_id),
                             (COL_UNIQUE, False),
                             (COL_AUTO, False)):
            w, cb = self._make_checkbox_widget(checked)
            cb.toggled.connect(self._refresh_auto_states)
            self.table.setCellWidget(r, col, w)
        self.table.setItem(r, COL_DEFAULT, QTableWidgetItem(""))
        self._refresh_auto_states()

    def _checkbox(self, r, col):
        w = self.table.cellWidget(r, col)
        if w is None:
            return None
        return w.findChild(QCheckBox)

    def _del_column(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        if not rows:
            QMessageBox.information(self, "提示", "请先选择要删除的列")
            return
        for r in rows:
            self.table.removeRow(r)
        self._refresh_auto_states()

    def _move_column(self, delta):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if len(rows) != 1:
            return
        r = rows[0]
        nr = r + delta
        if nr < 0 or nr >= self.table.rowCount():
            return
        self._swap_rows(r, nr)
        self.table.selectRow(nr)

    def _get_row(self, r):
        name = self.table.item(r, COL_NAME).text() if self.table.item(
            r, COL_NAME) else ""
        tw = self.table.cellWidget(r, COL_TYPE)
        ctype = tw.currentText() if tw else "INT"
        length = self.table.item(r, COL_LENGTH).text() if self.table.item(
            r, COL_LENGTH) else ""
        default = self.table.item(r, COL_DEFAULT).text() if self.table.item(
            r, COL_DEFAULT) else ""

        def _ck(col):
            cb = self._checkbox(r, col)
            return cb.isChecked() if cb else False
        return (name, ctype, length, _ck(COL_PK), _ck(COL_NN),
                _ck(COL_UNIQUE), _ck(COL_AUTO), default)

    def _set_row(self, r, data):
        if len(data) == 7:  # 兼容旧 7 值（无 unique）格式
            name, ctype, length, pk, nn, ai, default = data
            unique = False
        else:
            name, ctype, length, pk, nn, unique, ai, default = data
        self.table.setItem(r, COL_NAME, QTableWidgetItem(name))
        cw = self.table.cellWidget(r, COL_TYPE)
        if cw:
            cw.blockSignals(True)
            cw.setCurrentText(ctype)
            cw.blockSignals(False)
        self.table.setItem(r, COL_LENGTH, QTableWidgetItem(length))
        for col, checked in ((COL_PK, pk), (COL_NN, nn),
                             (COL_UNIQUE, unique), (COL_AUTO, ai)):
            cb = self._checkbox(r, col)
            if cb:
                cb.blockSignals(True)
                cb.setChecked(checked)
                cb.blockSignals(False)
        self.table.setItem(r, COL_DEFAULT, QTableWidgetItem(default))
        self._refresh_auto_states()

    def _swap_rows(self, r1, r2):
        d1 = self._get_row(r1)
        d2 = self._get_row(r2)
        self._set_row(r1, d2)
        self._set_row(r2, d1)

    # ---------- AUTO_INCREMENT 联动 ----------

    def auto_increment_allowed(self, r):
        """该行是否允许 AUTO_INCREMENT：整数类型 且 主键/唯一。"""
        row = self._get_row(r)
        name, ctype, _length, pk, _nn, unique, _ai, _default = row
        return is_integer_type(ctype) and (pk or unique)

    def auto_increment_tooltip(self, r):
        ctype = self._get_row(r)[1]
        pk, unique = self._get_row(r)[3], self._get_row(r)[5]
        if not is_integer_type(ctype):
            return ('AUTO_INCREMENT 仅支持整数列（INT/BIGINT/SMALLINT/'
                    'TINYINT/MEDIUMINT）；当前类型: ' + str(ctype))
        if not (pk or unique):
            return 'AUTO_INCREMENT 需要该列为主键或唯一键'
        return '合法：整数列 + 主键/唯一键，可勾选自增'

    def _refresh_auto_states(self, *_args):
        for r in range(self.table.rowCount()):
            auto_cb = self._checkbox(r, COL_AUTO)
            if auto_cb is None:
                continue
            allowed = self.auto_increment_allowed(r)
            tooltip = self.auto_increment_tooltip(r)
            auto_cb.setToolTip(tooltip)
            auto_cb.setEnabled(allowed)
            if not allowed and auto_cb.isChecked():
                auto_cb.blockSignals(True)
                auto_cb.setChecked(False)
                auto_cb.blockSignals(False)

    # ---------- 收集 / 校验 / DDL ----------

    def _collect_columns(self):
        cols = []
        for r in range(self.table.rowCount()):
            name = (self.table.item(r, COL_NAME).text().strip()
                    if self.table.item(r, COL_NAME) else "")
            if not name:
                continue
            _n, ctype, length, pk, nn, unique, ai, default = self._get_row(r)
            cols.append({
                'name': name, 'type': ctype, 'length': length,
                'primary': pk, 'notnull': nn, 'unique': unique,
                'auto_increment': ai, 'default': default,
            })
        return cols

    def validate_columns(self, columns=None):
        """返回 (ok, error)；AUTO_INCREMENT 非法时显式拒绝。"""
        cols = list(columns if columns is not None else self._collect_columns())
        if not cols:
            return False, '请至少填写一列'
        names = [c['name'] for c in cols]
        if len(names) != len(set(names)):
            return False, '列名重复'
        auto_cols = [c for c in cols if c.get('auto_increment')]
        if len(auto_cols) > 1:
            return False, 'AUTO_INCREMENT 只能设置在一个列上'
        for c in auto_cols:
            if not is_integer_type(c.get('type')):
                return False, (
                    f"AUTO_INCREMENT 仅支持整数列；列 {c['name']} 类型为 "
                    f"{c.get('type')}")
            if not (c.get('primary') or c.get('unique')):
                return False, (
                    f"AUTO_INCREMENT 列 {c['name']} 必须是主键或唯一键")
        return True, ''

    def _generate_sql(self):
        self.last_validation_error = ''
        table_name = self.name_edit.text().strip()
        if not table_name:
            return None
        cols = self._collect_columns()
        ok, error = self.validate_columns(cols)
        if not ok:
            self.last_validation_error = error
            return None
        return build_create_table(table_name, cols)

    def _preview_sql(self):
        sql = self._generate_sql()
        if sql:
            self.sql_preview.setPlainText(sql)
            self.info_label.setText('')
        else:
            self.sql_preview.setPlainText(
                '-- ' + (self.last_validation_error
                         or '请填写表名和至少一列'))
            self.info_label.setText(self.last_validation_error)
            self.info_label.setStyleSheet('color: #f48771;')

    # ---------- 编辑已有表回显 ----------

    def load_existing_table(self, table_name):
        """读取现有表列/索引回显（含自增勾选）与 SHOW CREATE/TABLE STATUS。"""
        self.edit_table = str(table_name)
        self.is_edit = True
        self.name_edit.setText(self.edit_table)
        self.name_edit.setReadOnly(True)
        self.table.setRowCount(0)
        try:
            columns = list(self.conn.get_columns(self.edit_table) or [])
        except Exception as exc:                      # noqa: BLE001
            self.last_error = f'读取列失败: {exc}'
            self.info_label.setText(self.last_error)
            self.info_label.setStyleSheet('color: #f48771;')
            return False
        # SHOW INDEX 用于补充唯一键（列 key=UNI 已有；复合唯一取第一列）
        unique_cols = set()
        try:
            _cols, rows = self.conn.query(
                f"SHOW INDEX FROM `{self.edit_table}`")
            for row in rows or []:
                if len(row) >= 5 and int(row[1]) == 0 and str(
                        row[2]).upper() != 'PRIMARY':
                    unique_cols.add(str(row[4]))
        except Exception:
            pass
        for col in columns:
            r = self.table.rowCount()
            self._add_column()
            base, length = _split_type(col.get('type', ''))
            key = str(col.get('key') or '').upper()
            extra = str(col.get('extra') or '').lower()
            self._set_row(r, (
                str(col.get('name', '')), base or 'INT', length,
                key == 'PRI', str(col.get('null', 'YES')).upper() == 'NO',
                key == 'UNI' or str(col.get('name')) in unique_cols,
                'auto_increment' in extra,
                '' if col.get('default') is None else str(
                    col.get('default'))))
        self._refresh_auto_states()
        self._load_table_info()
        return True

    def _load_table_info(self):
        """SHOW CREATE / SHOW TABLE STATUS.Auto_increment（失败显式）。"""
        lines = []
        create_sql = ''
        try:
            _cols, rows = self.conn.query(
                f"SHOW CREATE TABLE `{self.edit_table}`")
            if rows:
                create_sql = str(rows[0][1])
                self.show_create_view.setPlainText(create_sql)
                self.show_create_view.setVisible(True)
                if 'AUTO_INCREMENT' in create_sql.upper():
                    lines.append('SHOW CREATE TABLE：含 AUTO_INCREMENT 定义')
        except Exception as exc:                      # noqa: BLE001
            lines.append(f'SHOW CREATE TABLE 不可用: {exc}')
        try:
            cols, rows = self.conn.query('SHOW TABLE STATUS')
            try:
                idx = [str(c).lower() for c in cols].index('auto_increment')
            except ValueError:
                idx = -1
            if idx >= 0:
                for row in rows or []:
                    if str(row[0]) == self.edit_table:
                        value = row[idx]
                        lines.append(
                            f'SHOW TABLE STATUS.Auto_increment = {value}')
                        break
                else:
                    lines.append('SHOW TABLE STATUS：未找到该表')
            else:
                lines.append('SHOW TABLE STATUS：无 Auto_increment 列（引擎不支持）')
        except Exception as exc:                      # noqa: BLE001
            lines.append(f'SHOW TABLE STATUS 不可用: {exc}')
        self.info_label.setText('\n'.join(lines) or '（无自增信息）')
        self.info_label.setStyleSheet('color: #d7ba7d;')

    # ---------- 执行 ----------

    def _on_accept(self):
        if self.is_edit:
            # 不做假成功：编辑回显不静默生成 ALTER
            QMessageBox.information(
                self, '编辑模式',
                '当前仅回显已有表结构（含 AUTO_INCREMENT）与 SHOW CREATE/'
                'Auto_increment 信息；自动保存 ALTER 尚未开放，'
                '请使用 SQL 执行修改。')
            return
        sql = self._generate_sql()
        if not sql:
            QMessageBox.warning(
                self, "无法创建表",
                self.last_validation_error or "请填写表名和至少一列")
            return
        try:
            _n, err = self.conn.execute(sql)
        except Exception as exc:                      # noqa: BLE001
            self.last_error = str(exc)
            QMessageBox.critical(self, "创建失败", f"引擎错误: {exc}")
            return
        if err:
            self.last_error = str(err)
            QMessageBox.critical(self, "创建失败", str(err))
            return
        self.last_error = ''
        QMessageBox.information(self, "成功", "表已创建")
        self.accept()

    # ---------- 测试/外部辅助 ----------

    def set_auto_increment(self, row, checked=True):
        """测试辅助：设置自增单元格（非法时复选框本身会禁用并自动取消）。"""
        cb = self._checkbox(row, COL_AUTO)
        if cb is not None:
            cb.setChecked(bool(checked))
        self._refresh_auto_states()
        return cb.isChecked() if cb is not None else False
