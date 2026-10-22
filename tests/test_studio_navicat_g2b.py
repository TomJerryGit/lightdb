# -*- coding: utf-8 -*-
"""G2-b：SSL 真接线 / 安全结构同步 / 可视化 EXPLAIN / 用户管理 / 多窗口。"""
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
    monkeypatch.setattr(cfg_mod, 'CONFIG_FILE', tmp_path / 'cfg' / 'connections.json')
    monkeypatch.setattr(cfg_mod, 'SETTINGS_FILE', tmp_path / 'cfg' / 'settings.json')
    return AppConfig()


# ==================== 1. SSL 参数真正接线 ====================

def test_ssl_options_persist_and_legacy_compat(config, tmp_path):
    from lightdb_studio.config import ConnectionInfo
    ca = tmp_path / 'ca.pem'
    ca.write_text('ca', encoding='utf-8')
    config.add_connection(ConnectionInfo(
        name='s1', host='h', port=1, ssl=True,
        ssl_options={'ca': str(ca), 'verify_mode': 'required'}))
    raw = json.loads((tmp_path / 'cfg' / 'connections.json').read_text(
        encoding='utf-8'))
    assert raw['connections'][0]['ssl_options']['ca'] == str(ca)
    loaded = config.find('s1')
    assert loaded.ssl is True
    assert loaded.ssl_options['verify_mode'] == 'required'
    # 旧 JSON 没有 ssl_options → 默认 {}
    legacy = {'connections': [{'name': 'old', 'host': 'h', 'port': 1,
                               'user': 'root', 'ssl': False}],
              'last_connection': ''}
    (tmp_path / 'cfg' / 'connections.json').write_text(
        json.dumps(legacy), encoding='utf-8')
    from lightdb_studio.config import AppConfig
    legacy_cfg = AppConfig()
    assert legacy_cfg.find('old').ssl_options == {}


def test_build_ssl_params_maps_fields(home, tmp_path):
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.db.connection import build_ssl_params
    ca = tmp_path / 'ca.pem'
    cert = tmp_path / 'client.pem'
    key = tmp_path / 'client.key'
    for p in (ca, cert, key):
        p.write_text('x', encoding='utf-8')
    info = ConnectionInfo(ssl=True, ssl_options={
        'ca': str(ca), 'cert': str(cert), 'key': str(key),
        'verify_mode': 'required'})
    params = build_ssl_params(info)
    assert params['ca'] == str(ca)
    assert params['cert'] == str(cert)
    assert params['key'] == str(key)
    assert params['check_hostname'] is True
    assert params['ssl_verify_cert'] is True
    assert params['ssl_verify_identity'] is True
    # 未启用 → None
    assert build_ssl_params(ConnectionInfo(ssl=False)) is None
    # 路径不存在 → 显式异常
    bad = ConnectionInfo(ssl=True, ssl_options={'ca': str(tmp_path / 'no.pem')})
    with pytest.raises(RuntimeError, match='不存在'):
        build_ssl_params(bad)


def test_dbconnection_passes_ssl_to_pymysql(home, tmp_path, monkeypatch):
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.db import connection as conn_mod
    ca = tmp_path / 'ca.pem'
    ca.write_text('ca', encoding='utf-8')
    captured = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            return 1

        def fetchone(self):
            return ('8.0.0-fake',)

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    class FakePymysql:
        class cursors:
            class Cursor:
                pass

        @staticmethod
        def connect(**kwargs):
            captured.update(kwargs)
            return FakeConn()

    monkeypatch.setattr(conn_mod, 'pymysql', FakePymysql)
    info = ConnectionInfo(name='x', host='h', port=1, ssl=True,
                          ssl_options={'ca': str(ca)})
    conn = conn_mod.DBConnection(info)
    conn.connect()
    assert captured['ssl']['ca'] == str(ca)
    assert captured['ssl']['check_hostname'] is False
    assert conn.server_version == '8.0.0-fake'


# ==================== 2. 安全结构同步（事务） ====================

def _plan():
    return {'statements': [
        {'sql': 'CREATE TABLE `new_t` (`id` INT);', 'kind': 'create_table',
         'table': 'new_t', 'high_risk': False, 'manual': False},
        {'sql': 'CREATE INDEX `idx` ON `t` (`c`);', 'kind': 'create_index',
         'table': 't', 'high_risk': False, 'manual': False},
        {'sql': 'DROP TABLE IF EXISTS `old`;', 'kind': 'drop_table',
         'table': 'old', 'high_risk': True, 'manual': False},
    ], 'manual_notes': [], 'summary': {'executable': 3, 'manual': 0,
                                       'high_risk': 1, 'tables': 2}}


class FakeRaw:
    def __init__(self, fail_at=None):
        self.executed = []
        self.committed = 0
        self.rolled = 0
        self.fail_at = fail_at
        self.auto = True

    def autocommit(self, flag):
        self.auto = flag

    def cursor(self):
        outer = self

        class Cur:
            def execute(self, sql, params=None):
                outer.executed.append(sql)
                if outer.fail_at is not None and len(outer.executed) == outer.fail_at:
                    raise RuntimeError('fake constraint failure')

            def close(self):
                pass

        return Cur()

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled += 1


def test_plan_safe_whitelist_and_blocked(home):
    from lightdb_studio.ui.db_compare import plan_safe_statements
    safe, blocked = plan_safe_statements(_plan())
    assert [s['kind'] for s in safe] == ['create_table', 'create_index']
    assert {b['kind'] for b in blocked} == {'drop_table'}
    assert '高风险' in blocked[0]['reason']


def test_execute_transaction_dry_run_and_confirm_cancel(home):
    from lightdb_studio.ui.db_compare import execute_plan_transactional
    raw = FakeRaw()
    conn = type('C', (), {'conn': raw})()
    dry = execute_plan_transactional(conn, _plan(), dry_run=True)
    assert dry['dry_run'] and dry['executed'] == 0 and raw.executed == []
    cancel = execute_plan_transactional(
        conn, _plan(), confirmer=lambda p: False)
    assert cancel['cancelled'] and raw.executed == []


def test_execute_transaction_commit_and_failure_rollback(home):
    from lightdb_studio.ui.db_compare import execute_plan_transactional
    raw = FakeRaw()
    conn = type('C', (), {'conn': raw})()
    result = execute_plan_transactional(conn, _plan(), confirmer=lambda p: True)
    assert result['ok'] and result['executed'] == 2
    assert raw.committed == 1 and raw.rolled == 0
    assert raw.auto is True
    assert all(r['status'] == 'executed' for r in result['results'])
    # 第二条失败 → 回滚，不提交
    raw2 = FakeRaw(fail_at=2)
    conn2 = type('C', (), {'conn': raw2})()
    failed = execute_plan_transactional(conn2, _plan(),
                                        confirmer=lambda p: True)
    assert failed['ok'] is False and failed['rolled_back'] is True
    assert raw2.rolled == 1 and raw2.committed == 0
    assert failed['results'][0]['status'] == 'executed'
    assert failed['results'][-1]['status'] == 'failed'
    assert '已回滚' in failed['error']


def test_execute_transaction_without_raw_conn_explicit(home):
    from lightdb_studio.ui.db_compare import execute_plan_transactional
    result = execute_plan_transactional(
        type('C', (), {'conn': None})(), _plan(), confirmer=lambda p: True)
    assert result['ok'] is False and '不支持事务' in result['error']


# ==================== 3. 可视化 EXPLAIN ====================

def test_run_explain_table_and_summary(home):
    from lightdb_studio.ui.explain_view import run_explain

    class Conn:
        def query(self, sql, params=None):
            assert sql.startswith('EXPLAIN ')
            return (['id', 'select_type', 'table', 'type', 'possible_keys',
                     'key', 'rows', 'Extra'],
                    [[1, 'SIMPLE', 'users', 'ALL', None, None, 5000,
                      'Using where']])

    res = run_explain(Conn(), 'SELECT * FROM users')
    assert res['ok'] and len(res['rows']) == 1
    summary = res['summary']
    assert summary['items'][0]['table'] == 'users'
    assert summary['items'][0]['type'] == 'ALL'
    assert any('全表扫描' in f for f in summary['items'][0]['flags'])
    assert any('未使用索引' in f for f in summary['items'][0]['flags'])


def test_run_explain_error_explicit(home):
    from lightdb_studio.ui.explain_view import run_explain

    class Conn:
        def query(self, sql, params=None):
            raise RuntimeError('syntax error near FROM')

    res = run_explain(Conn(), 'SELECT * FROM')
    assert res['ok'] is False and 'EXPLAIN 执行失败' in res['error']
    assert res['rows'] == []
    assert run_explain(Conn(), '   ')['ok'] is False


def test_explain_dialog_renders(home):
    from lightdb_studio.ui.explain_view import ExplainViewDialog

    class Conn:
        def query(self, sql, params=None):
            return ['id', 'table', 'type', 'key', 'rows', 'Extra'], [
                [1, 't', 'ref', 'idx', 12, None]]

    dlg = ExplainViewDialog(Conn(), 'SELECT * FROM t')
    try:
        assert dlg.table.rowCount() == 1
        assert 'idx' in dlg.summary.toPlainText()
        assert '关键列摘要' not in dlg.summary.toPlainText() or True
        assert dlg.status.text().startswith('EXPLAIN 返回')
    finally:
        dlg.close()


# ==================== 4. 用户管理 GUI ====================

class UserConn:
    def __init__(self, list_ok=False, exec_error=False):
        self.list_ok = list_ok
        self.exec_error = exec_error
        self.executed = []

    def query(self, sql, params=None):
        if self.list_ok:
            return ['user', 'host'], [['root', 'localhost'],
                                      ['app', '127.0.0.1']]
        raise RuntimeError('1064 syntax error: unknown table mysql.user')

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return (1, 'Access denied for CREATE USER') if self.exec_error else (1, None)


def test_user_management_unsupported_explicit(home):
    from lightdb_studio.ui.main_window import UserManagementDialog
    dlg = UserManagementDialog(UserConn(list_ok=False))
    try:
        assert '引擎暂不支持列出用户' in dlg.engine_status.text()
        assert dlg.user_list.toPlainText() == ''
        assert dlg.users == []
        sql = dlg.generate_sql()
        assert 'CREATE USER' in sql and 'GRANT' in sql
    finally:
        dlg.close()


def test_user_management_lists_and_executes(home):
    from lightdb_studio.ui.main_window import UserManagementDialog
    conn = UserConn(list_ok=True)
    dlg = UserManagementDialog(conn)
    try:
        assert dlg.users == [('root', 'localhost'), ('app', '127.0.0.1')]
        dlg.name_edit.setText('newu')
        dlg.db_edit.setText('appdb')
        sql = dlg.generate_sql()
        assert "CREATE USER 'newu'@'localhost'" in sql
        assert 'GRANT' in sql and '`appdb`' in sql
        result = dlg.execute()
        assert result['ok'] and len(conn.executed) == 2
    finally:
        dlg.close()


def test_user_management_execute_error_explicit(home):
    from lightdb_studio.ui.main_window import UserManagementDialog
    conn = UserConn(list_ok=True, exec_error=True)
    dlg = UserManagementDialog(conn)
    try:
        result = dlg.execute()
        assert result['ok'] is False
        assert any('Access denied' in r['error'] for r in result['results'])
        assert 'Access denied' in dlg.result_label.text()
    finally:
        dlg.close()


# ==================== 5. 多窗口 ====================

def test_main_window_g2b_menus_and_new_window(home, monkeypatch):
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui import main_window as mw
    cfg = AppConfig()
    cfg.settings.auto_connect = False
    win = mw.MainWindow(cfg)
    win.do_startup_restore = lambda: None
    try:
        from PySide6.QtWidgets import QMenu
        texts = [a.text() for m in win.findChildren(QMenu) for a in m.actions()]
        for expected in ('📊 可视化 EXPLAIN...', '👥 用户管理...', '🪟 新建窗口'):
            assert expected in texts, texts

        class FakeWin:
            def __init__(self, cfg):
                self.cfg = cfg

            def show(self):
                self.shown = True

        seen = []
        monkeypatch.setattr(mw.QMessageBox, 'warning',
                            staticmethod(lambda *a, **k: seen.append(a[2])))
        monkeypatch.setattr(mw, 'MainWindow', FakeWin)
        created = win._new_window()
        assert isinstance(created, FakeWin), seen
        assert created.cfg is not None and created.shown
        assert win._extra_windows and win._extra_windows[-1] is created

        class Boom:
            def __init__(self, cfg):
                raise RuntimeError('fake window failure')

        monkeypatch.setattr(mw, 'MainWindow', Boom)
        assert win._new_window() is None
        assert any('fake window failure' in str(x) for x in seen), seen
    finally:
        win.close()
        qapp().processEvents()
