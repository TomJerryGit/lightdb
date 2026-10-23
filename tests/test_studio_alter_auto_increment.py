# -*- coding: utf-8 -*-
"""task-48：编辑已有表保存 AUTO_INCREMENT/列变更（最小 ALTER + 真实引擎）。"""
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
        self.columns = columns or [
            {'name': 'id', 'type': 'INT', 'null': 'NO', 'key': 'PRI',
             'extra': '', 'default': None},
            {'name': 'name', 'type': 'VARCHAR(32)', 'null': 'YES',
             'key': '', 'extra': '', 'default': None},
        ]
        self.exec_error = exec_error
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return 0, self.exec_error

    def get_columns(self, table):
        return list(self.columns)

    def query(self, sql, params=None):
        upper = str(sql).upper()
        if 'SHOW CREATE' in upper:
            return ['Table', 'Create Table'], [
                ('t', 'CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(64))')]
        if 'SHOW INDEX' in upper:
            return ['Table', 'Non_unique', 'Key_name', 'Seq_in_index',
                    'Column_name'], [('t', 0, 'PRIMARY', 1, 'id')]
        if 'SHOW TABLE STATUS' in upper:
            return ['Name', 'Auto_increment'], [('t', 10)]
        return [], []


def designer(conn=None):
    from lightdb_studio.ui.table_designer import TableDesigner
    return TableDesigner(conn or FakeConn(), table_name='t')


def change_col(dlg, row, name=None, ctype=None, length=None, nn=None,
               auto=None):
    from PySide6.QtWidgets import QTableWidgetItem
    from lightdb_studio.ui.table_designer import (
        COL_AUTO, COL_LENGTH, COL_NAME, COL_NN, COL_TYPE)
    if name is not None:
        dlg.table.setItem(row, COL_NAME, QTableWidgetItem(name))
    if ctype is not None:
        dlg.table.cellWidget(row, COL_TYPE).setCurrentText(ctype)
    if length is not None:
        dlg.table.setItem(row, COL_LENGTH, QTableWidgetItem(length))
    if nn is not None:
        dlg._checkbox(row, COL_NN).setChecked(nn)
    if auto is not None:
        dlg.set_auto_increment(row, auto)


# ==================== 1. 最小 ALTER 生成 ====================

def test_alter_type_null_default_minimal(home):
    dlg = designer()
    try:
        change_col(dlg, 1, ctype='VARCHAR', length='64', nn=True)
        statements, error = dlg.build_alter_statements()
        assert error == ''
        assert statements == [
            'ALTER TABLE `t` MODIFY COLUMN `name` VARCHAR(64) NOT NULL;']
        # 无变化 → 明确提示
        dlg.original_columns = dlg._normalize_columns(
            dlg._collect_columns())
        assert dlg.build_alter_statements() == ([], '没有可保存的修改')
        # 默认值变化
        from PySide6.QtWidgets import QTableWidgetItem
        from lightdb_studio.ui.table_designer import COL_DEFAULT
        dlg.table.setItem(1, COL_DEFAULT, QTableWidgetItem("'x'"))
        statements, error = dlg.build_alter_statements()
        assert error == '' and 'DEFAULT ' + "'x'" in statements[0]
    finally:
        dlg.close()


def test_alter_add_remove_auto_increment(home):
    dlg = designer()
    try:
        # 原表 id 非自增 → 勾选后生成 MODIFY + AUTO_INCREMENT
        change_col(dlg, 0, auto=True)
        statements, error = dlg.build_alter_statements()
        assert error == ''
        assert statements == [
            'ALTER TABLE `t` MODIFY COLUMN `id` INT NOT NULL AUTO_INCREMENT;']
        # 模拟原表已有自增 → 试图去掉：引擎会静默保留 → 预检拒绝
        dlg.original_columns[0]['auto_increment'] = True
        dlg.original_auto_start = 10
        change_col(dlg, 0, auto=False)
        statements, error = dlg.build_alter_statements()
        assert statements == [] and '无法移除' in error
        # 设置起始号（仅当显式修改；此时原表已非自增 → 重新开启自增）
        dlg.original_columns[0]['auto_increment'] = False
        change_col(dlg, 0, auto=True)
        dlg.set_auto_start(100)
        statements, error = dlg.build_alter_statements()
        assert error == ''
        assert 'ALTER TABLE `t` MODIFY COLUMN `id` INT NOT NULL AUTO_INCREMENT;' \
            in statements
        assert 'ALTER TABLE `t` AUTO_INCREMENT = 100;' in statements
        # 未修改 spin → 不生成 AUTO_INCREMENT 语句
        dlg.set_auto_start(10)
        statements, _ = dlg.build_alter_statements()
        assert not any('AUTO_INCREMENT =' in s for s in statements)
    finally:
        dlg.close()


def test_alter_precheck_unsupported(home):
    dlg = designer()
    try:
        # 添加/删除列预检拒绝
        cols = dlg._collect_columns()
        cols.append({'name': 'extra', 'type': 'INT', 'length': '',
                     'primary': False, 'notnull': False, 'unique': False,
                     'auto_increment': False, 'default': ''})
        statements, error = dlg.build_alter_statements(cols)
        assert statements == [] and '添加/删除列' in error
        cols = dlg._collect_columns()[:-1]
        statements, error = dlg.build_alter_statements(cols)
        assert statements == [] and '添加/删除列' in error
        # 主键/唯一变更（引擎不支持 ALTER 主键/唯一）→ 预检拒绝，不发半套 SQL
        cols = dlg._collect_columns()
        from PySide6.QtWidgets import QTableWidgetItem
        from lightdb_studio.ui.table_designer import COL_UNIQUE
        dlg._checkbox(1, COL_UNIQUE).setChecked(True)
        statements, error = dlg.build_alter_statements()
        assert statements == [] and '唯一约束' in error
        dlg._checkbox(1, COL_UNIQUE).setChecked(False)
        dlg._checkbox(1, 3).setChecked(True)   # primary on name
        statements, error = dlg.build_alter_statements()
        assert statements == [] and '主键' in error
    finally:
        dlg.close()


def test_alter_invalid_auto_rejected(home):
    from lightdb_studio.ui.table_designer import TableDesigner
    dlg = TableDesigner(FakeConn(columns=[
        {'name': 'id', 'type': 'VARCHAR(10)', 'null': 'YES', 'key': 'PRI',
         'extra': '', 'default': None}]))
    dlg.load_existing_table('t')
    try:
        cols = dlg._collect_columns()
        cols[0]['auto_increment'] = True
        statements, error = dlg.build_alter_statements(cols)
        assert statements == [] and '整数' in error
    finally:
        dlg.close()


# ==================== 2. 保存流程（确认/失败保留） ====================

def test_save_alter_confirm_and_failure(home, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    conn = FakeConn()
    dlg = designer(conn)
    try:
        change_col(dlg, 1, ctype='VARCHAR', length='64', nn=True)
        assert dlg._preview_sql() is None
        assert 'MODIFY COLUMN' in dlg.sql_preview.toPlainText()
        # 取消 → 不执行
        monkeypatch.setattr(QMessageBox, 'question',
                            staticmethod(lambda *a, **k: QMessageBox.No))
        assert dlg._on_accept() is False
        assert conn.executed == []
        # 确认 → 执行
        monkeypatch.setattr(QMessageBox, 'question',
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        seen = []
        monkeypatch.setattr(QMessageBox, 'information',
                            staticmethod(lambda *a, **k: seen.append(a[2])))
        assert dlg._on_accept() is True
        assert conn.executed == [
            'ALTER TABLE `t` MODIFY COLUMN `name` VARCHAR(64) NOT NULL;']
        from PySide6.QtWidgets import QDialog
        assert dlg.result() == QDialog.Accepted
        # 失败：显式错误，对话框保留（不 accept）
        conn2 = FakeConn(exec_error='ERROR 1064: fake alter failure')
        dlg2 = designer(conn2)
        try:
            change_col(dlg2, 1, ctype='TEXT')
            monkeypatch.setattr(QMessageBox, 'question',
                                staticmethod(lambda *a, **k: QMessageBox.Yes))
            critical = []
            monkeypatch.setattr(QMessageBox, 'critical',
                                staticmethod(lambda *a, **k:
                                             critical.append(a[2])))
            assert dlg2._on_accept() is False
            assert critical and 'fake alter failure' in critical[0]
            assert 'fake alter failure' in dlg2.last_error
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
    conn = DBConnection(ConnectionInfo(name='alter', host='127.0.0.1',
                                       port=port, user='root', password=''))
    conn.connect()
    return conn, srv, db


def test_real_engine_alter_save_roundtrip(home, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from lightdb_studio.ui.table_designer import (
        TableDesigner, COL_AUTO, COL_LENGTH, COL_NN, COL_TYPE)
    from PySide6.QtWidgets import QTableWidgetItem
    conn, srv, db = _server(tmp_path)
    asked = []
    infos = []
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *a, **k:
                                     asked.append(a[2]) or QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, 'information',
                        staticmethod(lambda *a, **k: infos.append(a[2])))
    monkeypatch.setattr(QMessageBox, 'critical',
                        staticmethod(lambda *a, **k: infos.append(a[2])))
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning',
                        staticmethod(lambda *a, **k: warnings.append(a[2])))
    try:
        assert conn.execute(
            'CREATE TABLE alter_ai (id INT PRIMARY KEY, '
            'name VARCHAR(32), email VARCHAR(64))')[1] is None
        assert conn.execute(
            "INSERT INTO alter_ai (id, name, email) VALUES (1, 'a', 'a@x')")[1] is None
        dlg = TableDesigner(conn, table_name='alter_ai')
        try:
            # 1) 改类型/长度/NULL → 精确 SQL + SHOW CREATE 回显
            from lightdb_studio.ui.table_designer import COL_NAME
            change_col(dlg, 1, ctype='VARCHAR', length='64', nn=True)
            statements, error = dlg.build_alter_statements()
            assert error == ''
            assert statements == [
                'ALTER TABLE `alter_ai` MODIFY COLUMN `name` VARCHAR(64) NOT NULL;']
            assert dlg._on_accept() is True
            _cols, create = conn.query('SHOW CREATE TABLE alter_ai')
            assert 'name VARCHAR(64) NOT NULL' in create[0][1]
            assert 'VARCHAR(64)' in dlg.show_create_view.toPlainText()
            # 2) 加自增
            change_col(dlg, 0, auto=True)
            statements, error = dlg.build_alter_statements()
            assert error == ''
            assert statements and 'AUTO_INCREMENT' in statements[0]
            assert dlg._on_accept() is True
            _cols, create = conn.query('SHOW CREATE TABLE alter_ai')
            assert 'AUTO_INCREMENT' in create[0][1].upper()
            # 3) 设置起始号 100
            dlg.set_auto_start(100)
            statements, error = dlg.build_alter_statements()
            assert error == ''
            assert 'ALTER TABLE `alter_ai` AUTO_INCREMENT = 100;' in statements
            assert dlg._on_accept() is True
            cols, status = conn.query('SHOW TABLE STATUS')
            idx = [str(c).lower() for c in cols].index('auto_increment')
            row = [r for r in status if str(r[0]) == 'alter_ai'][0]
            assert int(row[idx]) == 100
            _cols, create = conn.query('SHOW CREATE TABLE alter_ai')
            assert 'AUTO_INCREMENT=100' in create[0][1]
            # 4) 去自增：本引擎 MODIFY 会静默保留 AUTO_INCREMENT → 预检拒绝
            change_col(dlg, 0, auto=False)
            statements, error = dlg.build_alter_statements()
            assert statements == [] and '无法移除' in error
            assert dlg._on_accept() is False
            assert dlg.last_error and '无法移除' in dlg.last_error
            _cols, create = conn.query('SHOW CREATE TABLE alter_ai')
            assert 'AUTO_INCREMENT' in create[0][1].upper()
            # 对话框保留；新开一个回显对话框验证失败路径（引擎真实错误）
            dlg2 = TableDesigner(conn, table_name='alter_ai')
            try:
                change_col(dlg2, 0, ctype='VARCHAR', length='10')
                critical = []
                monkeypatch.setattr(QMessageBox, 'critical',
                                    staticmethod(lambda *a, **k:
                                                 critical.append(a[2])))
                # id 仍自增 + 改为非整数 → 预检拒绝（不发送 SQL）
                assert dlg2._on_accept() is False
                assert dlg2.last_error
                from PySide6.QtWidgets import QDialog
                assert dlg2.result() != QDialog.Accepted
            finally:
                dlg2.close()
        finally:
            dlg.close()
        assert len(asked) == 3
    finally:
        conn.close()
        for res in (srv, db):
            _shutdown(res)
