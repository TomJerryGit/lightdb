# -*- coding: utf-8 -*-
"""后台查询 worker（P1-1 响应性）。

设计：
- `DBConnection.query_chunked()` / `DBConnection.execute()` 只在 worker 线程
  调用；worker **绝不**触碰任何 QWidget。
- 结果 / 异常 / 取消通过 Qt 信号回到主线程（跨线程 emit 自动 QueuedConnection，
  由主线程事件循环投递），主线程再更新 ResultTable/状态栏。
- QRunnable 设置 `setAutoDelete(False)`：由调用方持有 worker 与 signals 引用，
  避免 C++ 对象在排队信号投递前被线程池删除（PySide 常见崩溃点）。
- 取消：主线程调用 `cancel(reason)` 置位线程安全事件；主窗口随后用**另一条
  独立连接**执行 `KILL <thread_id>`（本引擎 KILL 是终断连接，不支持 KILL QUERY），
  被取消连接需要重连。
"""
import threading
import time
import traceback

from PySide6.QtCore import QObject, QRunnable, Signal

# 与 MainWindow 同步路径保持一致的"查询类语句"判定
_QUERY_PREFIXES = ('SELECT', 'SHOW', 'DESCRIBE', 'DESC', 'EXPLAIN', 'WITH')


class QueryCancelled(Exception):
    """worker 内部：检测到取消请求后主动中止。"""


class QueryStatementError(Exception):
    """execute() 返回的完整错误文本（format_db_error 格式）包装。"""

    def __init__(self, text: str, statement: str = ''):
        self.text = str(text or '')
        self.statement = statement or ''
        super().__init__(self.text)


def is_query_statement(stmt: str) -> bool:
    return str(stmt).lstrip().upper().startswith(_QUERY_PREFIXES)


class QuerySignals(QObject):
    """主线程接收端：全部由 worker 线程 emit，由 Qt 投递回主线程。"""
    result = Signal(object)       # dict（见 QueryWorker.run 注释）
    failed = Signal(object)       # BaseException（DBQueryError 保留 errno/sqlstate）
    cancelled = Signal(str)       # 取消/超时原因
    finished = Signal()           # 无论成功/失败/取消，最后都会发一次


class QueryWorker(QRunnable):
    """在 QThreadPool 里执行一组 SQL 语句。

    `run()` 结束后（且仅一次）依次 emit 以下之一：
      * `signals.result(payload)`   —— 成功
      * `signals.failed(exc)`       —— 失败（未取消）
      * `signals.cancelled(reason)` —— 取消/超时
    最后一定 emit `signals.finished()`；payload 结构：
        {'columns': [...], 'rows': [...], 'truncated': bool,
         'fetched': int, 'affected': int, 'message': str,
         'elapsed_ms': float, 'statements': [...], 'sql': str,
         'thread_id': int|None}
    """

    def __init__(self, conn, statements, max_rows=None, chunk_size=1000):
        super().__init__()
        self.setAutoDelete(False)
        self.conn = conn
        self.statements = [str(s).strip() for s in statements if str(s).strip()]
        self.max_rows = int(max_rows) if max_rows and int(max_rows) > 0 else None
        self.chunk_size = max(50, int(chunk_size or 1000))
        self.signals = QuerySignals()
        self._cancel_event = threading.Event()
        self._cancel_reason = ''
        self.thread_id = self._read_thread_id()

    # ---------- 取消 ----------

    def _read_thread_id(self):
        try:
            tid = self.conn.thread_id()
            return int(tid) if tid is not None else None
        except Exception:
            return None

    def cancel(self, reason: str = '用户取消'):
        self._cancel_reason = str(reason or '用户取消')
        self._cancel_event.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    # ---------- 执行 ----------

    def run(self):                                    # noqa: C901 - 保持单入口
        t0 = time.time()
        last_cols, last_rows = [], []
        last_msg = ''
        affected = 0
        truncated = False
        try:
            for stmt in self.statements:
                if self._cancel_event.is_set():
                    raise QueryCancelled(self._cancel_reason)
                if is_query_statement(stmt):
                    res = self.conn.query_chunked(
                        stmt, max_rows=self.max_rows,
                        chunk_size=self.chunk_size)
                    last_cols = list(res.get('columns') or [])
                    last_rows = list(res.get('rows') or [])
                    truncated = bool(res.get('truncated'))
                    last_msg = f"查询返回 {len(last_rows)} 行" + (
                        '（已截断）' if truncated else '')
                else:
                    rowcount, err = self.conn.execute(stmt)
                    if err:
                        raise QueryStatementError(str(err), stmt)
                    affected += int(rowcount or 0)
                    last_cols, last_rows = [], []
                    last_msg = f"影响 {rowcount} 行"
            if self._cancel_event.is_set():
                raise QueryCancelled(self._cancel_reason)
            payload = {
                'columns': last_cols,
                'rows': last_rows,
                'truncated': truncated,
                'fetched': len(last_rows),
                'affected': affected,
                'message': last_msg,
                'elapsed_ms': (time.time() - t0) * 1000.0,
                'statements': list(self.statements),
                'sql': ';\n'.join(self.statements),
                'thread_id': self.thread_id,
            }
            self.signals.result.emit(payload)
        except QueryCancelled as exc:
            self.signals.cancelled.emit(str(exc) or self._cancel_reason)
        except QueryStatementError as exc:
            if self._cancel_event.is_set():
                self.signals.cancelled.emit(self._cancel_reason or '已取消')
            else:
                self.signals.failed.emit(RuntimeError(exc.text))
        except BaseException as exc:                  # noqa: BLE001 - 统一上报，不吞
            if self._cancel_event.is_set():
                self.signals.cancelled.emit(self._cancel_reason or '已取消')
            else:
                self.signals.failed.emit(exc)
        finally:
            self.signals.finished.emit()

    # ---------- 调试辅助 ----------

    def describe(self) -> str:
        return (f"QueryWorker(stmts={len(self.statements)}, "
                f"max_rows={self.max_rows}, thread_id={self.thread_id})")


def format_worker_exception(exc: BaseException) -> str:
    """把 worker 异常转成可读文本（保留 DBQueryError 的完整字段）。"""
    try:
        text = str(exc)
    except Exception:
        text = repr(exc)
    if not text:
        text = traceback.format_exception_only(type(exc), exc)[-1].strip()
    return text
