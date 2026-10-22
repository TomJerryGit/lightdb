# -*- coding: utf-8 -*-
"""G2（Navicat 第二批）offscreen 测试。

连接导入/导出（密码默认不导出/DPAPI 可选/冲突策略/坏文件不改配置）、
SSL 高级字段校验、补全深度（函数/别名/表列）、JSON + SQL INSERT 导出
（真实文件/转义/取消）、对象树拖拽 payload。
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')
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


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    qapp()
    return tmp_path


@pytest.fixture
def config(home, tmp_path, monkeypatch):
    import lightdb_studio.config as cfg_mod
    from lightdb_studio.config import AppConfig
    monkeypatch.setattr(cfg_mod, 'CONFIG_DIR', tmp_path / 'cfg')
    monkeypatch.setattr(cfg_mod, 'CONFIG_FILE',
                        tmp_path / 'cfg' / 'connections.json')
    monkeypatch.setattr(cfg_mod, 'SETTINGS_FILE',
                        tmp_path / 'cfg' / 'settings.json')
    return AppConfig()


# ==================== 1. 连接导出 / 导入 ====================

def test_export_connections_passwords_default_off(config, tmp_path):
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.ui import connection_dialog as cd
    conn = ConnectionInfo(name='prod', host='db.example', port=3306,
                          user='admin', password='TopSecret!',
                          database='app', ssl=True)
    conn.ssl_options = {'ca': '/etc/ca.pem', 'cert': '/etc/client.pem',
                        'key': '/etc/client.key'}
    config.add_connection(conn)
    path = tmp_path / 'export.json'
    res = cd.export_connections_json(config.connections, path)
    assert res['ok'] and res['count'] == 1
    assert res['passwords_included'] is False and res['sensitive'] is False
    raw = path.read_text(encoding='utf-8')
    assert 'TopSecret!' not in raw
    data = json.loads(raw)
    item = data['connections'][0]
    assert item['name'] == 'prod' and item['port'] == 3306
    assert item['ssl'] is True
    assert item['ssl_options']['ca'] == '/etc/ca.pem'
    assert 'password' not in item
    assert '未导出密码' in res['warning']


def test_export_with_encrypted_password(config, tmp_path):
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.core.credentials import is_encrypted
    from lightdb_studio.ui import connection_dialog as cd
    config.add_connection(ConnectionInfo(name='p', host='h', port=1,
                                        password='SensitivePw'))
    path = tmp_path / 'with_pw.json'
    res = cd.export_connections_json(config.connections, path,
                                     include_passwords=True)
    assert res['ok'] and res['passwords_included'] is True
    raw = path.read_text(encoding='utf-8')
    assert 'SensitivePw' not in raw
    item = json.loads(raw)['connections'][0]
    assert is_encrypted(item['password'])
    assert '敏感' in res['warning']


def test_import_roundtrip_and_conflicts(config, tmp_path):
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.ui import connection_dialog as cd
    src = tmp_path / 'src.json'
    config.add_connection(ConnectionInfo(name='c1', host='h1', port=1,
                                        user='u1', password='pw1'))
    assert cd.export_connections_json(
        config.connections, src, include_passwords=True)['ok']
    # 导入空配置：需要重填密码标记（未含密码导出场景）
    from lightdb_studio.config import AppConfig
    dst = AppConfig()
    no_pw = tmp_path / 'no_pw.json'
    cd.export_connections_json(config.connections, no_pw)
    res = cd.import_connections_json(dst, no_pw)
    assert res['ok'] and res['imported'] == 1
    assert res['needs_password'] == ['c1']
    assert dst.find('c1').host == 'h1'
    # 重名 rename 策略
    res2 = cd.import_connections_json(dst, src, conflict='rename')
    assert res2['ok'] and res2['renamed']
    assert dst.find('c1 (2)') is not None
    # skip 策略
    before = len(dst.connections)
    res3 = cd.import_connections_json(dst, src, conflict='skip')
    assert res3['ok'] and res3['skipped'] == ['c1']
    assert len(dst.connections) == before
    # overwrite 需要确认；取消 → 不改
    old_host = dst.find('c1').host
    cancel = cd.import_connections_json(
        dst, src, conflict='overwrite',
        confirm=lambda q: False)
    assert cancel.get('cancelled') and dst.find('c1').host == old_host
    ok = cd.import_connections_json(
        dst, src, conflict='overwrite', confirm=lambda q: True)
    assert ok['ok'] and dst.find('c1').host == 'h1'


def test_import_bad_json_does_not_touch_connections(config, tmp_path):
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.ui import connection_dialog as cd
    config.add_connection(ConnectionInfo(name='keep', host='h', port=1))
    bad = tmp_path / 'bad.json'
    bad.write_text('{not-json', encoding='utf-8')
    res = cd.import_connections_json(config, bad)
    assert res['ok'] is False and '现有连接未改动' in res['error']
    assert [c.name for c in config.connections] == ['keep']
    # 版本/字段错误
    bad2 = tmp_path / 'bad2.json'
    bad2.write_text(json.dumps({'version': 99, 'connections': []}),
                    encoding='utf-8')
    assert cd.import_connections_json(config, bad2)['ok'] is False
    assert config.find('keep') is not None


# ==================== 2. SSL 高级字段 ====================

def test_ssl_path_validation_and_params(home, tmp_path):
    from lightdb_studio.ui import connection_dialog as cd
    dlg = cd.ConnectionDialog()
    assert dlg.validate_ssl_paths() == (True, '')
    dlg.ssl_check.setChecked(True)
    assert dlg.validate_ssl_paths()[0] is True
    assert dlg.build_ssl_params() == {'check_hostname': False}
    ca = tmp_path / 'ca.pem'
    ca.write_text('ca', encoding='utf-8')
    dlg.ssl_ca_edit.setText(str(ca))
    assert dlg.build_ssl_params()['ca'] == str(ca)
    # 证书/私钥必须成对
    dlg.ssl_cert_edit.setText(str(ca))
    ok, error = dlg.validate_ssl_paths()
    assert not ok and '同时提供' in error
    dlg.ssl_key_edit.setText(str(tmp_path / 'missing.key'))
    ok, error = dlg.validate_ssl_paths()
    assert not ok and '不存在' in error
    dlg.ssl_cert_edit.setText('')
    dlg.ssl_key_edit.setText('')
    dlg.ssl_check.setChecked(False)
    assert dlg.validate_ssl_paths() == (True, '')
    dlg.close()


def test_connection_dialog_collect_ssl(home, tmp_path):
    from lightdb_studio.ui import connection_dialog as cd
    dlg = cd.ConnectionDialog()
    dlg.name_edit.setText('sslconn')
    dlg.ssl_check.setChecked(True)
    ca = tmp_path / 'ca.pem'
    ca.write_text('ca', encoding='utf-8')
    dlg.ssl_ca_edit.setText(str(ca))
    info = dlg._collect()
    assert info.ssl is True
    assert info.ssl_options['ca'] == str(ca)
    dlg.close()


# ==================== 3. 补全深度 ====================

def _editor(sql=''):
    from lightdb_studio.ui.sql_editor import SQLEditor
    e = SQLEditor()
    e.show()
    e.set_schema_provider(lambda: (
        ['users', 'orders'],
        {'users': ['id', 'name'], 'orders': ['id', 'amount']}))
    e.setPlainText(sql)
    cur = e.textCursor()
    cur.movePosition(cur.MoveOperation.End)
    e.setTextCursor(cur)
    return e


def test_function_completion_list(home):
    from lightdb_studio.ui.sql_editor import SQL_FUNCTIONS
    e = _editor()
    for fn in ('COUNT', 'SUM', 'AVG', 'COALESCE', 'IFNULL', 'NOW',
               'DATE_FORMAT', 'JSON_EXTRACT'):
        assert fn in SQL_FUNCTIONS
    candidates = e.completion_candidates('CO', context_table='users')
    assert 'COUNT' in candidates and 'COALESCE' in candidates


def test_table_dot_and_alias_completion(home):
    e = _editor('SELECT * FROM users AS u WHERE u.')
    assert e.schema_aliases() == {'u': 'users'}
    assert e._context_table() == 'users'
    cands = e.completion_candidates('', context_table=e._context_table())
    assert 'name' in cands and 'id' in cands
    assert 'amount' not in cands   # 只提示该表列
    e.setPlainText('SELECT * FROM orders o WHERE o.')
    cur = e.textCursor()
    cur.movePosition(cur.MoveOperation.End)
    e.setTextCursor(cur)
    assert e.schema_aliases() == {'o': 'orders'}
    assert e._context_table() == 'orders'
    cands2 = e.completion_candidates('', context_table=e._context_table())
    assert 'amount' in cands2


def test_auto_complete_switch_and_ctrl_space(home):
    e = _editor('SEL')
    e.set_auto_complete(False)
    assert e.show_completion(force=False) is False
    assert e.show_completion(force=True) is True
    e.set_auto_complete(True)
    assert e._auto_complete_enabled is True


# ==================== 4. JSON / SQL INSERT 导出 ====================

def test_export_json_real_file(home, tmp_path):
    from lightdb_studio.ui.import_export import write_export_chunked
    cols = ['id', 'name', 'amount']
    rows = [[1, "O'Reilly 中文", 12.5], [2, None, None],
            [3, 'line\nbreak', 0.0]]
    path = tmp_path / 'out.json'
    written = write_export_chunked(path, cols, rows, fmt='json')
    assert written == 3
    data = json.loads(path.read_text(encoding='utf-8'))
    assert len(data) == 3
    assert data[0] == {'id': 1, 'name': "O'Reilly 中文", 'amount': 12.5}
    assert data[1]['name'] is None
    assert data[2]['name'] == 'line\nbreak'


def test_export_sql_insert_real_file(home, tmp_path):
    from lightdb_studio.ui.import_export import write_export_chunked
    cols = ['id', 'name', 'amount']
    rows = [[1, "O'Reilly", 12.5], [2, None, None],
            [3, 'back\\slash', 0.0]]
    path = tmp_path / 'out.sql'
    written = write_export_chunked(path, cols, rows, fmt='sql_insert',
                                   table_name='users')
    assert written == 3
    text = path.read_text(encoding='utf-8')
    assert 'INSERT INTO `users` (`id`, `name`, `amount`) VALUES (1,' in text
    assert "O\\'Reilly" in text
    assert 'VALUES (2, NULL, NULL);' in text
    assert 'back\\\\slash' in text


def test_export_cancel_removes_incomplete(home, tmp_path):
    from lightdb_studio.ui.import_export import (
        ExportCancelled, write_export_chunked)
    path = tmp_path / 'cancel.json'
    with pytest.raises(ExportCancelled):
        write_export_chunked(path, ['id'], [[i] for i in range(50)],
                             fmt='json', batch_size=1,
                             cancel_cb=lambda: True)
    assert not path.exists()


def test_export_dialog_formats(home, tmp_path):
    from lightdb_studio.ui.import_export import ExportCSVDialog
    dlg = ExportCSVDialog(['id'], [[1]], default_name='my_table.csv')
    labels = [dlg.fmt_combo.itemData(i) for i in range(dlg.fmt_combo.count())]
    assert set(('csv', 'xlsx', 'json', 'sql_insert')) <= set(labels)
    assert dlg.table_name == 'my_table'
    dlg.fmt_combo.setCurrentIndex(labels.index('json'))
    dlg._on_format_changed(0)
    assert dlg.path_edit.text().endswith('.json')
    dlg.fmt_combo.setCurrentIndex(labels.index('sql_insert'))
    dlg._on_format_changed(0)
    assert dlg.path_edit.text().endswith('.sql')
    dlg.close()


# ==================== 5. 对象树拖拽 ====================

def _tree():
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.ui.object_tree import ObjectTree, NODE_TABLE, NODE_COLUMN
    from PySide6.QtWidgets import QTreeWidgetItem
    from PySide6.QtCore import Qt
    tree = ObjectTree()

    class Fc:
        config = ConnectionInfo(name='fc', host='x', port=1, user='root')

        def execute(self, sql, params=None):
            return 0, None

    tree.connections['fc'] = Fc()
    root = QTreeWidgetItem(tree.tree)
    root.setData(0, Qt.UserRole, ('conn', 'fc'))
    tree._add_db_node(root, 'fc', 'db1')
    db = root.child(0)
    titem = QTreeWidgetItem(db)
    titem.setText(0, 't1')
    titem.setData(0, Qt.UserRole, (NODE_TABLE, 't1'))
    titem.setData(0, Qt.UserRole + 1, {'conn': 'fc', 'db': 'db1'})
    citem = QTreeWidgetItem(titem)
    citem.setText(0, 'id INT')
    citem.setData(0, Qt.UserRole, (NODE_COLUMN, 'id'))
    citem.setData(0, Qt.UserRole + 1,
                  {'conn': 'fc', 'db': 'db1', 'table': 't1', 'type': 'INT'})
    return tree, db, titem, citem


def test_tree_drag_payloads_and_editor_drop(home):
    tree, db, titem, citem = _tree()
    table_payload = tree.drag_payload(titem)
    assert table_payload['kind'] == 'table'
    assert table_payload['text'] == 'SELECT * FROM `t1` LIMIT 200;'
    column_payload = tree.drag_payload(citem)
    assert column_payload['kind'] == 'column'
    assert column_payload['text'] == 'id'
    assert tree.drag_payload(db) is None
    from lightdb_studio.ui.sql_editor import SQLEditor
    e = SQLEditor()
    e.show()
    assert e.drop_payload(table_payload) == 'SELECT * FROM `t1` LIMIT 200;'
    assert 'SELECT * FROM `t1` LIMIT 200;' in e.toPlainText()
    assert e.drop_payload(column_payload) == 'id'
    assert e.drop_payload({'kind': 'db', 'name': 'x'}) == ''


def test_tree_has_custom_drag_mime(home):
    from lightdb_studio.ui.object_tree import ObjectTree, _DraggableTree
    tree = ObjectTree()
    assert isinstance(tree.tree, _DraggableTree)
    assert tree.tree.dragEnabled() is True
    assert ObjectTree.DRAG_MIME == 'application/x-lightdb-object'


# ==================== 6. 主窗口接线 ====================

def test_main_window_g2_menu_actions(home):
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui.main_window import MainWindow
    cfg = AppConfig()
    cfg.settings.auto_connect = False
    win = MainWindow(cfg)
    win.do_startup_restore = lambda: None
    try:
        from PySide6.QtWidgets import QMenu
        texts = [a.text() for m in win.findChildren(QMenu)
                 for a in m.actions()]
        for expected in ('导出连接...', '导入连接...',
                         '导出当前结果为 JSON...',
                         '导出当前结果为 SQL INSERT...'):
            assert expected in texts, texts
        for attr in ('_export_connections', '_import_connections',
                     '_export_current_result_as'):
            assert hasattr(win, attr)
    finally:
        win.close()
        qapp().processEvents()
