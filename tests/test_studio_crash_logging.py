# -*- coding: utf-8 -*-
"""P3 崩溃日志 / 脱敏测试（不依赖数据库）。

覆盖：
- redact：password/passwd/pwd、IDENTIFIED BY、Authorization Bearer、token/
  secret/api_key，大小写/中文/多行；
- setup_logging：落盘、重复调用幂等、env 覆盖目录、日志中无明文口令；
- RotatingFileHandler 轮转（5×2MB 配置可用小阈值验证备份生成）；
- sys / threading excepthook → crash_<ts>.log（注入 handler 断言 + 脱敏）；
- Qt 消息处理器安装 + qWarning 脱敏落盘（offscreen）。
"""
import os
import sys
import threading
import time
import traceback
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from lightdb_studio.utils import crashlog

_APP = None


def qapp():
    global _APP
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


@pytest.fixture(autouse=True)
def clean_logging(tmp_path, monkeypatch):
    monkeypatch.setenv('LIGHTDB_STUDIO_LOG_DIR', str(tmp_path / 'logs'))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    crashlog.shutdown_logging()
    crashlog.uninstall_excepthooks()
    yield tmp_path / 'logs'
    crashlog.shutdown_logging()
    crashlog.uninstall_excepthooks()


# ==================== 1. 脱敏 ====================

def test_redact_covers_common_secret_patterns():
    text = (
        'connecting user=root password=SuperSecret1\n'
        'PASSWORD: "中文口令2"  passwd=\'secret3\' pwd:Plain4\n'
        "SQL: CREATE USER u IDENTIFIED BY 'IdBySecret5';\n"
        'Authorization: Bearer abc.def.ghi token=tok123 secret: sec456 '
        'api_key=key789 API_KEY: KEY999\n'
        'multi line --password CliSecret0')
    out = crashlog.redact(text)
    for leaked in ('SuperSecret1', '中文口令2', 'secret3', 'Plain4',
                   'IdBySecret5', 'abc.def.ghi', 'tok123', 'sec456',
                   'key789', 'KEY999', 'CliSecret0'):
        assert leaked not in out, f'未脱敏: {leaked!r}\n{out}'
    assert out.count('***') >= 11, out
    # 非敏感字段保留
    assert 'user=root' in out
    assert '多层' not in out


def test_redact_is_multiline_and_case_insensitive():
    text = 'PASSWORD=abc123\nTOKEN=xyz\nIdentified By \'p@ss\'\n'
    out = crashlog.redact(text)
    for leaked in ('abc123', 'xyz', 'p@ss'):
        assert leaked not in out
    assert out.count('\n') == text.count('\n')


# ==================== 2. setup_logging / 落盘 / 幂等 / 轮转 ====================

def test_setup_logging_writes_redacted_file(clean_logging):
    import logging
    path = crashlog.setup_logging(level=logging.INFO)
    assert path and Path(path).exists()
    assert Path(path).name == 'studio.log'
    logging.getLogger('test.crashlog').info(
        'login user=root password=LogSecret123 token=tokabc')
    crashlog.shutdown_logging()
    text = Path(path).read_text(encoding='utf-8')
    assert 'LogSecret123' not in text
    assert 'tokabc' not in text
    assert '***' in text
    print(f"  → 日志落盘 {len(text)}B，明文口令未出现")


def test_setup_logging_is_idempotent(clean_logging):
    import logging
    crashlog.setup_logging()
    crashlog.setup_logging()
    logger = logging.getLogger('test.crashlog.idem')
    logger.info('unique-marker-idem password=IdemSecret')
    crashlog.shutdown_logging()
    text = (clean_logging / 'studio.log').read_text(encoding='utf-8')
    assert text.count('unique-marker-idem') == 1, '重复 setup 导致日志重复写入'
    assert 'IdemSecret' not in text


def test_log_dir_env_override(clean_logging, monkeypatch):
    alt = clean_logging.parent / 'alt_logs'
    monkeypatch.setenv('LIGHTDB_STUDIO_LOG_DIR', str(alt))
    path = crashlog.setup_logging()
    assert path is not None
    assert Path(path).parent == alt
    assert Path(path).exists()


def test_rotating_file_handler_configuration(clean_logging):
    import logging
    path = crashlog.setup_logging(max_bytes=400, backup_count=2)
    logger = logging.getLogger('test.crashlog.rotate')
    for i in range(30):
        logger.info('rotate message %03d %s', i, 'x' * 80)
    crashlog.shutdown_logging()
    log = clean_logging / 'studio.log'
    backups = sorted(clean_logging.glob('studio.log.*'))
    assert log.exists()
    assert backups, '未触发日志轮转（备份文件不存在）'
    assert len(backups) <= 2, backups
    assert log.stat().st_size <= 2000, log.stat().st_size
    print(f"  → 轮转备份 {len(backups)} 个，主日志 {log.stat().st_size}B")


# ==================== 3. 异常钩子 / crash 文件 ====================

def test_sys_excepthook_writes_redacted_crash_file(clean_logging):
    captured = []
    crashlog.setup_logging()
    crashlog.install_excepthooks(handler=captured.append)
    try:
        raise RuntimeError('boom password=CrashSecret999 token=crashTok')
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    crash_files = list(clean_logging.glob('crash_*.log'))
    assert crash_files, '未生成 crash_*.log'
    text = crash_files[-1].read_text(encoding='utf-8')
    assert 'Traceback' in text and 'RuntimeError' in text
    assert 'CrashSecret999' not in text and 'crashTok' not in text
    assert '***' in text
    assert captured, '可注入 handler 未被调用'
    assert 'CrashSecret999' not in captured[-1]
    print(f"  → crash 文件 {crash_files[-1].name} 已脱敏，handler 已收到")


def test_threading_excepthook_writes_crash(clean_logging):
    captured = []
    crashlog.setup_logging()
    crashlog.install_excepthooks(handler=captured.append)
    try:
        raise ValueError('thread boom password=ThreadSecret456')
    except ValueError:
        exc_type, exc, tb = sys.exc_info()
        args = threading.ExceptHookArgs(
            (exc_type, exc, tb, threading.current_thread()))
        threading.excepthook(args)
    crash_files = list(clean_logging.glob('crash_*.log'))
    assert crash_files, '线程异常未生成 crash 文件'
    text = crash_files[-1].read_text(encoding='utf-8')
    assert 'ThreadSecret456' not in text
    assert captured, '线程异常 handler 未被调用'


# ==================== 4. Qt 消息处理器 ====================

def test_qt_message_handler_redacts_warning(clean_logging):
    qapp()
    crashlog.setup_logging()
    assert crashlog.install_qt_message_handler() is True
    from PySide6.QtCore import qWarning
    qWarning('qt-warning password=QtSecret321')
    crashlog.shutdown_logging()
    text = (clean_logging / 'studio.log').read_text(encoding='utf-8')
    assert 'Qt:' in text and 'qt-warning' in text
    assert 'QtSecret321' not in text
    assert '***' in text
    print('  → Qt qWarning 已映射 logging 并脱敏')
