"""
Master 进程（修复版）

修复:
- 共享状态初始化容错
- 信号处理
- run_master 加配置校验
- Database 只主进程初始化
- shutdown 优雅
"""
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Dict

from ..concurrency.multiprocess import WorkerPool
from ..concurrency.shared_memory import SharedMemoryManager
from ..cluster.txn_coordinator import SharedTxnCoordinator
from ..cluster.stats_hub import StatsHub
from ..cluster.shared_buffer import SharedBufferPool
from ..logger import logger


class MasterServer:
    """Master 服务器"""

    def __init__(self, config: dict, db=None, executor=None):
        self.config = config
        self.db = db
        self.executor = executor
        self.workers = None
        self.shared_state = {}
        self.mgr = SharedMemoryManager(namespace='lightdb')
        self._stop = False
        self._monitor_thread = None

        # 共享组件
        self.txn_coord = None
        self.stats_hub = None
        self.buffer_pool = None

    # ==================== 初始化 ====================

    def _init_shared(self):
        """初始化共享状态"""
        logger.info("初始化共享内存...")

        worker_count = (
            self.config.get('workers')
            or os.cpu_count() or 4)
        if worker_count < 1:
            worker_count = 1
        self.worker_count = worker_count

        # 1. 事务协调器
        try:
            self.txn_coord = SharedTxnCoordinator(
                name='txn_coord', size_mb=1)
            logger.info("  ✓ 事务协调器就绪")
        except Exception as e:
            logger.warn(f"  ✗ 事务协调器初始化失败: {e}")
            self.txn_coord = None

        # 2. 统计中心
        try:
            self.stats_hub = StatsHub(
                name='stats',
                worker_count=worker_count,
                create=True)
            logger.info(
                f"  ✓ 统计中心就绪 ({worker_count} 槽)")
        except Exception as e:
            logger.warn(f"  ✗ 统计中心初始化失败: {e}")
            self.stats_hub = None

        # 3. 共享 Buffer Pool
        try:
            self.buffer_pool = SharedBufferPool(
                name='buffer_pool',
                slot_count=self.config.get(
                    'buffer_pool_size', 1024),
                create=True)
            stats = self.buffer_pool.stats()
            logger.info(
                f"  ✓ 共享 Buffer Pool 就绪 "
                f"({stats.get('total_size_mb', 0):.0f}MB)")
        except Exception as e:
            logger.warn(f"  ✗ 共享 Buffer Pool 初始化失败: {e}")
            self.buffer_pool = None

        self.shared_state = {
            'txn_coord': self.txn_coord,
            'stats_hub': self.stats_hub,
            'buffer_pool': self.buffer_pool,
            'db': self.db,
            'executor': self.executor,
            'config': self.config,
        }

    # ==================== 启动 ====================

    def start(self):
        logger.info("=" * 60)
        logger.info("  LightDB v4.0 - 多进程架构")
        logger.info("=" * 60)
        logger.info(f"  Master PID: {os.getpid()}")
        logger.info(
            f"  Worker 数: "
            f"{self.config.get('workers') or os.cpu_count()}")
        logger.info(
            f"  监听: "
            f"{self.config.get('host')}:"
            f"{self.config.get('port')}")
        logger.info("=" * 60)

        self._init_shared()

        # 安装信号
        try:
            signal.signal(signal.SIGTERM,
                          self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        except Exception:
            pass

        # 启动 Worker 池
        from .worker import worker_main

        try:
            self.workers = WorkerPool(
                worker_func=worker_main,
                size=self.worker_count,
                config=self.config,
                shared_state=self.shared_state,
            )
            self.workers.start()
        except Exception as e:
            logger.error(f"Worker 池启动失败: {e}")
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
        logger.info(
            f"[{time.strftime('%H:%M:%S')}] 状态")
        logger.info(
            f"  Workers: "
            f"{wstats.get('ready', 0)}/"
            f"{wstats.get('size', 0)} 就绪，"
            f"重启 {wstats.get('total_restarts', 0)} 次")
        logger.info(
            f"  Txn:     累计 {tstats.get('begun', 0)}，"
            f"提交 {tstats.get('committed', 0)}")
        totals = sagg.get('totals', {})
        logger.info(
            f"  Queries: {totals.get('queries', 0)}")
        logger.info(
            f"  Conn:    {totals.get('connections', 0)}")
        logger.info(
            f"  Errors:  {totals.get('errors', 0)}")
        logger.info("─" * 60)

    # ==================== 停止 ====================

    def shutdown(self):
        logger.info("Master 停止中...")
        self._stop = True

        if self.workers:
            try:
                self.workers.stop(timeout=10)
            except Exception as e:
                logger.warn(f"Worker 池停止失败: {e}")

        try:
            self.mgr.close_all()
        except Exception:
            pass

        # 关闭共享组件
        for comp in (self.txn_coord, self.stats_hub,
                     self.buffer_pool):
            if comp is not None and hasattr(comp, 'close'):
                try:
                    comp.close()
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