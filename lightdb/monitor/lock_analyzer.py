"""
锁等待分析
- 实时锁等待列表
- 死锁历史
- 等待图
- 阻塞链
"""
import time
from collections import defaultdict, deque
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Tuple


@dataclass
class LockWait:
    """锁等待记录"""
    wait_id: str
    waiting_pid: int
    blocking_pid: int
    waiting_sql: str = ""
    blocking_sql: str = ""
    lock_type: str = "X"          # S / X
    lock_object: str = ""
    wait_seconds: float = 0
    timestamp: float = 0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class DeadlockRecord:
    """死锁记录"""
    ts: float
    victim_pid: int
    participants: List[int]
    cycle: List[str]           # 锁链描述
    waiting_sqls: List[str] = field(default_factory=list)


class LockAnalyzer:
    """锁分析器"""

    def __init__(self, conn=None):
        self.conn = conn
        self.wait_history: deque = deque(maxlen=10000)
        self.deadlock_history: List[DeadlockRecord] = []

    # ==================== 实时监控 ====================

    def get_current_waits(self) -> List[LockWait]:
        """获取当前锁等待"""
        if not self.conn:
            return []

        waits = []
        try:
            # 尝试查询锁表
            cols, rows = self.conn.query(
                "SELECT * FROM information_schema.INNODB_LOCK_WAITS")
            # 简化：失败则返回空
        except Exception:
            pass

        try:
            # 从 SHOW PROCESSLIST 推断
            cols, rows = self.conn.query("SHOW PROCESSLIST")
            # 找 state=Locked 的
            for row in rows:
                d = dict(zip([c.lower() for c in cols], row))
                state = str(d.get('state', '')).lower()
                if 'lock' in state or 'waiting' in state:
                    w = LockWait(
                        wait_id=f"w_{d.get('id')}",
                        waiting_pid=d.get('id', 0),
                        blocking_pid=0,
                        waiting_sql=str(d.get('info', ''))[:200],
                        lock_type='X',
                        wait_seconds=float(d.get('time', 0) or 0),
                    )
                    waits.append(w)
        except Exception:
            pass

        self.wait_history.extend(waits)
        return waits

    # ==================== 阻塞链 ====================

    def build_blocking_chain(self,
                              waits: List[LockWait]) -> List[List]:
        """构建阻塞链（A→B→C）"""
        # 建图：waiting -> blocking
        graph = {}
        for w in waits:
            if w.blocking_pid:
                graph[w.waiting_pid] = w.blocking_pid

        # 找根
        all_pids = set(graph.keys()) | set(graph.values())
        roots = all_pids - set(graph.values())

        chains = []
        for root in roots:
            chain = [root]
            current = root
            visited = {root}
            while current in graph:
                nxt = graph[current]
                if nxt in visited:
                    chain.append(f"{nxt}(循环)")
                    break
                chain.append(nxt)
                visited.add(nxt)
                current = nxt
            if len(chain) > 1:
                chains.append(chain)

        return chains

    # ==================== 死锁检测 ====================

    def record_deadlock(self, victim_pid: int,
                        participants: List[int],
                        waiting_sqls: List[str] = None):
        """记录死锁"""
        record = DeadlockRecord(
            ts=time.time(),
            victim_pid=victim_pid,
            participants=participants,
            cycle=[f"{pid} →" for pid in participants] + [f"{participants[0]}"],
            waiting_sqls=waiting_sqls or [],
        )
        self.deadlock_history.append(record)
        if len(self.deadlock_history) > 500:
            self.deadlock_history = self.deadlock_history[-200:]

    # ==================== 等待图可视化 ====================

    def render_wait_graph(self) -> str:
        """ASCII 渲染锁等待图"""
        waits = self.get_current_waits()
        if not waits:
            return "（无锁等待）"

        chains = self.build_blocking_chain(waits)
        if not chains:
            return "（无阻塞链）"

        lines = ["阻塞链:"]
        for chain in chains:
            lines.append("  " + " → ".join(str(p) for p in chain))

        lines.append("")
        lines.append("详细等待:")
        for w in waits:
            lines.append(
                f"  PID {w.waiting_pid} 等待 {w.wait_seconds:.1f}s: "
                f"{w.waiting_sql[:80]}")

        return '\n'.join(lines)

    # ==================== 统计 ====================

    def stats(self) -> Dict:
        return {
            'history_waits': len(self.wait_history),
            'deadlocks': len(self.deadlock_history),
            'current_waits': len(self.get_current_waits()),
        }

    def report(self) -> str:
        lines = ["=" * 60]
        lines.append("  锁等待分析报告")
        lines.append("=" * 60)

        stats = self.stats()
        lines.append(f"  历史等待次数: {stats['history_waits']}")
        lines.append(f"  死锁次数:     {stats['deadlocks']}")
        lines.append(f"  当前等待:     {stats['current_waits']}")
        lines.append("")
        lines.append(self.render_wait_graph())

        if self.deadlock_history:
            lines.append("")
            lines.append("最近死锁:")
            for d in self.deadlock_history[-3:]:
                lines.append(
                    f"  [{time.strftime('%H:%M:%S', time.localtime(d.ts))}] "
                    f"victim={d.victim_pid}, "
                    f"参与: {d.participants}")

        lines.append("=" * 60)
        return '\n'.join(lines)