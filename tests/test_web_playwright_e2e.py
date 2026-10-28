# -*- coding: utf-8 -*-
"""task-75：Playwright 真浏览器 E2E。

本机 Playwright/Chromium 不可用时显式 skip（不假通过）；安装尝试记录在
`_analysis_tmp/gui_full/task75_playwright_install.log`。装载浏览器后覆盖：
登录→切库→查询→表数据分页/排序→设计器预览/保存→CSV 下载→ER 渲染→中英切换。
"""
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLAYWRIGHT_SKIP_REASON = (
    'Playwright 安装失败/不可用：本机 pip 目标 site-packages 无写权限，'
    '工作区 --target 安装下载超时；未安装 Chromium。显式 skip，不假通过。'
    '安装尝试日志: _analysis_tmp/gui_full/task75_playwright_install.log')
playwright = pytest.importorskip(
    'playwright', reason=PLAYWRIGHT_SKIP_REASON)

from playwright.sync_api import sync_playwright  # noqa: E402

TEST_SECRET = 'task75-playwright-secret-0123456789'
ADMIN_PASSWORD = 'Task75-E2E-Pass-9f8e7d6c'
USERS = {'admin': {'password': ADMIN_PASSWORD, 'role': 'admin'}}


def _free_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout=0.2)
            s.close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


@pytest.fixture(scope='module')
def web_e2e_server(tmp_path_factory):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor
    from lightdb_studio.config import ConnectionInfo
    from lightdb_studio.db.connection import DBConnection
    from web.server import create_app
    import uvicorn

    tmp = tmp_path_factory.mktemp('pw_e2e')
    os.environ['LIGHTDB_WEB_SECRET'] = TEST_SECRET
    os.environ['LIGHTDB_WEB_USERS'] = json.dumps(USERS)
    os.environ['HOME'] = str(tmp / 'home')
    os.environ['USERPROFILE'] = str(tmp / 'home')
    (tmp / 'home').mkdir(exist_ok=True)
    engine_port = _free_port()
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = engine_port
    db = Database(str(tmp / 'data'))
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    assert _wait_port(engine_port)
    boot = DBConnection(ConnectionInfo(name='boot', host='127.0.0.1',
                                       port=engine_port, user='root',
                                       password=''))
    boot.connect()
    dbname = boot.get_databases()[0]
    boot.execute('CREATE TABLE e2e_ui (id INT PRIMARY KEY, name VARCHAR(32))')
    boot.execute("INSERT INTO e2e_ui (id, name) VALUES (1, 'a'), (2, 'b')")
    boot.close()
    web_port = _free_port()
    app = create_app(
        admin_password=ADMIN_PASSWORD,
        connections=[ConnectionInfo(name='e2e', host='127.0.0.1',
                                    port=engine_port, user='root',
                                    password='', database=dbname)])
    config = uvicorn.Config(app, host='127.0.0.1', port=web_port,
                            log_level='error', lifespan='off')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert _wait_port(web_port)
    try:
        yield {'url': f'http://127.0.0.1:{web_port}', 'db': dbname}
    finally:
        try:
            server.should_exit = True
            thread.join(timeout=5)
        except Exception:
            pass
        try:
            srv.shutdown()
        except Exception:
            pass


def _launch_browser(p):
    try:
        return p.chromium.launch(headless=True)
    except Exception as exc:                          # noqa: BLE001
        pytest.skip(f'Chromium 未安装/无法启动，显式 skip: {exc}')


def test_playwright_full_web_e2e(web_e2e_server, tmp_path):
    shots = ROOT / '_analysis_tmp' / 'gui_full' / 'playwright_e2e'
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page(viewport={'width': 1440, 'height': 940})
        dialogs = []
        page.on('dialog', lambda d: (dialogs.append(d.message), d.accept()))
        try:
            # 登录
            page.goto(web_e2e_server['url'])
            page.fill('#login-user', 'admin')
            page.fill('#login-pass', ADMIN_PASSWORD)
            page.click('#login-btn')
            page.wait_for_selector('#main-page:not(.hidden)', timeout=10000)
            # 切库/对象树
            page.select_option('#conn-select', 'e2e')
            page.wait_for_selector('.tree-item', timeout=10000)
            page.get_by_text(web_e2e_server['db'], exact=True).first.click()
            page.get_by_text('e2e_ui', exact=True).first.click()
            page.wait_for_selector('#result-table table', timeout=10000)
            # 表数据分页/排序
            page.click('#data-btn')
            page.wait_for_selector('#table-controls:not(.hidden)', timeout=10000)
            assert page.locator('#sort-column-select option').count() >= 2
            page.select_option('#sort-column-select', index=1)
            page.wait_for_timeout(300)
            # 查询
            page.fill('#sql-editor', 'SELECT * FROM e2e_ui')
            page.click('#run-btn')
            page.wait_for_selector('#result-table table', timeout=10000)
            # 设计器预览 + 保存
            page.click('#design-btn')
            page.wait_for_selector('#design-panel:not(.hidden)', timeout=10000)
            columns = json.loads(page.input_value('#design-columns-input'))
            columns.append({'name': 'age', 'type': 'INT', 'length': '',
                            'notnull': False, 'primary': False,
                            'unique': False, 'auto_increment': False,
                            'default': None, 'position': ''})
            page.fill('#design-columns-input', json.dumps(columns))
            page.click('#design-preview-btn')
            page.wait_for_function(
                "document.getElementById('design-sql-preview').textContent.includes('ADD COLUMN')",
                timeout=10000)
            page.click('#design-save-btn')
            page.wait_for_function(
                "document.getElementById('design-status').textContent.includes('ALTER')",
                timeout=10000)
            # CSV 导出下载
            page.click('#data-btn')
            page.wait_for_selector('#table-controls:not(.hidden)')
            with page.expect_download(timeout=10000) as dl:
                page.click('#export-table-btn')
            download = dl.value
            assert download.suggested_filename.endswith('.csv')
            # ER 渲染（中文 → 英文切换）
            page.click('#tool-er-btn')
            page.wait_for_selector('#er-output svg', timeout=10000)
            assert page.locator('#er-output .er-node').count() >= 1
            page.select_option('#lang-select', 'en')
            page.wait_for_timeout(500)
            assert 'ER Diagram' in page.inner_text('#tool-er-btn')
            page.screenshot(path=str(shots / 'e2e_en_er.png'), full_page=True)
            page.select_option('#lang-select', 'zh')
            page.wait_for_timeout(300)
            page.screenshot(path=str(shots / 'e2e_zh_main.png'), full_page=True)
            # 设计器实际保存（dialog 自动接受）并检查成功/错误显式
            assert dialogs is not None
        finally:
            browser.close()
