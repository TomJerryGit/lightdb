"""
向量化聚合器
"""
import math
from typing import Any, List, Optional

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


class Aggregator:
    """通用聚合器"""

    @staticmethod
    def aggregate(values: List, func: str) -> Any:
        func = func.upper()
        vals = [v for v in values if v is not None]

        if func == 'COUNT':
            return len(values)
        if func == 'COUNT_DISTINCT':
            return len(set(str(v) for v in vals))
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
        return None


class VectorizedAggregator:
    """向量化聚合（NumPy）"""

    def __init__(self):
        self.partials = []

    def add_batch(self, values, func: str):
        """添加一批"""
        self.partials.append((values, func))

    def finalize(self, func: str):
        if not HAS_NUMPY:
            # 回退
            all_values = []
            for v, _ in self.partials:
                all_values.extend(v)
            return Aggregator.aggregate(all_values, func)

        arrays = []
        for v, _ in self.partials:
            if isinstance(v, np.ndarray):
                arrays.append(v)
            else:
                arrays.append(np.array(v, dtype=np.float64))

        if not arrays:
            return None

        arr = np.concatenate(arrays)
        arr = arr[~np.isnan(arr)]

        if len(arr) == 0:
            return None

        f = func.upper()
        if f == 'SUM': return float(arr.sum())
        if f == 'AVG': return float(arr.mean())
        if f == 'MIN': return float(arr.min())
        if f == 'MAX': return float(arr.max())
        if f in ('STDDEV', 'STDDEV_POP'): return float(arr.std())
        if f == 'STDDEV_SAMP': return float(arr.std(ddof=1))
        if f in ('VARIANCE', 'VAR_POP'): return float(arr.var())
        if f == 'VAR_SAMP': return float(arr.var(ddof=1))
        if f == 'MEDIAN': return float(np.median(arr))
        return None