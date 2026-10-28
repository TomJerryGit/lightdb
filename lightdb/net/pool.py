"""服务端连接池（task-71 生产化 B）

为什么需要
==========

当前 `MySQLServer` 基于 `ThreadingTCPServer`：**每条连接新建一个线程**，
连接结束后线程销毁。短连接/轮询型客户端（Web、管理工具、健康检查）频繁
建连时，线程创建/销毁与栈内存成了纯开销；同时"线程数"没有独立上限，
只受 `max_connections` 准入约束。

本模块把**连接处理线程池化**，但不改变 MySQL 协议与会话语义：

* 每个连接仍由一个 worker 线程**独占处理整个生命周期**：`MySQLHandler`
  实例、`session`（事务句柄/用户变量/autocommit/当前库）都是 per-connection
  新建的，线程在连接存活期间不会去处理第二条连接 —— 所以
  **长事务/长查询不会被复用、抢占或中途换线程**；
* 连接结束后 worker 归还池子，下一个连接**复用**同一个线程（省掉线程
  创建/销毁）；
* 空闲 worker 超过 `idle_timeout` 且总 worker 数大于 `min_idle` 时退出
  （缩容，避免长期占着栈内存）；
* 超限策略可配：
    - ``reject``（默认）：worker 满时在握手前显式回 ERR 1040
      （"Too many connections (pool exhausted)"）并关闭，不排队；
    - ``queue``：有界等待（`queue_timeout` 秒），等到空闲 worker 才处理，
      超时同样显式 ERR 1040；
* 健康检查：每次分配/按间隔回收时清理意外退出的 worker 线程并计数
  （`dead_workers`），必要时补足 `min_idle`；
* 指标：worker 总数/忙/闲、创建/复用次数、排队/拒绝/超时、缩容、健康检查、
  长连接数，经 `/metrics` 与 `SHOW STATUS` 暴露（真实计数，非估算）。

环境变量（不新增 Config 字段，符合本任务范围）
=============================================

===============================  =========  ================================
变量                              默认       说明
===============================  =========  ================================
`LIGHTDB_POOL_ENABLED`           `1`        `0` = 退回每连接一线程旧行为
`LIGHTDB_POOL_MAX_WORKERS`       取         `max_connections`（上限）
`LIGHTDB_POOL_MIN_IDLE`           `1`        常驻空闲 worker 下限
`LIGHTDB_POOL_IDLE_TIMEOUT`       `60`       空闲 worker 退出阈值（秒）
`LIGHTDB_POOL_OVERFLOW`          `reject`  `reject` / `queue`
`LIGHTDB_POOL_QUEUE_TIMEOUT`      `5.0`      queue 模式等待上限（秒）
`LIGHTDB_POOL_QUEUE_SIZE`         `64`       等待队列上限（满则拒绝）
`LIGHTDB_POOL_HEALTH_INTERVAL`    `5.0`      健康检查最小间隔（秒）
`LIGHTDB_POOL_LONG_RUNNING_S`     `30`       "长连接/长事务"统计阈值（秒）
===============================  =========  ================================

语义边界（如实说明）
====================

* 池化的是**线程**，不是会话：连接与会话一一对应，池从不把一条连接的
  会话状态给另一个客户端（会话隔离由 `MySQLHandler` 每连接新建保证）；
* worker 忙时**不会**被心跳/超时回收（只有连接结束才会归还），因此长事务
  期间连接一直占用一个 worker 与一个准入名额（这正是"不被复用"的保证）；
* 排队中的连接在建立 TCP 后等待（尚未握手），期间不占用 worker；客户端
  会看到连接建立但握手响应延迟；
* 不做连接级健康探活（MySQL 协议层已有 COM_PING）；健康检查针对的是
  **worker 线程**是否还活着。
"""
import os
import queue
import threading
import time
from collections import deque

from ..logger import logger

#: worker 退出哨兵
_STOP = object()


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    try:
        val = int(raw) if raw is not None else int(default)
    except (TypeError, ValueError):
        val = int(default)
    return val if val >= minimum else minimum


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    try:
        val = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        val = float(default)
    return val if val >= minimum else minimum


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ('0', 'false', 'off', 'no', '')


class _PoolWorker:
    """池化 worker：独占处理一条连接，结束后回到空闲队列"""

    __slots__ = ('worker_id', 'pool', 'thread', 'busy', 'retired',
                 'created_at', 'last_active', 'current_started', 'jobs_done')

    def __init__(self, pool, worker_id: int):
        self.pool = pool
        self.worker_id = worker_id
        self.thread = threading.Thread(
            target=self._run, daemon=True,
            name=f'lightdb-pool-worker-{worker_id}')
        self.busy = False
        self.retired = False
        self.created_at = time.time()
        self.last_active = time.time()
        self.current_started = None
        self.jobs_done = 0

    def start(self):
        self.thread.start()

    def _run(self):
        try:
            while True:
                job = self.pool._take_job(self)
                if job is _STOP:
                    break
                self.busy = True
                self.current_started = time.time()
                try:
                    self.pool._serve_connection(self, job)
                except BaseException as e:       # pragma: no cover - 防御
                    # 连接处理线程里的异常不能带走 worker（否则池子漏线程）
                    try:
                        logger.warn(
                            f"连接池 worker-{self.worker_id} 处理连接异常: {e}")
                    except Exception:
                        pass
                finally:
                    self.busy = False
                    self.current_started = None
                    self.jobs_done += 1
                    self.last_active = time.time()
                    self.pool._worker_finished(self)
        finally:
            self.pool._worker_exited(self)


class ConnectionPool:
    """服务端连接 worker 池（见模块 docstring 的语义与 env 配置）"""

    def __init__(self, server, max_workers: int = None,
                 min_idle: int = None, idle_timeout: float = None,
                 overflow: str = None, queue_timeout: float = None,
                 queue_size: int = None, enabled: bool = None):
        self.server = server
        self.enabled = (_env_flag('LIGHTDB_POOL_ENABLED', True)
                        if enabled is None else bool(enabled))
        if max_workers is None:
            max_workers = _env_int('LIGHTDB_POOL_MAX_WORKERS',
                                   int(getattr(server, 'max_connections', 100)))
        self.max_workers = max(1, int(max_workers))
        self.min_idle = min(
            self.max_workers,
            _env_int('LIGHTDB_POOL_MIN_IDLE', 1, minimum=0)
            if min_idle is None else max(0, int(min_idle)))
        self.idle_timeout = (
            _env_float('LIGHTDB_POOL_IDLE_TIMEOUT', 60.0, minimum=1.0)
            if idle_timeout is None else max(0.1, float(idle_timeout)))
        overflow = (os.environ.get('LIGHTDB_POOL_OVERFLOW', 'reject')
                    if overflow is None else overflow).strip().lower()
        self.overflow = overflow if overflow in ('reject', 'queue') else 'reject'
        self.queue_timeout = (
            _env_float('LIGHTDB_POOL_QUEUE_TIMEOUT', 5.0, minimum=0.1)
            if queue_timeout is None else max(0.1, float(queue_timeout)))
        self.queue_size = (
            _env_int('LIGHTDB_POOL_QUEUE_SIZE', 64)
            if queue_size is None else max(1, int(queue_size)))
        self.health_interval = _env_float(
            'LIGHTDB_POOL_HEALTH_INTERVAL', 5.0, minimum=0.5)
        self.long_running_s = _env_float(
            'LIGHTDB_POOL_LONG_RUNNING_S', 30.0, minimum=0.0)

        self._lock = threading.RLock()
        self._jobs = queue.Queue()             # worker 取活队列
        self._workers = {}                     # worker_id -> _PoolWorker
        self._workers_created = 0
        self._assignments = 0
        self._jobs_completed = 0
        self._queued = 0                       # 已入队但未被 worker 取走
        self._queue_waits = 0
        self._queue_timeouts = 0
        self._rejected = 0
        self._idle_shrinks = 0
        self._dead_workers = 0
        self._health_checks = 0
        self._max_busy = 0
        self._last_health_check = 0.0
        self._started_at = time.time()
        self._stop = threading.Event()
        self._next_id = 1
        # queue 模式：等待空闲 worker 的连接（已 accept、未握手）
        self._pending = deque()
        self._pending_cond = threading.Condition(self._lock)
        self._dispatcher = None

        with self._lock:
            for _ in range(self.min_idle):
                self._spawn_locked()
        if self.overflow == 'queue':
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop, daemon=True,
                name='lightdb-pool-dispatcher')
            self._dispatcher.start()

    # ==================== 内部：spawn/取活/回收 ====================

    def _spawn_locked(self) -> _PoolWorker:
        wid = self._next_id
        self._next_id += 1
        worker = _PoolWorker(self, wid)
        self._workers[wid] = worker
        self._workers_created += 1
        worker.start()
        return worker

    def _take_job(self, worker: _PoolWorker):
        """worker 主循环：取一个连接任务；空闲超时且可缩容时返回 _STOP"""
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=min(1.0, self.idle_timeout))
            except queue.Empty:
                job = None
            if job is _STOP:
                return _STOP
            if job is not None:
                with self._lock:
                    self._queued = max(0, self._queued - 1)
                return job
            # 空闲：检查是否需要缩容
            now = time.time()
            if (not worker.busy
                    and now - worker.last_active >= self.idle_timeout):
                with self._lock:
                    if len(self._workers) > self.min_idle:
                        worker.retired = True
                        return _STOP
        return _STOP

    def _worker_finished(self, worker: _PoolWorker):
        with self._lock:
            self._jobs_completed += 1
            idle = sum(1 for w in self._workers.values() if not w.busy)
            busy = len(self._workers) - idle
            if busy > self._max_busy:
                self._max_busy = busy
            if self._dispatcher is not None:
                self._pending_cond.notify_all()

    def _worker_exited(self, worker: _PoolWorker):
        with self._lock:
            self._workers.pop(worker.worker_id, None)
            if worker.retired:
                self._idle_shrinks += 1
            # 维持 min_idle（进程没停就补）
            if not self._stop.is_set():
                while len(self._workers) < self.min_idle:
                    self._spawn_locked()
            if self._dispatcher is not None:
                self._pending_cond.notify_all()

    def _health_locked(self, force: bool = False):
        """健康检查：清理意外退出的 worker 线程，按需补 min_idle"""
        now = time.time()
        if (not force) and (now - self._last_health_check
                            < self.health_interval):
            return
        self._health_checks += 1
        self._last_health_check = now
        dead = [wid for wid, w in self._workers.items()
                if not w.thread.is_alive()]
        for wid in dead:
            self._workers.pop(wid, None)
            self._dead_workers += 1
        if dead:
            logger.warn(f"连接池健康检查清理 {len(dead)} 个已退出 worker")
            # 被清理的 worker 若带未完成的连接：不再有线程为它服务，
            # 由 socketserver 的关闭路径回收；池只保证不再把它当空闲槽。
            self._queued = max(0, self._queued - 0)
        if not self._stop.is_set():
            while len(self._workers) < self.min_idle:
                self._spawn_locked()

    def _capacity_locked(self) -> int:
        """当前还能立刻接收的连接数（未计排队）"""
        self._health_locked()
        busy = sum(1 for w in self._workers.values() if w.busy)
        free = len(self._workers) - busy - self._queued
        if free <= 0 and len(self._workers) < self.max_workers:
            self._spawn_locked()
            free = 1
        return max(0, free)

    def _enqueue_locked(self, job) -> bool:
        if len(self._pending) >= self.queue_size:
            return False
        self._pending.append((job, time.time()))
        self._queue_waits += 1
        return True

    # ==================== 对外：提交 / 拒绝 ====================

    def submit(self, request, client_address) -> bool:
        """提交一条已 accept 的连接；返回 False = 已拒绝/已关闭"""
        job = (request, client_address)
        with self._lock:
            free = self._capacity_locked()
            if free > 0:
                self._jobs.put_nowait(job)
                self._queued += 1
                self._assignments += 1
                return True
            # 满
            if self.overflow == 'queue':
                if self._enqueue_locked(job):
                    self._pending_cond.notify_all()
                    return True
            self._rejected += 1
        self.reject(request, 'pool exhausted')
        return False

    def reject(self, request, reason: str, code: int = 1040):
        """握手前显式拒绝（ERR 包 + 关闭 + 回落准入计数）"""
        try:
            fn = getattr(self.server, '_pool_reject_connection', None)
            if callable(fn):
                fn(request, reason, code)
                return
        except Exception as e:                   # pragma: no cover - 防御
            logger.warn(f"连接池拒绝连接失败: {e}")
        try:
            request.close()
        except Exception:                        # pragma: no cover - 防御
            pass

    def _dispatch_loop(self):
        """queue 模式：把等待中的连接交给空闲 worker；超时显式拒绝"""
        while not self._stop.is_set():
            with self._lock:
                if not self._pending:
                    self._pending_cond.wait(timeout=0.2)
                    continue
                job, enqueued_at = self._pending[0]
                if self._capacity_locked() > 0:
                    self._pending.popleft()
                    self._jobs.put_nowait(job)
                    self._queued += 1
                    self._assignments += 1
                    continue
                waited = time.time() - enqueued_at
                if waited >= self.queue_timeout:
                    self._pending.popleft()
                    self._queue_timeouts += 1
                    self._rejected += 1
                    request = job[0]
                else:
                    request = None
                    self._pending_cond.wait(
                        timeout=min(0.1, self.queue_timeout - waited))
            if request is not None:
                self.reject(
                    request,
                    f'pool queue timeout ({self.queue_timeout:.1f}s)')

    def _serve_connection(self, worker: _PoolWorker, job):
        """worker 线程内处理一条完整连接（会话隔离见模块 docstring）"""
        request, client_address = job
        fn = getattr(self.server, '_serve_pooled_connection', None)
        if callable(fn):
            fn(request, client_address)
        else:                                    # pragma: no cover - 防御
            self.server.process_request_thread(request, client_address)

    # ==================== 指标 / 生命周期 ====================

    def snapshot(self) -> dict:
        """真实池状态（/metrics 与 SHOW STATUS 的数据源）"""
        with self._lock:
            self._health_locked()
            now = time.time()
            busy = 0
            long_running = 0
            for w in self._workers.values():
                if w.busy:
                    busy += 1
                    if (w.current_started is not None
                            and now - w.current_started
                            >= self.long_running_s):
                        long_running += 1
            total = len(self._workers)
            return {
                'enabled': self.enabled,
                'overflow': self.overflow,
                'max_workers': self.max_workers,
                'min_idle': self.min_idle,
                'idle_timeout': self.idle_timeout,
                'queue_timeout': self.queue_timeout,
                'queue_size': self.queue_size,
                'workers_total': total,
                'workers_busy': busy,
                'workers_idle': max(0, total - busy),
                'jobs_queued': self._queued,
                'jobs_pending_accept': len(self._pending),
                'workers_created': self._workers_created,
                'assignments': self._assignments,
                'reused': max(0, self._assignments - self._workers_created),
                'jobs_completed': self._jobs_completed,
                'queue_waits': self._queue_waits,
                'queue_timeouts': self._queue_timeouts,
                'rejected': self._rejected,
                'idle_shrinks': self._idle_shrinks,
                'dead_workers': self._dead_workers,
                'health_checks': self._health_checks,
                'max_busy': self._max_busy,
                'long_running': long_running,
                'uptime': round(now - self._started_at, 3),
            }

    def shutdown(self):
        """停池：拒掉排队连接、通知 worker 退出、等待线程结束"""
        self._stop.set()
        # 排队中的连接：显式拒绝（不再握手）
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
        for job, _ts in pending:
            try:
                self.reject(job[0], 'server shutting down')
            except Exception:                    # pragma: no cover - 防御
                pass
        with self._lock:
            n = len(self._workers)
        for _ in range(max(n, 1)):
            try:
                self._jobs.put_nowait(_STOP)
            except Exception:                    # pragma: no cover - 防御
                break
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            try:
                w.thread.join(timeout=3)
            except Exception:                    # pragma: no cover - 防御
                pass
        if self._dispatcher is not None:
            try:
                self._dispatcher.join(timeout=3)
            except Exception:                    # pragma: no cover - 防御
                pass
            self._dispatcher = None
