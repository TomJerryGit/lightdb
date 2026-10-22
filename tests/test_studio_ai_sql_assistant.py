# -*- coding: utf-8 -*-
"""task-43：AI SQL 助手（建表/存储过程生成 + 离线示例库）offscreen 测试。"""
import json
import os
import sys
import types
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


# ==================== 1. 离线示例库 ====================

def test_examples_all_parser_valid(home):
    from lightdb_studio.ui import ai_examples
    from lightdb_studio.ui.ai_examples import validate_examples
    report = validate_examples()
    assert report['ok'] is True, report['errors']
    assert report['count'] >= 6
    categories = {e['category'] for e in ai_examples.all_examples()}
    assert categories == {'table', 'index', 'view', 'procedure',
                          'query', 'update'}
    for example in ai_examples.all_examples():
        assert example['title'] and example['description']
        assert example['sql'].strip()
        assert 'warning' in example
        assert example['sql'] == ai_examples.get_example(
            example['id'])['sql']


def test_procedure_example_engine_compatible(home):
    from lightdb_studio.core.ai_sql_assistant import procedure_compatibility
    from lightdb_studio.ui.ai_examples import get_example
    proc = get_example('procedure_counter')
    assert procedure_compatibility(proc['sql']) == []
    assert 'CALL' in proc['warning'] or 'SELECT' in proc['warning']


# ==================== 2. 响应解析 / 语法校验 ====================

def test_parse_ai_sql_response_json_code_plain(home):
    from lightdb_studio.core.ai_sql_assistant import (
        AISQLParseError, parse_ai_sql_response)
    sql = 'CREATE TABLE `t_demo` (`id` INT PRIMARY KEY);'
    r1 = parse_ai_sql_response(json.dumps(
        {'sql': sql, 'explanation': 'demo'}, ensure_ascii=False))
    assert r1['sql'] == sql and r1['format'] == 'json'
    assert r1['explanation'] == 'demo'
    r2 = parse_ai_sql_response('说明文字\n```sql\n' + sql + '\n```\n结束')
    assert r2['sql'] == sql and r2['format'] == 'code_block'
    r3 = parse_ai_sql_response('这是 SQL：\n' + sql + '\n请预览')
    assert r3['sql'].startswith('CREATE TABLE') and r3['format'] == 'plain_sql'
    with pytest.raises(AISQLParseError):
        parse_ai_sql_response('抱歉，我不能生成这个。')
    with pytest.raises(AISQLParseError):
        parse_ai_sql_response('')


def test_validate_sql_explicit_failure(home):
    from lightdb_studio.core.ai_sql_assistant import validate_sql
    assert validate_sql('CREATE TABLE `ok` (`id` INT);')['ok'] is True
    bad = validate_sql('SELECT FROM;')
    assert bad['ok'] is False and bad['error']
    assert validate_sql('')['ok'] is False


# ==================== 3. AI 未配置 / 失败显式 ====================

def test_ai_unconfigured_no_network(home, monkeypatch):
    from lightdb_studio.core import ai_integration, ai_sql_assistant
    from lightdb_studio.core.ai_integration import AIClient, AIConfig, AIConfigStore
    called = []

    def _no_network(*a, **k):
        called.append(1)
        raise AssertionError('未配置 AI 时不应发起网络请求')

    monkeypatch.setattr(ai_integration.urllib.request, 'urlopen', _no_network)
    store = AIConfigStore()
    store.load = lambda: AIConfig(provider='disabled')
    assistant = ai_sql_assistant.AISQLAssistant(config_store=store)
    assert assistant.is_configured() is False
    with pytest.raises(ai_sql_assistant.AISQLUnavailableError,
                       match='未配置'):
        assistant.generate_table('创建用户表')
    assert called == [], '未配置时仍发起了网络请求'


class FakeClient:
    def __init__(self, response='', error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.config = types.SimpleNamespace(provider='fake')

    def is_enabled(self):
        return True

    def _chat_sync(self, messages, schema=None):
        self.calls.append((messages, schema))
        if self.error:
            raise self.error
        return self.response


def test_generate_table_with_schema_context(home):
    from lightdb_studio.core.ai_sql_assistant import AISQLAssistant
    sql = ('CREATE TABLE `users` (\n  `id` INT PRIMARY KEY AUTO_INCREMENT,\n'
           '  `name` VARCHAR(64) NOT NULL\n);')
    client = FakeClient(json.dumps({'sql': sql,
                                    'explanation': '创建用户表'}))
    assistant = AISQLAssistant(client=client)
    result = assistant.generate_table(
        '创建用户表:id主键自增,name 非空',
        schema_tables=['users', 'orders'],
        schema_columns={'users': ['id', 'name']})
    assert result.valid is True and result.sql == sql
    assert result.explanation == '创建用户表'
    assert result.engine_compatible is True
    messages, schema = client.calls[0]
    assert '创建用户表' in messages[0]['content']
    assert schema['tables'] == ['users', 'orders']
    assert 'users' in assistant.last_prompt
    assert 'id, name' in assistant.last_prompt


def test_generate_failure_explicit(home):
    from lightdb_studio.core.ai_sql_assistant import (
        AISQLGenerationError, AISQLAssistant)
    assistant = AISQLAssistant(client=FakeClient(error=RuntimeError('401')))
    with pytest.raises(AISQLGenerationError, match='AI 请求失败'):
        assistant.generate_table('建表')


def test_generate_invalid_sql_marked(home):
    from lightdb_studio.core.ai_sql_assistant import AISQLAssistant
    result = AISQLAssistant(client=FakeClient('SELECT FROM;')).generate_table(
        '建表')
    assert result.valid is False and result.error
    assert result.sql == 'SELECT FROM;'


def test_procedure_capability_and_incompatible(monkeypatch, home):
    from lightdb_studio.core import ai_sql_assistant
    from lightdb_studio.core.ai_sql_assistant import (
        AISQLAssistant, PROCEDURE_CAPABILITY,
        probe_procedure_capabilities, procedure_compatibility)
    cap = probe_procedure_capabilities()
    assert cap['parser_accepts_sample'] is True
    assert cap['call_returns_result_set'] is False
    assert cap['notes']
    executable = ('CREATE PROCEDURE p(IN n INT) BEGIN DECLARE v INT; '
                  'SET v = n + 1; END')
    bad = ('CREATE PROCEDURE p(IN n INT) BEGIN DECLARE v INT; '
           'SET v = n + 1; SELECT v; END')
    assert procedure_compatibility(executable) == []
    assert procedure_compatibility(bad), '含结果集 SELECT 必须标为引擎不兼容'
    client = FakeClient(json.dumps({'sql': bad, 'explanation': 'proc'}))
    assistant = AISQLAssistant(client=client)
    result = assistant.generate_procedure('创建过程')
    assert result.valid is True  # parser 通过
    assert result.engine_compatible is False
    assert result.engine_issues
    assert result.capability['call_returns_result_set'] is False
    messages, _schema = client.calls[0]
    assert 'CALL' in assistant.last_prompt or '结果集' in assistant.last_prompt
    assert '创建过程' in messages[0]['content']


# ==================== 4. 对话框：预览/填入/确认 ====================

TABLE_SQL = ('CREATE TABLE `demo` (`id` INT PRIMARY KEY, '
             '`name` VARCHAR(32) NOT NULL);')


def make_dialog(response=TABLE_SQL):
    from lightdb_studio.core.ai_sql_assistant import AISQLAssistant
    from lightdb_studio.ui.ai_sql_dialog import AISQLDialog
    assistant = AISQLAssistant(client=FakeClient(json.dumps({'sql': response})))
    dlg = AISQLDialog(assistant=assistant,
                      schema_provider=lambda: (['users'], {'users': ['id']}))
    return dlg, assistant


def test_dialog_preview_fill_and_confirm_execute(home, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    dlg, _assistant = make_dialog()
    try:
        dlg.description.setPlainText('创建 demo 表')
        result = dlg.generate()
        assert result is not None and result.valid
        assert TABLE_SQL in dlg.preview.toPlainText()
        assert dlg.fill_btn.isEnabled() and dlg.execute_btn.isEnabled()
        inserted = []
        executed = []
        dlg.insert_requested.connect(inserted.append)
        dlg.execute_requested.connect(executed.append)
        assert dlg.fill_editor() is True
        assert inserted == [TABLE_SQL] and executed == []
        # 取消二次确认 → 不执行
        monkeypatch.setattr(QMessageBox, 'question',
                            staticmethod(lambda *a, **k: QMessageBox.No))
        assert dlg.confirm_and_execute() is False
        assert executed == []
        # 确认 → 执行信号
        monkeypatch.setattr(QMessageBox, 'question',
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        assert dlg.confirm_and_execute() is True
        assert executed == [TABLE_SQL]
    finally:
        dlg.close()


def test_dialog_unconfigured_explicit_examples_usable(home):
    from lightdb_studio.core.ai_integration import AIClient, AIConfig
    from lightdb_studio.core.ai_sql_assistant import AISQLAssistant
    from lightdb_studio.ui.ai_sql_dialog import AISQLDialog
    assistant = AISQLAssistant(client=AIClient(AIConfig(provider='disabled')))
    dlg = AISQLDialog(assistant=assistant)
    try:
        dlg.description.setPlainText('建表')
        assert dlg.generate() is None
        assert '未配置' in dlg.status.text()
        assert dlg.preview.toPlainText() == ''
        assert dlg.execute_btn.isEnabled() is False
        # 示例库离线可用
        example = dlg.load_selected_example()
        assert example is not None
        assert dlg.preview.toPlainText().strip()
        assert dlg.fill_btn.isEnabled()
        inserted = []
        dlg.insert_requested.connect(inserted.append)
        assert dlg.fill_editor() is True
        assert len(inserted) == 1
    finally:
        dlg.close()


def test_dialog_invalid_and_procedure_incompatible_blocked(home):
    dlg, _ = make_dialog(response='SELECT FROM;')
    try:
        dlg.description.setPlainText('建表')
        dlg.generate()
        assert dlg.execute_btn.isEnabled() is False
        assert '校验' in dlg.status.text() or '失败' in dlg.status.text()
    finally:
        dlg.close()
    # 过程含结果集 SELECT → parser 通过但禁止执行
    from lightdb_studio.core.ai_sql_assistant import AISQLAssistant
    from lightdb_studio.ui.ai_sql_dialog import AISQLDialog
    proc = ('CREATE PROCEDURE p() BEGIN DECLARE v INT; '
            'SET v = 1; SELECT v; END')
    assistant = AISQLAssistant(client=FakeClient(json.dumps({'sql': proc})))
    dlg2 = AISQLDialog(assistant=assistant)
    try:
        dlg2.kind_combo.setCurrentIndex(1)
        assert 'CALL' in dlg2.procedure_hint.text() or \
            '结果集' in dlg2.procedure_hint.text()
        dlg2.description.setPlainText('创建过程')
        dlg2.generate()
        assert dlg2.execute_btn.isEnabled() is False
        assert '不兼容' in dlg2.status.text()
        executed = []
        dlg2.execute_requested.connect(executed.append)
        assert dlg2.confirm_and_execute() is False
        assert executed == []
    finally:
        dlg2.close()


# ==================== 5. 主窗口菜单接线 ====================

def test_main_window_ai_menu_and_example_insert(home, tmp_path, monkeypatch):
    import lightdb_studio.config as cfg_mod
    from lightdb_studio.config import AppConfig
    from lightdb_studio.ui.main_window import MainWindow
    monkeypatch.setattr(cfg_mod, 'CONFIG_DIR', tmp_path / 'cfg')
    monkeypatch.setattr(cfg_mod, 'CONFIG_FILE',
                        tmp_path / 'cfg' / 'connections.json')
    monkeypatch.setattr(cfg_mod, 'SETTINGS_FILE',
                        tmp_path / 'cfg' / 'settings.json')
    cfg = AppConfig()
    cfg.settings.auto_connect = False
    win = MainWindow(cfg)
    win.do_startup_restore = lambda: None
    try:
        from PySide6.QtGui import QAction
        texts = [a.text() for a in win.findChildren(QAction)]
        assert '🪄 AI SQL 助手...' in texts
        assert '📚 SQL 示例库' in texts
        assert hasattr(win, '_open_ai_sql_dialog')
        assert win._insert_sql_example('create_users') is True
        editor = win.tabs.widget(0).widget(0)
        assert 'CREATE TABLE IF NOT EXISTS demo_users' in \
            editor.toPlainText()
        assert '已插入示例' in win.status_label.text()
    finally:
        win.close()
        qapp().processEvents()
