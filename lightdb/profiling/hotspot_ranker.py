"""
热点排行引擎
- 多维度排行
- 优化收益预估
- Cython/多进程 收益评估
"""
from dataclasses import dataclass, asdict, field
from typing import Dict, List
from .cprofile_analyzer import FunctionStat


@dataclass
class Hotspot:
    """热点"""
    rank: int
    name: str
    file: str
    line: int
    tottime: float
    pct: float
    # 优化建议
    optimize_via: str = ""     # multiprocess / cython / algorithm
    est_gain_pct: float = 0    # 预估收益
    reason: str = ""


# 优化路线判定规则
OPTIMIZATION_RULES = {
    # 文件名/函数名 匹配 → 优化方式
    'btree': ('cython', 60, 'B+Tree 密集指针运算，Cython 收益大'),
    'buffer_pool': ('cython', 50, '页操作，Cython 收益大'),
    'row_format': ('cython', 55, '序列化密集，Cython 收益大'),
    'page': ('cython', 50, '定长字节操作，Cython 收益大'),
    'wal': ('cython', 40, '日志写入，Cython 收益中'),
    'predicate': ('cython', 45, '谓词求值，Cython 收益中'),
    'parser': ('algorithm', 20, '解析可优化算法'),
    'executor': ('multiprocess', 80, '执行层可用多进程并行'),
    'plan_executor': ('multiprocess', 80, '执行层多进程'),
    'server': ('multiprocess', 60, '网络层多进程'),
    'handler': ('multiprocess', 60, '连接处理多进程'),
    'lock': ('algorithm', 30, '锁优化'),
    'json': ('cython', 70, 'JSON 序列化开销大'),
    'codec': ('cython', 65, '编解码'),
}


class HotspotRanker:
    """热点排行器"""

    def __init__(self, functions: List[FunctionStat],
                 total_time: float):
        self.functions = functions
        self.total_time = total_time or 1

    def rank(self, top_n: int = 30,
             exclude_std: bool = True) -> List[Hotspot]:
        """排行"""
        funcs = self.functions
        if exclude_std:
            funcs = [f for f in funcs
                     if not self._is_std(f.file)]

        # 按自身耗时排序
        funcs.sort(key=lambda x: -x.tottime)

        hotspots = []
        for i, f in enumerate(funcs[:top_n], 1):
            pct = f.tottime / self.total_time * 100
            via, gain, reason = self._analyze_optimization(f)
            hotspots.append(Hotspot(
                rank=i,
                name=f.name,
                file=f.file,
                line=f.line,
                tottime=f.tottime,
                pct=pct,
                optimize_via=via,
                est_gain_pct=gain,
                reason=reason,
            ))

        return hotspots

    def _analyze_optimization(self, f: FunctionStat):
        """分析优化方式"""
        text = (f.name + ' ' + f.file).lower()
        for key, (via, gain, reason) in OPTIMIZATION_RULES.items():
            if key in text:
                return via, gain, reason
        return 'algorithm', 10, '待分析'

    def _is_std(self, file: str) -> bool:
        return not file or 'site-packages' in file or \
               file.startswith('<')

    def by_optimization_type(self, top_n: int = 30) -> Dict[str, Dict]:
        """按优化方式汇总"""
        hotspots = self.rank(top_n)
        summary = {}

        for h in hotspots:
            via = h.optimize_via
            if via not in summary:
                summary[via] = {
                    'count': 0,
                    'total_time': 0.0,
                    'total_pct': 0.0,
                    'avg_gain': 0.0,
                    'functions': [],
                }
            s = summary[via]
            s['count'] += 1
            s['total_time'] += h.tottime
            s['total_pct'] += h.pct
            s['avg_gain'] += h.est_gain_pct
            s['functions'].append(h.name)

        for v, s in summary.items():
            s['avg_gain'] /= s['count'] if s['count'] else 1

        return summary

    def estimate_total_gain(self, top_n: int = 30) -> Dict:
        """
        预估总体优化收益
        假设：
        - multiprocess: 让 CPU 边界部分加速（假设 40% 可加速，8核 → 4x）
        - cython: 让 CPU 边界部分加速 3x
        - algorithm: 平均 20% 提升
        """
        hotspots = self.rank(top_n)
        summary = self.by_optimization_type(top_n)

        # 基础假设
        total_pct = 0
        est_new_pct = 0

        for h in hotspots:
            total_pct += h.pct
            if h.optimize_via == 'multiprocess':
                # 假设 40% 可并行，8 核有效 4x
                new_pct = h.pct * (0.6 + 0.4 / 4)
            elif h.optimize_via == 'cython':
                # 假设 3x 加速
                new_pct = h.pct / 3
            else:
                # 假设 20% 提升
                new_pct = h.pct * 0.8
            est_new_pct += new_pct

        # 加速比
        if est_new_pct > 0:
            speedup = total_pct / est_new_pct
        else:
            speedup = 1.0

        return {
            'current_total_pct': total_pct,
            'estimated_new_pct': est_new_pct,
            'estimated_speedup': speedup,
            'by_type': summary,
        }

    def format_report(self, top_n: int = 30) -> str:
        """文本报告"""
        hotspots = self.rank(top_n)
        summary = self.by_optimization_type(top_n)
        est = self.estimate_total_gain(top_n)

        lines = []
        lines.append("=" * 100)
        lines.append(f"  热点排行 & 优化路线 ({top_n} 个热点)")
        lines.append("=" * 100)

        lines.append("\n【Top 30 热点及优化方式】\n")
        lines.append(
            f"  {'#':>3} {'耗时':>8} {'占比':>7} "
            f"{'优化':15s} {'收益':>6} {'函数':45s} {'原因'}")
        lines.append("-" * 100)

        for h in hotspots:
            lines.append(
                f"  {h.rank:3d} {h.tottime:8.3f} {h.pct:6.2f}% "
                f"{h.optimize_via:15s} {h.est_gain_pct:5.0f}% "
                f"{h.name[:45]:45s} {h.reason}")

        lines.append("\n【按优化方式汇总】\n")
        for via, info in summary.items():
            lines.append(
                f"  {via:15s} {info['count']:3d} 个函数, "
                f"耗时 {info['total_time']:7.2f}s "
                f"({info['total_pct']:5.1f}%), "
                f"平均收益 {info['avg_gain']:.0f}%")

        lines.append("\n【总体收益预估】\n")
        lines.append(f"  当前耗时: {est['current_total_pct']:.1f}%")
        lines.append(f"  优化后: {est['estimated_new_pct']:.1f}%")
        lines.append(f"  预估加速: {est['estimated_speedup']:.2f}x")

        lines.append("\n" + "=" * 100)
        return '\n'.join(lines)