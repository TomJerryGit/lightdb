# -*- coding: utf-8 -*-
"""崩溃日志 / 脱敏（P3：崩溃与日志）

- `setup_logging()`：RotatingFileHandler（UTF-8，5 × 2MB）写
  `~/.lightdb_studio/logs/studio.log`（`LIGHTDB_STUDIO_LOG_DIR` 可覆盖）+ stderr；
  重复调用幂等；初始化失败只打印到 stderr，不抛异常影响启动。
- `redact()`：password/passwd/pwd、IDENTIFIED BY、Authorization Bearer、
  token/secret/api_key 等一律替换为 `***`；格式化器对整条日志（含 traceback）
  再做一次脱敏，保证明文口令不落盘。
- `install_excepthooks()`：sys / threading excepthook → 主日志 + `crash_<ts>.log`；
- `install_qt_message_handler()`：Qt qDebug/qWarning/qCritical → logging（脱敏）；
- `install_all()`：组合安装；`shutdown_logging()` 供测试清理。
"""
import logging
import os
import re
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

ENV_LOG_DIR = 'LIGHTDB_STUDIO_LOG_DIR'
DEFAULT_LOG_DIR = Path.home() / '.lightdb_studio' / 'logs'
LOG_FILE_NAME = 'studio.log'
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5
REDACTED = '***'
LOGGER_NAME = 'lightdb_studio'
_HANDLER_TAG = '_lightdb_crashlog_handler'
_STATE = {'dir': None, 'file': None, 'handlers': [], 'installed_hooks': None}

# 值可能是带引号字符串、裸字符串（到空白/逗号/分号为止）
_QUOTED_OR_TOKEN = r'(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,;&]+)'
_REDACT_PATTERNS = [
    # password=... / PASSWORD: "..." / pwd: ... / secret=... / token=... / api_key=...
    re.compile(
        r'(?i)(\b(?:password|passwd|pwd|secret|token|api[_-]?key|'
        r'access[_-]?token|refresh[_-]?token)\b\s*[:=]\s*)('
        + _QUOTED_OR_TOKEN + r')'),
    # SQL: IDENTIFIED BY '...'
    re.compile(r'(?i)(IDENTIFIED\s+BY\s+)(' + _QUOTED_OR_TOKEN + r')'),
    # HTTP: Authorization: Bearer xxx
    re.compile(r'(?i)(Authorization\s*:\s*Bearer\s+)(\S+)'),
    # 裸 Bearer xxx（日志里可能只截到 token）
    re.compile(r'(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{6,})'),
    # CLI: --password xxx
    re.compile(r'(?i)(--(?:password|passwd|pwd|token|secret|api-key)\s+)('
               + _QUOTED_OR_TOKEN + r')'),
]


def redact(text) -> str:
    """把文本里的口令/令牌替换为 ``***``（大小写不敏感、多行）。"""
    if text is None:
        return ''
    result = str(text)
    for pattern in _REDACT_PATTERNS:
        result = pattern.sub(lambda m: m.group(1) + REDACTED, result)
    return result


class RedactingFormatter(logging.Formatter):
    """在格式化后统一脱敏（覆盖 traceback / 多行报文）。"""

    def format(self, record):
        return redact(super().format(record))


class RedactingFilter(logging.Filter):
    def filter(self, record):
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args)
        except Exception:
            pass
        return True


def get_log_dir(log_dir=None) -> Path:
    if log_dir:
        return Path(log_dir)
    env_dir = (os.environ.get(ENV_LOG_DIR) or '').strip()
    if env_dir:
        return Path(env_dir)
    return Path.home() / '.lightdb_studio' / 'logs'


def setup_logging(log_dir=None, level=logging.INFO,
                  max_bytes: int = MAX_BYTES,
                  backup_count: int = BACKUP_COUNT):
    """安装日志（幂等）。返回日志文件路径；失败返回 None 并显式 stderr。"""
    global _STATE
    target_dir = get_log_dir(log_dir)
    log_file = target_dir / LOG_FILE_NAME
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger()
        # 幂等：先移除本模块安装过的 handler
        for h in list(logger.handlers):
            if getattr(h, _HANDLER_TAG, False):
                logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        fmt = RedactingFormatter(
            '%(asctime)s %(levelname)s [%(name)s] %(message)s')
        file_handler = RotatingFileHandler(
            str(log_file), maxBytes=max_bytes, backupCount=backup_count,
            encoding='utf-8')
        setattr(file_handler, _HANDLER_TAG, True)
        file_handler.setFormatter(fmt)
        file_handler.addFilter(RedactingFilter())
        file_handler.setLevel(level)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler(sys.stderr)
        setattr(stream_handler, _HANDLER_TAG, True)
        stream_handler.setFormatter(fmt)
        stream_handler.addFilter(RedactingFilter())
        stream_handler.setLevel(level)
        logger.addHandler(stream_handler)

        if logger.level == logging.NOTSET or logger.level > level:
            logger.setLevel(level)
        _STATE.update({'dir': str(target_dir), 'file': str(log_file),
                       'handlers': [file_handler, stream_handler]})
        return str(log_file)
    except Exception as exc:                          # noqa: BLE001
        print(f'[crashlog] 日志初始化失败（继续启动）: {exc}', file=sys.stderr)
        return None


def shutdown_logging():
    """移除并关闭本模块安装的日志 handler（测试清理用）。"""
    logger = logging.getLogger()
    for h in list(logger.handlers):
        if getattr(h, _HANDLER_TAG, False):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    _STATE['handlers'] = []
    return True


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


def _write_crash_file(text: str) -> str:
    log_dir = get_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"crash_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.log"
        path.write_text(text, encoding='utf-8')
        return str(path)
    except Exception as exc:                          # noqa: BLE001
        print(f'[crashlog] 写崩溃日志失败: {exc}', file=sys.stderr)
        return ''


def _format_exception(exc_type, exc_value, exc_tb, thread_name='') -> str:
    text = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    header = (f'-- LightDB Studio crash {time.strftime("%Y-%m-%d %H:%M:%S")}'
              f' thread={thread_name or threading.current_thread().name}\n'
              f'-- Python {sys.version}\n')
    return redact(header + text)


def _capture(exc_type, exc_value, exc_tb, handler=None,
             thread_name='') -> str:
    text = _format_exception(exc_type, exc_value, exc_tb, thread_name)
    crash_path = _write_crash_file(text)
    try:
        get_logger().error('未捕获异常（crash file=%s）:\n%s',
                           crash_path or '(写入失败)', text)
    except Exception:
        pass
    if handler is not None:
        try:
            handler(text)
        except Exception as exc:                      # noqa: BLE001
            print(f'[crashlog] 自定义异常 handler 失败: {exc}', file=sys.stderr)
    return crash_path


def install_excepthooks(handler=None):
    """安装 sys.excepthook + threading.excepthook（可注入 handler 供测试）。"""
    original_sys = sys.excepthook
    original_thread = getattr(threading, 'excepthook', None)

    def _sys_hook(exc_type, exc_value, exc_tb):
        try:
            _capture(exc_type, exc_value, exc_tb, handler)
        except Exception as exc:                      # noqa: BLE001
            print(f'[crashlog] 记录 sys 异常失败: {exc}', file=sys.stderr)
        # 不调用原始 hook：避免 Python 默认 hook 把未脱敏的原样 traceback
        # 打到 stderr（口令不能泄露到终端）。

    def _thread_hook(args):
        try:
            _capture(args.exc_type, args.exc_value, args.exc_traceback,
                     handler, thread_name=getattr(
                         getattr(args, 'thread', None), 'name', ''))
        except Exception as exc:                      # noqa: BLE001
            print(f'[crashlog] 记录线程异常失败: {exc}', file=sys.stderr)
        # 同上：不调用原始 threading.excepthook（避免未脱敏输出）

    sys.excepthook = _sys_hook
    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass
    _STATE['installed_hooks'] = {'sys': original_sys,
                                 'threading': original_thread}
    return True


def uninstall_excepthooks():
    hooks = _STATE.get('installed_hooks') or {}
    if hooks.get('sys') is not None:
        sys.excepthook = hooks['sys']
    if hooks.get('threading') is not None:
        try:
            threading.excepthook = hooks['threading']
        except Exception:
            pass
    _STATE['installed_hooks'] = None
    return True


_qt_handler = None


def install_qt_message_handler():
    """安装 Qt 消息处理器（qDebug/qWarning/qCritical → logging，脱敏）。"""
    global _qt_handler
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
        QtDebugMsg = QtMsgType.QtDebugMsg
        QtInfoMsg = QtMsgType.QtInfoMsg
        QtWarningMsg = QtMsgType.QtWarningMsg
        QtCriticalMsg = QtMsgType.QtCriticalMsg
        QtFatalMsg = QtMsgType.QtFatalMsg
    except Exception as exc:                          # noqa: BLE001
        print(f'[crashlog] 未安装 PySide6，跳过 Qt 消息处理器: {exc}',
              file=sys.stderr)
        return False
    level_map = {
        QtDebugMsg: logging.DEBUG,
        QtInfoMsg: logging.INFO,
        QtWarningMsg: logging.WARNING,
        QtCriticalMsg: logging.ERROR,
        QtFatalMsg: logging.CRITICAL,
    }

    def _handler(msg_type, context, message):
        try:
            level = level_map.get(msg_type, logging.INFO)
            get_logger('lightdb_studio.qt').log(
                level, 'Qt: %s', redact(message))
        except Exception:
            pass

    try:
        qInstallMessageHandler(_handler)
        _qt_handler = _handler
        return True
    except Exception as exc:                          # noqa: BLE001
        print(f'[crashlog] 安装 Qt 消息处理器失败: {exc}', file=sys.stderr)
        return False


def install_all(log_dir=None, level=logging.INFO):
    """组合安装：日志 + 异常钩子 + Qt 消息处理器（每步失败不影响启动）。"""
    log_file = setup_logging(log_dir=log_dir, level=level)
    try:
        install_excepthooks()
    except Exception as exc:                          # noqa: BLE001
        print(f'[crashlog] 安装异常钩子失败: {exc}', file=sys.stderr)
    try:
        install_qt_message_handler()
    except Exception as exc:                          # noqa: BLE001
        print(f'[crashlog] 安装 Qt 处理器失败: {exc}', file=sys.stderr)
    return {'log_file': log_file,
            'log_dir': str(get_log_dir(log_dir)),
            'excepthooks': True}
