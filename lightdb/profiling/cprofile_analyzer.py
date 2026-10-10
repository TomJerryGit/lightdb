"""
cProfile 深度分析
- 采集 + 解析
- 热点排行
- 按模块聚合
- 调用图分析
"""
import cProfile
import pstats
import io
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class FunctionStat:
    """函数统计"""
    name: str
    file: str = ""
    line: int = 0
    ncalls: int = 0
    tottime: float = 0     # 本函数耗时（不含子调用）
    cumtime: float = 0     # 累计耗时（含子调用）
    percall: float = 0     # tottime / ncalls
    cumpercall: float = 0  # cumtime / ncalls
    callers: List[Dict] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


class CProfileAnalyzer:
    """cProfile 分析器"""

    def __init__(self):
        self.stats: Optional[pstats.Stats] = None
        self.functions: List[FunctionStat] = []
        self.total_time: float = 0

    # ==================== 采集 ====================

    def profile_call(self, func: Callable, *args,
                     output: str = None, **kwargs) -> Dict:
        """采集单次调用"""
        profiler = cProfile.Profile()
        profiler.enable()
        t0 = time.perf_counter()
        try:
            result = func(*args, **kwargs)
        finally:
            profiler.disable()
            elapsed = time.perf_counter() - t0
            profiler.create_stats()

            if output:
                profiler.dump_stats(output)
            else:
                import tempfile
                output = tempfile.mktemp(suffix='.prof')
                profiler.dump_stats(output)

            self._load_stats(output)
            if not output or not os.path.exists(output):
                return {'elapsed': elapsed}

        return {
            'elapsed': elapsed,
            'result': result,
            'profile_file': output,
        }

    def profile_workload(self, runner: Callable,
                          duration: int = 30,
                          output: str = 'workload.prof') -> str:
        """
        采集一段负载
        runner 需为阻塞函数（内部自行控制时长）
        """
        profiler = cProfile.Profile()
        profiler.enable()

        try:
            runner(duration)
        finally:
            profiler.disable()
            profiler.dump_stats(output)

        self._load_stats(output)
        return output

    def _load_stats(self, path: str):
        """加载 pstats"""
        if not os.path.exists(path):
            return
        self.stats = pstats.Stats(path)
        self.total_time = self.stats.total_tt
        self._parse_functions()

    # ==================== 解析 ====================

    def _parse_functions(self):
        """从 pstats 提取函数统计"""
        if not self.stats:
            return

        self.functions = []

        for (file, line, name), (cc, nc, tt, ct, callers) in \
                self.stats.stats.items():
            fs = FunctionStat(
                name=name,
                file=file,
                line=line,
                ncalls=nc,
                tottime=tt,
                cumtime=ct,
                percall=(tt / nc if nc > 0 else 0),
                cumpercall=(ct / nc if nc > 0 else 0),
            )

            for caller, _ in list(callers.items())[:5]:
                fs.callers.append({
                    'name': caller[2],
                    'file': caller[0],
                    'line': caller[1],
                })

            self.functions.append(fs)

    # ==================== 排行 ====================

    def top_by_tottime(self, n: int = 30,
                       exclude_std: bool = True) -> List[FunctionStat]:
        """按自身耗时排行"""
        funcs = self.functions
        if exclude_std:
            funcs = [f for f in funcs if not self._is_stdlib(f.file)]
        return sorted(funcs, key=lambda x: -x.tottime)[:n]

    def top_by_cumtime(self, n: int = 30,
                       exclude_std: bool = True) -> List[FunctionStat]:
        """按累计耗时排行"""
        funcs = self.functions
        if exclude_std:
            funcs = [f for f in funcs if not self._is_stdlib(f.file)]
        return sorted(funcs, key=lambda x: -x.cumtime)[:n]

    def by_module(self) -> Dict[str, Dict]:
        """按模块聚合"""
        modules = defaultdict(lambda: {
            'tottime': 0.0, 'cumtime': 0.0,
            'functions': 0, 'calls': 0,
        })

        for f in self.functions:
            mod = self._module_name(f.file)
            m = modules[mod]
            m['tottime'] += f.tottime
            m['cumtime'] += f.cumtime
            m['functions'] += 1
            m['calls'] += f.ncalls

        # 计算百分比
        total = self.total_time or 1
        for mod, m in modules.items():
            m['pct'] = m['tottime'] / total * 100

        return dict(sorted(modules.items(),
                             key=lambda x: -x[1]['tottime']))

    def by_category(self) -> Dict[str, float]:
        """按类别聚合（数据库 vs 标准库 vs 三方）"""
        categories = {
            'lightdb': 0.0,
            'stdlib': 0.0,
            'third_party': 0.0,
            'other': 0.0,
        }

        for f in self.functions:
            cat = self._categorize(f.file)
            categories[cat] += f.tottime

        total = self.total_time or 1
        return {
            k: {'time': v, 'pct': v / total * 100}
            for k, v in categories.items()
        }

    # ==================== 工具 ====================

    def _is_stdlib(self, file: str) -> bool:
        """判断是否标准库"""
        if not file:
            return False
        # 系统标准库路径
        return (sys.prefix in file and 'lightdb' not in file) or \
               '<frozen' in file or file.startswith('<')

    def _module_name(self, file: str) -> str:
        """提取模块名"""
        if not file:
            return 'unknown'
        # lightdb/storage/btree.py → lightdb.storage
        parts = file.replace('\\', '/').split('/')
        if 'lightdb' in parts:
            i = parts.index('lightdb')
            if i + 1 < len(parts):
                return f"lightdb.{parts[i+1]}"
        if 'site-packages' in parts:
            i = parts.index('site-packages')
            if i + 1 < len(parts):
                return f"third.{parts[i+1]}"
        return parts[-1] if parts else 'unknown'

    def _categorize(self, file: str) -> str:
        if not file:
            return 'other'
        if 'lightdb' in file.replace('\\', '/'):
            return 'lightdb'
        if 'site-packages' in file:
            return 'third_party'
        if self._is_stdlib(file):
            return 'stdlib'
        return 'other'

    # ==================== 报告 ====================

    def format_report(self, top_n: int = 30) -> str:
        """生成文本报告"""
        lines = []
        lines.append("=" * 100)
        lines.append(f"  cProfile 分析报告 (总耗时: {self.total_time:.2f}s)")
        lines.append("=" * 100)

        # 类别分布
        lines.append("\n【耗时分布】")
        cats = self.by_category()
        for cat, info in sorted(cats.items(),
                                 key=lambda x: -x[1]['time']):
            bar = '█' * int(info['pct'] / 2)
            lines.append(
                f"  {cat:15s} {info['time']:8.2f}s "
                f"({info['pct']:5.1f}%) {bar}")

        # 模块分布
        lines.append("\n【按模块 Top 15】")
        mods = list(self.by_module().items())[:15]
        for mod, info in mods:
            bar = '█' * int(info['pct'] / 2)
            lines.append(
                f"  {mod:30s} {info['tottime']:8.2f}s "
                f"({info['pct']:5.1f}%) {bar}")

        # Top 函数
        lines.append(f"\n【Top {top_n} 热点函数（自身耗时）】")
        lines.append(f"  {'#':>3} {'tottime':>9} {'ncalls':>10} "
                      f"{'function':45} {'file'}")
        lines.append("-" * 100)

        for i, f in enumerate(self.top_by_tottime(top_n), 1):
            file_short = f.file.split('/')[-1] if f.file else '?'
            lines.append(
                f"  {i:3d} {f.tottime:9.3f} {f.ncalls:>10} "
                f"{f.name:45s} {file_short}:{f.line}")

        lines.append("\n" + "=" * 100)
        return '\n'.join(lines)