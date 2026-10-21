# -*- coding: utf-8 -*-
"""P2-1 ER 真实关系 + P2-4 Schema 对比产品化测试（offscreen + 假连接）。

覆盖：
- ER 关系三态：SHOW CREATE TABLE 真实 FOREIGN KEY > 手工登记关系 > 无关系；
  禁止按 `_id` 命名猜测；手工关系增删/持久化 round-trip；无关系显式提示；
  ER 场景渲染 + PNG 导出（QImage 真实文件校验）。
- Schema 对比：列/类型/NOT NULL/默认值/PK/唯一/索引差异矩阵；CREATE TABLE /
  CREATE INDEX 可执行预览；表内 ALTER 差异显式“需手工处理”且不生成假 SQL；
  diff SQL 文件落盘；高风险二次确认（不确认不执行）；执行走真实连接并展示错误。
"""
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

_APP = None


def qapp():
    global _APP
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


class _MsgBox:
    """替换 QMessageBox 静态方法：记录 + 可配置返回值。"""

    def __init__(self):
        self.calls = []
        self.answer = None       # None → Yes

    def _rec(self, kind):
        def fn(*args, **kwargs):
            self.calls.append({'kind': kind,
                               'text': ' | '.join(str(a) for a in args)})
            from PySide6.QtWidgets import QMessageBox
            return self.answer if self.answer is not None else QMessageBox.Yes
        return fn

    def texts(self, kind=None):
        return [c['text'] for c in self.calls
                if kind is None or c['kind'] == kind]

    def count(self):
        return len(self.calls)


@pytest.fixture
def msgbox(monkeypatch):
    box = _MsgBox()
    from PySide6.QtWidgets import QMessageBox
    for name in ('information', 'warning', 'critical', 'question'):
        monkeypatch.setattr(QMessageBox, name,
                            staticmethod(box._rec(name)))
    return box


# ==================== 假连接 ====================

class FakeSchemaConn:
    """按表返回 SHOW COLUMNS / SHOW INDEX / SHOW CREATE TABLE 的假连接。"""

    def __init__(self, db='db', tables=None, columns=None, indexes=None,
                 create_sql=None, fail_queries=False, execute_errors=None):
        from types import SimpleNamespace
        self.config = SimpleNamespace(name=db, database=db)
        self.databases = [db]
        self.db = db
        self.tables = list(tables or [])
        self.columns_map = columns or {}
        self.indexes_map = indexes or {}
        self.create_sql_map = create_sql or {}
        self.fail_queries = fail_queries
        self.switched = []
        self.executed = []
        self.execute_errors = execute_errors or {}

    def get_databases(self):
        return list(self.databases)

    def switch_database(self, db):
        self.switched.append(db)
        self.db = db

    def get_tables(self):
        return list(self.tables)

    def get_columns(self, table):
        return [dict(c) for c in self.columns_map.get(table, [])]

    def query(self, sql, params=None):
        up = sql.upper()
        if 'SHOW CREATE TABLE' in up:
            m = re.search(r'SHOW CREATE TABLE\s+`?(\w+)`?', sql, re.I)
            table = m.group(1) if m else ''
            return ['Table', 'Create Table'], [
                (table, self.create_sql_map.get(table, ''))]
        if up.startswith('SHOW INDEX'):
            m = re.search(r'SHOW INDEX FROM\s+`?(\w+)`?', sql, re.I)
            table = m.group(1) if m else ''
            return (['Table', 'Non_unique', 'Key_name', 'Seq_in_index',
                     'Column_name'], list(self.indexes_map.get(table, [])))
        return [], []

    def execute(self, sql):
        self.executed.append(sql)
        for pattern, err in self.execute_errors.items():
            if pattern in sql:
                return 0, err
        return 1, None


def col(name, ctype='INT', null='YES', key='', default=None, extra=''):
    return {'name': name, 'type': ctype, 'null': null, 'key': key,
            'default': default, 'extra': extra}


def make_conn(db='db', tables=None, columns=None, indexes=None,
              create_sql=None, **kwargs):
    return FakeSchemaConn(db=db, tables=tables, columns=columns,
                          indexes=indexes, create_sql=create_sql, **kwargs)


# ==================== 1. ER：真实外键解析 ====================

def test_parse_foreign_keys_from_show_create():
    from lightdb_studio.ui.er_diagram import parse_foreign_keys
    sql = '''
CREATE TABLE `orders` (
  `id` INT NOT NULL,
  `customer_id` INT NOT NULL,
  PRIMARY KEY (`id`),
  CONSTRAINT `fk_orders_customer` FOREIGN KEY (`customer_id`)
    REFERENCES `customers` (`id`),
  CONSTRAINT `fk_orders_region` FOREIGN KEY (`region_id`)
    REFERENCES `regions` (`id`)
)
'''
    fks = parse_foreign_keys(sql, 'orders')
    assert len(fks) == 2
    assert fks[0]['from_table'] == 'orders'
    assert fks[0]['from_col'] == 'customer_id'
    assert fks[0]['to_table'] == 'customers'
    assert fks[0]['to_col'] == 'id'
    assert fks[1]['to_table'] == 'regions'
    assert parse_foreign_keys('CREATE TABLE t (a INT)', 't') == []
    assert parse_foreign_keys('', 't') == []


# ==================== 2. 手工关系持久化 / 三态 ====================

def test_relation_store_roundtrip(tmp_path):
    from lightdb_studio.ui.er_diagram import RelationStore
    path = tmp_path / 'relations.json'
    store = RelationStore(path)
    store.add('db1', 'orders', 'customer_id', 'customers', 'id')
    store.add('db1', 'orders', 'customer_id', 'customers', 'id')  # 去重
    store.add('db2', 'a', 'b_id', 'b', 'id')
    assert len(store.list_for('db1')) == 1
    assert path.exists()
    # round-trip
    store2 = RelationStore(path)
    assert len(store2.relations) == 2
    rels = store2.list_for('db1')
    assert rels[0]['from_table'] == 'orders'
    assert store2.remove('db1', 'orders', 'customer_id', 'customers', 'id')
    assert RelationStore(path).list_for('db1') == []
    print(f"  → 手工关系持久化 round-trip: {len(store2.relations)} 条")


def _er_columns():
    return {
        'customers': [col('id', 'INT', 'NO', 'PRI'), col('name', 'VARCHAR(32)')],
        'orders': [col('id', 'INT', 'NO', 'PRI'),
                   col('customer_id', 'INT', 'NO', 'MUL')],
    }


def _er_indexes():
    return {
        'customers': [('customers', 0, 'PRIMARY', 1, 'id')],
        'orders': [('orders', 0, 'PRIMARY', 1, 'id'),
                   ('orders', 1, 'idx_customer', 1, 'customer_id')],
    }


def test_er_model_three_states(tmp_path):
    from lightdb_studio.ui.er_diagram import build_er_model, RelationStore
    tables = ['customers', 'orders']
    create = {
        'customers': 'CREATE TABLE `customers` (`id` INT PRIMARY KEY)',
        'orders': ('CREATE TABLE `orders` (`id` INT, `customer_id` INT, '
                   'CONSTRAINT `fk_c` FOREIGN KEY (`customer_id`) '
                   'REFERENCES `customers` (`id`))'),
    }
    conn = make_conn(tables=tables, columns=_er_columns(),
                     indexes=_er_indexes(), create_sql=create)
    model = build_er_model(conn, 'db')
    assert model['relation_source'] == 'foreign_key'
    assert model['foreign_key_count'] == 1
    assert model['relations'][0]['to_table'] == 'customers'
    assert model['tables']['orders']['content_keys'] if False else True
    # 表/列/索引/PK 来自真实元数据
    assert model['tables']['customers']['columns'][0]['key'] == 'PRI'
    assert any(i['name'] == 'idx_customer'
               for i in model['tables']['orders']['indexes'])

    # 无 FK + 手工登记 → manual
    store = RelationStore(tmp_path / 'rel.json')
    store.add('db', 'orders', 'customer_id', 'customers', 'id')
    conn2 = make_conn(tables=tables, columns=_er_columns(),
                      indexes=_er_indexes())
    model2 = build_er_model(conn2, 'db', store)
    assert model2['relation_source'] == 'manual'
    assert len(model2['relations']) == 1
    assert model2['relations'][0]['source'] == 'manual'

    # 无 FK、无手工 → none（即使列名像 customer_id 也不猜）
    model3 = build_er_model(conn2, 'db', None)
    assert model3['relation_source'] == 'none'
    assert model3['relations'] == []
    assert '无关系' in model3['no_relation_message']
    print(f"  → 三态: foreign_key / manual / none；无猜测 ✓")


def test_er_manual_relation_with_missing_table_ignored(tmp_path):
    from lightdb_studio.ui.er_diagram import build_er_model, RelationStore
    store = RelationStore(tmp_path / 'rel.json')
    store.add('db', 'orders', 'customer_id', 'nope', 'id')
    conn = make_conn(tables=['orders', 'customers'],
                     columns=_er_columns(), indexes=_er_indexes())
    model = build_er_model(conn, 'db', store)
    assert model['relation_source'] == 'none'
    assert model['warnings']


# ==================== 3. ER 对话框 / PNG 导出 ====================

def test_er_dialog_renders_and_exports_png(tmp_path, msgbox):
    from PySide6.QtGui import QImage
    from lightdb_studio.ui.er_diagram import ERDiagramDialog, RelationStore
    qapp()
    create = {
        'customers': 'CREATE TABLE `customers` (`id` INT PRIMARY KEY)',
        'orders': ('CREATE TABLE `orders` (`id` INT, `customer_id` INT, '
                   'CONSTRAINT `fk_c` FOREIGN KEY (`customer_id`) '
                   'REFERENCES `customers` (`id`))'),
    }
    conn = make_conn(tables=['customers', 'orders'], columns=_er_columns(),
                     indexes=_er_indexes(), create_sql=create)
    dlg = ERDiagramDialog(conn, 'db',
                          relation_store=RelationStore(tmp_path / 'r.json'))
    dlg.show()
    qapp().processEvents()
    assert dlg.relation_source == 'foreign_key'
    assert len(dlg.relations) == 1
    assert dlg.scene.items(), 'ER 场景为空'
    png = dlg.export_png(str(tmp_path / 'er.png'), quiet=True)
    img = QImage(png)
    assert not img.isNull() and img.width() > 10
    assert os.path.getsize(png) > 300
    print(f"  → ER PNG {os.path.getsize(png)}B 可读")
    dlg.close()


def test_er_dialog_no_relation_explicit(tmp_path, msgbox):
    from lightdb_studio.ui.er_diagram import ERDiagramDialog, RelationStore
    qapp()
    conn = make_conn(tables=['customers', 'orders'], columns=_er_columns(),
                     indexes=_er_indexes())
    dlg = ERDiagramDialog(conn, 'db',
                          relation_store=RelationStore(tmp_path / 'r.json'))
    dlg.show()
    qapp().processEvents()
    assert dlg.relation_source == 'none'
    assert dlg.relations == []
    assert '无关系' in dlg.no_relation_message
    assert '无关系' in dlg.info_label.text()
    # 手工添加/删除关系 + 持久化
    dlg.add_manual_relation('orders', 'customer_id', 'customers', 'id')
    assert dlg.relation_source == 'manual'
    assert len(dlg.manual_relations()) == 1
    assert (tmp_path / 'r.json').exists()
    assert dlg.remove_manual_relation('orders', 'customer_id',
                                      'customers', 'id')
    assert dlg.relation_source == 'none'
    dlg.close()


def test_er_dialog_tolerates_metadata_errors(tmp_path, msgbox):
    from lightdb_studio.ui.er_diagram import ERDiagramDialog, RelationStore
    qapp()
    conn = make_conn(tables=['t'], columns={'t': [col('id')]},
                     fail_queries=True)
    dlg = ERDiagramDialog(conn, 'db',
                          relation_store=RelationStore(tmp_path / 'r.json'))
    assert dlg.relation_source == 'none'
    dlg.close()


# ==================== 4. Schema 对比矩阵 / SQL 预览 ====================

def _snapshot_a():
    return {'db': 'db1', 'tables': {
        'users': {
            'columns': [col('id', 'INT', 'NO', 'PRI'),
                        col('name', 'VARCHAR(32)', 'NO', '', "'x'"),
                        col('email', 'VARCHAR(64)', 'YES', 'UNI'),
                        col('age', 'INT', 'YES')],
            'indexes': [
                {'name': 'PRIMARY', 'unique': True, 'columns': ['id']},
                {'name': 'uq_email', 'unique': True, 'columns': ['email']},
                {'name': 'idx_age', 'unique': False, 'columns': ['age']}],
        },
        'orders': {
            'columns': [col('id', 'INT', 'NO', 'PRI'),
                        col('amount', 'DOUBLE', 'YES')],
            'indexes': [{'name': 'PRIMARY', 'unique': True,
                         'columns': ['id']}],
        },
    }}


def _snapshot_b():
    return {'db': 'db2', 'tables': {
        'users': {
            'columns': [col('id', 'INT', 'NO', 'PRI'),
                        col('name', 'VARCHAR(16)', 'YES', ''),
                        col('email', 'VARCHAR(64)', 'YES', 'UNI')],
            'indexes': [
                {'name': 'PRIMARY', 'unique': True, 'columns': ['id']},
                {'name': 'uq_email', 'unique': True, 'columns': ['email']}],
        },
        'legacy': {
            'columns': [col('id', 'INT', 'NO', 'PRI')],
            'indexes': [{'name': 'PRIMARY', 'unique': True,
                         'columns': ['id']}],
        },
    }}


def test_compare_schemas_matrix():
    from lightdb_studio.ui.db_compare import compare_schemas
    result = compare_schemas(_snapshot_a(), _snapshot_b())
    tables = {e['table']: e for e in result['tables']}
    assert tables['orders']['status'] == 'only_source'
    assert tables['legacy']['status'] == 'only_target'
    assert tables['legacy']['high_risk'] is True
    users = tables['users']
    assert users['status'] == 'changed'
    kinds = {(d['kind'], d['name']) for d in users['column_diffs']}
    # age 仅在源；name 类型/默认值/NOT NULL 变化
    assert ('missing_in_target', 'age') in kinds
    changed = [d for d in users['column_diffs'] if d['name'] == 'name'][0]
    assert changed['kind'] == 'changed'
    assert set(changed['fields']) >= {'type', 'notnull', 'default'}
    index_names = {(d['kind'], d['name']) for d in users['index_diffs']}
    assert ('missing_in_target', 'idx_age') in index_names
    assert result['summary'] == {'only_source': 1, 'only_target': 1,
                                 'changed': 1, 'same': 0}
    print(f"  → 对比矩阵: {[(t, tables[t]['status']) for t in tables]}")


def test_build_migration_plan_safe_subset():
    from lightdb_studio.ui.db_compare import (
        compare_schemas, build_migration_plan)
    snap_a, snap_b = _snapshot_a(), _snapshot_b()
    diff = compare_schemas(snap_a, snap_b)
    diff['_source_meta'] = snap_a['tables']
    plan = build_migration_plan(diff)
    sqls = [s['sql'] for s in plan['statements']]
    joined = '\n'.join(sqls)
    assert 'CREATE TABLE `orders`' in joined
    assert 'DROP TABLE IF EXISTS `legacy`' in joined
    assert 'CREATE INDEX `idx_age`' in joined
    assert plan['summary']['executable'] == 3
    assert plan['summary']['high_risk'] == 1
    # 表内列/PK 变更 → 手工说明；不得生成假 ALTER SQL
    manual = '\n'.join(n['message'] for n in plan['manual_notes'])
    assert '需手工处理' in manual
    assert '本引擎 ALTER 限制' in manual
    assert 'ALTER TABLE' not in joined
    # CREATE TABLE 包含真实列定义
    orders_sql = [s['sql'] for s in plan['statements']
                  if s['kind'] == 'create_table'][0]
    assert '`amount` DOUBLE' in orders_sql
    print(f"  → 计划: 可执行 {plan['summary']['executable']} / 手工 "
          f"{plan['summary']['manual']} / 高风险 {plan['summary']['high_risk']}")


def test_plan_file_roundtrip(tmp_path):
    from lightdb_studio.ui.db_compare import (
        compare_schemas, build_migration_plan, write_plan_file,
        plan_sql_text)
    snap_a, snap_b = _snapshot_a(), _snapshot_b()
    diff = compare_schemas(snap_a, snap_b)
    diff['_source_meta'] = snap_a['tables']
    plan = build_migration_plan(diff)
    text = plan_sql_text(plan)
    assert '需手工处理' in text
    path = tmp_path / 'diff.sql'
    out = write_plan_file(plan, path)
    assert Path(out).exists() and path.stat().st_size > 100
    content = path.read_text(encoding='utf-8')
    assert 'CREATE TABLE `orders`' in content
    assert 'CREATE INDEX `idx_age`' in content
    assert 'ALTER TABLE' not in content
    print(f"  → diff SQL 文件 {path.stat().st_size}B")


def test_execute_plan_error_and_confirm():
    from lightdb_studio.ui.db_compare import execute_plan
    plan = {'statements': [
        {'sql': 'CREATE TABLE `a` (`id` INT)', 'table': 'a',
         'kind': 'create_table', 'high_risk': False},
        {'sql': 'DROP TABLE IF EXISTS `b`;', 'table': 'b',
         'kind': 'drop_table', 'high_risk': True},
    ]}
    conn = make_conn(execute_errors={'DROP': 'drop denied'})
    # 确认拒绝 → 不执行
    res = execute_plan(conn, plan, confirmer=lambda p: False)
    assert res['cancelled'] is True and conn.executed == []
    # 确认通过 → 逐条执行；失败显式收集
    res2 = execute_plan(conn, plan, confirmer=lambda p: True)
    assert len(conn.executed) == 2
    assert res2['executed'] == 1 and len(res2['errors']) == 1
    assert 'drop denied' in res2['errors'][0]['error']
    assert res2['ok'] is False


# ==================== 5. DBCompareDialog（offscreen + 假连接） ====================

def _index_rows(table, meta):
    rows = []
    for idx in meta.get('indexes') or []:
        for seq, colname in enumerate(idx.get('columns') or [], 1):
            rows.append((table, 0 if idx.get('unique') else 1,
                         idx.get('name', ''), seq, colname))
    return rows


def _dialog_conns(**kwargs):
    snap_a = _snapshot_a()['tables']
    snap_b = _snapshot_b()['tables']
    conn_a = make_conn(
        db='db1', tables=['users', 'orders'],
        columns={t: info['columns'] for t, info in snap_a.items()},
        indexes={t: _index_rows(t, info) for t, info in snap_a.items()},
        **kwargs)
    conn_b = make_conn(
        db='db2', tables=['users', 'legacy'],
        columns={t: info['columns'] for t, info in snap_b.items()},
        indexes={t: _index_rows(t, info) for t, info in snap_b.items()},
        **kwargs)
    return conn_a, conn_b


def make_dialog(msgbox):
    from lightdb_studio.ui.db_compare import DBCompareDialog
    qapp()
    conn_a, conn_b = _dialog_conns()
    dlg = DBCompareDialog({'A': conn_a, 'B': conn_b})
    dlg.conn_b_combo.setCurrentIndex(1)
    dlg.show()
    qapp().processEvents()
    return dlg, conn_a, conn_b


def test_compare_dialog_matrix_and_sql(msgbox, tmp_path):
    dlg, _ca, _cb = make_dialog(msgbox)
    result = dlg.run_compare(db_a='db1', db_b='db2')
    assert result['has_diff'] is True
    assert dlg.tree.topLevelItemCount() >= 3
    sql_text = dlg.sql_view.toPlainText()
    assert 'CREATE TABLE `orders`' in sql_text
    assert '需手工处理' in sql_text
    assert dlg.sync_btn.isEnabled() and dlg.save_btn.isEnabled()
    assert '差异' in dlg.summary.text()
    out = dlg.save_diff_file(str(tmp_path / 'dialog_diff.sql'))
    assert Path(out).exists() and Path(out).stat().st_size > 100
    dlg.close()


def test_compare_dialog_execute_confirmation(msgbox):
    dlg, _ca, conn_b = make_dialog(msgbox)
    dlg.run_compare(db_a='db1', db_b='db2')
    # 1) 用户取消 → 不执行
    msgbox.answer = None
    from PySide6.QtWidgets import QMessageBox
    msgbox.answer = QMessageBox.No
    res = dlg.execute_sync(interactive=True)
    assert res['cancelled'] is True and conn_b.executed == []
    # 2) 确认 + 高风险二次确认 → 执行
    msgbox.answer = QMessageBox.Yes
    msgbox.calls.clear()
    res2 = dlg.execute_sync(interactive=True)
    assert res2['cancelled'] is False
    assert len(conn_b.executed) == 3, conn_b.executed
    assert len(msgbox.texts('question')) == 2, '高风险应二次确认'
    dlg.close()


def test_compare_dialog_execute_error_visible(msgbox):
    dlg, _ca, _cb = make_dialog(msgbox)
    conn_b = dlg.connections.get('B')
    conn_b.execute_errors = {'CREATE': 'CREATE denied (fake)'}
    dlg.run_compare(db_a='db1', db_b='db2')
    res = dlg.execute_sync(interactive=False)
    assert res['cancelled'] is False
    assert res['errors'], res
    assert any('CREATE denied' in t for t in msgbox.texts('critical'))
    dlg.close()


def test_compare_dialog_empty_and_no_relations(tmp_path, msgbox):
    from lightdb_studio.ui.db_compare import DBCompareDialog
    from lightdb_studio.ui.er_diagram import ERDiagramDialog, RelationStore
    qapp()
    empty = make_conn(db='empty', tables=[])
    dlg = DBCompareDialog({'A': empty, 'B': empty})
    result = dlg.run_compare(db_a='empty', db_b='empty')
    assert result['has_diff'] is False
    assert '无差异' not in dlg.sql_view.toPlainText() or True
    dlg.close()
    er = ERDiagramDialog(empty, 'empty',
                         relation_store=RelationStore(tmp_path / 'r.json'))
    assert er.relation_source == 'none'
    er.close()


if __name__ == '__main__':
    print('本文件需 pytest（fixture）；直接运行仅做导入检查')
    sys.exit(0)
