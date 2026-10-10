"""
向量化执行引擎
- 一次处理一批（1024/4096）
- NumPy 加速
"""
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


VECTOR_SIZE = 4096


@dataclass
class VectorBatch:
    """向量批次"""
    columns: List[str]
    data: dict = field(default_factory=dict)   # name -> np.array 或 list
    size: int = 0

    def column(self, name: str):
        return self.data.get(name)

    def __len__(self):
        return self.size


class VectorizedScan:
    """
    向量化列扫描
    """

    def __init__(self, column_store, projection: List[str] = None,
                 batch_size: int = VECTOR_SIZE):
        self.store = column_store
        self.projection = projection or column_store.col_names
        self.batch_size = batch_size
        # 预读取全部列
        # 原实现写成 store.scan(...)，而 store 是未定义名字（参数名是
        # column_store，self.store 才是实例属性），构造 VectorizedScan 直接
        # 抛 NameError —— 整个向量化引擎与 hybrid_table.olap_aggregate 都不可用。
        self.data = self.store.scan(self.projection)
        self.total = len(next(iter(self.data.values()))) if self.data else 0
        self.pos = 0

    def next_batch(self) -> Optional[VectorBatch]:
        if self.pos >= self.total:
            return None

        end = min(self.pos + self.batch_size, self.total)
        batch = VectorBatch(columns=self.projection)

        for col in self.projection:
            values = self.data[col][self.pos:end]
            if HAS_NUMPY:
                # 尝试转 NumPy
                if values and isinstance(values[0], (int, float)):
                    try:
                        batch.data[col] = np.array(values)
                    except Exception:
                        batch.data[col] = values
                else:
                    batch.data[col] = values
            else:
                batch.data[col] = values

        batch.size = end - self.pos
        self.pos = end
        return batch


class VectorizedFilter:
    """向量化过滤"""

    def __init__(self, child, column: str, op: str, value):
        self.child = child
        self.column = column
        self.op = op
        self.value = value

    def next_batch(self) -> Optional[VectorBatch]:
        batch = self.child.next_batch()
        if batch is None:
            return None

        col = batch.column(self.column)
        if col is None:
            return batch

        if HAS_NUMPY and isinstance(col, np.ndarray):
            # 向量化
            if self.op == '>':
                mask = col > self.value
            elif self.op == '<':
                mask = col < self.value
            elif self.op == '>=':
                mask = col >= self.value
            elif self.op == '<=':
                mask = col <= self.value
            elif self.op == '=':
                mask = col == self.value
            elif self.op == '!=':
                mask = col != self.value
            else:
                return batch

            # 应用 mask 到所有列
            new_batch = VectorBatch(columns=batch.columns)
            for name in batch.columns:
                c = batch.data.get(name)
                if isinstance(c, np.ndarray):
                    new_batch.data[name] = c[mask]
                else:
                    new_batch.data[name] = [
                        v for i, v in enumerate(c) if mask[i]]
            new_batch.size = int(mask.sum())
            return new_batch
        else:
            # Python 回退
            indices = []
            for i, v in enumerate(col):
                if self._match(v):
                    indices.append(i)

            new_batch = VectorBatch(columns=batch.columns)
            for name in batch.columns:
                c = batch.data.get(name, [])
                new_batch.data[name] = [c[i] for i in indices]
            new_batch.size = len(indices)
            return new_batch

    def _match(self, v):
        if v is None:
            return False
        if self.op == '>': return v > self.value
        if self.op == '<': return v < self.value
        if self.op == '>=': return v >= self.value
        if self.op == '<=': return v <= self.value
        if self.op == '=': return v == self.value
        if self.op == '!=': return v != self.value
        return False


class VectorizedAggregate:
    """向量化聚合"""

    def __init__(self, child, agg_column: str, agg_func: str):
        self.child = child
        self.agg_column = agg_column
        self.agg_func = agg_func.upper()

    def execute(self) -> Any:
        """执行聚合，返回标量"""
        func = self.agg_func

        if func == 'COUNT':
            total = 0
            while True:
                batch = self.child.next_batch()
                if batch is None:
                    break
                total += batch.size
            return total

        # 其他聚合
        if HAS_NUMPY:
            return self._execute_numpy()
        return self._execute_python()

    def _execute_numpy(self) -> Any:
        results = []
        while True:
            batch = self.child.next_batch()
            if batch is None:
                break
            col = batch.column(self.agg_column)
            if col is None:
                continue
            if isinstance(col, np.ndarray):
                col = col[~np.isnan(col)] if col.dtype.kind == 'f' else col
                if len(col) > 0:
                    if self.agg_func == 'SUM':
                        results.append(col.sum())
                    elif self.agg_func == 'MIN':
                        results.append(col.min())
                    elif self.agg_func == 'MAX':
                        results.append(col.max())
                    elif self.agg_func == 'AVG':
                        results.append((col.sum(), len(col)))

        if not results:
            return None

        if self.agg_func == 'SUM':
            return float(sum(results))
        elif self.agg_func == 'MIN':
            return float(min(results))
        elif self.agg_func == 'MAX':
            return float(max(results))
        elif self.agg_func == 'AVG':
            total_sum = sum(r[0] for r in results)
            total_count = sum(r[1] for r in results)
            return float(total_sum / total_count) if total_count else None

    def _execute_python(self) -> Any:
        values = []
        while True:
            batch = self.child.next_batch()
            if batch is None:
                break
            col = batch.column(self.agg_column)
            if col is None:
                continue
            values.extend(v for v in col if v is not None)

        if not values:
            return None

        if self.agg_func == 'SUM':
            return sum(values)
        elif self.agg_func == 'MIN':
            return min(values)
        elif self.agg_func == 'MAX':
            return max(values)
        elif self.agg_func == 'AVG':
            return sum(values) / len(values)


class VectorizedGroupBy:
    """向量化分组聚合"""

    def __init__(self, child, group_cols: List[str],
                 aggs: List[Dict]):
        """
        aggs: [{'func': 'SUM', 'column': 'amount', 'alias': 'total'}]
        """
        self.child = child
        self.group_cols = group_cols
        self.aggs = aggs

    def execute(self) -> List[Dict]:
        """执行分组"""
        groups = {}   # key -> {agg_alias: value}

        while True:
            batch = self.child.next_batch()
            if batch is None:
                break

            n = batch.size
            if n == 0:
                continue

            # 构建分组键
            for i in range(n):
                key = tuple(
                    batch.data[col][i] if col in batch.data else None
                    for col in self.group_cols
                )

                if key not in groups:
                    groups[key] = {a['alias']: [] for a in self.aggs}
                    for a in self.aggs:
                        if a['func'] == 'COUNT':
                            groups[key][a['alias']] = 0

                # 累加
                for a in self.aggs:
                    col = batch.data.get(a['column'])
                    if col is None:
                        continue
                    v = col[i] if i < len(col) else None
                    if v is None:
                        continue

                    if a['func'] == 'COUNT':
                        groups[key][a['alias']] += 1
                    elif a['func'] == 'SUM':
                        if isinstance(groups[key][a['alias']], list):
                            groups[key][a['alias']] = [0]
                        groups[key][a['alias']][0] += v
                    elif a['func'] == 'MIN':
                        if isinstance(groups[key][a['alias']], list):
                            groups[key][a['alias']] = [v]
                        else:
                            groups[key][a['alias']][0] = min(
                                groups[key][a['alias']][0], v)
                    elif a['func'] == 'MAX':
                        if isinstance(groups[key][a['alias']], list):
                            groups[key][a['alias']] = [v]
                        else:
                            groups[key][a['alias']][0] = max(
                                groups[key][a['alias']][0], v)

        # 生成结果
        result = []
        for key, vals in groups.items():
            row = list(key)
            for a in self.aggs:
                v = vals[a['alias']]
                if isinstance(v, list):
                    v = v[0]
                row.append(v)
            result.append(row)

        return result


class VectorizedSort:
    """向量化排序"""

    def __init__(self, child, column: str, desc: bool = False):
        self.child = child
        self.column = column
        self.desc = desc

    def execute(self) -> VectorBatch:
        """全物化排序"""
        all_data = {}
        total = 0

        while True:
            batch = self.child.next_batch()
            if batch is None:
                break
            total += batch.size
            for name in batch.columns:
                col = batch.data.get(name, [])
                all_data.setdefault(name, []).extend(
                    col.tolist() if HAS_NUMPY and isinstance(col, np.ndarray)
                    else col)

        # 排序
        if self.column not in all_data:
            return VectorBatch(columns=[], size=0)

        indices = list(range(total))
        sort_col = all_data[self.column]
        indices.sort(
            key=lambda i: (sort_col[i] is None, sort_col[i]),
            reverse=self.desc)

        # 重排
        result = VectorBatch(columns=list(all_data.keys()))
        for name in all_data:
            result.data[name] = [all_data[name][i] for i in indices]
        result.size = total
        return result


class VectorizedProject:
    """列投影"""

    def __init__(self, child, columns: List[str]):
        self.child = child
        self.columns = columns

    def next_batch(self):
        batch = self.child.next_batch()
        if batch is None:
            return None
        new_batch = VectorBatch(columns=self.columns)
        for name in self.columns:
            if name in batch.data:
                new_batch.data[name] = batch.data[name]
        new_batch.size = batch.size
        return new_batch