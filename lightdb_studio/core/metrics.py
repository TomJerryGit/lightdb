"""
Prometheus 指标导出
- 无依赖实现（文本格式）
- 可被 Prometheus 抓取
"""
import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Callable


@dataclass
class Metric:
    name: str
    help: str = ""
    type: str = "gauge"             # gauge / counter / histogram
    labels: Dict[str, str] = field(default_factory=dict)
    value: float = 0


class MetricsRegistry:
    """指标注册表"""

    def __init__(self):
        self.counters = {}          # (name, labels_key) -> value
        self.gauges = {}
        self.histograms = defaultdict(list)  # 保留最近 N 个样本
        self.lock = threading.RLock()
        self.help_texts = {}
        self.types = {}

    def counter(self, name: str, help: str = '', labels: Dict = None,
                increment: float = 1):
        """递增计数器"""
        with self.lock:
            key = (name, self._labels_key(labels))
            self.counters[key] = self.counters.get(key, 0) + increment
            if name not in self.help_texts:
                self.help_texts[name] = help
                self.types[name] = 'counter'

    def gauge(self, name: str, value: float, help: str = '',
              labels: Dict = None):
        """设置 gauge"""
        with self.lock:
            key = (name, self._labels_key(labels))
            self.gauges[key] = value
            if name not in self.help_texts:
                self.help_texts[name] = help
                self.types[name] = 'gauge'

    def histogram(self, name: str, value: float, help: str = '',
                  labels: Dict = None, max_samples: int = 1000):
        """记录直方图样本"""
        with self.lock:
            key = (name, self._labels_key(labels))
            self.histograms[key].append(value)
            if len(self.histograms[key]) > max_samples:
                self.histograms[key] = self.histograms[key][-max_samples:]
            if name not in self.help_texts:
                self.help_texts[name] = help
                self.types[name] = 'histogram'

    def _labels_key(self, labels):
        if not labels:
            return ''
        return ','.join(f'{k}="{v}"' for k, v in sorted(labels.items()))

    # ==================== 导出 ====================

    def export_prometheus(self) -> str:
        """Prometheus 文本格式"""
        lines = []
        written = set()

        with self.lock:
            # Counters
            for (name, labels), value in sorted(self.counters.items()):
                if name not in written:
                    help_text = self.help_texts.get(name, '')
                    lines.append(f"# HELP {name} {help_text}")
                    lines.append(f"# TYPE {name} counter")
                    written.add(name)
                l = f'{{{labels}}}' if labels else ''
                lines.append(f"{name}{l} {value}")

            # Gauges
            for (name, labels), value in sorted(self.gauges.items()):
                if name not in written:
                    help_text = self.help_texts.get(name, '')
                    lines.append(f"# HELP {name} {help_text}")
                    lines.append(f"# TYPE {name} gauge")
                    written.add(name)
                l = f'{{{labels}}}' if labels else ''
                lines.append(f"{name}{l} {value}")

            # Histograms → 简化为 summary
            for (name, labels), samples in sorted(self.histograms.items()):
                if not samples:
                    continue
                n = len(samples)
                total = sum(samples)
                sorted_samples = sorted(samples)
                p50 = sorted_samples[n // 2]
                p90 = sorted_samples[int(n * 0.9)] if n > 1 else p50
                p99 = sorted_samples[int(n * 0.99)] if n > 1 else p50

                base = name
                if base not in written:
                    lines.append(f"# HELP {base} {self.help_texts.get(base, '')}")
                    lines.append(f"# TYPE {base} summary")
                    written.add(base)

                base_labels = f'{{{labels}}}' if labels else ''
                lines.append(f"{base}_count{base_labels} {n}")
                lines.append(f"{base}_sum{base_labels} {total}")

                for q, v in [('0.5', p50), ('0.9', p90), ('0.99', p99)]:
                    l = f'{{{labels},quantile="{q}"}}' if labels \
                        else f'{{quantile="{q}"}}'
                    lines.append(f"{base}{l} {v}")

        return '\n'.join(lines) + '\n'


# 全局注册表
_registry = MetricsRegistry()


def get_registry():
    return _registry


# ==================== 数据库指标收集器 ====================

class DBMetricsCollector:
    """从连接定期采集指标"""

    def __init__(self, conn_getter, registry=None, interval: float = 10):
        self.conn_getter = conn_getter
        self.registry = registry or get_registry()
        self.interval = interval
        self._running = False
        self._thread = None
        self._last_queries = 0
        self._last_ts = 0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='DBMetrics')
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self):
        while self._running:
            try:
                self._collect_once()
            except Exception:
                pass
            time.sleep(self.interval)

    def _collect_once(self):
        conn = self.conn_getter()
        if conn is None or not conn.is_connected():
            return

        try:
            cols, rows = conn.query("SHOW STATUS")
            status = dict(rows)
        except Exception:
            return

        def _int(v):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0

        queries = _int(status.get('Questions', 0))
        connections = _int(status.get('Threads_connected', 0))
        threads_running = _int(status.get('Threads_running', 0))

        # QPS
        now = time.time()
        if self._last_ts > 0:
            dt = now - self._last_ts
            if dt > 0:
                qps = (queries - self._last_queries) / dt
                self.registry.gauge(
                    'lightdb_qps', qps,
                    help='Queries per second')
                self.registry.histogram(
                    'lightdb_qps_samples', qps,
                    help='QPS samples over time')

        self._last_queries = queries
        self._last_ts = now

        self.registry.gauge(
            'lightdb_connections', connections,
            help='Current connections')
        self.registry.gauge(
            'lightdb_threads_running', threads_running,
            help='Running threads')
        self.registry.gauge(
            'lightdb_queries_total', queries,
            help='Total queries served')
        self.registry.gauge(
            'lightdb_up', 1, help='Database is up')

        # 每张表的行数
        try:
            tables = conn.get_tables()
            for t in tables[:20]:  # 最多 20 张
                try:
                    _, r = conn.query(f"SELECT COUNT(*) FROM `{t}`")
                    n = r[0][0] if r else 0
                    self.registry.gauge(
                        'lightdb_table_rows', n,
                        help='Rows per table',
                        labels={'table': t})
                except Exception:
                    pass
        except Exception:
            pass


# ==================== 应用层指标 ====================

def record_api_request(endpoint: str, method: str,
                       status: int, elapsed: float):
    """记录一次 API 请求"""
    r = get_registry()
    r.counter(
        'lightdb_http_requests_total', 
        help='Total HTTP requests',
        labels={'endpoint': endpoint, 'method': method,
                'status': str(status)})
    r.histogram(
        'lightdb_http_request_duration_seconds',
        help='HTTP request duration',
        value=elapsed,
        labels={'endpoint': endpoint})


def record_sql_query(sql_type: str, elapsed: float, success: bool):
    """记录 SQL 执行"""
    r = get_registry()
    r.counter(
        'lightdb_sql_queries_total',
        help='Total SQL queries',
        labels={'type': sql_type,
                'status': 'ok' if success else 'error'})
    r.histogram(
        'lightdb_sql_query_duration_seconds',
        help='SQL query duration',
        value=elapsed,
        labels={'type': sql_type})