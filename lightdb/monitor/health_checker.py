"""
健康检查（用于监控告警）
"""
import time
from dataclasses import dataclass, asdict, field
from typing import Callable, Dict, List


@dataclass
class HealthCheck:
    name: str
    status: str         # pass / warn / fail
    value: float = 0
    threshold: float = 0
    message: str = ""
    checked_at: float = 0

    def __post_init__(self):
        if not self.checked_at:
            self.checked_at = time.time()


class HealthChecker:
    """健康检查器"""

    # 默认阈值
    DEFAULTS = {
        'connections_max': 0.8,           # 使用率 80%
        'memory_usage_max': 0.9,          # 90%
        'cpu_usage_max': 0.85,            # 85%
        'buffer_pool_hit_min': 0.95,      # 95%
        'slow_query_threshold': 100,      # 慢查询数/分钟
        'replica_lag_max': 30,            # 30 秒
        'disk_usage_max': 0.85,           # 85%
        'deadlock_threshold': 5,          # 5 次/小时
    }

    def __init__(self, metrics_provider: Callable = None,
                 conn_provider: Callable = None):
        self.metrics_provider = metrics_provider
        self.conn_provider = conn_provider
        self.thresholds = dict(self.DEFAULTS)

    # ==================== 检查项 ====================

    def check_all(self) -> List[HealthCheck]:
        """执行所有检查"""
        checks = []

        # 采集指标
        metrics = self.metrics_provider() if self.metrics_provider else {}

        # 1. 连接数
        conns = self._get_metric(
            metrics, 'lightdb_threads_connected', 0)
        max_conns = 151   # 从配置读取
        ratio = conns / max_conns if max_conns > 0 else 0
        checks.append(self._make_check(
            'connections_usage',
            ratio,
            self.thresholds['connections_max'],
            message=f"连接 {conns}/{max_conns}"))

        # 2. 缓冲池命中率
        hit = self._get_metric(
            metrics, 'lightdb_buffer_pool_hit_rate', 1.0)
        checks.append(self._make_check(
            'buffer_pool_hit_rate',
            hit,
            self.thresholds['buffer_pool_hit_min'],
            higher_better=True,
            message=f"命中率 {hit*100:.2f}%"))

        # 3. 复制延迟
        lag = self._get_metric(
            metrics, 'lightdb_slave_lag_seconds', 0)
        checks.append(self._make_check(
            'replica_lag',
            lag,
            self.thresholds['replica_lag_max'],
            message=f"延迟 {lag:.1f}s"))

        # 4. 死锁
        deadlocks = self._get_metric(
            metrics, 'lightdb_deadlocks_total', 0)
        checks.append(self._make_check(
            'deadlocks',
            deadlocks,
            self.thresholds['deadlock_threshold'],
            message=f"累计 {deadlocks} 次"))

        # 5. 内存
        try:
            import psutil
            mem = psutil.virtual_memory()
            ratio = mem.percent / 100.0
            checks.append(self._make_check(
                'memory_usage',
                ratio,
                self.thresholds['memory_usage_max'],
                message=f"内存使用 {mem.percent:.1f}%"))
        except Exception:
            pass

        # 6. 磁盘
        try:
            import shutil
            from pathlib import Path
            disk = shutil.disk_usage(str(Path.home()))
            ratio = disk.used / disk.total
            checks.append(self._make_check(
                'disk_usage',
                ratio,
                self.thresholds['disk_usage_max'],
                message=f"磁盘 {ratio*100:.1f}%"))
        except Exception:
            pass

        return checks

    def _get_metric(self, metrics: Dict, name: str,
                     default=0) -> float:
        d = metrics.get(name, {})
        if not d:
            return default
        # 取第一个值
        for v in d.values():
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
        return default

    def _make_check(self, name: str, value: float,
                     threshold: float,
                     higher_better: bool = False,
                     message: str = "") -> HealthCheck:
        if higher_better:
            if value >= threshold:
                status = 'pass'
            elif value >= threshold * 0.9:
                status = 'warn'
            else:
                status = 'fail'
        else:
            if value <= threshold:
                status = 'pass'
            elif value <= threshold * 1.1:
                status = 'warn'
            else:
                status = 'fail'

        return HealthCheck(
            name=name,
            status=status,
            value=value,
            threshold=threshold,
            message=message,
        )

    # ==================== 报告 ====================

    def format_report(self, checks: List[HealthCheck] = None) -> str:
        if checks is None:
            checks = self.check_all()

        lines = ["=" * 60]
        lines.append("  LightDB 健康检查")
        lines.append("=" * 60)

        pass_count = sum(1 for c in checks if c.status == 'pass')
        warn_count = sum(1 for c in checks if c.status == 'warn')
        fail_count = sum(1 for c in checks if c.status == 'fail')

        for c in checks:
            icon = {'pass': '✅', 'warn': '⚠️',
                    'fail': '❌'}[c.status]
            lines.append(
                f"  {icon} {c.name:30s} {c.message}")

        lines.append("")
        lines.append(f"  通过: {pass_count}  警告: {warn_count}  "
                      f"失败: {fail_count}")

        overall = 'fail' if fail_count > 0 else \
                  'warn' if warn_count > 0 else 'pass'
        icon = {'pass': '✅', 'warn': '⚠️', 'fail': '❌'}[overall]
        lines.append(f"  总体: {icon} {overall.upper()}")
        lines.append("=" * 60)

        return '\n'.join(lines)