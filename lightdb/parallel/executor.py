"""
并行执行器（正确性止血版，2026-10 OLAP 接入第 1 步）

- 分片级并行：扫描 / 聚合 / 分组聚合
- 结果合并
- 线程池 + 自适应并行度

接线状态（阶段 2 第 6 项，方案 A「摘牌 + 显式化」，保持**不变**）：

* 本模块**不引用 `lightdb.olap` / `lightdb.columnar` 的实现**（只引用
  `lightdb.olap.status` 的状态常量）。并行分片接口（`row_count` +
  `scan_range`）由调用方提供的只读列式视图满足：SQL 路径用的是
  `lightdb/parallel/table_view.py::TableColumnView`。
* OLAP / 列存在 SQL 执行路径上依旧零引用，也不计入任何性能声明；
  实测对照见 `benchmarks/README.md`。

本批次（正确性止血）修复的缺陷：
1. `parallel_aggregate` 的 `where` 参数以前**完全被忽略**（串行与分片
   都没用），过滤条件静默消失、聚合值偏大。现在按 SQL 三值语义逐行
   过滤；`_serial_aggregate` 同样实现。
2. `COUNT(*)`：`agg_column` 为 `'*'`/None 时以前走 `scan_range(['*'])`
   拿到空列表 → COUNT 恒 0（并行）/ None（串行）。现在按分片行数计数
   （`COUNT(col)` 仍只计非 NULL）。
3. 分片失败以前只打日志、丢掉该分片继续合并 → 聚合**少算**、分组
   **少行**，调用方看到的是"看起来合法"的数字。现在：
   - 聚合/扫描：任一分片失败 → 显式 RuntimeError（不返回部分结果）；
   - 分组：任一分片失败 → 回退**全量串行**计算（结果正确优先于并行度）。
4. 分组 AVG 以前没有实现（fragment/merge 都缺），AVG 列恒 None；现在
   fragment 保存 `{'sum','count'}`，由 `ResultMerger.merge_group_bys`
   合并出真实平均值（全 NULL 组为 None）。
5. 不支持的聚合函数以前静默返回 None / 只给 count；现在显式 ValueError。
"""
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor, as_completed)
from typing import Any, Callable, Dict, List, Optional

from ..logger import logger
from ..olap.status import (
    ensure_not_requested_via_sql, WIRED_TO_SQL_PATH as _OLAP_WIRED_TO_SQL)
from .fragment import (
    Fragment, FragmentResult, FragmentSplitter)
from .adaptive import AdaptiveParallelism
from .work_stealing import WorkStealingScheduler
from .merge import ResultMerger

#: 本执行器支持的聚合函数（不支持 → 显式 ValueError）
SUPPORTED_AGGS = ('SUM', 'AVG', 'COUNT', 'MIN', 'MAX')


def _required_columns(group_cols, aggs) -> List[str]:
    """分片真正需要读的列

    `COUNT(*)` 的 `column` 是 `'*'`（**不是**列名）。原实现直接把它塞进
    `scan_range` 的列清单，于是取回一个空的 `data['*']`，随后
    `data[col][i]` 抛 `list index out of range`；异常又被吞掉，partials
    为空，最终返回 0 组。这里把 `'*'` 与空列名剔掉。
    """
    cols = []
    for c in list(group_cols or []) + [a.get('column') for a in aggs or []]:
        if not c or c == '*':
            continue
        if c not in cols:
            cols.append(c)
    return cols


# ==================== 谓词求值（与 storage/olap 一致的 SQL 三值语义）====================

def _flatten_and(where: Optional[Dict]) -> List[Dict]:
    """把 AND 条件树拍平；OR 显式 NotImplementedError（不静默丢条件）"""
    if not where:
        return []
    if 'or' in where:
        raise NotImplementedError(
            "ParallelExecutor: 暂不支持 OR 谓词（为避免静默丢条件，这里"
            "显式报错；请在调用方拆分条件）")
    if 'and' in where:
        conds = []
        for c in (where.get('and') or []):
            conds.extend(_flatten_and(c))
        return conds
    if 'column' in where:
        return [where]
    raise NotImplementedError(
        f"ParallelExecutor: 无法理解的谓词形态 {where!r}（不静默忽略）")


def _where_columns(where: Optional[Dict]) -> List[str]:
    cols = []
    for c in _flatten_and(where):
        col = c.get('column')
        if col and col not in cols:
            cols.append(col)
    return cols


def _match_cell(v, op: str, val) -> bool:
    """单值谓词求值（NULL → 普通比较 UNKNOWN=False）"""
    op = str(op or '=').upper()
    if v is None:
        return op == 'IS NULL'
    if op == 'IS NULL':
        return False
    if op == 'IS NOT NULL':
        return True
    if val is None:
        return False
    try:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                val = type(v)(val)
            except (ValueError, TypeError):
                pass
        if op == '=':
            return v == val
        if op in ('!=', '<>'):
            return not (v == val)
        if op == '>':
            return v > val
        if op == '<':
            return v < val
        if op == '>=':
            return v >= val
        if op == '<=':
            return v <= val
        if op == 'LIKE':
            import re
            pat = str(val).replace('%', '.*').replace('_', '.')
            return bool(re.match('^' + pat + '$', str(v)))
    except (TypeError, ValueError):
        return False
    raise ValueError(f"ParallelExecutor: 不支持的比较符 {op!r}")


def _filter_data(data: Dict[str, List],
                 where: Optional[Dict]) -> Dict[str, List]:
    """对 `{列: [值...]}` 应用 WHERE（AND 链），返回过滤后的同构字典"""
    conds = _flatten_and(where)
    if not conds:
        return data
    cols = list(data.keys())
    n = len(data[cols[0]]) if cols else 0
    keep = [True] * n
    for i in range(n):
        for c in conds:
            col = c.get('column')
            if col not in data:
                raise KeyError(
                    f"ParallelExecutor: WHERE 列 {col!r} 不在扫描列 "
                    f"{cols} 中")
            if not _match_cell(data[col][i], c.get('op', '='),
                               c.get('value')):
                keep[i] = False
                break
    return {c: [data[c][i] for i in range(n) if keep[i]] for c in cols}


def _first_column(column_store) -> str:
    names = getattr(column_store, 'col_names', None) or []
    if not names:
        raise ValueError("ParallelExecutor: 列存储没有任何列，无法计数")
    return names[0]


class ParallelExecutor:
    """
    并行执行器

    `WIRED_TO_SQL_PATH`：本并行执行器接的是**调用方给的只读列式视图**，
    与实验性的 `lightdb/olap/**` / `lightdb/columnar/**` 没有实现依赖
    （见模块 docstring），因此 OLAP/列存层未接入 SQL 路径这一状态不受
    本模块影响。
    """

    #: OLAP / 列存层是否接入 SQL 路径（阶段 2 第 6 项摘牌依据，恒为 False）
    WIRED_TO_SQL_PATH = False

    def __init__(self, max_workers: int = None):
        self.max_workers = max_workers or 8
        self.adaptive = AdaptiveParallelism(max_workers)

        # 线程池
        self.pool = ThreadPoolExecutor(max_workers=self.max_workers)

        # 工作窃取调度器
        self.ws = WorkStealingScheduler(self.max_workers)

        # 统计
        self.stats = {
            'total_queries': 0,
            'parallel_queries': 0,
            'serial_queries': 0,
            'fragments_executed': 0,
            'total_elapsed_ms': 0,
        }
        self.lock = threading.RLock()

    def shutdown(self):
        self.pool.shutdown(wait=False)
        self.ws.stop()

    # ==================== 并行扫描 ====================

    def parallel_scan(self, column_store,
                       columns: List[str] = None,
                       where: Dict = None,
                       workers: int = None) -> Dict[str, List]:
        """并行列扫描（支持 where 过滤；失败分片显式报错）"""
        total_rows = column_store.row_count
        if workers is None:
            workers = self.adaptive.decide(
                total_rows, is_expensive=False)

        out_cols = (list(columns) if columns
                    else list(getattr(column_store, 'col_names', [])))
        needed = list(out_cols)
        for c in _where_columns(where):
            if c not in needed:
                needed.append(c)
        # 过滤/计数需要至少一列；纯 COUNT(*) 由 aggregate 处理
        if not needed:
            needed = [_first_column(column_store)]

        with self.lock:
            self.stats['total_queries'] += 1

        if workers <= 1 or total_rows <= 0:
            with self.lock:
                self.stats['serial_queries'] += 1
            data = column_store.scan(needed)
            data = _filter_data(data, where)
            return {c: data.get(c, []) for c in out_cols}

        with self.lock:
            self.stats['parallel_queries'] += 1

        # 1. 生成分片
        fragments = FragmentSplitter.split_by_range(total_rows, workers)

        # 2. 并行执行
        t0 = time.perf_counter()
        results = []
        futures = []

        for frag in fragments:
            future = self.pool.submit(
                self._scan_fragment, column_store,
                needed, where, frag)
            futures.append((future, frag))

        for future, frag in futures:
            try:
                result = future.result(timeout=60)
                results.append(result)
            except Exception as e:
                frag.status = 'failed'
                frag.error = str(e)

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 3. 合并（失败分片 → 显式 RuntimeError）
        merged = ResultMerger.merge_scan_results(results, needed)

        with self.lock:
            self.stats['fragments_executed'] += len(fragments)
            self.stats['total_elapsed_ms'] += elapsed_ms

        return {c: merged.get(c, []) for c in out_cols}

    def _scan_fragment(self, column_store, columns,
                        where, frag: Fragment) -> FragmentResult:
        """扫描一个分片（带 where 过滤）"""
        frag.started_at = time.time()
        result = FragmentResult(frag.fragment_id)

        try:
            data = column_store.scan_range(
                columns, frag.offset, frag.offset + frag.limit)
            data = _filter_data(data, where)
            result.data = data
            first = next(iter(data.values()), [])
            result.row_count = len(first)
            frag.status = 'done'
            frag.rows_processed = frag.limit
        except Exception as e:
            result.error = str(e)
            frag.status = 'failed'

        frag.finished_at = time.time()
        result.elapsed_ms = frag.elapsed_ms()
        return result

    # ==================== 并行聚合 ====================

    def parallel_aggregate(self, column_store,
                            agg_column: str,
                            agg_func: str,
                            where: Dict = None,
                            workers: int = None) -> Any:
        """并行聚合（COUNT(*)/COUNT(col)/SUM/AVG/MIN/MAX，支持 where）"""
        func = str(agg_func or '').upper()
        if func not in SUPPORTED_AGGS:
            raise ValueError(
                f"ParallelExecutor.parallel_aggregate: 不支持的聚合函数 "
                f"{agg_func!r}，仅支持 {list(SUPPORTED_AGGS)}")
        if agg_column not in (None, '*') and func in ('SUM', 'AVG'):
            pass  # 列名合法性由 scan_range 负责（不存在 → KeyError）
        if agg_column in (None, '*') and func != 'COUNT':
            raise ValueError(
                f"ParallelExecutor.parallel_aggregate: {func} 需要具体列名，"
                f"不接受 {agg_column!r}")
        if agg_column not in (None, '*'):
            available = getattr(column_store, 'col_names', None) or []
            if agg_column not in available:
                # 列不存在的错误要在入口显式给出（而不是被分片异常包装成
                # RuntimeError —— 两种都显式，但 KeyError 更准确）
                raise KeyError(
                    f"ParallelExecutor.parallel_aggregate: 列不存在 "
                    f"{agg_column!r}，可用列 {available}")

        total_rows = column_store.row_count
        if workers is None:
            workers = self.adaptive.decide(
                total_rows, is_expensive=True)

        with self.lock:
            self.stats['total_queries'] += 1

        if workers <= 1 or total_rows <= 0:
            with self.lock:
                self.stats['serial_queries'] += 1
            return self._serial_aggregate(
                column_store, agg_column, func, where)

        with self.lock:
            self.stats['parallel_queries'] += 1

        # 1. 分片
        fragments = FragmentSplitter.split_by_range(
            total_rows, workers)

        # 2. 并行局部聚合
        t0 = time.perf_counter()
        futures = []
        for frag in fragments:
            future = self.pool.submit(
                self._agg_fragment, column_store,
                agg_column, func, where, frag)
            futures.append(future)

        partials = []
        errors = []
        for future in as_completed(futures):
            try:
                r = future.result()
                if getattr(r, 'error', ''):
                    errors.append(r)
                else:
                    partials.append(r)
            except Exception as e:
                errors.append(e)

        if errors:
            # 少一个分片 = 聚合少算：显式失败，绝不返回部分结果
            detail = "; ".join(
                f"{getattr(e, 'fragment_id', '?')}: "
                f"{getattr(e, 'error', e)}" for e in errors)
            raise RuntimeError(
                f"并行聚合存在失败分片，结果不完整（{len(errors)}/"
                f"{len(fragments)}）: {detail}")

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 3. 合并
        merged = ResultMerger.merge_aggregations(partials, func)

        with self.lock:
            self.stats['fragments_executed'] += len(fragments)
            self.stats['total_elapsed_ms'] += elapsed_ms

        return merged

    def _agg_fragment(self, column_store, agg_column,
                        agg_func, where, frag):
        """单分片聚合"""
        frag.started_at = time.time()
        result = FragmentResult(frag.fragment_id)

        try:
            star = agg_column in (None, '*')
            needed = _where_columns(where)
            if not star:
                needed = list(dict.fromkeys(needed + [agg_column]))
            if not needed:
                needed = [_first_column(column_store)]

            data = column_store.scan_range(
                needed, frag.offset, frag.offset + frag.limit)
            data = _filter_data(data, where)
            row_count = len(next(iter(data.values()), []))

            if star:
                partial = {'count': row_count}
            else:
                values = [v for v in data.get(agg_column, [])
                          if v is not None]
                if agg_func == 'COUNT':
                    partial = {'count': len(values)}
                elif agg_func == 'SUM':
                    partial = {'count': len(values), 'sum': sum(values)}
                elif agg_func == 'MIN':
                    partial = {'count': len(values),
                               'min': min(values) if values else None}
                elif agg_func == 'MAX':
                    partial = {'count': len(values),
                               'max': max(values) if values else None}
                elif agg_func == 'AVG':
                    partial = {'count': len(values),
                               'sum': sum(values) if values else 0}
                else:                       # pragma: no cover - 入口已校验
                    raise ValueError(
                        f"不支持的聚合函数 {agg_func!r}")

            result.data = partial
            result.row_count = row_count
            frag.status = 'done'
            frag.rows_processed = frag.limit
        except Exception as e:
            result.error = str(e)
            frag.status = 'failed'

        frag.finished_at = time.time()
        result.elapsed_ms = frag.elapsed_ms()
        return result

    def _serial_aggregate(self, column_store, agg_column,
                            agg_func, where):
        """串行回退（与分片聚合语义完全一致）"""
        star = agg_column in (None, '*')
        needed = _where_columns(where)
        if not star:
            needed = list(dict.fromkeys(needed + [agg_column]))
        if not needed:
            needed = [_first_column(column_store)]

        data = column_store.scan(needed)
        data = _filter_data(data, where)
        row_count = len(next(iter(data.values()), []))

        if star:
            return row_count

        values = [v for v in data.get(agg_column, []) if v is not None]
        if agg_func == 'COUNT':
            return len(values)
        if not values:
            return None
        if agg_func == 'SUM':
            return sum(values)
        if agg_func == 'MIN':
            return min(values)
        if agg_func == 'MAX':
            return max(values)
        if agg_func == 'AVG':
            return sum(values) / len(values)
        raise ValueError(f"不支持的聚合函数 {agg_func!r}")

    # ==================== 并行分组聚合 ====================

    def parallel_group_by(self, column_store,
                            group_cols: List[str],
                            aggs: List[Dict],
                            workers: int = None) -> List[List]:
        """
        并行分组聚合（两阶段）
        1. 局部 hash 分组
        2. 全局 merge
        """
        for a in aggs or []:
            if str(a.get('func', '')).upper() not in SUPPORTED_AGGS:
                raise ValueError(
                    f"ParallelExecutor.parallel_group_by: 不支持的聚合函数 "
                    f"{a.get('func')!r}，仅支持 {list(SUPPORTED_AGGS)}")

        total_rows = column_store.row_count
        if workers is None:
            workers = self.adaptive.decide(
                total_rows, is_expensive=True)

        with self.lock:
            self.stats['total_queries'] += 1

        if workers <= 1 or total_rows <= 0:
            with self.lock:
                self.stats['serial_queries'] += 1
            return self._serial_group_by(
                column_store, group_cols, aggs)

        with self.lock:
            self.stats['parallel_queries'] += 1

        # 1. 分片
        fragments = FragmentSplitter.split_by_range(
            total_rows, workers)

        # 2. 并行局部聚合
        t0 = time.perf_counter()
        futures = []
        for frag in fragments:
            future = self.pool.submit(
                self._group_by_fragment, column_store,
                group_cols, aggs, frag)
            futures.append(future)

        partials = []
        for future in as_completed(futures):
            try:
                partials.append(future.result())
            except Exception as e:
                logger.warn(f"并行分组分片失败: {e}")

        bad = [r for r in partials if getattr(r, 'error', '')]
        for r in bad:
            logger.warn(f"并行分组分片 {r.fragment_id} 失败: {r.error}")
        good = [r for r in partials if not getattr(r, 'error', '')]
        if bad:
            # 部分失败会静默少行 → 回退全量串行，结果正确优先于并行度
            logger.warn(
                f"并行分组有 {len(bad)} 个分片失败，回退串行执行以保证结果完整")
            with self.lock:
                self.stats['serial_queries'] += 1
            return self._serial_group_by(column_store, group_cols, aggs)
        if fragments and not good:
            with self.lock:
                self.stats['serial_queries'] += 1
            return self._serial_group_by(column_store, group_cols, aggs)

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 3. 合并
        merged = ResultMerger.merge_group_bys(
            good, group_cols, aggs)

        with self.lock:
            self.stats['fragments_executed'] += len(fragments)
            self.stats['total_elapsed_ms'] += elapsed_ms

        return merged

    def _group_by_fragment(self, column_store, group_cols,
                             aggs, frag):
        """单分片分组"""
        frag.started_at = time.time()
        result = FragmentResult(frag.fragment_id)

        try:
            # 读取需要的列（`COUNT(*)` 的 '*' 不是列名，必须剔除）
            needed = _required_columns(group_cols, aggs)
            if not needed:
                needed = [_first_column(column_store)]
            data = column_store.scan_range(
                needed, frag.offset, frag.offset + frag.limit)
            n = len(next(iter(data.values()), []))

            groups = {}

            for i in range(n):
                # 分组键
                key = tuple(
                    data[col][i] if col in data else None
                    for col in group_cols
                )

                if key not in groups:
                    init = {}
                    for a in aggs:
                        func = str(a['func']).upper()
                        if func == 'COUNT':
                            init[a['alias']] = 0
                        elif func == 'AVG':
                            init[a['alias']] = {'sum': 0, 'count': 0}
                        else:
                            init[a['alias']] = None
                    groups[key] = init

                # 累加聚合
                for a in aggs:
                    col = a.get('column')
                    func = str(a['func']).upper()
                    alias = a['alias']

                    if func == 'COUNT' and (col is None or col == '*'):
                        groups[key][alias] += 1
                        continue
                    if col is None or col not in data:
                        continue
                    v = data[col][i]
                    if v is None:
                        continue

                    if func == 'COUNT':
                        groups[key][alias] += 1
                    elif func == 'SUM':
                        cur = groups[key][alias]
                        groups[key][alias] = (
                            v if cur is None else cur + v)
                    elif func == 'MIN':
                        cur = groups[key][alias]
                        groups[key][alias] = (
                            v if cur is None else min(cur, v))
                    elif func == 'MAX':
                        cur = groups[key][alias]
                        groups[key][alias] = (
                            v if cur is None else max(cur, v))
                    elif func == 'AVG':
                        cur = groups[key][alias]
                        cur['sum'] += v
                        cur['count'] += 1
                    else:               # pragma: no cover - 入口已校验
                        raise ValueError(f"不支持的聚合函数 {func!r}")

            result.data = groups
            result.row_count = len(groups)
            frag.status = 'done'
        except Exception as e:
            result.error = str(e)
            frag.status = 'failed'

        frag.finished_at = time.time()
        result.elapsed_ms = frag.elapsed_ms()
        return result

    def _serial_group_by(self, column_store, group_cols, aggs):
        """串行分组聚合（**不依赖 olap / 列存实现**）

        改前（阶段 2 第 6 项之前）：串行回退悄悄调用实验性
        `lightdb/olap/vectorized_engine`（静默改道），且 `COUNT(*)` 的
        `'*'` 不在扫描列里导致计数错误。现在是与并行分片同语义的逐行
        实现：NULL 聚合值忽略、COUNT(*) 计行、AVG = sum/count、返回
        `[分组键..., 聚合值...]`。

        最后走一次状态检查点 `ensure_not_requested_via_sql`：如果哪天
        有人把 OLAP/列存真接进 SQL 路径，这里会**显式报错**并指向
        `benchmarks/README.md`，而不是让这条回退路径悄悄改变语义。
        """
        ensure_not_requested_via_sql('ParallelExecutor._serial_group_by')
        assert _OLAP_WIRED_TO_SQL is False, (
            'lightdb/olap/status.py 声称 OLAP 已接入 SQL 路径，但 '
            'lightdb/parallel/executor.py 已不引用 olap/列存实现；'
            '请同步两者（口径见 benchmarks/README.md）')

        for a in aggs or []:
            if str(a.get('func', '')).upper() not in SUPPORTED_AGGS:
                raise ValueError(
                    f"ParallelExecutor._serial_group_by: 不支持的聚合函数 "
                    f"{a.get('func')!r}，仅支持 {list(SUPPORTED_AGGS)}")

        needed = _required_columns(group_cols, aggs)
        if not needed:
            needed = [_first_column(column_store)]
        data = column_store.scan(needed)
        total = len(next(iter(data.values()), []))

        groups = {}
        for i in range(total):
            key = tuple(
                data[col][i] if col in data else None
                for col in group_cols
            )
            if key not in groups:
                init = {}
                for a in aggs:
                    func = str(a['func']).upper()
                    if func == 'COUNT':
                        init[a['alias']] = 0
                    elif func == 'AVG':
                        init[a['alias']] = {'sum': 0, 'count': 0}
                    else:
                        init[a['alias']] = None
                groups[key] = init

            for a in aggs:
                col = a.get('column')
                func = str(a['func']).upper()
                alias = a['alias']
                if func == 'COUNT' and (col is None or col == '*'):
                    groups[key][alias] += 1
                    continue
                if col is None or col not in data or i >= len(data[col]):
                    continue
                v = data[col][i]
                if v is None:
                    continue
                if func == 'COUNT':
                    groups[key][alias] += 1
                elif func == 'SUM':
                    cur = groups[key][alias]
                    groups[key][alias] = v if cur is None else cur + v
                elif func == 'MIN':
                    cur = groups[key][alias]
                    groups[key][alias] = v if cur is None else min(cur, v)
                elif func == 'MAX':
                    cur = groups[key][alias]
                    groups[key][alias] = v if cur is None else max(cur, v)
                elif func == 'AVG':
                    cur = groups[key][alias]
                    cur['sum'] += v
                    cur['count'] += 1
                else:                   # pragma: no cover - 入口已校验
                    raise ValueError(f"不支持的聚合函数 {func!r}")

        result = []
        for key, vals in groups.items():
            row = list(key)
            for a in aggs:
                func = str(a['func']).upper()
                v = vals[a['alias']]
                if func == 'AVG':
                    v = (v['sum'] / v['count']) if v['count'] else None
                row.append(v)
            result.append(row)
        return result

    # ==================== 统计 ====================

    def stats_report(self) -> dict:
        with self.lock:
            return {
                **self.stats,
                'adaptive': self.adaptive.stats_report(),
                'avg_elapsed_ms': (
                    self.stats['total_elapsed_ms'] /
                    max(1, self.stats['total_queries'])),
            }
