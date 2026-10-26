# -*- coding: utf-8 -*-
"""task-54：表设计器编辑已有表 ADD/DROP COLUMN（预览/确认/失败保留/真实引擎）。"""
import os
import socket
import sys
import threading
import time

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


class FakeConn:
    def __init__(self, exec_error=None):
        self.exec_error = exec_error
        self.executed = []
        self.columns = [
            {'name': 'id', 'type': 'INT', 'null': 'NO', 'key': 'PRI',
             'extra': '', 'default': None},
            {'name': 'name', 'type': 'VARCHAR(32)', 'null': 'YES',
             'key': '', 'extra': '', 'default': None},
        ]

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return 0, self.exec_error

    def get_columns(self, table):
        return list(self.columns)

    def query(self, sql, params=None):
        upper = str(sql).upper()
        if 'SHOW CREATE' in upper:
            return ['Table', 'Create Table'], [(
                't', 'CREATE TABLE t (id INT NOT NULL PRIMARY KEY, '
                     'name VARCHAR(32) NULL)')]
        if 'SHOW INDEX' in upper:
            return ['Table', 'Non_unique', 'Key_name', 'Seq_in_index',
                    'Column_name'], [('t', 0, 'PRIMARY', 1, 'id')]
        if 'SHOW TABLE STATUS' in upper:
            return ['Name', 'Auto_increment'], [('t', None)]
        return [], []


def designer(conn=None):
    from lightdb_studio.ui.table_designer import TableDesigner
    return TableDesigner(conn or FakeConn(), table_name='t')


def add_row(dlg, name, ctype='INT', length='', nn=False, default='',
            primary=False, unique=False, auto=False, position='末尾'):
    """在编辑设计器中新增一行并填写属性。返回行号。"""
    from PySide6.QtWidgets import QTableWidgetItem
    from lightdb_studio.ui.table_designer import (
        COL_DEFAULT, COL_LENGTH, COL_NAME, COL_NN, COL_PK, COL_TYPE,
        COL_UNIQUE)
    r = dlg.table.rowCount()
    dlg._add_column()
    dlg.table.setItem(r, COL_NAME, QTableWidgetItem(name))
    dlg.table.cellWidget(r, COL_TYPE).setCurrentText(ctype)
    dlg.table.setItem(r, COL_LENGTH, QTableWidgetItem(str(length)))
    dlg.table.setItem(r, COL_DEFAULT, QTableWidgetItem(default))
    dlg._checkbox(r, COL_PK).setChecked(primary)
    dlg._checkbox(r, COL_NN).setChecked(nn)
    dlg._checkbox(r, COL_UNIQUE).setChecked(unique)
    dlg.set_column_position(r, position)
    if auto:
        assert dlg.set_auto_increment(r, True) is True
    return r


def set_pos(dlg, row, value):
    return dlg.set_column_position(row, value)


# ==================== 1. ADD 位置 SQL 精确匹配 ====================

def test_add_column_tail_first_after_sql(home):
    dlg = designer()
    try:
        r = add_row(dlg, 'age', 'INT', length='', position='末尾')
        statements, error = dlg.build_alter_statements()
        assert error == ''
        assert statements == ['ALTER TABLE `t` ADD COLUMN `age` INT NULL;']
        set_pos(dlg, r, 'FIRST')
        statements, _ = dlg.build_alter_statements()
        assert statements == [
            'ALTER TABLE `t` ADD COLUMN `age` INT NULL FIRST;']
        set_pos(dlg, r, 'AFTER name')
        statements, _ = dlg.build_alter_statements()
        assert statements == [
            'ALTER TABLE `t` ADD COLUMN `age` INT NULL AFTER `name`;']
        # DEFAULT 值 / NOT NULL
        add_row(dlg, 'note', 'VARCHAR', '20', nn=True, default="'x'")
        statements, _ = dlg.build_alter_statements()
        assert any('ADD COLUMN `note` VARCHAR(20) NOT NULL DEFAULT ' + "'x'"
                   in s for s in statements)
    finally:
        dlg.close()


def test_add_auto_increment_requires_unique(home):
    dlg = designer()
    try:
        r = add_row(dlg, 'ai', 'INT', unique=True)
        assert dlg.set_auto_increment(r, True) is True
        statements, error = dlg.build_alter_statements()
        assert error == ''
        assert statements == [
            'ALTER TABLE `t` ADD COLUMN `ai` INT NOT NULL UNIQUE '
            'AUTO_INCREMENT;']
        # 新列勾主键 → 引擎不支持 ADD PRIMARY KEY，预检拒绝
        add_row(dlg, 'pk2', 'INT', primary=True)
        statements, error = dlg.build_alter_statements()
        assert statements == [] and 'PRIMARY KEY' in error
    finally:
        dlg.close()


def test_add_drop_multiple_order_safe(home):
    dlg = designer()
    try:
        # 新增 age（尾部），删除现有 name
        add_row(dlg, 'age', 'INT', length='')
        dlg.table.selectRow(1)  # name
        dlg._del_column()
        statements, error = dlg.build_alter_statements()
        assert error == ''
        assert statements == [
            'ALTER TABLE `t` DROP COLUMN `name`;',
            'ALTER TABLE `t` ADD COLUMN `age` INT NULL;',
        ]
        # 再加一个 FIRST + 一个 AFTER age，顺序展示全部 SQL
        add_row(dlg, 'first2', 'VARCHAR', '10', position='FIRST')
        add_row(dlg, 'after2', 'INT', position='AFTER age')
        statements, error = dlg.build_alter_statements()
        assert error == ''
        assert statements[0] == 'ALTER TABLE `t` DROP COLUMN `name`;'
        assert any('ADD COLUMN `first2` VARCHAR(10) NULL FIRST;' in s
                   for s in statements)
        assert any('ADD COLUMN `after2` INT NULL AFTER `age`;' in s
                   for s in statements)
        # 位置锚点被删除 → 预检拒绝
        dlg2 = designer()
        try:
            add_row(dlg2, 'bad', 'INT', position='AFTER name')
            dlg2.table.selectRow(1)
            dlg2._del_column()
            # 删除 name 后，手工把位置强制设为已删除的锚点 → 预检拒绝
            set_pos(dlg2, 1, 'AFTER name')
            statements, error = dlg2.build_alter_statements()
            assert statements == [] and '将被删除' in error
        finally:
            dlg2.close()
    finally:
        dlg.close()


# ==================== 2. 预览 / 确认 / 失败保留 ====================

def test_preview_confirm_and_failure_keeps_dialog(home, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    conn = FakeConn()
    dlg = designer(conn)
    try:
        add_row(dlg, 'age', 'INT')
        dlg._preview_sql()
        assert 'ADD COLUMN' in dlg.sql_preview.toPlainText()
        # 取消 → 不执行
        monkeypatch.setattr(QMessageBox, 'question',
                            staticmethod(lambda *a, **k: QMessageBox.No))
        assert dlg._on_accept() is False
        assert conn.executed == []
        # 确认 → 执行；成功 accept
        monkeypatch.setattr(QMessageBox, 'question',
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        infos = []
        monkeypatch.setattr(QMessageBox, 'information',
                            staticmethod(lambda *a, **k: infos.append(a[2])))
        assert dlg._on_accept() is True
        assert conn.executed == ['ALTER TABLE `t` ADD COLUMN `age` INT NULL;']
        assert dlg.last_execution_results[-1]['ok'] is True
        # 失败：引擎真实错误 + 已执行/未执行 + 对话框保留
        conn2 = FakeConn(exec_error='错误码  : 1091\n报文: DROP 被索引引用')
        dlg2 = designer(conn2)
        try:
            dlg2.table.selectRow(1)
            dlg2._del_column()
            add_row(dlg2, 'age', 'INT')
            question_text = []
            monkeypatch.setattr(QMessageBox, 'question',
                                staticmethod(lambda *a, **k:
                                             question_text.append(a[2]) or
                                             QMessageBox.Yes))
            critical = []
            monkeypatch.setattr(QMessageBox, 'critical',
                                staticmethod(lambda *a, **k:
                                             critical.append(a[2])))
            assert dlg2._on_accept() is False
            assert critical and '1091' in critical[0]
            assert '已执行 0 条' in critical[0] and '剩余 1 条未执行' in critical[0]
            assert any('不可逆' in str(t) for t in question_text)
            assert dlg2.last_failure_index == 0
            from PySide6.QtWidgets import QDialog
            assert dlg2.result() != QDialog.Accepted
        finally:
            dlg2.close()
    finally:
        dlg.close()


# ==================== 3. 真实 in-process 引擎 ====================

def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _shutdown(res):
    if res is None:
        return
    for name in ('shutdown', 'server_close', 'stop', 'close'):
        fn = getattr(res, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


def _server(tmp_path):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.db.connection import DBConnection
    port = _free_port()
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    db = Database(str(tmp_path / 'data'))
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout=0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.05)
    conn = DBConnection(ConnectionInfo(name='addrop', host='127.0.0.1',
                                       port=port, user='root', password=''))
    conn.connect()
    return conn, srv, db


def _patch_yes(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    calls = {'questions': [], 'criticals': [], 'infos': []}
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *a, **k:
                                     calls['questions'].append(a[2]) or
                                     QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, 'critical',
                        staticmethod(lambda *a, **k:
                                     calls['criticals'].append(a[2])))
    monkeypatch.setattr(QMessageBox, 'information',
                        staticmethod(lambda *a, **k:
                                     calls['infos'].append(a[2])))
    monkeypatch.setattr(QMessageBox, 'warning',
                        staticmethod(lambda *a, **k:
                                     calls['infos'].append(a[2])))
    return calls


def test_real_engine_add_positions_and_refresh(home, tmp_path, monkeypatch):
    from lightdb_studio.ui.table_designer import TableDesigner
    conn, srv, db = _server(tmp_path)
    _patch_yes(monkeypatch)
    try:
        assert conn.execute(
            'CREATE TABLE add_t (id INT PRIMARY KEY, name VARCHAR(32))')[1] is None
        assert conn.execute(
            "INSERT INTO add_t (id, name) VALUES (1, 'a')")[1] is None
        dlg = TableDesigner(conn, table_name='add_t')
        try:
            # 尾部
            add_row(dlg, 'age', 'INT', length='', default='5')
            statements, error = dlg.build_alter_statements()
            assert error == ''
            assert statements == [
                'ALTER TABLE `add_t` ADD COLUMN `age` INT NULL DEFAULT 5;']
            assert dlg._on_accept() is True
            cols = [c['name'] for c in conn.get_columns('add_t')]
            assert cols == ['id', 'name', 'age'], cols
            assert dlg.table.rowCount() == 3
            assert 'ADD COLUMN' in dlg.show_create_view.toPlainText() or \
                'age' in dlg.show_create_view.toPlainText()
            # FIRST
            add_row(dlg, 'first_col', 'VARCHAR', '10', position='FIRST')
            assert dlg._on_accept() is True
            cols = [c['name'] for c in conn.get_columns('add_t')]
            assert cols[0] == 'first_col', cols
            # AFTER name
            add_row(dlg, 'after_col', 'BIGINT', length='',
                    position='AFTER name')
            assert dlg._on_accept() is True
            cols = [c['name'] for c in conn.get_columns('add_t')]
            assert cols.index('after_col') == cols.index('name') + 1, cols
            # NOT NULL DEFAULT 回填
            add_row(dlg, 'note', 'VARCHAR', '20', nn=True, default="'x'")
            assert dlg._on_accept() is True
            _cols, rows = conn.query('SELECT note FROM add_t')
            assert rows == [('x',)], rows
            # 自增新列（UNIQUE）
            add_row(dlg, 'ai', 'INT', unique=True, auto=True)
            assert dlg._on_accept() is True
            cols_meta = {c['name']: c for c in conn.get_columns('add_t')}
            assert 'auto_increment' in str(
                cols_meta['ai'].get('extra', '')).lower()
            assert dlg.table.rowCount() == len(cols_meta)
        finally:
            dlg.close()
    finally:
        conn.close()
        for res in (srv, db):
            _shutdown(res)


def test_real_engine_drop_column_errors_and_success(home, tmp_path, monkeypatch):
    from lightdb_studio.ui.table_designer import TableDesigner
    conn, srv, db = _server(tmp_path)
    calls = _patch_yes(monkeypatch)
    try:
        assert conn.execute(
            'CREATE TABLE drop_t (id INT PRIMARY KEY, name VARCHAR(32), '
            'email VARCHAR(64))')[1] is None
        assert conn.execute('CREATE INDEX idx_email ON drop_t(email)')[1] is None
        dlg = TableDesigner(conn, table_name='drop_t')
        try:
            # 删除索引列 → 1091 原样显示，对话框保留
            dlg.table.selectRow(2)  # email
            dlg._del_column()
            assert dlg._on_accept() is False
            assert dlg.last_error and '1091' in dlg.last_error
            assert any('1091' in str(c) for c in calls['criticals'])
            from PySide6.QtWidgets import QDialog
            assert dlg.result() != QDialog.Accepted
            # 先 DROP INDEX，再删列成功
            assert conn.execute('DROP INDEX idx_email ON drop_t')[1] is None
            assert dlg._on_accept() is True
            cols = [c['name'] for c in conn.get_columns('drop_t')]
            assert cols == ['id', 'name'], cols
            _cols, ddl = conn.query('SHOW CREATE TABLE drop_t')
            assert 'email' not in ddl[0][1]
        finally:
            dlg.close()
        # 最后一列 → 1090 原样显示
        assert conn.execute('CREATE TABLE single (only_col INT)')[1] is None
        dlg2 = TableDesigner(conn, table_name='single')
        try:
            dlg2.table.selectRow(0)
            dlg2._del_column()
            assert dlg2._on_accept() is False
            assert '1090' in dlg2.last_error
            assert any('1090' in str(c) for c in calls['criticals'])
        finally:
            dlg2.close()
    finally:
        conn.close()
        for res in (srv, db):
            _shutdown(res)


def test_real_engine_multi_change_stops_on_first_failure(home, tmp_path,
                                                        monkeypatch):
    from lightdb_studio.ui.table_designer import TableDesigner
    conn, srv, db = _server(tmp_path)
    calls = _patch_yes(monkeypatch)
    try:
        assert conn.execute(
            'CREATE TABLE multi (id INT PRIMARY KEY, email VARCHAR(64), '
            'old_col INT)')[1] is None
        assert conn.execute('CREATE INDEX idx_email ON multi(email)')[1] is None
        dlg = TableDesigner(conn, table_name='multi')
        try:
            add_row(dlg, 'added', 'INT')
            # 删除被索引的 email → DROP 在列表最前，失败后 ADED 不应执行
            dlg.table.selectRow(1)
            dlg._del_column()
            statements, error = dlg.build_alter_statements()
            assert error == ''
            assert statements[0] == 'ALTER TABLE `multi` DROP COLUMN `email`;'
            assert 'ADD COLUMN `added`' in statements[-1]
            assert dlg._on_accept() is False
            assert dlg.last_error and '1091' in dlg.last_error
            assert dlg.last_failure_index == 0
            assert len(dlg.last_execution_results) == 1
            assert dlg.last_execution_results[0]['ok'] is False
            cols = [c['name'] for c in conn.get_columns('multi')]
            assert 'added' not in cols and 'email' in cols, cols
            assert any('已执行 0 条' in str(c) and '未执行' in str(c)
                       for c in calls['criticals'])
            # 移除索引后重试：多列变更按 DROP → ADD 顺序执行成功
            assert conn.execute('DROP INDEX idx_email ON multi')[1] is None
            assert dlg._on_accept() is True
            cols = [c['name'] for c in conn.get_columns('multi')]
            assert 'email' not in cols and 'added' in cols, cols
            assert dlg.last_failure_index is None
            assert all(r['ok'] for r in dlg.last_execution_results)
        finally:
            dlg.close()
    finally:
        conn.close()
        for res in (srv, db):
            _shutdown(res)
