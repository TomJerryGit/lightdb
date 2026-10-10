"""
数据质量 SLI/SLO
- SLI 定义（指标）
- SLO 目标设定
- 达标监控
- 错误预算
"""
import json
import threading
import time
from collections import deque
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Callable, Optional


SLO_FILE = Path.home() / '.lightdb_studio' / 'slo.json'


@dataclass
class SLI:
    """Service Level Indicator"""
    name: str = ""              # 唯一标识
    display_name: str = ""
    description: str = ""
    # 指标来源
    metric: str = ""            # row_count / null_ratio / freshness / quality_score
    target_table: str = ""      # 监控的表
    target_column: str = ""     # 可选的列
    # 单位
    unit: str = ""
    # 方向: higher_better / lower_better
    direction: str = "lower_better"

    def evaluate(self, conn) -> Optional[float]:
        """执行评估，返回当前值"""
        if not conn:
            return None

        try:
            if self.metric == 'row_count':
                _, r = conn.query(
                    f"SELECT COUNT(*) FROM `{self.target_table}`")
                return float(r[0][0]) if r else 0

            elif self.metric == 'null_ratio':
                _, r = conn.query(
                    f"SELECT COUNT(*) FROM `{self.target_table}` "
                    f"WHERE `{self.target_column}` IS NULL")
                null_cnt = r[0][0] if r else 0
                _, r2 = conn.query(
                    f"SELECT COUNT(*) FROM `{self.target_table}`")
                total = r2[0][0] if r2 else 0
                return null_cnt / total if total else 0

            elif self.metric == 'distinct_ratio':
                _, r = conn.query(
                    f"SELECT COUNT(DISTINCT `{self.target_column}`), "
                    f"COUNT(*) FROM `{self.target_table}`")
                if r:
                    d, t = r[0]
                    return d / t if t else 0
                return 0

            elif self.metric == 'freshness':
                # 数据新鲜度（秒）
                if not self.target_column:
                    return None
                _, r = conn.query(
                    f"SELECT MAX(`{self.target_column}`) "
                    f"FROM `{self.target_table}`")
                if r and r[0][0]:
                    from datetime import datetime
                    try:
                        ts = r[0][0]
                        if isinstance(ts, str):
                            dt = datetime.strptime(
                                ts[:19], '%Y-%m-%d %H:%M:%S')
                            return time.time() - dt.timestamp()
                        elif isinstance(ts, (int, float)):
                            return time.time() - ts
                    except Exception:
                        return None
                return None

            elif self.metric == 'max_value':
                _, r = conn.query(
                    f"SELECT MAX(`{self.target_column}`) "
                    f"FROM `{self.target_table}`")
                return float(r[0][0]) if r and r[0][0] is not None else None

            elif self.metric == 'min_value':
                _, r = conn.query(
                    f"SELECT MIN(`{self.target_column}`) "
                    f"FROM `{self.target_table}`")
                return float(r[0][0]) if r and r[0][0] is not None else None

            elif self.metric == 'avg_value':
                _, r = conn.query(
                    f"SELECT AVG(`{self.target_column}`) "
                    f"FROM `{self.target_table}`")
                return float(r[0][0]) if r and r[0][0] is not None else None

        except Exception:
            return None

        return None


@dataclass
class SLO:
    """Service Level Objective"""
    name: str = ""
    sli_name: str = ""
    description: str = ""
    # 目标
    target: float = 0
    # 允许的偏差
    tolerance: float = 0
    # 窗口
    window_hours: int = 24
    # 状态
    enabled: bool = True
    # 计算周期（秒）
    check_interval: int = 300
    # 告警
    notify_channels: List[str] = field(default_factory=list)
    notify_on_breach: bool = True
    # 统计
    last_check: float = 0
    last_value: float = 0
    last_status: str = "unknown"   # ok / warning / breach
    breach_count: int = 0
    # 历史
    samples: List[Dict] = field(default_factory=list)
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()

    def is_met(self, value: float) -> bool:
        """是否达标"""
        if value is None:
            return True
        direction = 'lower_better'  # 从 SLI 继承
        # 判断
        if self.tolerance:
            if self.target > 0:
                # 允许 target ± tolerance
                return abs(value - self.target) <= self.tolerance
        return value >= self.target


class SLOManager:
    """SLI/SLO 管理器"""

    def __init__(self, conn_provider=None,
                 notification_mgr=None):
        self.file = SLO_FILE
        self.conn_provider = conn_provider
        self.notification_mgr = notification_mgr
        self.slis: Dict[str, SLI] = {}
        self.slos: Dict[str, SLO] = {}
        self._callbacks: List[Callable] = []
        self._running = False
        self._thread = None
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for n, sd in data.get('slis', {}).items():
                self.slis[n] = SLI(**sd)
            for n, sd in data.get('slos', {}).items():
                self.slos[n] = SLO(**sd)
        except Exception:
            pass

    def save(self):
        data = {
            'slis': {n: asdict(s) for n, s in self.slis.items()},
            'slos': {n: asdict(s) for n, s in self.slos.items()},
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== CRUD ====================

    def add_sli(self, sli: SLI):
        self.slis[sli.name] = sli
        self.save()

    def add_slo(self, slo: SLO):
        if slo.sli_name not in self.slis:
            raise ValueError(f"SLI {slo.sli_name} 不存在")
        self.slos[slo.name] = slo
        self.save()

    def remove_sli(self, name: str):
        if name in self.slis:
            del self.slis[name]
            self.save()

    def remove_slo(self, name: str):
        if name in self.slos:
            del self.slos[name]
            self.save()

    # ==================== 评估 ====================

    def check_slo(self, name: str,
                  connection: str = '') -> Dict:
        """检查单个 SLO"""
        slo = self.slos.get(name)
        if not slo:
            return {'error': 'SLO 不存在'}

        sli = self.slis.get(slo.sli_name)
        if not sli:
            return {'error': 'SLI 不存在'}

        conn = self.conn_provider(connection) \
            if self.conn_provider else None
        value = sli.evaluate(conn)

        now = time.time()
        slo.last_check = now
        slo.last_value = value if value is not None else 0

        # 判断状态
        status = 'unknown'
        if value is None:
            status = 'error'
        else:
            # 根据 direction 判断
            if sli.direction == 'lower_better':
                # 目标是 ≤ target
                if value <= slo.target:
                    status = 'ok'
                elif value <= slo.target + slo.tolerance:
                    status = 'warning'
                else:
                    status = 'breach'
            else:
                # higher_better
                if value >= slo.target:
                    status = 'ok'
                elif value >= slo.target - slo.tolerance:
                    status = 'warning'
                else:
                    status = 'breach'

        prev_status = slo.last_status
        slo.last_status = status

        # 记录样本
        slo.samples.append({
            'ts': now,
            'value': value,
            'status': status,
        })
        # 保留窗口内
        cutoff = now - slo.window_hours * 3600
        slo.samples = [s for s in slo.samples if s['ts'] >= cutoff]

        # 状态转换：ok → breach
        if status == 'breach' and prev_status != 'breach':
            slo.breach_count += 1
            self._fire_breach(slo, sli, value)

        self.save()

        # 通知回调
        for cb in self._callbacks:
            try:
                cb(slo, status, value)
            except Exception:
                pass

        return {
            'name': slo.name,
            'sli': sli.name,
            'value': value,
            'target': slo.target,
            'status': status,
            'compliance': self._compliance(slo),
        }

    def _compliance(self, slo: SLO) -> float:
        """合规率"""
        if not slo.samples:
            return 1.0
        ok = sum(1 for s in slo.samples if s['status'] == 'ok')
        return ok / len(slo.samples)

    def error_budget(self, name: str) -> Dict:
        """错误预算"""
        slo = self.slos.get(name)
        if not slo:
            return {}

        # 合规目标：比如 99%，则错误预算是 1%
        compliance = self._compliance(slo)
        target_compliance = 0.99   # 默认 99%

        return {
            'target': target_compliance,
            'current': compliance,
            'remaining': max(0, compliance - target_compliance),
            'budget_used': max(0, (target_compliance - compliance) /
                                (1 - target_compliance)) if target_compliance < 1 else 0,
            'samples': len(slo.samples),
        }

    # ==================== 通知 ====================

    def _fire_breach(self, slo: SLO, sli: SLI, value: float):
        """触发 SLO 违约"""
        if not slo.notify_on_breach:
            return
        if not self.notification_mgr:
            return

        from .notifications import NotificationMessage
        msg = NotificationMessage(
            title=f"SLO 未达标: {slo.name}",
            content=(f"指标: {sli.display_name or sli.name}\n"
                     f"当前值: {value}\n"
                     f"目标: {slo.target}\n"
                     f"表: {sli.target_table}"),
            level='error',
            source='slo',
        )
        self.notification_mgr.notify(msg)

    def on_check(self, callback):
        self._callbacks.append(callback)

    # ==================== 后台监控 ====================

    def start_monitoring(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True,
            name='SLOMonitor')
        self._thread.start()

    def stop_monitoring(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _monitor_loop(self):
        last_check = {}
        while self._running:
            now = time.time()
            for name, slo in self.slos.items():
                if not slo.enabled:
                    continue
                if now - last_check.get(name, 0) >= slo.check_interval:
                    try:
                        self.check_slo(name)
                    except Exception:
                        pass
                    last_check[name] = now
            time.sleep(10)

    # ==================== 内置模板 ====================

    def add_default_slis(self, tables: List[str]):
        """为表批量添加默认 SLI"""
        for t in tables[:20]:  # 限制数量
            # 行数 SLI
            name = f"{t}_row_count"
            if name not in self.slis:
                self.slis[name] = SLI(
                    name=name,
                    display_name=f"{t} 行数",
                    metric='row_count',
                    target_table=t,
                    direction='higher_better',
                )

        self.save()

    def stats(self) -> dict:
        statuses = {'ok': 0, 'warning': 0, 'breach': 0, 'unknown': 0}
        for slo in self.slos.values():
            statuses[slo.last_status] = statuses.get(
                slo.last_status, 0) + 1
        return {
            'total_slis': len(self.slis),
            'total_slos': len(self.slos),
            'enabled': sum(1 for s in self.slos.values() if s.enabled),
            'statuses': statuses,
        }