"""
Master 进程（多进程架构的存储与调度进程）

接线后的职责：
- **独占 Database / Executor**：整个集群里只有本进程读写存储文件。多进程各自
  打开同一份文件型存储会各自持有页缓存与 BTree root，互相覆盖写坏数据，
  因此 Worker 不直接访问存储，而是把 SQL 发回本进程执行。
- 创建共享内存组件（事务协调器 / 统计中心 / 共享 Buffer Pool）
- 创建进程间队列：每 Worker 一个连接队列 + 一个响应队列，另有一个执行请求队列
- 在没有 SO_REUSEPORT 的平台（Windows）绑定监听端口，把 accept 到的连接交给 Worker
- 运行执行服务线程：Worker 发来的 SQL 由本进程的 Executor 执行后回传
- 优雅停机：停监听 → 发哨兵 → 停 Worker → 销毁共享内存
"""
import json
import multiprocessing as mp
import os
import signal
import sys
import threading
import time
from typing import Dict, List

from ..concurrency.multiprocess import WorkerPool
from ..concurrency.shared_memory import SharedMemoryManager
from ..cluster.txn_coordinator import SharedTxnCoordinator
from ..cluster.stats_hub import StatsHub
from ..cluster.shared_buffer import SharedBufferPool
from ..logger import logger

from .listener import create_listener, HAS_REUSEPORT


class MasterServer:
    """Master 服务器"""

    def __init__(self, config: dict, db=None, executor=None):
        self.config = config
        self.db = db
        self.executor = executor
        self.workers = None
        self.shared_state = {}
        self._stop = False
        self._shutdown_done = False
        self._monitor_thread = None

        # 监听器（仅无 SO_REUSEPORT 的平台使用）
        self.listener = None

        # 进程间通信
        self.conn_queues: List[mp.Queue] = []
        self.resp_queues: List[mp.Queue] = []
        self.ready_queue = None
        self.exec_queue = None
        self._svc_thread = None

        # Worker 数
        self.worker_count = int(
            config.get('workers') or os.cpu_count() or 4)
        if self.worker_count < 1:
            self.worker_count = 1

        # 共享内存目录：优先配置，否则放到数据目录下的 shm/
        self.shm_dir = self._resolve_shm_dir()
        self.mgr = SharedMemoryManager(
            namespace='lightdb', shm_dir=self.shm_dir)

        # 共享组件
        self.txn_coord = None
        self.stats_hub = None
        self.buffer_pool = None

    # ==================== 路径 ====================

    def _resolve_shm_dir(self) -> str:
        configured = self.config.get('shm_dir')
        if configured:
            return str(configured)
        data_dir = os.path.abspath(
            self.config.get('data_dir') or 'data')
        return os.path.join(data_dir, 'shm')

    # ==================== 初始化 ====================

    def _init_queues(self):
        """创建进程间队列（必须在启动 Worker 之前完成）"""
        self.conn_queues = [
            mp.Queue() for _ in range(self.worker_count)]
        self.resp_queues = [
            mp.Queue() for _ in range(self.worker_count)]
        self.ready_queue = mp.Queue()
        self.exec_queue = mp.Queue()

    def _init_shared(self):
        """初始化共享状态"""
        logger.info("初始化共享内存...")
        logger.info(f"  共享内存目录: {self.shm_dir}")

        # 1. 事务协调器（原实现漏了 create=True，导致打开不存在的文件而失败）
        try:
            self.txn_coord = SharedTxnCoordinator(
                name='txn_coord', size_mb=1, create=True,
                shm_dir=self.shm_dir)
            logger.info("  ✓ 事务协调器就绪")
        except Exception as e:
            logger.warn(f"  ✗ 事务协调器初始化失败: {e}")
            self.txn_coord = None

        # 2. 统计中心
        try:
            self.stats_hub = StatsHub(
                name='stats',
                worker_count=self.worker_count,
                create=True,
                shm_dir=self.shm_dir)
            logger.info(
                f"  ✓ 统计中心就绪 ({self.worker_count} 槽)")
        except Exception as e:
            logger.warn(f"  ✗ 统计中心初始化失败: {e}")
            self.stats_hub = None

        # 3. 共享 Buffer Pool
        try:
            self.buffer_pool = SharedBufferPool(
                name='buffer_pool',
                slot_count=self.config.get(
                    'buffer_pool_size', 1024),
                create=True,
                shm_dir=self.shm_dir)
            stats = self.buffer_pool.stats()
            logger.info(
                f"  ✓ 共享 Buffer Pool 就绪 "
                f"({stats.get('total_size_mb', 0):.0f}MB)")
        except Exception as e:
            logger.warn(f"  ✗ 共享 Buffer Pool 初始化失败: {e}")
            self.buffer_pool = None

        # ★ 传给 Worker 的必须是「可 pickle 的基本量」：
        #   原实现把 db / executor / 共享内存对象塞进 shared_state，
        #   在 spawn（Windows）下无法序列化，Worker 根本起不来；
        #   即使能起来，每个 Worker 也会拿到一份独立的存储副本。
        self.shared_state = {
            'config': self.config,
            'shm_dir': self.shm_dir,
            'master_pid': os.getpid(),
            'worker_count': self.worker_count,
            'stats_name': 'stats' if self.stats_hub else None,
            'txn_name': 'txn_coord' if self.txn_coord else None,
            'buffer_name': 'buffer_pool' if self.buffer_pool else None,
            'conn_queues': self.conn_queues,
            'resp_queues': self.resp_queues,
            'ready_queue': self.ready_queue,
            'exec_queue': self.exec_queue,
        }

    # ==================== 执行服务 ====================

    def _start_exec_service(self):
        """启动执行服务线程（Worker 的 SQL 由本进程执行）"""
        self._svc_thread = threading.Thread(
            target=self._exec_loop, daemon=True,
            name='MasterExecService')
        self._svc_thread.start()

    def _exec_loop(self):
        """消费 Worker 的执行请求，把结果回传到对应 Worker 的响应队列"""
        logger.info("  执行服务已启动（Worker 的 SQL 由 Master 执行）")
        while not self._stop:
            try:
                req = self.exec_queue.get(timeout=0.5)
            except Exception:
                continue
            if req is None:
                break

            req_id, worker_id, kind, payload = req
            try:
                result = self._handle_request(kind, payload)
                ok = True
            except Exception as e:
                logger.warn(
                    f"执行请求失败 (worker={worker_id}, "
                    f"kind={kind}): {e}")
                result = {'type': 'error', 'code': 1105,
                          'message': str(e)[:200]}
                ok = False

            if kind == 'execute' and self.stats_hub is not None:
                try:
                    self.stats_hub.report(worker_id, 'queries')
                    if not ok:
                        self.stats_hub.report(worker_id, 'errors')
                except Exception:
                    pass

            try:
                self.resp_queues[worker_id % len(self.resp_queues)].put(
                    (req_id, ok, result))
            except Exception as e:
                logger.warn(f"回传结果失败 (worker={worker_id}): {e}")

    def _handle_request(self, kind: str, payload):
        if kind == 'ping':
            return {'ok': True}
        if kind == 'snapshot':
            return self._snapshot()
        if kind == 'execute':
            parsed, session = payload
            return self.executor.execute(parsed, session)
        raise ValueError(f'未知的请求类型: {kind}')

    def _snapshot(self) -> Dict:
        """给 Worker 的元数据快照（鉴权用户表 + 表结构）"""
        tables = {}
        for name, tbl in self.db.tables.items():
            try:
                cols = [dict(c) for c in tbl.cols]
            except Exception:
                cols = []
            tables[name] = {'cols': cols}

        users = {}
        for uname, info in getattr(self.db, 'users', {}).items():
            users[uname] = dict(info) if isinstance(info, dict) else info

        return {
            'users': users,
            'tables': tables,
            'autocommit': bool(getattr(self.db, 'autocommit', True)),
        }

    # ==================== 启动 ====================

    def start(self):
        logger.info("=" * 60)
        logger.info("  LightDB v4.0 - 多进程架构")
        logger.info("=" * 60)
        logger.info(f"  Master PID: {os.getpid()}")
        logger.info(f"  Worker 数: {self.worker_count}")
        logger.info(
            f"  监听: "
            f"{self.config.get('host')}:"
            f"{self.config.get('port')}")
        logger.info(
            f"  存储归属: Master（Worker 通过队列回传 SQL）")
        logger.info("=" * 60)

        self._init_queues()
        self._init_shared()

        # 安装信号
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        except Exception:
            pass

        # 执行服务必须先于 Worker 启动，否则 Worker 初始化时的
        # snapshot 请求会拿不到响应
        self._start_exec_service()

        # 启动 Worker 池
        from .worker import worker_main

        try:
            self.workers = WorkerPool(
                worker_func=worker_main,
                size=self.worker_count,
                config=self.config,
                shared_state=self.shared_state,
                conn_queues=self.conn_queues,
                ready_queue=self.ready_queue,
            )
            self.workers.start()
        except Exception as e:
            logger.error(f"Worker 池启动失败: {e}")
            self.shutdown()
            return

        # 监听端口：有 SO_REUSEPORT 时各 Worker 自行监听，
        # 否则由 Master 监听并把连接交给 Worker
        if HAS_REUSEPORT:
            logger.info("  Worker 自行监听（SO_REUSEPORT 可用）")
        else:
            host = self.config.get('host', '0.0.0.0')
            port = int(self.config.get('port', 8888))
            try:
                self.listener = create_listener(
                    host, port, mode='master',
                    worker_pool=self.workers)
                self.listener.bind()
                self.listener.start_accept_loop()
            except Exception as e:
                logger.error(f"监听 {host}:{port} 失败: {e}")
                self.shutdown()
                return

        # 监控线程
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

        # 主循环
        try:
            while not self._stop:
                time.sleep(5)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def _on_signal(self, signum, frame):
        logger.info(f"收到信号 {signum}，准备关闭...")
        self._stop = True

    def _monitor_loop(self):
        while not self._stop:
            time.sleep(30)
            try:
                self._print_status()
            except Exception as e:
                logger.warn(f"监控异常: {e}")

    def _print_status(self):
        wstats = (self.workers.stats()
                  if self.workers else {})
        tstats = (self.txn_coord.stats()
                  if self.txn_coord else {})
        sagg = (self.stats_hub.aggregate()
                if self.stats_hub else {})

        logger.info("─" * 60)
        logger.info(f"[{time.strftime('%H:%M:%S')}] 状态")
        logger.info(
            f"  Workers: "
            f"{wstats.get('ready', 0)}/"
            f"{wstats.get('size', 0)} 就绪，"
            f"重启 {wstats.get('total_restarts', 0)} 次")
        logger.info(
            f"  Txn:     累计 {tstats.get('begun', 0)}，"
            f"提交 {tstats.get('committed', 0)}")
        totals = sagg.get('totals', {})
        logger.info(f"  Queries: {totals.get('queries', 0)}")
        logger.info(f"  Conn:    {totals.get('connections', 0)}")
        logger.info(f"  Errors:  {totals.get('errors', 0)}")
        logger.info("─" * 60)

    # ==================== 停止 ====================

    def shutdown(self):
        # 幂等：信号触发的停机与 start() 的 finally 都会调用它
        if self._shutdown_done:
            return
        self._shutdown_done = True

        logger.info("Master 停止中...")
        self._stop = True

        # 1. 停止接受新连接
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception as e:
                logger.warn(f"监听器停止失败: {e}")
            self.listener = None

        # 2. 通知执行服务退出
        if self.exec_queue is not None:
            try:
                self.exec_queue.put(None)
            except Exception:
                pass

        # 3. 停止 Worker（WorkerPool 会先发哨兵再终止）
        if self.workers:
            try:
                self.workers.stop(timeout=10)
            except Exception as e:
                logger.warn(f"Worker 池停止失败: {e}")

        if self._svc_thread is not None:
            self._svc_thread.join(timeout=3)
            self._svc_thread = None

        # 4. 销毁共享内存（原实现只 close 不 unlink，每次运行都留文件）
        for comp in (self.txn_coord, self.stats_hub, self.buffer_pool):
            if comp is None:
                continue
            for meth in ('destroy', 'close'):
                fn = getattr(comp, meth, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
                    break
        self.txn_coord = self.stats_hub = self.buffer_pool = None

        try:
            self.mgr.close_all()
        except Exception:
            pass

        # 5. 关闭队列
        for q in (list(self.conn_queues) + list(self.resp_queues)
                  + [self.ready_queue, self.exec_queue]):
            if q is None:
                continue
            try:
                q.close()
                q.join_thread()
            except Exception:
                pass

        logger.info("Master 已退出")


# ==================== 入口 ====================

def run_master(config_path: str = 'config.json'):
    """启动 Master（供 main.py / main_v4.py 调用）"""
    config = {
        'host': '0.0.0.0',
        'port': 8888,
        'workers': os.cpu_count() or 4,
        'buffer_pool_size': 1024,
        'data_dir': 'data',
        'log_level': 'INFO',
    }

    # 加载配置
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, 'r',
                      encoding='utf-8') as f:
                loaded = json.load(f)
            config.update(loaded)
        except Exception as e:
            logger.warn(f"加载配置失败: {e}")

    # ---------- 初始化 Database / Executor ----------
    try:
        from ..db.database import Database
        from ..sql.executor import Executor

        data_dir = os.path.abspath(
            config.get('data_dir', 'data'))
        db = Database(data_dir)
        executor = Executor(db)
    except Exception as e:
        logger.error(f"初始化 Database 失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)

    master = MasterServer(config, db, executor)
    try:
        master.start()
    finally:
        try:
            db.shutdown()
        except Exception:
            pass
