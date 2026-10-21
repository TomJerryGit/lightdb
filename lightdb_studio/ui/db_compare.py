# -*- coding: utf-8 -*-
"""Schema 对比产品化（P2-4：真实差异矩阵 + 可执行 SQL 预览 + 有确认的执行）

对比真实元数据（列名/类型/NOT NULL/默认值/主键/唯一/索引），生成安全子集
SQL 预览：
- 可执行：`CREATE TABLE`（目标缺失的表）、缺失索引 `CREATE INDEX`；
- 其余表内变更（列增删/改类型/默认值/PK/唯一/索引变更）：显式标注
  “需手工处理（本引擎 ALTER 限制）”，**不生成假 ALTER SQL**；
- 高风险 DDL（DROP TABLE）二次确认；执行走真实 conn.execute 并展示结果/错误，
  绝不静默改库；支持把 diff SQL 落盘（真实文件校验）。
"""
import json
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QComboBox, QPushButton,
    QLabel, QTreeWidget, QTreeWidgetItem, QTextEdit, QSplitter,
    QMessageBox, QDialogButtonBox, QWidget, QFileDialog,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont

from ..db.table_ops import build_create_table
from .er_diagram import parse_show_index_rows


class SchemaCompareError(RuntimeError):
    pass


# ==================== 元数据快照 ====================

def split_sql_type(full_type):
    """'VARCHAR(32)' → ('VARCHAR','32')；无长度返回 ('INT','')。"""
    full = str(full_type or '').strip()
    if '(' in full:
        base, _, rest = full.partition('(')
        return base.strip().upper(), rest.rstrip(')').strip()
    return full.upper(), ''


def column_signature(meta):
    return {
        'name': meta.get('name', ''),
        'type': str(meta.get('type', '')).upper(),
        'notnull': meta.get('null', 'YES') == 'NO',
        'default': meta.get('default'),
        'primary': str(meta.get('key', '')).upper() == 'PRI',
        'unique': str(meta.get('key', '')).upper() == 'UNI',
        'auto_increment': 'auto_increment' in str(
            meta.get('extra', '') or '').lower(),
    }


def load_table_meta(conn, table):
    columns = list(conn.get_columns(table) or [])
    indexes = []
    try:
        _cols, rows = conn.query(f"SHOW INDEX FROM `{table}`")
        indexes = parse_show_index_rows(rows, table)
    except Exception:
        pass
    if not indexes:
        for col in columns:
            key = str(col.get('key') or '').upper()
            if key in ('PRI', 'UNI'):
                indexes.append({'name': key, 'unique': True,
                                'columns': [col.get('name')],
                                'derived': True})
    return {'columns': columns, 'indexes': indexes}


def load_schema_snapshot(conn, db_name=''):
    """读取一个库的真实结构快照。"""
    if db_name:
        conn.switch_database(db_name)
    tables = list(conn.get_tables() or [])
    snapshot = {'db': db_name, 'tables': {}}
    for table in tables:
        try:
            snapshot['tables'][table] = load_table_meta(conn, table)
        except Exception:
            snapshot['tables'][table] = {'columns': [], 'indexes': []}
    return snapshot


# ==================== 差异矩阵 ====================

def _column_diffs(cols_a, cols_b):
    map_a = {str(c.get('name')): column_signature(c) for c in cols_a or []}
    map_b = {str(c.get('name')): column_signature(c) for c in cols_b or []}
    diffs = []
    for name in map_a:
        if name not in map_b:
            diffs.append({'kind': 'missing_in_target', 'name': name,
                          'source': map_a[name], 'target': None})
    for name in map_b:
        if name not in map_a:
            diffs.append({'kind': 'missing_in_source', 'name': name,
                          'source': None, 'target': map_b[name]})
    for name in map_a:
        if name not in map_b:
            continue
        sa, sb = map_a[name], map_b[name]
        changed = [field for field in
                   ('type', 'notnull', 'default', 'primary', 'unique',
                    'auto_increment') if sa[field] != sb[field]]
        if changed:
            diffs.append({'kind': 'changed', 'name': name,
                          'fields': changed, 'source': sa, 'target': sb})
    return diffs


def _index_diffs(indexes_a, indexes_b):
    map_a = {str(i.get('name')): i for i in indexes_a or []}
    map_b = {str(i.get('name')): i for i in indexes_b or []}
    diffs = []
    for name, ia in map_a.items():
        if name not in map_b:
            diffs.append({'kind': 'missing_in_target', 'name': name,
                          'source': ia, 'target': None})
            continue
        ib = map_b[name]
        changed = []
        if bool(ia.get('unique')) != bool(ib.get('unique')):
            changed.append('unique')
        if [str(c) for c in (ia.get('columns') or [])] != \
                [str(c) for c in (ib.get('columns') or [])]:
            changed.append('columns')
        if changed:
            diffs.append({'kind': 'changed', 'name': name,
                          'fields': changed, 'source': ia, 'target': ib})
    for name, ib in map_b.items():
        if name not in map_a:
            diffs.append({'kind': 'missing_in_source', 'name': name,
                          'source': None, 'target': ib})
    return diffs


def _pk_columns(meta):
    return [str(c.get('name')) for c in meta.get('columns') or []
            if str(c.get('key') or '').upper() == 'PRI']


def compare_schemas(snap_a, snap_b):
    """比较两个结构快照，返回差异矩阵。

    status: only_source(仅源) / only_target(仅目标) / changed / same
    """
    tables_a = snap_a.get('tables') or {}
    tables_b = snap_b.get('tables') or {}
    result = {'a': snap_a.get('db', ''), 'b': snap_b.get('db', ''),
              'tables': [], 'summary': {'only_source': 0, 'only_target': 0,
                                        'changed': 0, 'same': 0},
              'has_diff': False}
    for table in sorted(set(tables_a) | set(tables_b)):
        ma = tables_a.get(table) or {'columns': [], 'indexes': []}
        mb = tables_b.get(table) or {'columns': [], 'indexes': []}
        entry = {'table': table, 'status': 'same',
                 'column_diffs': [], 'index_diffs': [],
                 'pk_source': _pk_columns(ma), 'pk_target': _pk_columns(mb),
                 'pk_changed': False, 'manual': False, 'high_risk': False}
        if table not in tables_b:
            entry['status'] = 'only_source'
        elif table not in tables_a:
            entry['status'] = 'only_target'
            entry['high_risk'] = True
        else:
            entry['column_diffs'] = _column_diffs(ma.get('columns'),
                                                  mb.get('columns'))
            entry['index_diffs'] = _index_diffs(ma.get('indexes'),
                                                mb.get('indexes'))
            entry['pk_changed'] = entry['pk_source'] != entry['pk_target']
            diff_count = (len(entry['column_diffs']) +
                          len(entry['index_diffs']) +
                          (1 if entry['pk_changed'] else 0))
            entry['status'] = 'changed' if diff_count else 'same'
        result['summary'][entry['status']] += 1
        result['tables'].append(entry)
        if entry['status'] != 'same':
            result['has_diff'] = True
    return result


# ==================== 可执行 SQL 预览 ====================

def _create_table_sql(table, meta):
    cols = []
    for col in meta.get('columns') or []:
        ctype, length = split_sql_type(col.get('type'))
        cols.append({
            'name': col.get('name', ''),
            'type': ctype,
            'length': length,
            'primary': str(col.get('key') or '').upper() == 'PRI',
            'notnull': col.get('null', 'YES') == 'NO',
            'unique': str(col.get('key') or '').upper() == 'UNI',
            'auto_increment': 'auto_increment' in str(
                col.get('extra', '') or '').lower(),
            'default': col.get('default'),
        })
    return build_create_table(table, cols) + ';'


def _column_diff_text(diff):
    name = diff['name']
    if diff['kind'] == 'missing_in_target':
        return f'列 {name}：源存在、目标缺失（需手工 ADD COLUMN）'
    if diff['kind'] == 'missing_in_source':
        return f'列 {name}：目标存在、源缺失（需手工确认是否 DROP COLUMN）'
    old, new = diff['source'], diff['target']
    parts = []
    for field in diff['fields']:
        parts.append(f'{field}: {old.get(field)!r} → {new.get(field)!r}')
    return f"列 {name} 变更：" + '; '.join(parts)


def build_migration_plan(diff_result):
    """把差异矩阵转成 SQL 计划：可执行子集 + 需手工处理说明。"""
    plan = {'statements': [], 'manual_notes': [],
            'summary': {'executable': 0, 'manual': 0, 'high_risk': 0,
                        'tables': 0}}
    for entry in diff_result.get('tables') or []:
        table = entry['table']
        if entry['status'] == 'same':
            continue
        plan['summary']['tables'] += 1
        if entry['status'] == 'only_source':
            snap_cols = diff_result.get('_source_meta', {}).get(table)
            if snap_cols:
                sql = _create_table_sql(table, snap_cols)
                plan['statements'].append({
                    'sql': sql, 'kind': 'create_table', 'table': table,
                    'high_risk': False, 'manual': False})
                plan['summary']['executable'] += 1
            continue
        if entry['status'] == 'only_target':
            plan['statements'].append({
                'sql': f'DROP TABLE IF EXISTS `{table}`;',
                'kind': 'drop_table', 'table': table,
                'high_risk': True, 'manual': False})
            plan['summary']['executable'] += 1
            plan['summary']['high_risk'] += 1
            continue
        # changed 表：列/PK 差异 → 手工；缺失索引 → CREATE INDEX
        manual_lines = []
        for diff in entry.get('column_diffs') or []:
            manual_lines.append(_column_diff_text(diff))
        if entry.get('pk_changed'):
            manual_lines.append(
                f"主键变更：{entry.get('pk_source')} → "
                f"{entry.get('pk_target')}（需手工 DROP/ADD PRIMARY KEY）")
        for diff in entry.get('index_diffs') or []:
            if diff['kind'] == 'missing_in_target':
                idx = diff['source']
                cols = ', '.join(f'`{c}`' for c in idx.get('columns') or [])
                unique = 'UNIQUE ' if idx.get('unique') else ''
                sql = (f"CREATE {unique}INDEX `{diff['name']}` "
                       f"ON `{table}` ({cols});")
                plan['statements'].append({
                    'sql': sql, 'kind': 'create_index', 'table': table,
                    'high_risk': False, 'manual': False})
                plan['summary']['executable'] += 1
            elif diff['kind'] == 'missing_in_source':
                manual_lines.append(
                    f"目标多出索引 {diff['name']}（需手工确认是否删除）")
            else:
                manual_lines.append(
                    f"索引 {diff['name']} 变更：{diff.get('fields')}"
                    f"（需手工 DROP/CREATE INDEX）")
        for line in manual_lines:
            plan['manual_notes'].append(
                {'table': table, 'message':
                 '需手工处理（本引擎 ALTER 限制，不自动生成假 SQL）：' + line})
            plan['summary']['manual'] += 1
    return plan


def plan_sql_text(plan, title='LightDB Schema diff'):
    lines = [f'-- {title}',
             f'-- generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
             '-- 可执行语句: %d，需手工处理: %d，高风险: %d'
             % (plan['summary']['executable'], plan['summary']['manual'],
                plan['summary']['high_risk']),
             '']
    for stmt in plan.get('statements') or []:
        tag = '[HIGH RISK] ' if stmt.get('high_risk') else ''
        lines.append(f'-- {tag}表 {stmt.get("table")}: {stmt.get("kind")}')
        lines.append(stmt.get('sql', ''))
        lines.append('')
    if plan.get('manual_notes'):
        lines.append('-- ==================== 需手工处理 ====================')
        for note in plan['manual_notes']:
            lines.append(f"-- [{note.get('table')}] {note.get('message')}")
        lines.append('')
    return '\n'.join(lines)


def write_plan_file(plan, filepath):
    """把完整 diff SQL 计划落盘（真实文件校验非空）。"""
    path = Path(filepath)
    text = plan_sql_text(plan)
    path.write_text(text, encoding='utf-8')
    if not path.exists() or path.stat().st_size <= 0:
        raise SchemaCompareError(f'diff 文件写入失败: {filepath}')
    return str(path)


def execute_plan(conn, plan, confirmer=None):
    """执行计划中可执行语句；confirmer(plan)->bool 用于二次确认。

    返回 {'ok','cancelled','executed','total','errors','results'}
    失败不中断后续语句（逐条记录），全部错误显式返回。
    """
    statements = [s for s in plan.get('statements') or []
                  if not s.get('manual')]
    result = {'ok': True, 'cancelled': False, 'executed': 0,
              'total': len(statements), 'errors': [], 'results': []}
    if not statements:
        return result
    if confirmer is not None and not confirmer(plan):
        result.update({'ok': False, 'cancelled': True})
        return result
    for stmt in statements:
        sql = stmt.get('sql', '')
        try:
            outcome = conn.execute(sql)
            if isinstance(outcome, tuple):
                _n = outcome[0] if outcome else 0
                err = outcome[1] if len(outcome) > 1 else None
            else:
                _n, err = outcome, None
        except Exception as exc:                      # noqa: BLE001
            _n, err = 0, str(exc)
        if err:
            result['errors'].append({'sql': sql, 'table': stmt.get('table'),
                                     'error': str(err)})
        else:
            result['executed'] += 1
            result['results'].append({'sql': sql, 'table': stmt.get('table')})
    result['ok'] = not result['errors']
    return result


# ==================== 对话框 ====================

class DBCompareDialog(QDialog):
    def __init__(self, connections: dict, parent=None):
        super().__init__(parent)
        self.connections = connections or {}
        self.diffs = {}
        self.snap_a = {}
        self.snap_b = {}
        self.diff_result = {}
        self.plan = {}
        self.last_diff_file = ''
        self.setWindowTitle('数据库结构对比')
        self.resize(1200, 760)
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel('源连接:'))
        self.conn_a_combo = QComboBox()
        self.conn_a_combo.addItems(list(self.connections.keys()))
        self.conn_a_combo.currentTextChanged.connect(self._on_conn_a_changed)
        top.addWidget(self.conn_a_combo)
        top.addWidget(QLabel('源库:'))
        self.db_a_combo = QComboBox()
        top.addWidget(self.db_a_combo)
        top.addSpacing(16)
        top.addWidget(QLabel('目标连接:'))
        self.conn_b_combo = QComboBox()
        self.conn_b_combo.addItems(list(self.connections.keys()))
        self.conn_b_combo.currentTextChanged.connect(self._on_conn_b_changed)
        top.addWidget(self.conn_b_combo)
        top.addWidget(QLabel('目标库:'))
        self.db_b_combo = QComboBox()
        top.addWidget(self.db_b_combo)
        top.addStretch()
        self.compare_btn = QPushButton('🔍 开始对比')
        self.compare_btn.clicked.connect(self._do_compare)
        top.addWidget(self.compare_btn)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['对象', '类型', '详情', '状态'])
        self.tree.setColumnWidth(0, 220)
        self.tree.setColumnWidth(1, 90)
        self.tree.setColumnWidth(2, 420)
        splitter.addWidget(self.tree)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.sql_view = QTextEdit()
        self.sql_view.setFont(QFont('Consolas', 10))
        self.sql_view.setStyleSheet('background: #1e1e1e; color: #ce9178;')
        rl.addWidget(QLabel('可执行 SQL 预览 / 手工处理说明:'))
        rl.addWidget(self.sql_view)
        self.summary = QLabel('')
        self.summary.setStyleSheet(
            'background: #252526; color: #dcdcaa; padding: 6px;')
        rl.addWidget(self.summary)
        splitter.addWidget(right)
        layout.addWidget(splitter)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self.save_btn = QPushButton('💾 保存 diff SQL')
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save_diff_dialog)
        bottom.addWidget(self.save_btn)
        self.sync_btn = QPushButton('⚡ 执行同步')
        self.sync_btn.setEnabled(False)
        self.sync_btn.clicked.connect(self._sync)
        bottom.addWidget(self.sync_btn)
        close_btn = QPushButton('关闭')
        close_btn.clicked.connect(self.reject)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)
        self._on_conn_a_changed(self.conn_a_combo.currentText())
        # 目标连接默认选第二个（避免源/目标默认同一个连接）
        if self.conn_b_combo.count() > 1:
            self.conn_b_combo.setCurrentIndex(1)
        self._on_conn_b_changed(self.conn_b_combo.currentText())

    def _db_list(self, conn):
        try:
            dbs = list(conn.get_databases() or [])
            return dbs or ['default']
        except Exception:
            return ['default']

    def _on_conn_a_changed(self, name):
        self.db_a_combo.clear()
        conn = self.connections.get(name)
        if conn is not None:
            self.db_a_combo.addItems(self._db_list(conn))

    def _on_conn_b_changed(self, name):
        self.db_b_combo.clear()
        conn = self.connections.get(name)
        if conn is not None:
            self.db_b_combo.addItems(self._db_list(conn))

    def _get_conns(self):
        conn_a = self.connections.get(self.conn_a_combo.currentText())
        conn_b = self.connections.get(self.conn_b_combo.currentText())
        return conn_a, conn_b

    # ---------- 对比 ----------

    def run_compare(self, db_a=None, db_b=None):
        conn_a, conn_b = self._get_conns()
        db_a = db_a if db_a is not None else self.db_a_combo.currentText()
        db_b = db_b if db_b is not None else self.db_b_combo.currentText()
        self.tree.clear()
        self.sql_view.clear()
        self.sync_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.plan = {}
        if not conn_a or not conn_b:
            raise SchemaCompareError('请选择源和目标连接')
        try:
            self.snap_a = load_schema_snapshot(conn_a, db_a)
            self.snap_b = load_schema_snapshot(conn_b, db_b)
        except Exception as exc:                      # noqa: BLE001
            raise SchemaCompareError(f'读取 schema 失败: {exc}') from exc
        self.diff_result = compare_schemas(self.snap_a, self.snap_b)
        # 为 CREATE TABLE 提供源表元数据
        self.diff_result['_source_meta'] = {
            t: meta for t, meta in (self.snap_a.get('tables') or {}).items()}
        self.plan = build_migration_plan(self.diff_result)
        self.diffs = {e['table']: e for e in self.diff_result['tables']}
        self._render_tree()
        self.sql_view.setPlainText(plan_sql_text(self.plan))
        have = bool(self.plan['statements'])
        self.sync_btn.setEnabled(have)
        self.save_btn.setEnabled(True)
        s = self.diff_result['summary']
        self.summary.setText(
            f"差异: 仅源 {s['only_source']} | 仅目标 {s['only_target']} | "
            f"变更 {s['changed']} | 一致 {s['same']} || "
            f"可执行 {self.plan['summary']['executable']} 条 | 需手工 "
            f"{self.plan['summary']['manual']} 处 | 高风险 "
            f"{self.plan['summary']['high_risk']} 条")
        return self.diff_result

    def _render_tree(self):
        for entry in self.diff_result.get('tables') or []:
            if entry['status'] == 'same':
                continue
            table_item = QTreeWidgetItem(self.tree)
            table_item.setText(0, f"表: {entry['table']}")
            kind = {'only_source': 'CREATE', 'only_target': 'DROP',
                    'changed': 'ALTER'}.get(entry['status'],
                                            entry['status'])
            table_item.setText(1, kind)
            table_item.setForeground(1, QColor(
                '#89d185' if kind == 'CREATE' else
                '#f48771' if kind == 'DROP' else '#dcdcaa'))
            detail = f"列差异 {len(entry['column_diffs'])} | " \
                     f"索引差异 {len(entry['index_diffs'])}"
            if entry['pk_changed']:
                detail += ' | 主键变化'
            table_item.setText(2, detail)
            table_item.setText(3, '需手工处理' if
                               (entry['status'] == 'changed') else '可执行')
            for diff in entry['column_diffs']:
                child = QTreeWidgetItem(table_item)
                child.setText(0, f"  {diff['name']}")
                child.setText(1, diff['kind'])
                child.setText(2, _column_diff_text(diff))
                child.setText(3, '需手工处理')
                child.setForeground(1, QColor('#dcdcaa'))
            for diff in entry['index_diffs']:
                child = QTreeWidgetItem(table_item)
                child.setText(0, f"  索引 {diff['name']}")
                child.setText(1, diff['kind'])
                child.setText(2, str(diff.get('fields') or ''))
                child.setText(3, '可执行 CREATE INDEX'
                             if diff['kind'] == 'missing_in_target'
                             else '需手工处理')
        self.tree.expandAll()

    def _do_compare(self):
        try:
            self.run_compare()
        except SchemaCompareError as exc:
            QMessageBox.warning(self, '提示', str(exc))
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.critical(self, '对比失败', str(exc))

    # ---------- diff 文件 ----------

    def save_diff_file(self, path):
        if not self.plan:
            raise SchemaCompareError('请先执行对比')
        self.last_diff_file = write_plan_file(self.plan, path)
        return self.last_diff_file

    def _save_diff_dialog(self):
        path, _ = QFileDialog.getSaveFileName(
            self, '保存 diff SQL', 'schema_diff.sql', 'SQL 文件 (*.sql)')
        if not path:
            return
        try:
            out = self.save_diff_file(path)
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.critical(self, '保存失败', str(exc))
            return
        QMessageBox.information(self, '成功', f'diff SQL 已保存:\n{out}')

    # ---------- 执行（二次确认，不静默） ----------

    def execute_sync(self, interactive=True):
        """执行同步。interactive=True 时弹两层确认（高风险 DDL 再确认）。"""
        if not self.plan:
            return {'ok': False, 'cancelled': True, 'error': '请先执行对比',
                    'executed': 0, 'errors': [], 'total': 0}
        conn_b = self._get_conns()[1]
        if conn_b is None:
            return {'ok': False, 'cancelled': True, 'error': '目标连接不可用',
                    'executed': 0, 'errors': [], 'total': 0}
        if interactive:
            ret = QMessageBox.question(
                self, '确认同步',
                '将在**目标库**执行差异 SQL，可能修改数据库结构。\n'
                '请确认已备份。是否继续？')
            if ret != QMessageBox.Yes:
                return {'ok': False, 'cancelled': True, 'executed': 0,
                        'errors': [], 'total': 0, 'error': '用户取消'}
            if self.plan['summary']['high_risk']:
                ret = QMessageBox.question(
                    self, '高风险操作确认',
                    f"计划包含 {self.plan['summary']['high_risk']} 条高风险 "
                    f'DDL（如 DROP TABLE）。\n再次确认继续？')
                if ret != QMessageBox.Yes:
                    return {'ok': False, 'cancelled': True, 'executed': 0,
                            'errors': [], 'total': 0, 'error': '用户取消高风险操作'}
        result = execute_plan(conn_b, self.plan)
        if result['errors']:
            lines = [f"{e['table']}: {e['error']}" for e in result['errors'][:6]]
            QMessageBox.critical(
                self, '部分失败',
                f"成功 {result['executed']} / 总 {result['total']} 条：\n"
                + '\n'.join(lines))
        else:
            QMessageBox.information(
                self, '执行完成',
                f"执行成功 {result['executed']} 条 SQL")
        return result

    def _sync(self):
        self.execute_sync(interactive=True)
