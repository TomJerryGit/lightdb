"""
LightDB 分级日志（修复版）

修复:
- 补 warning/warn 别名
- 支持动态 set_level
- 支持文件输出 + 轮转
- 线程安全
- 可挂载外部 handler
"""
import os
import sys
import time
import threading
from collections import deque
from datetime import datetime
from pathlib import Path


# ==================== 日志级别 ====================

LEVELS = {
    "DEBUG": 10,
    "INFO": 20,
    "WARN": 30,
    "WARNING": 30,   # 别名
    "ERROR": 40,
    "CRITICAL": 50,
    "FATAL": 50,     # 别名
}


# ==================== Logger ====================

class Logger:
    """
    分级日志。
    - 输出到 stdout/stderr
    - 可选输出到文件
    - 慢查询日志、错误日志环形缓冲
    """

    def __init__(self, name: str = "lightdb",
                 level: str = "INFO",
                 log_dir: str = None,
                 slow_log_size: int = 2000,
                 error_log_size: int = 500,
                 file_max_size: int = 100 * 1024 * 1024,  # 100MB
                 file_backup_count: int = 5):
        self.name = name
        self.level = self._parse_level(level)
        self.lock = threading.RLock()

        # 环形缓冲
        self.slow_log = deque(maxlen=slow_log_size)
        self.error_log = deque(maxlen=error_log_size)

        # 文件输出
        self.log_dir = Path(log_dir) if log_dir else None
        self.file_handle = None
        self.file_max_size = file_max_size
        self.file_backup_count = file_backup_count
        self.file_written = 0

        if self.log_dir:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                self._open_file()
            except Exception as e:
                print(f"[Logger] 打开日志文件失败: {e}",
                      file=sys.stderr)

        # 外部 handler（Studio/Web 可用）
        self._handlers = []

    # ==================== 内部 ====================

    @staticmethod
    def _parse_level(level) -> int:
        if isinstance(level, int):
            return level
        return LEVELS.get(str(level).upper(), 20)

    def _open_file(self):
        self.log_file = self.log_dir / f"{self.name}.log"
        self.file_handle = open(
            self.log_file, 'a', encoding='utf-8')
        try:
            self.file_written = self.file_handle.tell()
        except Exception:
            self.file_written = 0

    def _rotate_if_needed(self):
        if self.file_handle is None:
            return
        try:
            if self.file_written < self.file_max_size:
                return
        except Exception:
            return

        self.file_handle.close()

        # 轮转
        for i in range(self.file_backup_count - 1, 0, -1):
            src = self.log_dir / f"{self.name}.log.{i}"
            dst = self.log_dir / f"{self.name}.log.{i + 1}"
            if src.exists():
                try:
                    src.replace(dst)
                except Exception:
                    pass

        first_backup = self.log_dir / f"{self.name}.log.1"
        try:
            self.log_file.replace(first_backup)
        except Exception:
            pass

        self._open_file()

    # ==================== 级别 ====================

    def set_level(self, level):
        with self.lock:
            self.level = self._parse_level(level)

    def get_level(self) -> str:
        with self.lock:
            for name, val in LEVELS.items():
                if val == self.level and name != "WARNING":
                    return name
            return "INFO"

    # ==================== 核心 ====================

    def _format(self, level: str, msg: str) -> str:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return f"[{ts}] [{level:5s}] [{self.name}] {msg}"

    def _log(self, level: str, msg: str):
        lvl = LEVELS.get(level, 20)
        if lvl < self.level:
            return

        line = self._format(level, msg)

        with self.lock:
            # 控制台
            stream = sys.stderr if lvl >= 40 else sys.stdout
            try:
                print(line, file=stream, flush=True)
            except Exception:
                pass

            # 文件
            if self.file_handle is not None:
                try:
                    self.file_handle.write(line + '\n')
                    self.file_handle.flush()
                    self.file_written += len(line) + 1
                    self._rotate_if_needed()
                except Exception:
                    pass

            # 外部 handler
            for handler in self._handlers:
                try:
                    handler(level, msg, line)
                except Exception:
                    pass

    # ==================== 公开 API ====================

    def debug(self, msg, *args):
        if args:
            msg = msg % args if '%' in msg else f"{msg} {args}"
        self._log("DEBUG", str(msg))

    def info(self, msg, *args):
        if args:
            msg = msg % args if '%' in msg else f"{msg} {args}"
        self._log("INFO", str(msg))

    def warn(self, msg, *args):
        if args:
            msg = msg % args if '%' in msg else f"{msg} {args}"
        self._log("WARN", str(msg))

    # 别名
    warning = warn

    def error(self, msg, *args):
        if args:
            msg = msg % args if '%' in msg else f"{msg} {args}"
        text = str(msg)
        self._log("ERROR", text)
        with self.lock:
            self.error_log.append(
                f"{datetime.now()} {text}")

    def critical(self, msg, *args):
        if args:
            msg = msg % args if '%' in msg else f"{msg} {args}"
        text = str(msg)
        self._log("CRITICAL", text)
        with self.lock:
            self.error_log.append(
                f"{datetime.now()} [CRITICAL] {text}")

    fatal = critical

    def exception(self, msg="", *args):
        """记录异常（含堆栈）"""
        import traceback
        if args:
            msg = msg % args if '%' in msg else f"{msg} {args}"
        tb = traceback.format_exc()
        self.error(f"{msg}\n{tb}")

    # ==================== 慢查询 ====================

    def slow(self, sql: str, elapsed: float, user: str = "",
             threshold: float = 0.5, database: str = ""):
        """记录慢查询"""
        if elapsed < threshold:
            return

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            'ts': time.time(),
            'ts_str': ts,
            'user': user,
            'database': database,
            'elapsed': elapsed,
            'sql': sql[:2000],
        }

        with self.lock:
            self.slow_log.append(entry)

        self._log(
            "WARN",
            f"SLOW QUERY {elapsed:.3f}s "
            f"[{user}@{database}]: {sql[:200]}")

    def get_slow_queries(self, limit: int = 100):
        with self.lock:
            items = list(self.slow_log)[-limit:]
        return list(reversed(items))

    def clear_slow_log(self):
        with self.lock:
            self.slow_log.clear()

    # ==================== 错误日志 ====================

    def get_errors(self, limit: int = 100):
        with self.lock:
            items = list(self.error_log)[-limit:]
        return list(reversed(items))

    def clear_errors(self):
        with self.lock:
            self.error_log.clear()

    # ==================== 外部 handler ====================

    def add_handler(self, fn):
        """
        添加外部 handler。
        fn(level: str, msg: str, formatted_line: str)
        """
        with self.lock:
            self._handlers.append(fn)

    def remove_handler(self, fn):
        with self.lock:
            try:
                self._handlers.remove(fn)
            except ValueError:
                pass

    # ==================== 生命周期 ====================

    def close(self):
        with self.lock:
            if self.file_handle is not None:
                try:
                    self.file_handle.flush()
                    self.file_handle.close()
                except Exception:
                    pass
                self.file_handle = None

    def stats(self) -> dict:
        with self.lock:
            return {
                'name': self.name,
                'level': self.get_level(),
                'slow_log_count': len(self.slow_log),
                'error_log_count': len(self.error_log),
                'file': str(self.log_file)
                if self.file_handle else None,
                'file_written': self.file_written,
            }


# ==================== 全局实例 ====================

# 默认实例（模块导入即可用）
logger = Logger()


def get_logger(name: str = None, **kwargs) -> Logger:
    """
    获取 logger。
    - 无 name 时返回全局实例
    - 有 name 时返回新实例
    """
    if name is None:
        return logger
    return Logger(name=name, **kwargs)


def setup_logger(level: str = "INFO",
                 log_dir: str = None,
                 **kwargs) -> Logger:
    """
    配置全局 logger。
    通常在 main.py 中调用：
        setup_logger(level=cfg.log_level, log_dir='/app/logs')
    """
    global logger
    logger.close()

    logger = Logger(
        name="lightdb",
        level=level,
        log_dir=log_dir,
        **kwargs)
    return logger