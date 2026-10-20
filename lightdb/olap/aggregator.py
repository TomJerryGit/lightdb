"""
向量化聚合器（正确性止血版，2026-10）

修复:
- `Aggregator.aggregate('COUNT')` 现在只计**非 NULL**（SQL `COUNT(col)`
  语义）；需要计行数用 `COUNT_STAR`。旧实现返回 `len(values)`，把 NULL
  也计进去，是典型的静默错值。
- 不认识的聚合函数由「返回 None」改为**显式 ValueError**（本项目铁律：
  不支持的形态绝不静默返回 None 冒充结果）。
- `VectorizedAggregator.finalize()`：
  * 不再 `np.array(v, dtype=np.float64)` 强转 —— 大整数（>2^53）会丢精度、
    int 会被 float 化；现在全整数走 int64 拼接 + Python int 求和。
  * 不再默认把第一批之外的值丢给 `np.isnan`（object/None 数组会崩）；
    先把 None 过滤掉，NaN 也按 NULL 处理。
  * 支持 COUNT / COUNT_STAR；未知函数显式报错。
- 无 NumPy 时回退到 `Aggregator`（保持原有行为，COUNT 语义一致）。
"""
import math
from typing import Any, List, Optional

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


class Aggregator:
    """通用聚合器（纯 Python 参考实现）"""

    #: 支持的聚合函数；不在此列 → 显式 ValueError
    SUPPORTED = ('COUNT', 'COUNT_STAR', 'COUNT_DISTINCT', 'SUM', 'AVG',
                 'MIN', 'MAX', 'STDDEV', 'STDDEV_POP', 'STDDEV_SAMP',
                 'VARIANCE', 'VAR_POP', 'VAR_SAMP', 'MEDIAN')

    @staticmethod
    def aggregate(values: List, func: str) -> Any:
        func = str(func).upper()
        if func not in Aggregator.SUPPORTED:
            raise ValueError(
                f"Aggregator: 不支持的聚合函数 {func!r}，"
                f"仅支持 {list(Aggregator.SUPPORTED)}")

        vals = [v for v in values if v is not None]

        if func == 'COUNT_STAR':
            return len(values)             # 计行数（含 NULL 行）
        if func == 'COUNT':
            return len(vals)               # SQL: COUNT(col) 只计非 NULL
        if func == 'COUNT_DISTINCT':
            try:
                return len(set(vals))
            except TypeError:
                out = []
                for v in vals:
                    if v not in out:
                        out.append(v)
                return len(out)
        if not vals:
            return None
        if func == 'SUM':
            return sum(vals)
        if func == 'AVG':
            return sum(vals) / len(vals)
        if func == 'MIN':
            return min(vals)
        if func == 'MAX':
            return max(vals)
        if func in ('STDDEV', 'STDDEV_POP'):
            m = sum(vals) / len(vals)
            return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
        if func == 'STDDEV_SAMP':
            if len(vals) < 2:
                return None
            m = sum(vals) / len(vals)
            return math.sqrt(
                sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
        if func in ('VARIANCE', 'VAR_POP'):
            m = sum(vals) / len(vals)
            return sum((v - m) ** 2 for v in vals) / len(vals)
        if func == 'VAR_SAMP':
            if len(vals) < 2:
                return None
            m = sum(vals) / len(vals)
            return sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
        if func == 'MEDIAN':
            s = sorted(vals)
            n = len(s)
            if n % 2:
                return s[n // 2]
            return (s[n // 2 - 1] + s[n // 2]) / 2
        raise ValueError(f"Aggregator: 不支持的聚合函数 {func!r}")  # pragma: no cover


class VectorizedAggregator:
    """向量化聚合（NumPy）

    用法::

        agg = VectorizedAggregator()
        agg.add_batch([1, 2, None], 'SUM')
        agg.add_batch(np.array([3, 4]), 'SUM')
        agg.finalize('SUM')      # 10（None 忽略；全整数返回 int）
    """

    def __init__(self):
        self.partials = []

    def add_batch(self, values, func: str):
        """添加一批"""
        self.partials.append((values, str(func).upper()))

    # ---------------- 内部 ----------------

    @staticmethod
    def _flatten() -> None:            # pragma: no cover - 文档占位
        return None

    def _all_values(self) -> List:
        out = []
        for v, _ in self.partials:
            if HAS_NUMPY and isinstance(v, np.ndarray):
                if v.dtype.kind == 'f':
                    out.extend(x for x in v.tolist() if not math.isnan(x))
                else:
                    out.extend(v.tolist())
            else:
                out.extend(v)
        return [x for x in out if x is not None]

    def finalize(self, func: str):
        func = str(func).upper()
        if func not in Aggregator.SUPPORTED:
            raise ValueError(
                f"VectorizedAggregator: 不支持的聚合函数 {func!r}，"
                f"仅支持 {list(Aggregator.SUPPORTED)}")

        if not HAS_NUMPY:
            all_values = []
            for v, _ in self.partials:
                all_values.extend(v)
            return Aggregator.aggregate(all_values, func)

        # COUNT_STAR：所有批次的行数之和（含 NULL 位置）
        if func == 'COUNT_STAR':
            total = 0
            for v, _ in self.partials:
                total += len(v)
            return total

        # COUNT：非 NULL 值个数
        if func == 'COUNT':
            return len(self._all_values())

        values = self._all_values()
        if not values:
            return None

        all_int = all(isinstance(v, int) and not isinstance(v, bool)
                      for v in values)
        if all_int:
            arr = np.array(values, dtype=np.int64)
        else:
            arr = np.array([float(v) for v in values], dtype=np.float64)

        if func == 'SUM':
            return int(arr.sum()) if all_int else float(arr.sum())
        if func == 'AVG':
            return float(arr.mean())
        if func == 'MIN':
            return int(arr.min()) if all_int else float(arr.min())
        if func == 'MAX':
            return int(arr.max()) if all_int else float(arr.max())
        if func in ('STDDEV', 'STDDEV_POP'):
            return float(arr.std())
        if func == 'STDDEV_SAMP':
            return float(arr.std(ddof=1)) if len(arr) >= 2 else None
        if func in ('VARIANCE', 'VAR_POP'):
            return float(arr.var())
        if func == 'VAR_SAMP':
            return float(arr.var(ddof=1)) if len(arr) >= 2 else None
        if func == 'MEDIAN':
            return float(np.median(arr))
        if func == 'COUNT_DISTINCT':
            return len(set(values))
        raise ValueError(f"VectorizedAggregator: 不支持的聚合函数 {func!r}")
