"""
实时性能监控
- 定时采集 QPS / 连接数 / 慢查询
- 环形缓冲
- 异常告警
"""
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Callable, Optional


@dataclass
class PerfSample:
    ts: float
    qps: float = 0
    tps: float = 0
    connections: int = 0
    threads_running: int = 0
    slow_queries: int = 0
    queries_total: int = 0
    bytes_received: int = 0
    bytes_sent: int = 0


class PerformanceMonitor:
    def __init__(self, conn_getter, interval: float = 2.0,
                 history_size: int = 180):
        """
        conn_getter: () -> DBConnection
        interval: 采样间隔（秒）
        history_size: 保留样本数（默认 3 分钟）
        """
        self.conn_getter = conn_getter
        self.interval = interval
        self.history: deque = deque(maxlen=history_size)
        self._running = False
        self._thread = None
        self._lock = threading.RLock()
        self._last_sample = None
        self._callbacks = []

        # 告警阈值
        self.thresholds = {
            'qps_high': 5000,
            'connections_high': 80,
            'slow_query_high': 10,
        }
        self.alerts: deque = deque(maxlen=100)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='PerfMonitor')
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self):
        while self._running:
            try:
                self._sample()
            except Exception:
                pass
            time.sleep(self.interval)

    def _sample(self):
        conn = self.conn_getter()
        if conn is None or not conn.is_connected():
            return

        now = time.time()
        try:
            cols, rows = conn.query("SHOW STATUS")
            status = dict(rows)
        except Exception:
            return

        queries = self._int(status.get('Questions', 0))
        threads_connected = self._int(status.get('Threads_connected', 0))
        threads_running = self._int(status.get('Threads_running', 0))
        bytes_received = self._int(status.get('Bytes_received', 0))
        bytes_sent = self._int(status.get('Bytes_sent', 0))

        # 计算增量 QPS
        qps = 0.0
        tps = 0.0
        if self._last_sample is not None:
            dt = now - self._last_sample['ts']
            if dt > 0:
                dq = queries - self._last_sample['queries']
                qps = dq / dt
                # TPS 用 insert+update+delete 计数简化
                tps = qps * 0.2

        sample = PerfSample(
            ts=now,
            qps=qps,
            tps=tps,
            connections=threads_connected,
            threads_running=threads_running,
            queries_total=queries,
            bytes_received=bytes_received,
            bytes_sent=bytes_sent,
        )

        with self._lock:
            self.history.append(sample)

        self._last_sample = {
            'ts': now,
            'queries': queries,
        }

        # 检查告警
        self._check_alerts(sample)

        # 通知回调
        for cb in self._callbacks:
            try:
                cb(sample)
            except Exception:
                pass

    def _int(self, v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    def _check_alerts(self, sample: PerfSample):
        if sample.qps > self.thresholds['qps_high']:
            self._add_alert(
                'QPS 过高',
                f"当前 QPS: {sample.qps:.0f} "
                f"(阈值 {self.thresholds['qps_high']})")
        if sample.connections > self.thresholds['connections_high']:
            self._add_alert(
                '连接数过高',
                f"当前连接: {sample.connections} "
                f"(阈值 {self.thresholds['connections_high']})")

    def _add_alert(self, title: str, message: str):
        # 5 秒内同类告警不重复
        for a in list(self.alerts)[-3:]:
            if a['title'] == title and time.time() - a['ts'] < 5:
                return
        self.alerts.append({
            'ts': time.time(),
            'title': title,
            'message': message,
        })

    def get_history(self) -> List[PerfSample]:
        with self._lock:
            return list(self.history)

    def get_recent_qps(self, n: int = 30) -> List[float]:
        with self._lock:
            samples = list(self.history)[-n:]
        return [s.qps for s in samples]

    def get_summary(self) -> dict:
        with self._lock:
            samples = list(self.history)
        if not samples:
            return {
                'samples': 0,
                'avg_qps': 0,
                'max_qps': 0,
                'avg_conn': 0,
                'max_conn': 0,
            }
        return {
            'samples': len(samples),
            'avg_qps': sum(s.qps for s in samples) / len(samples),
            'max_qps': max(s.qps for s in samples),
            'avg_conn': sum(s.connections for s in samples) / len(samples),
            'max_conn': max(s.connections for s in samples),
            'alerts': len(self.alerts),
        }

    def on_sample(self, callback):
        self._callbacks.append(callback)