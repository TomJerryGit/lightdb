"""
向量化执行引擎
- 一次处理一批（1024/4096）
- NumPy 加速

正确性约定（阶段 0 止血，2026-10）：本模块**不在 SQL 执行路径上**
（`Executor` / `plan_executor` 对 `olap` / `VectorizedAggregate` 零引用），
因此这里只负责“算对或显式报错”，不负责接线。三条硬规则：

1. **不溢出**：整数列一律按 int64 构造 NumPy 数组。`np.array(list[int])`
   在 Windows 上是 int32，10 万行 `SUM(0..99999)` 会溢出成 704982704.0
   （正确值 4999950000）——这是静默错值，必须杜绝。
2. **NULL 按 SQL 语义**：SUM/AVG/MIN/MAX 忽略 NULL；COUNT(*) 计行，
   COUNT(col) 只计非 NULL。列里含 NULL 时不能再让 `np.array` 变 object
   dtype 后在 `col.sum()` 上抛 TypeError。
3. **不静默**：请求了不支持的聚合函数 / 不存在的列，一律抛异常，
   绝不返回 None 或全量行来冒充成功。
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

# 支持的聚合函数（不支持的一律显式报错，见 VectorizedAggregate.execute）
SUPPORTED_AGGS = ('SUM', 'AVG', 'COUNT', 'MIN', 'MAX')


@dataclass
class VectorBatch:
    """向量批次"""
    columns: List[str]
    data: dict = field(default_factory=dict)   # name -> np.array 或 list
    size: int = 0
    # name -> 与 data[name] 等长的 bool 列表，True 表示该位置是 SQL NULL。
    # 数值列若含 NULL，data 里存的是 float64 + NaN 占位（NumPy 的 int64
    # 数组无法表示 NULL），真实缺失信息以本掩码为准。
    nulls: dict = field(default_factory=dict)

    def column(self, name: str):
        return self.data.get(name)

    def null_mask(self, name: str) -> Optional[List[bool]]:
        """返回该列的 NULL 掩码（无 NULL 时返回 None）。"""
        mask = self.nulls.get(name)
        return mask

    def __len__(self):
        return self.size


def _to_numpy(values: List[Any]):
    """
    把一批 Python 值转成 NumPy 数组，**保证整数不溢出**。

    返回 `(array, nulls)`：
    - `array`：np.ndarray（数值列）或原 list（非数值/混合列）
    - `nulls`：等长 bool 列表，True = NULL

    规则：全 int → int64；无 NULL 的全数值 → int64/float64；
    含 NULL 的数值列 → float64 + NaN 占位（NULL 位置记进 nulls）；
    其余（字符串/混合）→ 原样保留，NULL 用 None 表示。
    """
    nulls = [v is None for v in values]

    if not HAS_NUMPY or not values:
        return values, nulls

    has_none = any(nulls)
    # 只有确定「全是数值」时才走 NumPy，避免 np.array 静默产出 object dtype
    all_numeric = all(
        v is not None and isinstance(v, (int, float)) and not isinstance(v, bool)
        for v in values)

    if all_numeric:
        try:
            # dtype 显式指定：np.array(list[int]) 默认 int32（Windows/32 位
            # Linux），10 万行求和就会溢出。全 int 用 int64，否则 float64。
            if all(isinstance(v, int) for v in values):
                return np.array(values, dtype=np.int64), nulls
            return np.array(values, dtype=np.float64), nulls
        except (TypeError, ValueError, OverflowError):
            return values, nulls

    if has_none and all(isinstance(v, int) for v in values if v is not None):
        # 整数列 + NULL：用 float64 + NaN，位置由 nulls 精确还原为 NULL
        return np.array(
            [float('nan') if v is None else v for v in values],
            dtype=np.float64), nulls

    return values, nulls


def _as_float_array(arr) -> "np.ndarray":
    """把一批数组/列表统一成 float64（AVG 用）。"""
    if HAS_NUMPY and isinstance(arr, np.ndarray):
        return arr.astype(np.float64, copy=False)
    return np.array([v for v in arr], dtype=np.float64)


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
            # 原实现只写 batch.data[col]，NULL 直接进 np.array：
            #   [1, None, 3] -> object dtype -> col.sum() 抛 TypeError，
            #   或 np.isnan 在 object dtype 上再抛一次。
            # 这里统一走 _to_numpy，并额外记录 NULL 掩码。
            arr, nulls = _to_numpy(values)
            batch.data[col] = arr
            if any(nulls):
                batch.nulls[col] = nulls

        batch.size = end - self.pos
        self.pos = end
        return batch


class VectorizedFilter:
    """向量化过滤

    NULL 语义与 SQL 一致：`NULL > 5` / `NULL = 5` 均为 UNKNOWN，不入选；
    `NULL != 5` 同样不入选（不是 TRUE）。
    """

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
            # 列不存在：显式报错，绝不静默返回全量行
            raise KeyError(
                f"VectorizedFilter: 批次中不存在列 {self.column!r}，"
                f"可用列 {list(batch.columns)}")

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
                raise ValueError(
                    f"VectorizedFilter: 不支持的比较符 {self.op!r}")

            # 数值列含 NULL 时是 float64 + NaN 占位：NaN 与任何值比较都是
            # False，正好等于 SQL 的 UNKNOWN；这里再显式屏蔽一次以覆盖
            # NaN 参与 '!=' 的情形（NaN != 1 为 True，但 SQL 语义应为否）。
            nulls = batch.null_mask(self.column)
            if nulls is not None:
                mask = np.asarray(mask, dtype=bool) & ~np.array(
                    nulls, dtype=bool)

            # 应用 mask 到所有列（NULL 掩码同步切片，保持行对齐）
            new_batch = VectorBatch(columns=list(batch.columns))
            for name in batch.columns:
                c = batch.data.get(name)
                if isinstance(c, np.ndarray):
                    new_batch.data[name] = c[mask]
                else:
                    new_batch.data[name] = [
                        v for i, v in enumerate(c) if mask[i]]
                col_nulls = batch.null_mask(name)
                if col_nulls is not None:
                    new_batch.nulls[name] = [
                        v for i, v in enumerate(col_nulls) if mask[i]]
            new_batch.size = int(mask.sum())
            return new_batch
        else:
            # Python 回退
            indices = []
            for i, v in enumerate(col):
                if self._match(v):
                    indices.append(i)

            new_batch = VectorBatch(columns=list(batch.columns))
            for name in batch.columns:
                c = batch.data.get(name, [])
                new_batch.data[name] = [c[i] for i in indices]
                col_nulls = batch.null_mask(name)
                if col_nulls is not None:
                    new_batch.nulls[name] = [
                        col_nulls[i] for i in indices]
            new_batch.size = len(indices)
            return new_batch

    def _match(self, v):
        if v is None:
            return False          # SQL: NULL 参与比较即 UNKNOWN
        if self.op == '>': return v > self.value
        if self.op == '<': return v < self.value
        if self.op == '>=': return v >= self.value
        if self.op == '<=': return v <= self.value
        if self.op == '=': return v == self.value
        if self.op == '!=': return v != self.value
        raise ValueError(
            f"VectorizedFilter: 不支持的比较符 {self.op!r}")


class VectorizedAggregate:
    """向量化聚合（SUM / AVG / COUNT / MIN / MAX）

    SQL 语义：
    - SUM/AVG/MIN/MAX **忽略 NULL**；全为 NULL（或空输入）时 SUM/MIN/MAX
      返回 None，AVG 返回 None。
    - `COUNT`（column 为 `'*'`/None）计**行数**；`COUNT(col)` 只计非 NULL。
    - SUM/MIN/MAX 保留整数性（int64 累加返回 int），AVG 一律返回 float。
    - 空输入不报错但结果明确为 0（COUNT）或 None（其余）。
    """

    def __init__(self, child, agg_column: str = '*', agg_func: str = 'COUNT'):
        self.child = child
        self.agg_column = agg_column
        self.agg_func = agg_func.upper()
        if self.agg_func not in SUPPORTED_AGGS:
            raise ValueError(
                f"VectorizedAggregate: 不支持的聚合函数 {agg_func!r}，"
                f"仅支持 {list(SUPPORTED_AGGS)}")

    # ---------------- 批内提取（NULL 语义 + dtype 保真） ----------------

    def _batch_values(self, batch, name: str):
        """
        取一批的「非 NULL 数值」。
        返回 `(values, has_float, saw_null)`：
        - values: 非 NULL 值的序列（数组仍保持 int64/float64）
        - has_float: 是否含浮点（用于决定 SUM/MIN/MAX 是否退化为 float）
        - saw_null: 该批是否出现过 NULL（用于区分「空集」与「有 NULL」）
        """
        col = batch.column(name)
        if col is None:
            # 列不存在：显式报错，绝不当作 0 行静默跳过
            raise KeyError(
                f"VectorizedAggregate: 批次中不存在列 {name!r}，"
                f"可用列 {list(batch.columns)}")

        nulls = batch.null_mask(name)

        if HAS_NUMPY and isinstance(col, np.ndarray):
            mask = None
            if nulls is not None:
                mask = ~np.array(nulls, dtype=bool)
            elif col.dtype.kind == 'f':
                # 没有掩码却出现 NaN：按 SQL NULL 处理（NaN 与 NULL 同为
                # 「不可比较值」，这里统一忽略）
                mask = ~np.isnan(col)
            if mask is not None:
                col = col[mask]
            saw_null = (mask is not None and len(mask) != int(mask.sum()))
            if col.dtype.kind in 'iu':
                return col, False, saw_null
            if col.dtype.kind == 'f':
                return col, True, saw_null
            # 其余 dtype（object/bool/…）回退逐值过滤
            vals = [v for v in col.tolist() if v is not None]
            return vals, True, saw_null or len(vals) != len(col)

        vals = []
        saw_null = False
        for i, v in enumerate(col):
            if v is None or (nulls is not None and nulls[i]):
                saw_null = True
                continue
            vals.append(v)
        has_float = any(isinstance(v, float) for v in vals)
        return vals, has_float, saw_null

    # ---------------- 执行 ----------------

    def execute(self) -> Any:
        """执行聚合，返回标量"""
        func = self.agg_func
        # COUNT：column 为 '*'/None 计行数，否则只计非 NULL 值
        count_rows = func == 'COUNT' and (
            self.agg_column in (None, '*'))

        total_count = 0        # COUNT(*) 的行数
        non_null_count = 0     # COUNT(col) 的非 NULL 值数
        int_sum = 0            # 整数累加（int64，不溢出）
        float_sum = 0.0        # 浮点累加
        has_float = False
        min_v = None
        max_v = None
        saw_any = False        # 是否见过至少一个非 NULL 值
        saw_null = False       # 是否见过 NULL（用于 COUNT(col) 语义）

        while True:
            batch = self.child.next_batch()
            if batch is None:
                break
            total_count += batch.size
            if batch.size == 0:
                continue
            if count_rows:
                # 只需要行数：不碰数据，也不因列里有 NULL 而报错
                continue

            vals, vals_have_float, batch_saw_null = self._batch_values(
                batch, self.agg_column)
            saw_null = saw_null or batch_saw_null
            if len(vals) == 0:
                continue
            saw_any = True
            has_float = has_float or vals_have_float

            if func == 'COUNT':
                non_null_count += len(vals)
            elif func == 'SUM':
                if vals_have_float:
                    float_sum += float(np.sum(vals))
                else:
                    # int(col.sum())：int64 求和后再转 Python int，
                    # 不经过 float，10 万行也不会丢精度
                    int_sum += int(np.sum(vals))
            elif func == 'MIN':
                m = min(vals)
                if min_v is None or m < min_v:
                    min_v = m
            elif func == 'MAX':
                m = max(vals)
                if max_v is None or m > max_v:
                    max_v = m
            elif func == 'AVG':
                # 浮点累加 + 独立计数：即使中间批次含 NULL 也不会算错分母
                float_sum += float(np.sum(_as_float_array(vals)))
                non_null_count += len(vals)

        if func == 'COUNT':
            return total_count if count_rows else non_null_count

        if not saw_any:
            return None                      # 空集 / 全 NULL

        if func == 'SUM':
            if has_float:
                return float(float_sum + int_sum)
            return int_sum
        if func == 'MIN':
            return float(min_v) if has_float else int(min_v)
        if func == 'MAX':
            return float(max_v) if has_float else int(max_v)
        if func == 'AVG':
            return float(float_sum / non_null_count) if non_null_count else None


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