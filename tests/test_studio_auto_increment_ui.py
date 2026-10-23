# -*- coding: utf-8 -*-
"""task-47：表设计器 AUTO_INCREMENT UI（offscreen + 真实 in-process 引擎）。"""
import os
import socket
import sys
import threading
import time
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


class FakeConn:
    def __init__(self, columns=None, exec_error=None):
        self.executed = []
        self.exec_error = exec_error
        self.columns = columns or []
        self.query_calls = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return 0, self.exec_error

    def get_columns(self, table):
        return list(self.columns)

    def query(self, sql, params=None):
        self.query_calls.append(sql)
        upper = str(sql).upper()
        if 'SHOW CREATE' in upper:
            return ['Table', 'Create Table'], [
                ('t', 'CREATE TABLE t (\n id INT NOT NULL AUTO_INCREMENT,\n'
                       ' PRIMARY KEY (id)\n) AUTO_INCREMENT=7')]
        if 'SHOW INDEX' in upper:
            return ['Table', 'Non_unique', 'Key_name', 'Seq_in_index',
                    'Column_name'], [('t', 0, 'PRIMARY', 1, 'id')]
        if 'SHOW TABLE STATUS' in upper:
            return ['Name', 'Auto_increment'], [('t', 7)]
        return [], []


def designer(conn=None):
    from lightdb_studio.ui.table_designer import TableDesigner
    return TableDesigner(conn or FakeConn())


# ==================== 1. 复选框可用性联动 ====================

def test_auto_checkbox_only_integer_key(home):
    from lightdb_studio.ui.table_designer import (
        COL_AUTO, COL_PK, COL_TYPE, COL_UNIQUE)
    dlg = designer()
    try:
        # 默认 id: INT + 主键 → 自增可用
        assert dlg.auto_increment_allowed(0) is True
        cb = dlg._checkbox(0, COL_AUTO)
        assert cb.isEnabled() is True
        assert '合法' in cb.toolTip()
        # 加一列：默认 VARCHAR 非键 → 禁用 + tooltip
        dlg._add_column()
        assert dlg.auto_increment_allowed(1) is False
        cb1 = dlg._checkbox(1, COL_AUTO)
        assert cb1.isEnabled() is False
        assert '整数' in cb1.toolTip()
        # 改成 INT 但仍非键 → 仍禁用，tooltip 说明需要主键/唯一
        dlg.table.cellWidget(1, COL_TYPE).setCurrentText('INT')
        qapp().processEvents()
        assert dlg.auto_increment_allowed(1) is False
        assert '主键或唯一' in dlg._checkbox(1, COL_AUTO).toolTip()
        # 改唯一 → 可用
        dlg._checkbox(1, COL_UNIQUE).setChecked(True)
        qapp().processEvents()
        assert dlg.auto_increment_allowed(1) is True
        assert dlg._checkbox(1, COL_AUTO).isEnabled() is True
        # 改为 BIGINT 仍可用；主键加在非整数列则禁用
        dlg.table.cellWidget(1, COL_TYPE).setCurrentText('BIGINT')
        assert dlg.auto_increment_allowed(1) is True
        dlg.table.cellWidget(1, COL_TYPE).setCurrentText('VARCHAR')
        dlg._checkbox(1, COL_PK).setChecked(True)
        qapp().processEvents()
        assert dlg.auto_increment_allowed(1) is False
        assert dlg._checkbox(1, COL_AUTO).isChecked() is False
    finally:
        dlg.close()


def test_auto_checkbox_disabled_tooltip_and_uncheck(home):
    from lightdb_studio.ui.table_designer import COL_AUTO, COL_TYPE
    dlg = designer()
    try:
        dlg._add_column()
        dlg.table.cellWidget(1, COL_TYPE).setCurrentText('INT')
        cb = dlg._checkbox(1, COL_AUTO)
        # 程序设置勾选后，刷新联动必须自动取消（非法）
        dlg.set_auto_increment(1, True)
        assert cb.isChecked() is False
        assert cb.isEnabled() is False
        assert cb.toolTip()
    finally:
        dlg.close()


# ==================== 2. DDL 生成 ====================

def test_generate_ddl_with_auto_increment(home):
    dlg = designer()
    try:
        dlg.name_edit.setText('auto_t')
        dlg._add_column()
        dlg.table.setItem(1, 0, __import__(
            'PySide6.QtWidgets', fromlist=['QTableWidgetItem']).QTableWidgetItem(
                'name'))
        dlg.table.cellWidget(1, 1).setCurrentText('VARCHAR')
        dlg.table.setItem(1, 2, __import__(
            'PySide6.QtWidgets', fromlist=['QTableWidgetItem']).QTableWidgetItem(
                '64'))
        assert dlg.set_auto_increment(0, True) is True
        sql = dlg._generate_sql()
        assert sql and 'AUTO_INCREMENT' in sql
        assert 'PRIMARY KEY' in sql
        assert '`auto_t`' in sql
        # 唯一列也可自增（非主键）
        dlg2 = designer()
        try:
            dlg2.name_edit.setText('u')
            dlg2._add_column()
            from PySide6.QtWidgets import QTableWidgetItem
            dlg2.table.setItem(1, 0, QTableWidgetItem('code'))
            dlg2.table.cellWidget(1, 1).setCurrentText('INT')
            dlg2._checkbox(1, 5).setChecked(True)
            assert dlg2.set_auto_increment(1, True) is True
            sql2 = dlg2._generate_sql()
            assert 'AUTO_INCREMENT' in sql2 and 'UNIQUE' in sql2
        finally:
            dlg2.close()
    finally:
        dlg.close()


# ==================== 3. 非法勾选阻止 ====================

def test_invalid_auto_validation_blocks(home, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    dlg = designer()
    try:
        dlg.name_edit.setText('bad')
        invalid = [{'name': 'id', 'type': 'VARCHAR', 'length': '10',
                    'primary': True, 'notnull': True, 'unique': False,
                    'auto_increment': True, 'default': ''}]
        ok, error = dlg.validate_columns(invalid)
        assert ok is False and '整数' in error
        ok2, error2 = dlg.validate_columns([{
            'name': 'id', 'type': 'INT', 'length': '', 'primary': False,
            'notnull': False, 'unique': False, 'auto_increment': True,
            'default': ''}])
        assert ok2 is False and '主键或唯一' in error2
        # 强制非法 1 列 + 2 个自增
        bad = [
            {'name': 'a', 'type': 'INT', 'length': '', 'primary': True,
             'notnull': True, 'unique': False, 'auto_increment': True,
             'default': ''},
            {'name': 'b', 'type': 'INT', 'length': '', 'primary': False,
             'notnull': False, 'unique': True, 'auto_increment': True,
             'default': ''}]
        ok3, error3 = dlg.validate_columns(bad)
        assert ok3 is False and '只能设置在一个列' in error3
        # _on_accept 不允许执行；提示显式
        seen = []
        monkeypatch.setattr(QMessageBox, 'warning',
                            staticmethod(lambda *a, **k: seen.append(a[2])))
        dlg._collect_columns = lambda: invalid
        dlg._on_accept()
        assert seen and '整数' in seen[0]
        assert dlg.conn.executed == []
    finally:
        dlg.close()


# ==================== 4. 编辑已有表回显 ====================

def test_edit_existing_reflects_auto_increment(home):
    from lightdb_studio.ui.table_designer import COL_AUTO
    conn = FakeConn(columns=[
        {'name': 'id', 'type': 'INT', 'null': 'NO', 'key': 'PRI',
         'extra': 'auto_increment', 'default': None},
        {'name': 'name', 'type': 'VARCHAR(32)', 'null': 'YES', 'key': '',
         'extra': '', 'default': None}])
    dlg = designer(conn)
    try:
        dlg.load_existing_table('t')
        assert dlg.table.rowCount() == 2
        row0 = dlg._get_row(0)
        assert row0[0] == 'id' and row0[3] is True and row0[6] is True
        assert dlg._checkbox(0, COL_AUTO).isEnabled()
        assert 'AUTO_INCREMENT' in dlg.show_create_view.toPlainText()
        assert 'Auto_increment = 7' in dlg.info_label.text()
        assert dlg.name_edit.isReadOnly()
        # 编辑模式确认不静默执行 ALTER / 不假成功
        from PySide6.QtWidgets import QMessageBox
        monkeypatch_info = []
        import unittest.mock as mock
        with mock.patch.object(QMessageBox, 'information',
                               staticmethod(lambda *a, **k:
                                            monkeypatch_info.append(a[2]))):
            dlg._on_accept()
        assert monkeypatch_info and '回显' in monkeypatch_info[0]
        assert conn.executed == []
    finally:
        dlg.close()


# ==================== 5. 真实 in-process 引擎建表+插入 ====================

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


def test_real_engine_create_insert_auto_increment(home, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
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
    db = ex = srv = None
    conn = None
    seen = []
    monkeypatch.setattr(QMessageBox, 'information',
                        staticmethod(lambda *a, **k: seen.append(a[2])))
    monkeypatch.setattr(QMessageBox, 'critical',
                        staticmethod(lambda *a, **k: seen.append(a[2])))
    try:
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
        conn = DBConnection(ConnectionInfo(name='auto', host='127.0.0.1',
                                           port=port, user='root',
                                           password=''))
        conn.connect()
        dlg = __import__(
            'lightdb_studio.ui.table_designer', fromlist=['TableDesigner']
        ).TableDesigner(conn)
        try:
            dlg.name_edit.setText('auto_ui')
            dlg._add_column()
            from PySide6.QtWidgets import QTableWidgetItem
            dlg.table.setItem(1, 0, QTableWidgetItem('name'))
            dlg.table.cellWidget(1, 1).setCurrentText('VARCHAR')
            dlg.table.setItem(1, 2, QTableWidgetItem('32'))
            assert dlg.set_auto_increment(0, True) is True
            dlg._on_accept()
            assert dlg.last_error == ''
            assert any('表已创建' in s for s in seen), seen
        finally:
            dlg.close()
        # 插入省略自增列 → 引擎生成 id 1,2
        _n, err = conn.execute(
            "INSERT INTO `auto_ui` (`name`) VALUES ('a'), ('b')")
        assert err is None, err
        _cols, rows = conn.query('SELECT id, name FROM auto_ui ORDER BY id')
        assert rows == [(1, 'a'), (2, 'b')], rows
        # 数据网格显示自动 id
        from lightdb_studio.ui.data_editor import DataEditor
        de = DataEditor(conn, 'auto_ui', conn.get_columns('auto_ui'))
        try:
            de.reload()
            qapp().processEvents()
            assert de.table.rowCount() >= 2
            assert de.table.item(0, 0).text() == '1'
            assert de.table.item(1, 0).text() == '2'
        finally:
            de.close()
        # SHOW CREATE / TABLE STATUS 有自增信息
        _cols, ddl_rows = conn.query('SHOW CREATE TABLE auto_ui')
        assert 'AUTO_INCREMENT' in str(ddl_rows[0][1]).upper()
        cols, status_rows = conn.query('SHOW TABLE STATUS')
        idx = [str(c).lower() for c in cols].index('auto_increment')
        row = [r for r in status_rows if str(r[0]) == 'auto_ui'][0]
        assert int(row[idx]) >= 3
    finally:
        if conn is not None:
            conn.close()
        for res in (srv, db):
            _shutdown(res)
