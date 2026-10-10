"""
v3.0 核心指标系统
覆盖 30+ 指标，Prometheus 兼容
"""
import threading
import time
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Metric:
    name: str
    help: str = ""
    type: str = "gauge"     # gauge / counter / histogram / summary
    unit: str = ""
    labels: List[str] = field(default_factory=list)


# ==================== 指标定义 ====================

METRIC_DEFINITIONS = [
    # ============ 连接（6 个）============
    Metric('lightdb_connections_current', '当前连接数', 'gauge',
           labels=['state']),
    Metric('lightdb_connections_total', '累计连接数', 'counter'),
    Metric('lightdb_connections_aborted', '异常断开连接', 'counter',
           labels=['reason']),
    Metric('lightdb_threads_running', '运行中线程', 'gauge'),
    Metric('lightdb_threads_connected', '活跃线程', 'gauge'),
    Metric('lightdb_threads_created', '累计创建线程', 'counter'),

    # ============ 查询（5 个）============
    Metric('lightdb_queries_total', '总查询数', 'counter'),
    Metric('lightdb_queries_per_second', 'QPS', 'gauge'),
    Metric('lightdb_query_duration_seconds', '查询耗时', 'histogram',
           unit='seconds', labels=['command']),
    Metric('lightdb_com_select', 'SELECT 数', 'counter'),
    Metric('lightdb_com_insert', 'INSERT 数', 'counter'),
    Metric('lightdb_com_update', 'UPDATE 数', 'counter'),
    Metric('lightdb_com_delete', 'DELETE 数', 'counter'),

    # ============ 事务（4 个）============
    Metric('lightdb_transactions_total', '事务总数', 'counter',
           labels=['result']),
    Metric('lightdb_transactions_active', '活跃事务', 'gauge'),
    Metric('lightdb_transactions_committed', '已提交事务', 'counter'),
    Metric('lightdb_transactions_rollback', '回滚事务', 'counter'),

    # ============ 锁（4 个）============
    Metric('lightdb_lock_waits_total', '锁等待次数', 'counter'),
    Metric('lightdb_lock_wait_seconds', '锁等待时长', 'histogram',
           unit='seconds'),
    Metric('lightdb_deadlocks_total', '死锁次数', 'counter'),
    Metric('lightdb_row_locks_current', '当前行锁', 'gauge'),

    # ============ 缓冲池（6 个）============
    Metric('lightdb_buffer_pool_pages_total', '缓冲池总页数', 'gauge'),
    Metric('lightdb_buffer_pool_pages_free', '空闲页', 'gauge'),
    Metric('lightdb_buffer_pool_pages_dirty', '脏页', 'gauge'),
    Metric('lightdb_buffer_pool_hit_rate', '缓冲池命中率', 'gauge',
           unit='ratio'),
    Metric('lightdb_buffer_pool_reads_total', '物理读', 'counter'),
    Metric('lightdb_buffer_pool_write_requests', '写请求', 'counter'),

    # ============ 磁盘 IO（4 个）============
    Metric('lightdb_disk_read_bytes', '磁盘读字节', 'counter',
           unit='bytes'),
    Metric('lightdb_disk_write_bytes', '磁盘写字节', 'counter',
           unit='bytes'),
    Metric('lightdb_disk_read_iops', '读 IOPS', 'gauge'),
    Metric('lightdb_disk_write_iops', '写 IOPS', 'gauge'),

    # ============ Binlog / 复制（6 个）============
    Metric('lightdb_binlog_bytes_total', 'Binlog 字节数', 'counter',
           unit='bytes'),
    Metric('lightdb_binlog_position', 'Binlog 位置', 'gauge'),
    Metric('lightdb_slave_io_running', '从库 IO 线程', 'gauge'),
    Metric('lightdb_slave_sql_running', '从库 SQL 线程', 'gauge'),
    Metric('lightdb_slave_lag_seconds', '从库延迟', 'gauge',
           unit='seconds'),
    Metric('lightdb_semi_sync_avg_wait_ms', '半同步平均等待', 'gauge',
           unit='milliseconds'),

    # ============ 表级（4 个）============
    Metric('lightdb_table_rows', '表行数', 'gauge',
           labels=['table']),
    Metric('lightdb_table_size_bytes', '表大小', 'gauge',
           unit='bytes', labels=['table']),
    Metric('lightdb_table_scans_total', '表扫描次数', 'counter',
           labels=['table']),
    Metric('lightdb_table_row_locks_current', '表行锁', 'gauge',
           labels=['table']),

    # ============ 服务器（4 个）============
    Metric('lightdb_uptime_seconds', '运行时长', 'gauge',
           unit='seconds'),
    Metric('lightdb_server_memory_bytes', '内存使用', 'gauge',
           unit='bytes'),
    Metric('lightdb_server_cpu_usage', 'CPU 使用率', 'gauge',
           unit='ratio'),
    Metric('lightdb_server_open_files', '打开文件数', 'gauge'),
]


# ==================== 指标采集器 ====================

class MetricsCollector:
    """指标采集器"""

    def __init__(self, conn_provider=None):
        self.conn_provider = conn_provider
        self.metrics: Dict[str, Dict] = defaultdict(dict)
        self.histograms: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=1000))
        self.lock = threading.RLock()

        # 内部状态
        self.start_time = time.time()
        self._last_sample = {}
        self._last_ts = 0

    # ==================== 采集 ====================

    def collect(self) -> Dict:
        """采集一次所有指标"""
        conn = self.conn_provider() if self.conn_provider else None
        if not conn:
            return self._collect_offline()

        # 系统状态
        try:
            status = self._query_status(conn)
        except Exception:
            status = {}

        self._collect_server_metrics(status)
        self._collect_query_metrics(status)
        self._collect_connection_metrics(status)
        self._collect_buffer_pool_metrics(status)
        self._collect_transaction_metrics(status)
        self._collect_lock_metrics(status)
        self._collect_replication_metrics(conn)
        self._collect_table_metrics(conn)

        return self.metrics

    def _collect_offline(self) -> Dict:
        """无连接时返回基础指标"""
        with self.lock:
            self.metrics['lightdb_uptime_seconds'][''] = \
                time.time() - self.start_time
        return self.metrics

    def _query_status(self, conn) -> Dict:
        """SHOW STATUS"""
        try:
            cols, rows = conn.query("SHOW STATUS")
            return dict(rows)
        except Exception:
            return {}

    def _set(self, name: str, value, labels: str = ''):
        with self.lock:
            self.metrics[name][labels] = value

    # ==================== 各类指标 ====================

    def _collect_server_metrics(self, status: Dict):
        def _int(v):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0

        self._set('lightdb_uptime_seconds',
                  _int(status.get('Uptime', 0)))

        # 内存/CPU（如有）
        try:
            import psutil
            import os
            pid = os.getpid()
            proc = psutil.Process(pid)
            self._set('lightdb_server_memory_bytes',
                      proc.memory_info().rss)
            self._set('lightdb_server_cpu_usage',
                      proc.cpu_percent() / 100.0)
            self._set('lightdb_server_open_files',
                      len(proc.open_files()))
        except Exception:
            pass

    def _collect_query_metrics(self, status: Dict):
        def _int(v):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0

        queries = _int(status.get('Questions', 0))
        self._set('lightdb_queries_total', queries)

        # QPS
        now = time.time()
        if self._last_ts > 0:
            dt = now - self._last_ts
            if dt > 0:
                last_q = self._last_sample.get('queries', 0)
                qps = (queries - last_q) / dt
                self._set('lightdb_queries_per_second', qps)

        self._last_sample['queries'] = queries
        self._last_ts = now

        # Com_*
        for cmd in ['select', 'insert', 'update', 'delete']:
            key = f'Com_{cmd}'
            self._set(f'lightdb_com_{cmd}', _int(status.get(key, 0)))

    def _collect_connection_metrics(self, status: Dict):
        def _int(v):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0

        self._set('lightdb_threads_running',
                  _int(status.get('Threads_running', 0)))
        self._set('lightdb_threads_connected',
                  _int(status.get('Threads_connected', 0)))
        self._set('lightdb_connections_current',
                  _int(status.get('Threads_connected', 0)))
        self._set('lightdb_threads_created',
                  _int(status.get('Threads_created', 0)))
        self._set('lightdb_connections_total',
                  _int(status.get('Connections', 0)))
        self._set('lightdb_connections_aborted',
                  _int(status.get('Aborted_connects', 0)))

    def _collect_buffer_pool_metrics(self, status: Dict):
        def _int(v):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0

        total = _int(status.get('Innodb_buffer_pool_pages_total', 0))
        free = _int(status.get('Innodb_buffer_pool_pages_free', 0))
        dirty = _int(status.get('Innodb_buffer_pool_pages_dirty', 0))
        reads = _int(status.get('Innodb_buffer_pool_reads', 0))
        read_reqs = _int(status.get(
            'Innodb_buffer_pool_read_requests', 0))
        write_reqs = _int(status.get(
            'Innodb_buffer_pool_write_requests', 0))

        self._set('lightdb_buffer_pool_pages_total', total)
        self._set('lightdb_buffer_pool_pages_free', free)
        self._set('lightdb_buffer_pool_pages_dirty', dirty)
        self._set('lightdb_buffer_pool_reads_total', reads)
        self._set('lightdb_buffer_pool_write_requests', write_reqs)

        # 命中率
        if read_reqs > 0:
            hit = 1 - reads / read_reqs
            self._set('lightdb_buffer_pool_hit_rate', hit)

    def _collect_transaction_metrics(self, status: Dict):
        def _int(v):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0

        # 累计
        committed = _int(status.get('Com_commit', 0))
        rolled_back = _int(status.get('Com_rollback', 0))

        self._set('lightdb_transactions_committed', committed)
        self._set('lightdb_transactions_rollback', rolled_back)
        self._set('lightdb_transactions_total', committed + rolled_back)
        self._set('lightdb_transactions_active', 0)   # 需从锁表查询

    def _collect_lock_metrics(self, status: Dict):
        def _int(v):
            try:
                return int(v)
            except (ValueError, TypeError):
                return 0

        self._set('lightdb_lock_waits_total',
                  _int(status.get('Innodb_row_lock_waits', 0)))
        self._set('lightdb_deadlocks_total',
                  _int(status.get('Innodb_deadlocks', 0)))
        self._set('lightdb_row_locks_current',
                  _int(status.get('Innodb_row_lock_current_waits', 0)))

    def _collect_replication_metrics(self, conn):
        try:
            cols, rows = conn.query("SHOW MASTER STATUS")
            if rows:
                # 假设 (File, Position, ...)
                for i, c in enumerate(cols):
                    if c.lower() == 'position':
                        self._set('lightdb_binlog_position',
                                  int(rows[0][i] or 0))
                        break
        except Exception:
            pass

        try:
            cols, rows = conn.query("SHOW SLAVE STATUS")
            if rows:
                row = rows[0]
                for i, c in enumerate(cols):
                    cl = c.lower()
                    if cl == 'slave_running':
                        self._set('lightdb_slave_io_running',
                                  1 if row[i] else 0)
                        self._set('lightdb_slave_sql_running',
                                  1 if row[i] else 0)
                    elif cl == 'read_master_log_pos':
                        pass
        except Exception:
            pass

    def _collect_table_metrics(self, conn):
        try:
            tables = conn.get_tables()
            for t in tables[:50]:
                try:
                    cols, rows = conn.query(
                        f"SELECT COUNT(*) FROM `{t}`")
                    count = rows[0][0] if rows else 0
                    self._set('lightdb_table_rows', count,
                              labels=f'table="{t}"')
                except Exception:
                    pass
        except Exception:
            pass

    # ==================== 导出 ====================

    def export_prometheus(self) -> str:
        """Prometheus 格式导出"""
        lines = []

        # 指标帮助文本
        definitions = {m.name: m for m in METRIC_DEFINITIONS}

        with self.lock:
            for name, values in sorted(self.metrics.items()):
                m_def = definitions.get(name)
                help_text = m_def.help if m_def else name
                m_type = m_def.type if m_def else 'gauge'
                unit = m_def.unit if m_def else ''

                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {m_type}")

                for labels, value in values.items():
                    l = f'{{{labels}}}' if labels else ''
                    # 数值格式化
                    if isinstance(value, float):
                        lines.append(f"{name}{l} {value:.6g}")
                    else:
                        lines.append(f"{name}{l} {value}")

        return '\n'.join(lines) + '\n'

    def export_json(self) -> Dict:
        """JSON 格式导出"""
        with self.lock:
            return dict(self.metrics)

    # ==================== 统计 ====================

    def stats(self) -> Dict:
        with self.lock:
            return {
                'total_metrics': len(self.metrics),
                'metric_definitions': len(METRIC_DEFINITIONS),
                'samples': sum(len(v) for v in self.metrics.values()),
            }