"""
向量化执行引擎（正确性止血版，2026-10 OLAP 接入第 1 步）

- 一次处理一批（1024/4096）
- NumPy 加速；无 NumPy 时走纯 Python 回退
- 本模块**不在 SQL 执行路径上**（`Executor` / `plan_executor` 零引用），
  当前只承诺「算对或显式报错」，不承诺接线。

三条硬规则（从阶段 0 起）：
1. **不溢出**：整数列一律 int64；超过 int64 的值退回 Python 大整数列表，
   求和用 Python int，不做 float 中转。
2. **NULL 按 SQL 语义**：SUM/AVG/MIN/MAX 忽略 NULL；COUNT(*) 计行，
   COUNT(col) 只计非 NULL；全 NULL / 空集时 SUM/AVG/MIN/MAX 返回 None。
3. **不静默**：不支持的聚合函数、不存在的列、非法比较符、无法表示的
   值一律抛异常，绝不返回 None / 全量行 / 部分结果冒充成功。

本批次（2026-10-20）修复的正确性缺陷：
- `VectorizedGroupBy`：NULL 掩码被忽略（含 NULL 的数值列以 float64+NaN
  占位，SUM 直接变 NaN）；AVG 没有实现分支，结果 `[]` 在收尾处
  `v[0]` 抛 IndexError；COUNT(*) 无分支恒为 0；不支持的函数静默产出
  `[]`。现在统一走累加器，NULL/AVG/COUNT(*) 全部按 SQL 语义。
- `VectorizedFilter`：不支持 `IS NULL` / `IS NOT NULL`；批次 `columns`
  为空（例如上游只填了 `data`）时无法按列名过滤；NaN 参与 `!=` 时
  得到 True（SQL 应为 UNKNOWN）。现在把 NULL 掩码参与所有比较运算。
- `VectorizedScan`：投影列不存在时静默返回 0 行（treat as empty store）；
  现在显式 KeyError，并校验各列长度一致。
- `VectorizedSort`：排序列不存在时静默返回空批次；NULL 排序位置未定义。
  现在显式报错，且 ASC 时 NULL 在前、DESC 时 NULL 在后（MySQL 口径）。
- `VectorizedProject`：缺列静默跳过（投影少列但 size 不变）；现在显式报错。
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

#: 4A.3 向量化并行分片的固定 chunk 行数。
#: 分片边界**与线程数无关**（1 线程与 N 线程用同一套 chunk 划分、按 chunk
#: 顺序合并部分结果），因此单/多线程结果逐值一致，不会出现浮点求和顺序
#: 漂移。chunk 取 64K 行：numpy 求和能摊薄调度开销，1M 行可切 16 片。
PARALLEL_CHUNK_ROWS = 65536

#: `workers=0`（自动）时的并行档位。4A.3 实测 1M 行聚合的 1→4 线程加速比
#: 不足 1.2x（numpy 单线程求和已很快，线程池调度/分片开销吃掉收益），
#: 因此自动档取 **1（串行）**；显式 `workers=2/4` 可强制并行。
AUTO_PARALLEL_WORKERS = 1

# 支持的聚合函数（不支持的一律显式报错，见 VectorizedAggregate.execute）
SUPPORTED_AGGS = ('SUM', 'AVG', 'COUNT', 'MIN', 'MAX')

#: 聚合函数对应的列类型约束（COUNT 可作用于任意列/`*`）
_NUMERIC_ONLY_AGGS = ('SUM', 'AVG')


def resolve_workers(workers=None) -> int:
    """解析并行度：None/0 → 自动档（`AUTO_PARALLEL_WORKERS`）；>0 显式指定"""
    if workers is None:
        return AUTO_PARALLEL_WORKERS
    try:
        w = int(workers)
    except (TypeError, ValueError):
        return AUTO_PARALLEL_WORKERS
    if w <= 0:
        return AUTO_PARALLEL_WORKERS
    return min(w, 8)


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

    def column_names(self) -> List[str]:
        """批次实际可用的列名（`columns` 为空时回退到 data 的键）"""
        if self.columns:
            return list(self.columns)
        return list(self.data.keys())

    def __len__(self):
        return self.size


def _to_numpy(values: List[Any]):
    """
    把一批 Python 值转成 NumPy 数组，**保证整数不溢出**。

    返回 `(array, nulls)`：
    - `array`：np.ndarray（数值列）或原 list（非数值/含 NULL 的混合列）
    - `nulls`：等长 bool 列表，True = NULL

    实现注意（性能）：先让 NumPy **自己**决定 dtype（C 层扫描），再按
    dtype.kind 分支；不要对每个值做 `isinstance`/`all()` 扫描 —— 那是
    1M 行聚合从 20ms 退化成 1s 的元凶（本机实测 1M 行 SUM 960ms）。
    """
    nulls = [v is None for v in values]

    if not HAS_NUMPY or not values:
        return values, nulls

    try:
        arr = np.array(values)
    except (TypeError, ValueError, OverflowError):
        # 超出 int64 / 无法转数组（含大整数）：保留 Python 列表
        return values, nulls

    kind = arr.dtype.kind
    if kind in 'iu':
        try:
            return arr.astype(np.int64, copy=False), nulls
        except (OverflowError, ValueError):
            return values, nulls
    if kind == 'f':
        return arr.astype(np.float64, copy=False), nulls
    if kind in ('b', 'U', 'S'):
        # bool / 字符串：保持原 Python 对象（聚合/过滤按 Python 语义处理）
        return values, nulls

    # object dtype：含 NULL 的整数列，或混合了不可转的值
    has_none = any(nulls)
    if not has_none:
        return values, nulls
    non_null = [v for v in values if v is not None]
    if non_null and all(
            isinstance(v, (int, float)) and not isinstance(v, bool)
            for v in non_null):
        all_int = all(isinstance(v, int) for v in non_null)
        if all_int and any(abs(v) > 2 ** 53 for v in non_null):
            # 大整数 + NULL：float64 会丢精度，退回 Python 列表
            return values, nulls
        return np.array(
            [float('nan') if v is None else float(v) for v in values],
            dtype=np.float64), nulls

    return values, nulls


def _is_null_at(batch: VectorBatch, name: str, i: int) -> bool:
    """批次第 i 行第 name 列是否 SQL NULL（掩码优先，其次 NaN / None）"""
    nulls = batch.null_mask(name)
    if nulls is not None and i < len(nulls):
        if nulls[i]:
            return True
    col = batch.data.get(name)
    if col is None:
        return True
    try:
        v = col[i]
    except (IndexError, KeyError):
        return True
    if v is None:
        return True
    if HAS_NUMPY and isinstance(v, np.floating) and np.isnan(v):
        return True
    return False


def _cell(batch: VectorBatch, name: str, i: int):
    """取第 i 行第 name 列的 Python 值（NULL → None，NaN → None）

    ★ 必须把 NumPy 标量转成 Python 标量：Windows 上 `np.int64` **不是**
    `int` 的子类，`isinstance(v, int)` 为 False，聚合器会把它当成
    「非数值列」而误报；`np.float64` 同理（虽然通常是 float 子类）。
    """
    if _is_null_at(batch, name, i):
        return None
    v = batch.data[name][i]
    if HAS_NUMPY and isinstance(v, np.generic):
        v = v.item()
    return v


def _as_float_array(arr) -> "np.ndarray":
    """把一批数组/列表统一成 float64（AVG 用）。"""
    if HAS_NUMPY and isinstance(arr, np.ndarray):
        return arr.astype(np.float64, copy=False)
    return np.array([v for v in arr], dtype=np.float64)


def _sum_list(vals):
    """Python 列表求和：整数用 int（不丢精度），有浮点则 float"""
    if all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
        return sum(vals)
    return float(sum(float(v) for v in vals))


class VectorizedScan:
    """向量化列扫描"""

    def __init__(self, column_store, projection: List[str] = None,
                 batch_size: int = VECTOR_SIZE):
        self.store = column_store
        self.projection = list(projection) if projection else list(
            column_store.col_names)
        self.batch_size = max(1, int(batch_size))

        # 投影列必须真实存在：静默返回空列会被上层当成「0 行」
        available = set(column_store.col_names)
        unknown = [c for c in self.projection if c not in available]
        if unknown:
            raise KeyError(
                f"VectorizedScan: 列不存在 {unknown!r}，"
                f"可用列 {column_store.col_names}")

        self.data = self.store.scan(self.projection)
        # 各列长度必须一致（列存损坏/映射错误时显式失败）
        lengths = {len(v) for v in self.data.values()}
        if len(lengths) > 1:
            raise ValueError(
                f"VectorizedScan: 各列长度不一致 {lengths}，"
                f"列存储可能损坏")
        self.total = lengths.pop() if lengths else 0
        self.pos = 0

    def next_batch(self) -> Optional[VectorBatch]:
        if self.pos >= self.total:
            return None

        end = min(self.pos + self.batch_size, self.total)
        batch = VectorBatch(columns=list(self.projection))

        for col in self.projection:
            values = self.data[col][self.pos:end]
            # 统一走 _to_numpy，并记录 NULL 掩码
            arr, nulls = _to_numpy(values)
            batch.data[col] = arr
            if any(nulls):
                batch.nulls[col] = nulls

        batch.size = end - self.pos
        self.pos = end
        return batch


class VectorizedFilter:
    """向量化过滤

    NULL 语义与 SQL 一致：`NULL > 5` / `NULL = 5` / `NULL != 5` 均为
    UNKNOWN，不入选；`IS NULL` / `IS NOT NULL` 按掩码精确判定。
    """

    _CMP_OPS = ('>', '<', '>=', '<=', '=', '!=', '<>')
    _NULL_OPS = ('IS NULL', 'IS NOT NULL')

    def __init__(self, child, column: str, op: str, value=None):
        self.child = child
        self.column = column
        self.op = op
        self.value = value
        known = self._CMP_OPS + self._NULL_OPS
        if op not in known:
            # 显式报错：不认识的比较符若静默返回 False/True 会伪造过滤结果
            raise ValueError(
                f"VectorizedFilter: 不支持的比较符 {op!r}，仅支持 {list(known)}")

    def next_batch(self) -> Optional[VectorBatch]:
        batch = self.child.next_batch()
        if batch is None:
            return None

        names = batch.column_names()
        if self.column not in batch.data:
            raise KeyError(
                f"VectorizedFilter: 批次中不存在列 {self.column!r}，"
                f"可用列 {names}")

        if HAS_NUMPY and isinstance(batch.data[self.column], np.ndarray):
            mask = self._numpy_mask(batch)
        else:
            mask = self._python_mask(batch)

        # 应用 mask 到所有列（NULL 掩码同步切片，保持行对齐）
        new_batch = VectorBatch(columns=list(names))
        for name in names:
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
        new_batch.size = int(sum(1 for x in mask if x))
        return new_batch

    # ---------------- 掩码计算 ----------------

    def _null_vector(self, batch):
        """返回与批次等长的 NULL bool 列表（掩码优先，其次 NaN/None）"""
        n = batch.size
        col = batch.data[self.column]
        mask = batch.null_mask(self.column)
        if mask is not None:
            return [bool(x) for x in mask]
        return [_is_null_at(batch, self.column, i) for i in range(n)]

    def _numpy_mask(self, batch):
        import numpy as _np
        col = batch.data[self.column]
        nulls = self._null_vector(batch)
        null_arr = _np.array(nulls, dtype=bool)

        if self.op == 'IS NULL':
            return null_arr
        if self.op == 'IS NOT NULL':
            return ~null_arr

        if self.value is None:
            # 与 NULL 的比较恒为 UNKNOWN（WHERE 不入选）
            mask = _np.zeros(len(col), dtype=bool)
        elif self.op in ('=',):
            mask = _np.asarray(col == self.value, dtype=bool)
        elif self.op in ('!=', '<>'):
            mask = _np.asarray(col != self.value, dtype=bool)
        elif self.op == '>':
            mask = _np.asarray(col > self.value, dtype=bool)
        elif self.op == '<':
            mask = _np.asarray(col < self.value, dtype=bool)
        elif self.op == '>=':
            mask = _np.asarray(col >= self.value, dtype=bool)
        else:
            mask = _np.asarray(col <= self.value, dtype=bool)

        # NULL（NaN 占位）参与任何比较都是 UNKNOWN → 不入选；
        # 这一步同时修掉 `NaN != 1` 为 True 的语义错误。
        return mask & ~null_arr

    def _python_mask(self, batch):
        nulls = self._null_vector(batch)
        col = batch.data[self.column]
        out = []
        for i, v in enumerate(col):
            is_null = nulls[i] if i < len(nulls) else (v is None)
            if self.op == 'IS NULL':
                out.append(is_null)
                continue
            if self.op == 'IS NOT NULL':
                out.append(not is_null)
                continue
            if is_null or self.value is None:
                out.append(False)
                continue
            out.append(self._match(v))
        return out

    def _match(self, v):
        try:
            if self.op == '=': return v == self.value
            if self.op in ('!=', '<>'): return v != self.value
            if self.op == '>': return v > self.value
            if self.op == '<': return v < self.value
            if self.op == '>=': return v >= self.value
            if self.op == '<=': return v <= self.value
        except TypeError as e:
            raise ValueError(
                f"VectorizedFilter: 列 {self.column!r} 的值 {v!r} 与 "
                f"{self.value!r} 无法比较: {e}") from e
        raise ValueError(
            f"VectorizedFilter: 不支持的比较符 {self.op!r}")


class VectorizedAggregate:
    """向量化聚合（SUM / AVG / COUNT / MIN / MAX）

    SQL 语义：
    - SUM/AVG/MIN/MAX **忽略 NULL**；全为 NULL（或空输入）时返回 None。
    - `COUNT`（column 为 `'*'`/None）计**行数**；`COUNT(col)` 只计非 NULL。
    - SUM/MIN/MAX 保留整数性（int64/Python int 累加返回 int），AVG 返回 float。
    - 空输入不报错但结果明确为 0（COUNT）或 None（其余）。
    - 非数值列上的 SUM/AVG 显式 ValueError（MIN/MAX 允许字符串）。
    """

    def __init__(self, child, agg_column: str = '*', agg_func: str = 'COUNT'):
        self.child = child
        self.agg_column = agg_column
        self.agg_func = str(agg_func).upper()
        if self.agg_func not in SUPPORTED_AGGS:
            raise ValueError(
                f"VectorizedAggregate: 不支持的聚合函数 {agg_func!r}，"
                f"仅支持 {list(SUPPORTED_AGGS)}")
        if self.agg_func in _NUMERIC_ONLY_AGGS and agg_column in (None, '*'):
            raise ValueError(
                f"VectorizedAggregate: {self.agg_func} 需要一个列名，"
                f"不接受 {agg_column!r}")

    def _batch_values(self, batch, name: str):
        """
        取一批的非 NULL 值，返回 `(values, kind, saw_null)`：
        kind ∈ {'int','float','mixed','str','empty'}
        values 为 list（str）或 np.ndarray（数值，走向量化求和）。
        """
        col = batch.data.get(name)
        if col is None:
            raise KeyError(
                f"VectorizedAggregate: 批次中不存在列 {name!r}，"
                f"可用列 {batch.column_names()}")
        nulls = batch.null_mask(name)
        saw_null = False

        if HAS_NUMPY and isinstance(col, np.ndarray):
            if nulls is not None:
                mask = ~np.array(nulls, dtype=bool)
                saw_null = bool(len(mask) != int(mask.sum()))
                col = col[mask]
            elif col.dtype.kind == 'f':
                nan_mask = np.isnan(col)
                saw_null = bool(nan_mask.any())
                col = col[~nan_mask]
            if col.dtype.kind in 'iu':
                return col, 'int', saw_null
            if col.dtype.kind == 'f':
                return col, 'float', saw_null
            vals = [v for v in col.tolist() if v is not None]
            if not vals:
                return [], 'empty', saw_null
            return vals, 'str', saw_null

        vals = []
        for i, v in enumerate(col):
            if v is None or (nulls is not None and i < len(nulls)
                             and nulls[i]):
                saw_null = True
                continue
            vals.append(v)

        if not vals:
            return [], 'empty', saw_null
        if all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
            return vals, 'int', saw_null
        if all(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in vals):
            return vals, 'float', saw_null
        if any(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in vals):
            return vals, 'mixed', saw_null
        return vals, 'str', saw_null

    @staticmethod
    def _values_sum(vals):
        """数值序列求和：ndarray 走 NumPy；list 用 Python（整数不丢精度）"""
        if HAS_NUMPY and isinstance(vals, np.ndarray):
            return vals.sum()
        if all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
            return sum(vals)
        return sum(float(v) for v in vals)

    def execute(self) -> Any:
        func = self.agg_func
        count_rows = func == 'COUNT' and self.agg_column in (None, '*')

        total_rows = 0
        non_null_count = 0
        int_sum = 0
        float_sum = 0.0
        sum_is_float = False
        min_v = None
        max_v = None
        saw_any = False

        while True:
            batch = self.child.next_batch()
            if batch is None:
                break
            total_rows += batch.size
            if batch.size == 0:
                continue
            if count_rows:
                continue

            vals, kind, _saw_null = self._batch_values(
                batch, self.agg_column)
            n_vals = len(vals)
            if n_vals == 0:
                continue
            saw_any = True

            if func == 'COUNT':
                non_null_count += n_vals
            elif func == 'SUM':
                if kind == 'int':
                    int_sum += int(self._values_sum(vals))
                elif kind in ('float', 'mixed'):
                    sum_is_float = True
                    float_sum += float(self._values_sum(vals))
                else:
                    raise ValueError(
                        f"VectorizedAggregate: SUM 不支持非数值列 "
                        f"{self.agg_column!r}（值示例 {vals[:3]!r}）")
            elif func == 'AVG':
                if kind == 'int':
                    int_sum += int(self._values_sum(vals))
                elif kind in ('float', 'mixed'):
                    sum_is_float = True
                    float_sum += float(self._values_sum(vals))
                else:
                    raise ValueError(
                        f"VectorizedAggregate: AVG 不支持非数值列 "
                        f"{self.agg_column!r}（值示例 {vals[:3]!r}）")
                non_null_count += n_vals
            elif func == 'MIN':
                try:
                    m = vals.min() if (HAS_NUMPY and isinstance(
                        vals, np.ndarray)) else min(vals)
                except TypeError as e:
                    raise ValueError(
                        f"VectorizedAggregate: MIN 列 {self.agg_column!r} "
                        f"存在无法比较的值: {e}") from e
                if min_v is None or m < min_v:
                    min_v = m
            elif func == 'MAX':
                try:
                    m = vals.max() if (HAS_NUMPY and isinstance(
                        vals, np.ndarray)) else max(vals)
                except TypeError as e:
                    raise ValueError(
                        f"VectorizedAggregate: MAX 列 {self.agg_column!r} "
                        f"存在无法比较的值: {e}") from e
                if max_v is None or m > max_v:
                    max_v = m

        if func == 'COUNT':
            return total_rows if count_rows else non_null_count

        if not saw_any:
            return None                      # 空集 / 全 NULL

        if func == 'SUM':
            if sum_is_float:
                return float(float_sum + int_sum)
            return int(int_sum)
        if func == 'MIN':
            return min_v
        if func == 'MAX':
            return max_v
        if func == 'AVG':
            if non_null_count == 0:
                return None
            return float((float_sum + int_sum) / non_null_count)
        raise ValueError(f"不支持的聚合函数 {func!r}")   # pragma: no cover


class VectorizedGroupBy:
    """向量化分组聚合（支持 SUM/AVG/COUNT/COUNT(*)/MIN/MAX）

    4A.3：`workers > 1` 时按 `PARALLEL_CHUNK_ROWS` 行分片、线程池求局部
    分组、再按 chunk 顺序合并。分片边界与线程数无关，合并顺序固定，因此
    单/多线程结果逐值一致（整数求和精确；浮点求和在非二进制精确值上可能
    有末位差异，属浮点非结合性，对拍矩阵使用二进制精确值）。
    """

    def __init__(self, child, group_cols: List[str],
                 aggs: List[Dict], workers=None):
        """
        aggs: [{'func': 'SUM', 'column': 'amount', 'alias': 'total'}]
        workers: None/0 → 自动档；>0 显式线程数
        """
        self.child = child
        self.group_cols = list(group_cols or [])
        self.aggs = list(aggs or [])
        self.workers = resolve_workers(workers)
        for a in self.aggs:
            func = str(a.get('func') or '').upper()
            if func not in SUPPORTED_AGGS:
                raise ValueError(
                    f"VectorizedGroupBy: 不支持的聚合函数 {a.get('func')!r}，"
                    f"仅支持 {list(SUPPORTED_AGGS)}")
            col = a.get('column')
            if func in _NUMERIC_ONLY_AGGS and col in (None, '*'):
                raise ValueError(
                    f"VectorizedGroupBy: {func} 需要一个列名，"
                    f"不接受 {col!r}")

    def execute(self) -> List[List]:
        if self.workers > 1:
            return self._execute_parallel()
        groups: Dict[tuple, Dict[str, Any]] = {}

        while True:
            batch = self.child.next_batch()
            if batch is None:
                break
            n = batch.size
            if n == 0:
                continue

            # 聚合列必须在本批次里（COUNT(*) 除外）；列映射错误显式报错，
            # 不再静默跳过（旧实现 `continue` 会让该聚合恒为 0/None）。
            for a in self.aggs:
                col = a.get('column')
                if col in (None, '*'):
                    continue
                if col not in batch.data:
                    raise KeyError(
                        f"VectorizedGroupBy: 批次中不存在聚合列 {col!r}，"
                        f"可用列 {batch.column_names()}")

            for i in range(n):
                key = tuple(_cell(batch, g, i) for g in self.group_cols)
                state = groups.get(key)
                if state is None:
                    state = {}
                    for a in self.aggs:
                        state[a['alias']] = self._init_state(a)
                    groups[key] = state

                for a in self.aggs:
                    self._accumulate(state[a['alias']], a, batch, i)

        result = []
        for key, state in groups.items():
            row = list(key)
            for a in self.aggs:
                row.append(self._finalize(a, state[a['alias']]))
            result.append(row)
        return result

    # ---------------- 4A.3 分片并行 ----------------

    def _execute_parallel(self) -> List[List]:
        data, total = _materialize_child_data(self.child)
        for g in self.group_cols:
            if g not in data:
                raise KeyError(
                    f"VectorizedGroupBy: 分组列不存在 {g!r}，"
                    f"可用列 {list(data)}")
        for a in self.aggs:
            col = a.get('column')
            if col in (None, '*'):
                continue
            if col not in data:
                raise KeyError(
                    f"VectorizedGroupBy: 聚合列不存在 {col!r}，"
                    f"可用列 {list(data)}")

        chunks = [(i, min(i + PARALLEL_CHUNK_ROWS, total))
                  for i in range(0, total, PARALLEL_CHUNK_ROWS)]

        def work(rng):
            start, end = rng
            local: Dict[tuple, Dict[str, Any]] = {}
            for i in range(start, end):
                key = tuple(data[g][i] for g in self.group_cols)
                state = local.get(key)
                if state is None:
                    state = {}
                    for a in self.aggs:
                        state[a['alias']] = self._init_state(a)
                    local[key] = state
                for a in self.aggs:
                    col = a.get('column')
                    v = None if col in (None, '*') else data[col][i]
                    self._accumulate_value(state[a['alias']], a, v)
            return local

        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            partials = list(pool.map(work, chunks)) if chunks else []
        finally:
            pool.shutdown(wait=True)

        groups: Dict[tuple, Dict[str, Any]] = {}
        order = []
        for local in partials:                 # 按 chunk 顺序合并 → 顺序确定
            for key, states in local.items():
                if key not in groups:
                    groups[key] = states
                    order.append(key)
                else:
                    for a in self.aggs:
                        self._merge_state(groups[key][a['alias']],
                                          states[a['alias']], a)

        result = []
        for key in order:
            state = groups[key]
            row = list(key)
            for a in self.aggs:
                row.append(self._finalize(a, state[a['alias']]))
            result.append(row)
        return result

    @staticmethod
    def _accumulate_value(state, a, v):
        """按单个值累加（串行/并行共用，保证语义一致）"""
        func = str(a['func']).upper()
        if func == 'COUNT' and a.get('column') in (None, '*'):
            state['count'] += 1
            return
        if v is None:
            return                      # SQL 聚合忽略 NULL
        if func == 'COUNT':
            state['count'] += 1
        elif func == 'SUM' or func == 'AVG':
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(
                    f"VectorizedGroupBy: {func} 不支持非数值列 "
                    f"{a.get('column')!r}（值示例 {v!r}）")
            state['sum'] += v
            if func == 'AVG':
                state['count'] += 1
            else:
                state['has'] = True
        elif func == 'MIN':
            if (not state['has']) or v < state['v']:
                state['v'] = v
            state['has'] = True
        elif func == 'MAX':
            if (not state['has']) or v > state['v']:
                state['v'] = v
            state['has'] = True

    @staticmethod
    def _merge_state(acc, other, a):
        """合并两个分组累加器（并行分片用）"""
        func = str(a['func']).upper()
        if func == 'COUNT':
            acc['count'] += other['count']
        elif func == 'SUM':
            if other['has']:
                if not acc['has']:
                    acc['sum'] = 0
                acc['sum'] += other['sum']
                acc['has'] = True
        elif func == 'AVG':
            acc['sum'] += other['sum']
            acc['count'] += other['count']
        elif func == 'MIN':
            if other['has'] and ((not acc['has']) or other['v'] < acc['v']):
                acc['v'] = other['v']
                acc['has'] = True
        elif func == 'MAX':
            if other['has'] and ((not acc['has']) or other['v'] > acc['v']):
                acc['v'] = other['v']
                acc['has'] = True

    @staticmethod
    def _init_state(a) -> Dict[str, Any]:
        func = str(a['func']).upper()
        if func == 'COUNT':
            return {'count': 0}
        if func == 'AVG':
            return {'sum': 0, 'count': 0}
        if func == 'SUM':
            return {'sum': 0, 'has': False}
        if func == 'MIN':
            return {'v': None, 'has': False}
        if func == 'MAX':
            return {'v': None, 'has': False}
        raise ValueError(f"不支持的聚合函数 {func!r}")   # pragma: no cover

    @staticmethod
    def _accumulate(state, a, batch, i):
        func = str(a['func']).upper()
        col = a.get('column')
        if func == 'COUNT' and col in (None, '*'):
            state['count'] += 1
            return
        v = _cell(batch, col, i)
        if v is None:
            return                      # SQL 聚合忽略 NULL
        if func == 'COUNT':
            state['count'] += 1
        elif func == 'SUM' or func == 'AVG':
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(
                    f"VectorizedGroupBy: {func} 不支持非数值列 {col!r}"
                    f"（值示例 {v!r}）")
            state['sum'] += v
            if func == 'AVG':
                state['count'] += 1
            else:
                state['has'] = True
        elif func == 'MIN':
            if (not state['has']) or v < state['v']:
                state['v'] = v
            state['has'] = True
        elif func == 'MAX':
            if (not state['has']) or v > state['v']:
                state['v'] = v
            state['has'] = True

    @staticmethod
    def _finalize(a, state):
        func = str(a['func']).upper()
        if func == 'COUNT':
            return state['count']
        if func == 'SUM':
            return state['sum'] if state['has'] else None
        if func == 'AVG':
            return (state['sum'] / state['count']) if state['count'] else None
        if func in ('MIN', 'MAX'):
            return state['v'] if state['has'] else None
        raise ValueError(f"不支持的聚合函数 {func!r}")   # pragma: no cover


class VectorizedSort:
    """向量化排序（全物化）

    NULL 排序按 MySQL 口径：ASC 时 NULL 在最前，DESC 时 NULL 在最后。
    """

    def __init__(self, child, column: str, desc: bool = False):
        self.child = child
        self.column = column
        self.desc = bool(desc)

    def execute(self) -> VectorBatch:
        all_data: Dict[str, List] = {}
        total = 0

        while True:
            batch = self.child.next_batch()
            if batch is None:
                break
            total += batch.size
            names = batch.column_names()
            for name in names:
                col = batch.data.get(name, [])
                if isinstance(col, np.ndarray) and col.dtype.kind == 'f':
                    vals = col.tolist()
                elif isinstance(col, np.ndarray):
                    vals = col.tolist()
                else:
                    vals = list(col)
                all_data.setdefault(name, []).extend(vals)

        if self.column not in all_data:
            raise KeyError(
                f"VectorizedSort: 排序列不存在 {self.column!r}，"
                f"可用列 {list(all_data)}")

        sort_col = all_data[self.column]
        # NaN 占位还原为 None（ASC 时排最前）
        sort_vals = [None if (v is None or (
            HAS_NUMPY and isinstance(v, float) and np.isnan(v))) else v
            for v in sort_col]

        non_null = [i for i, v in enumerate(sort_vals) if v is not None]
        null_idx = [i for i, v in enumerate(sort_vals) if v is None]
        try:
            non_null.sort(key=lambda i: sort_vals[i], reverse=self.desc)
        except TypeError as e:
            raise ValueError(
                f"VectorizedSort: 排序列 {self.column!r} 存在无法比较的值: "
                f"{e}") from e
        indices = (null_idx + non_null) if not self.desc else (non_null + null_idx)

        result = VectorBatch(columns=list(all_data.keys()))
        for name in all_data:
            result.data[name] = [all_data[name][i] for i in indices]
            nulls = [v is None or (HAS_NUMPY and isinstance(v, float)
                                   and np.isnan(v))
                     for v in result.data[name]]
            if any(nulls):
                result.nulls[name] = nulls
        result.size = total
        return result


class VectorizedProject:
    """列投影（缺列显式报错；NULL 掩码随投影传递）"""

    def __init__(self, child, columns: List[str]):
        self.child = child
        self.columns = list(columns)

    def next_batch(self):
        batch = self.child.next_batch()
        if batch is None:
            return None
        names = batch.column_names()
        missing = [c for c in self.columns if c not in batch.data]
        if missing:
            raise KeyError(
                f"VectorizedProject: 批次中不存在列 {missing!r}，"
                f"可用列 {names}")
        new_batch = VectorBatch(columns=list(self.columns))
        for name in self.columns:
            new_batch.data[name] = batch.data[name]
            col_nulls = batch.null_mask(name)
            if col_nulls is not None:
                new_batch.nulls[name] = list(col_nulls)
        new_batch.size = batch.size
        return new_batch


# ==================== 4A.3：分片并行聚合 ====================

def _materialize_child_data(child):
    """把可流式 child（VectorizedScan/Filter/Project）物化成 `{列: [值]}`

    返回 `(data, row_count)`。数值列的 NaN 占位还原为 None（SQL NULL），
    保证后续纯 Python 分片算法与 SQL 语义一致。
    """
    data: Dict[str, List] = {}

    # VectorizedScan 已经把整列读进 data：直接借用（避免二次批次切片）
    if hasattr(child, 'data') and isinstance(getattr(child, 'data'), dict):
        n = int(getattr(child, 'total', 0))
        for name, col in child.data.items():
            vals = col.tolist() if (HAS_NUMPY and isinstance(col, np.ndarray)) \
                else list(col)
            data[name] = [
                None if (v is None or (HAS_NUMPY and isinstance(v, float)
                                       and np.isnan(v))) else v
                for v in vals]
        return data, n

    total = 0
    while True:
        batch = child.next_batch()
        if batch is None:
            break
        total += batch.size
        for name in batch.column_names():
            col = batch.data.get(name, [])
            if HAS_NUMPY and isinstance(col, np.ndarray):
                vals = col.tolist()
            else:
                vals = list(col)
            nulls = batch.null_mask(name)
            if nulls is not None:
                vals = [None if nulls[i] else v for i, v in enumerate(vals)]
            data.setdefault(name, []).extend(vals)
    return data, total


def _chunk_partial(values: List, func: str) -> Dict[str, Any]:
    """计算一个 chunk 的部分聚合（与线程数无关的确定性算法）"""
    import math

    arr, nulls = _to_numpy(values)
    rows = len(values)
    has_null = any(nulls)

    if HAS_NUMPY and isinstance(arr, np.ndarray):
        if arr.dtype.kind in 'iu':
            valid = arr[~np.array(nulls, dtype=bool)] if has_null else arr
            kind = 'int'
        elif arr.dtype.kind == 'f':
            if has_null:
                valid = arr[~np.array(nulls, dtype=bool)]
            else:
                nanmask = np.isnan(arr)
                if nanmask.any():
                    has_null = True
                    valid = arr[~nanmask]
                else:
                    valid = arr
            kind = 'float'
        else:
            valid = [v for i, v in enumerate(arr.tolist())
                     if not (v is None or (has_null and nulls[i]))]
            kind = 'str'
    else:
        valid = [v for i, v in enumerate(arr)
                 if not (v is None or (has_null and nulls[i]))]
        kind = ('int' if valid and all(isinstance(v, int)
                                       and not isinstance(v, bool)
                                       for v in valid)
                else 'float' if valid and all(
                    isinstance(v, (int, float)) and not isinstance(v, bool)
                    for v in valid) else 'str')

    count = len(valid)
    part = {'rows': rows, 'count': count, 'kind': kind,
            'sum': None, 'min': None, 'max': None}
    if func == 'COUNT' or count == 0:
        return part
    if func in _NUMERIC_ONLY_AGGS and kind == 'str':
        raise ValueError(f"聚合函数 {func} 不支持非数值列")
    if func in ('SUM', 'AVG'):
        if kind == 'int':
            part['sum'] = int(valid.sum()) if isinstance(valid, np.ndarray) \
                else sum(valid)
        else:
            part['sum'] = float(math.fsum(
                valid.tolist() if isinstance(valid, np.ndarray) else valid))
    elif func == 'MIN':
        part['min'] = (valid.min().item() if isinstance(valid, np.ndarray)
                       else min(valid))
        if isinstance(part['min'], np.generic):
            part['min'] = part['min'].item()
    elif func == 'MAX':
        part['max'] = (valid.max().item() if isinstance(valid, np.ndarray)
                       else max(valid))
        if isinstance(part['max'], np.generic):
            part['max'] = part['max'].item()
    return part


def _merge_chunk_partials(partials: List[Dict], func: str):
    """按 chunk 顺序合并部分聚合（int 求和保持精确；float 用 fsum 固定顺序）"""
    import math

    if func == 'COUNT':
        return sum(p['count'] for p in partials)
    rows = sum(p['rows'] for p in partials)
    count = sum(p['count'] for p in partials)
    if count == 0:
        return None
    kinds = [p['kind'] for p in partials if p['count']]
    kind = 'int' if kinds and all(k == 'int' for k in kinds) else 'float'
    if func == 'SUM':
        sums = [p['sum'] for p in partials if p['count']]
        if kind == 'int':
            return sum(int(s) for s in sums)
        return float(math.fsum(float(s) for s in sums))
    if func == 'AVG':
        sums = [p['sum'] for p in partials if p['count']]
        total = (sum(int(s) for s in sums) if kind == 'int'
                 else float(math.fsum(float(s) for s in sums)))
        return float(total / count)
    if func == 'MIN':
        vals = [p['min'] for p in partials if p['count']]
        return min(vals)
    if func == 'MAX':
        vals = [p['max'] for p in partials if p['count']]
        return max(vals)
    raise ValueError(f"不支持的聚合函数 {func!r}")


class VectorizedAggregateSet:
    """一次扫描计算**多个**聚合（4A.3：按 chunk 分片 + 线程池）

    - `specs`：[{'func': 'SUM', 'column': 'v', 'alias': 's'}, ...]
    - `workers`：None/0 → 自动档（`AUTO_PARALLEL_WORKERS`，当前 1=串行）；
      >0 显式线程数。分片边界固定（`PARALLEL_CHUNK_ROWS`），按 chunk 顺序
      合并部分结果 → **单/多线程结果逐值一致**。
    - 与 `VectorizedAggregate` 语义一致：COUNT(*) 计行、COUNT(col) 计非
      NULL、SUM/AVG/MIN/MAX 忽略 NULL、空集 COUNT=0 其余 None。
    """

    def __init__(self, child, specs: List[Dict], workers=None):
        self.child = child
        self.specs = list(specs or [])
        self.workers = resolve_workers(workers)
        for spec in self.specs:
            func = str(spec.get('func') or '').upper()
            if func not in SUPPORTED_AGGS:
                raise ValueError(
                    f"VectorizedAggregateSet: 不支持的聚合函数 "
                    f"{spec.get('func')!r}，仅支持 {list(SUPPORTED_AGGS)}")
            if func in _NUMERIC_ONLY_AGGS and spec.get('column') in (None, '*'):
                raise ValueError(
                    f"VectorizedAggregateSet: {func} 需要一个列名，"
                    f"不接受 {spec.get('column')!r}")

    def execute(self) -> List[Any]:
        data, total = _materialize_child_data(self.child)
        workers = self.workers
        if workers <= 1:
            return [self._compute_one(spec, data, total, None)
                    for spec in self.specs]

        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            return [self._compute_one(spec, data, total, pool)
                    for spec in self.specs]
        finally:
            # 不等待线程池里可能残留的 task（本方法返回前 map 已全部完成）
            pool.shutdown(wait=True)

    def _compute_one(self, spec, data, total, pool):
        func = str(spec['func']).upper()
        column = spec.get('column')
        if func == 'COUNT' and column in (None, '*'):
            return total
        if column not in data:
            raise KeyError(
                f"VectorizedAggregateSet: 列不存在 {column!r}，"
                f"可用列 {list(data)}")
        values = data[column]
        chunks = [values[i:i + PARALLEL_CHUNK_ROWS]
                  for i in range(0, len(values), PARALLEL_CHUNK_ROWS)]
        if not chunks:
            chunks = [[]]

        def work(chunk):
            return _chunk_partial(chunk, func)

        if pool is not None:
            partials = list(pool.map(work, chunks))
        else:
            partials = [work(c) for c in chunks]
        return _merge_chunk_partials(partials, func)
