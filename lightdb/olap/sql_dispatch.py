# lightdb/olap/sql_dispatch.py
"""OLAP / 列存 → SQL 执行路径的**显式分派**（决策 2 第 2 步，4A.2）

设计原则（与项目「不静默」铁律一致）：

1. **默认开启（可关闭）**：`config.olap_dispatch` 默认 True（2026-10-20
   用户要求）；`LIGHTDB_OLAP_DISPATCH=0` 关闭时 SQL 执行器根本不 import
   本模块（`Executor._cmd_select` 里按开关延迟 import），行为与接线前
   逐字节一致（行存）。
2. **白名单分派**：只处理单表 + 聚合（COUNT/SUM/AVG/MIN/MAX）+ 简单
   WHERE（列 op 字面量，AND 组合）+ GROUP BY（1-2 列）+ 简单 ORDER BY /
   LIMIT / OFFSET 的查询；JOIN、子查询、OR、HAVING、窗口、CTE、DISTINCT、
   表达式投影等一律返回 `None`，由执行器走原行存路径。
3. **结果逐字节对齐行存**：分派只替换「聚合计算」这一步，最终列名 /
   别名 / 顺序 / ORDER BY / LIMIT / 空结果语义复用行存既有实现
   （`Executor._project_rows` / `_finish_select` / 存储层 `Table.select`
   的列名规则），因此开关前后 `{'type':'resultset','columns','rows'}`
   完全一致。
4. **可见、可回退、可对拍**：
   - 每次派发/回退都计数（`dispatch_stats()`）并打日志；
   - 分派内部异常 → 显式 `logger.warn` 后返回 None 走行存（不返回错值）；
   - `config.olap_crosscheck=True` 时同时跑行存参考实现并逐行比对，
     不一致抛 `RuntimeError`（显式失败，不静默选一侧）。

本模块顶层**不 import** `lightdb.olap.*` 引擎模块（延迟 import），保证
关闭时 SQL 路径不加载列存/向量化代码。
"""
import re
import threading
from typing import Any, Dict, List, Optional

from ..logger import logger

# ==================== 分派计数（可见性） ====================

_STATS_LOCK = threading.Lock()
_STATS = {
    'attempted': 0,          # try_vectorized_select 被调用次数
    'dispatched': 0,         # 真正走向量化执行的次数
    'fallback': 0,           # 白名单不命中 / 行数不足 → 回退行存
    'fallback_error': 0,     # 分派内部异常 → 回退行存
    'crosscheck_ok': 0,      # 对拍一致次数
    'crosscheck_failed': 0,  # 对拍不一致次数（显式报错）
    'last': None,            # 最近一次分派的信息（dict）
}


def dispatch_stats() -> Dict[str, Any]:
    """返回分派计数快照（测试/状态页/EXPLAIN 注记共用同一数据源）"""
    with _STATS_LOCK:
        return dict(_STATS)


def reset_dispatch_stats() -> None:
    with _STATS_LOCK:
        for k in list(_STATS):
            _STATS[k] = None if k == 'last' else 0


def _bump(key: str, last: Optional[Dict] = None) -> None:
    with _STATS_LOCK:
        _STATS[key] = int(_STATS.get(key) or 0) + 1
        if last is not None:
            _STATS['last'] = last


# ==================== 白名单校验 ====================

_AGG_RE = re.compile(
    r'^(COUNT|SUM|AVG|MIN|MAX)\s*\(\s*(\*|[A-Za-z_]\w*)\s*\)$',
    re.IGNORECASE)
_COMPARE_OPS = ('=', '!=', '<>', '<', '<=', '>', '>=')
_AGG_FUNCS = ('COUNT', 'SUM', 'AVG', 'MIN', 'MAX')


def _where_ok(where) -> bool:
    """WHERE 是否属于可安全下推的形态（AND 链 + 列 op 字面量）"""
    if where is None:
        return True
    if not isinstance(where, dict):
        return False
    if 'and' in where:
        conds = where.get('and')
        return isinstance(conds, list) and all(_where_ok(c) for c in conds)
    if 'or' in where or 'not' in where or 'negate' in where:
        return False
    if 'subquery' in where or 'values' in where or 'expr_pred' in where:
        return False
    if 'agg' in where or 'value_column' in where:
        return False
    col = where.get('column')
    op = str(where.get('op') or '').upper()
    if not isinstance(col, str) or not col or '.' in col:
        return False
    if op not in _COMPARE_OPS:
        return False
    if not isinstance(where.get('value'), (int, float, str, bool, type(None))):
        return False
    return True


def _simple_column(name, table_cols) -> Optional[str]:
    """简单列名（无表限定/无括号）→ 返回表里真实列名；否则 None"""
    if not isinstance(name, str):
        return None
    s = name.strip().strip('`"')
    if not s or '.' in s or '(' in s or ')' in s or ' ' in s:
        return None
    if s not in table_cols:
        return None
    return s


def _parse_projection(text: str):
    """解析白名单投影项 → ('col', col) / ('agg', func, col, expr) / None"""
    s = str(text).strip()
    from ..sql.executor import Executor
    expr, _alias = Executor._split_alias(s)
    expr = expr.strip()
    m = _AGG_RE.match(expr)
    if m:
        func = m.group(1).upper()
        arg = m.group(2)
        return {'kind': 'agg', 'func': func,
                'column': '*' if arg == '*' else arg,
                'expr': expr}
    if re.match(r'^[A-Za-z_]\w*$', expr):
        return {'kind': 'col', 'column': expr}
    return None


def _analyze(executor, parsed) -> Optional[Dict]:
    """白名单分析：可派发 → 返回计划 dict；否则 None（调用方走行存）"""
    p = parsed
    if not isinstance(p, dict):
        return None
    if str(p.get('command') or '').upper() != 'SELECT':
        return None
    # 明确不支持的语法特征（出现即回退行存，绝不静默改道）
    for key in ('no_from', 'subquery', 'joins', 'windows', 'having',
                'distinct', 'cte', 'union', 'set_op'):
        if p.get(key):
            return None
    if p.get('limit') is not None and not isinstance(p.get('limit'), int):
        return None
    if p.get('offset') is not None and not isinstance(p.get('offset'), int):
        return None

    table = p.get('table')
    if not table or not isinstance(table, str):
        return None
    if table.upper() == '__BINLOG_FETCH__' or 'information_schema.' in (
            table.lower()):
        return None
    if table not in executor.db.tables:
        return None
    # CTE 物化关系优先于真实表：分派只看真实表
    try:
        if executor._cte_relations.get(table.lower()) is not None:
            return None
    except Exception:
        pass

    tbl = executor.db.get_table(table)
    table_cols = [str(c.get('name')) for c in (getattr(tbl, 'cols', []) or [])
                  if isinstance(c, dict)]

    where = p.get('where')
    if not _where_ok(where):
        return None

    raw_cols = [str(c).strip() for c in (p.get('columns') or [])]
    if not raw_cols or '*' in raw_cols or not raw_cols:
        return None

    group_by_raw = [str(g).strip() for g in (p.get('group_by') or [])]
    if len(group_by_raw) > 2:
        return None
    group_cols = []
    for g in group_by_raw:
        c = _simple_column(g, table_cols)
        if c is None:
            return None
        group_cols.append(c)

    # 投影：分组列（必须是分组键）或白名单聚合
    proj = []            # 与 raw_cols 对齐的分析结果
    agg_specs = []       # [{'func','column','expr'}]
    aliased = False
    for text in raw_cols:
        item = _parse_projection(text)
        if item is None:
            return None
        if item['kind'] == 'col':
            col = _simple_column(item['column'], table_cols)
            if col is None:
                return None
            if group_cols and col not in group_cols:
                return None          # 非分组键裸列：交给行存严格模式报错
            if not group_cols:
                return None          # 无聚合、无 GROUP BY 的裸列查询不派发
            item['column'] = col
        else:
            func, col = item['func'], item['column']
            if func != 'COUNT' and col == '*':
                return None
            if col != '*' and _simple_column(col, table_cols) is None:
                return None
            item['column'] = col
            agg_specs.append({'func': func, 'column': col,
                              'text': text})
        from ..sql.executor import Executor
        if Executor._split_alias(text)[1] is not None:
            aliased = True
        proj.append(item)

    if not agg_specs and not group_cols:
        return None                  # 纯列选择不是 OLAP 查询

    # GROUP BY 时裸列必须是分组键（严格 GROUP BY 已由调用方先校验，这里
    # 再兜一层：不满足就回退行存，让行存的报错信息保持一致）
    if group_cols:
        for item in proj:
            if item['kind'] == 'col' and item['column'] not in group_cols:
                return None

    # ORDER BY：仅 GROUP BY 场景支持；键必须是输出别名/分组列/投影聚合
    # 表达式，或受支持的聚合（作为隐藏排序列，与行存 _group_rows 一致）。
    order_keys = []
    order_aggs = []                  # 隐藏排序列 [{'func','column','text'}]
    if p.get('order_by'):
        if not group_cols:
            return None              # 无 GROUP BY 的排序交回行存（路径/标签差异）
        from ..sql.parser import normalize_order_by, \
            parse_scalar_expr, expr_has_agg
        try:
            norm = normalize_order_by(p.get('order_by'))
        except Exception:
            return None
        if len(norm) > 2:
            return None
        out_names = set()
        for text in raw_cols:
            from ..sql.executor import Executor
            expr, alias = Executor._split_alias(text)
            if alias:
                out_names.add(alias.lower())
            out_names.add(expr.strip().lower())
        for col_c in group_cols:
            out_names.add(col_c.lower())
        for text, desc in norm:
            key = str(text).strip()
            key_l = key.lower()
            if key_l in out_names:
                order_keys.append((key, bool(desc)))
                continue
            node = parse_scalar_expr(key)
            if node is None or not expr_has_agg(node):
                return None
            texts = executor._expr_agg_texts_in_node(node)
            for t in texts:
                t = str(t).strip()
                m = _AGG_RE.match(t)
                if not m:
                    return None
                func = m.group(1).upper()
                arg = m.group(2)
                if func != 'COUNT' and arg == '*':
                    return None
                if arg != '*' and _simple_column(arg, table_cols) is None:
                    return None
                if t not in [x['text'] for x in order_aggs]:
                    order_aggs.append({'func': func, 'column': arg, 'text': t})
            order_keys.append((key, bool(desc)))

    # 行数阈值：小表派发反而慢（且默认 1000 保证小库行为不变）
    min_rows = int(getattr(executor, 'olap_dispatch_min_rows', 1000) or 0)
    try:
        n_rows = int(tbl.row_count)
    except Exception:
        n_rows = 0
    if n_rows < min_rows:
        return None

    # 行存路径判定：有 GROUP BY / 别名 / IS NULL 谓词 → 走 _select_advanced
    # （标签为原始表达式/别名）；否则走存储层 _aggregate（标签全大写）。
    where_has_is_null = False
    try:
        where_has_is_null = bool(executor._where_has_is_null(where))
    except Exception:
        pass
    advanced = bool(group_cols) or aliased or where_has_is_null

    fetch_cols: List[str] = []
    for g in group_cols:
        if g not in fetch_cols:
            fetch_cols.append(g)
    for spec in agg_specs + order_aggs:
        c = spec['column']
        if c != '*' and c not in fetch_cols:
            fetch_cols.append(c)
    if not fetch_cols:
        # 只有 COUNT(*)：取第一列用来确定行数（不读数据值）
        if table_cols:
            fetch_cols.append(table_cols[0])
        else:
            return None

    return {
        'tbl': tbl,
        'table': table,
        'where': where,
        'group_cols': group_cols,
        'group_by_raw': group_by_raw,
        'raw_cols': raw_cols,
        'agg_specs': agg_specs,
        'order_aggs': order_aggs,
        'order_keys': order_keys,
        'fetch_cols': fetch_cols,
        'advanced': advanced,
        'rows_est': n_rows,
    }


# ==================== 内存列数据源（不落盘） ====================

class _ColumnSource:
    """把 SQL 查询读到的行值包装成向量化引擎需要的只读列式数据源

    只实现 `VectorizedScan` 用到的 `col_names` / `scan` / `row_count`；
    数据来自行存的表扫描（同一事务读视图），**不是**持久化列存。
    """

    def __init__(self, cols: List[str], data: Dict[str, List]):
        self.col_names = list(cols)
        self._data = data
        first = next(iter(data.values()), [])
        self.row_count = len(first)

    def scan(self, columns: List[str] = None) -> Dict[str, List]:
        target = list(columns) if columns else list(self.col_names)
        unknown = [c for c in target if c not in self._data]
        if unknown:
            raise KeyError(
                f"_ColumnSource: 列不存在 {unknown!r}，可用列 {self.col_names}")
        return {c: self._data[c] for c in target}

    def scan_range(self, columns: List[str], start: int, end: int):
        data = self.scan(columns)
        return {c: v[int(start):int(end)] for c, v in data.items()}


def _fetch_columns(tbl, cols: List[str], where, read_view):
    """用行存扫描取指定列（与行存同一读视图/过滤语义）"""
    _out_cols, rows = tbl.select(list(cols), where, None, None, read_view)
    data = {c: [] for c in cols}
    for r in rows:
        for i, c in enumerate(cols):
            data[c].append(r[i] if i < len(r) else None)
    return data, len(rows)


def _rowstore_reference(executor, parsed, session):
    """跑一遍行存路径作为对拍参考（通过 `_olap_bypass` 关闭分派）"""
    executor._olap_bypass = True
    try:
        return executor._cmd_select(parsed, session)
    finally:
        executor._olap_bypass = False


# ==================== 主入口 ====================

def try_vectorized_select(executor, parsed, session) -> Optional[Dict]:
    """尝试把一条 SELECT 派发给向量化引擎

    :return: 与行存完全一致的 `{'type':'resultset','columns','rows'}`；
             不适合派发（白名单不命中/行数不足/内部异常）→ `None`，
             调用方继续走行存。
    :raise RuntimeError: 仅当 `olap_crosscheck` 开启且行存/向量化结果
             不一致时（显式失败，绝不静默选一侧）。
    """
    if parsed is None or not executor.olap_dispatch:
        return None
    _bump('attempted')

    try:
        plan = _analyze(executor, parsed)
    except Exception as e:                       # 分析期异常：回退行存
        _bump('fallback_error')
        logger.warn(f"OLAP dispatch 分析失败，回退行存: {e}")
        return None
    if plan is None:
        _bump('fallback')
        return None

    t0 = __import__('time').perf_counter()
    try:
        result = _execute_plan(executor, parsed, session, plan)
    except RuntimeError:
        raise                                  # crosscheck 不一致：显式失败
    except Exception as e:
        _bump('fallback_error')
        logger.warn(
            f"OLAP dispatch 执行失败，显式回退行存"
            f"（table={plan.get('table')!r}）: "
            f"{type(e).__name__}: {e}")
        return None

    elapsed_ms = (__import__('time').perf_counter() - t0) * 1000
    info = {
        'table': plan['table'],
        'mode': 'group_by' if plan['group_cols'] else 'aggregate',
        'rows': plan['rows_est'],
        'aggs': len(plan['agg_specs']),
        'workers': int(getattr(executor, 'olap_parallel_workers', 0) or 0),
        'elapsed_ms': round(elapsed_ms, 3),
        'crosscheck': bool(getattr(executor, 'olap_crosscheck', False)),
    }
    _bump('dispatched', info)
    logger.info(
        f"OLAP dispatch 命中: table={info['table']} mode={info['mode']} "
        f"rows={info['rows']} aggs={info['aggs']} workers={info['workers']} "
        f"({elapsed_ms:.2f}ms) —— 显式分派（config.olap_dispatch=True）")

    # 对拍：同一条 SQL 再跑一次行存，逐列逐行比对；不一致显式报错
    if getattr(executor, 'olap_crosscheck', False):
        ref = _rowstore_reference(executor, parsed, session)
        try:
            _compare_or_raise(ref, result, parsed)
        except RuntimeError:
            _bump('crosscheck_failed')
            raise
        _bump('crosscheck_ok')

    return result


def _compare_or_raise(ref, result, parsed) -> None:
    """行存/向量化结果逐值比对；不一致抛 RuntimeError（显式失败）"""
    if not isinstance(ref, dict):
        raise RuntimeError(
            f"OLAP crosscheck: 行存参考结果非法（{ref!r}）"
            f"（SQL: {str(parsed.get('sql') or parsed)[:200]}）")
    if ref.get('type') != 'resultset':
        raise RuntimeError(
            f"OLAP crosscheck: 行存参考返回非结果集: {ref.get('type')} "
            f"{str(ref.get('message'))[:200]}")
    ref_cols = list(ref.get('columns') or [])
    got_cols = list(result.get('columns') or [])
    if ref_cols != got_cols:
        raise RuntimeError(
            f"OLAP crosscheck: 列名不一致 行存={ref_cols!r} "
            f"向量化={got_cols!r}")
    ref_rows = [list(r) for r in (ref.get('rows') or [])]
    got_rows = [list(r) for r in (result.get('rows') or [])]
    if len(ref_rows) != len(got_rows):
        raise RuntimeError(
            f"OLAP crosscheck: 行数不一致 行存={len(ref_rows)} "
            f"向量化={len(got_rows)} 行存前 5 行={ref_rows[:5]!r} "
            f"向量化前 5 行={got_rows[:5]!r}")
    for i, (a, b) in enumerate(zip(ref_rows, got_rows)):
        if a != b:
            raise RuntimeError(
                f"OLAP crosscheck: 第 {i} 行不一致 行存={a!r} 向量化={b!r}")


def _execute_plan(executor, parsed, session, plan) -> Dict:
    """按计划执行：行存取数（同一读视图）→ 向量化聚合 → 行存投影/排序/限行"""
    tbl = plan['tbl']
    # 读视图：与行存 _cmd_select 完全一致（显式事务读视图 / 新建快照）
    txn = session.get('txn') if isinstance(session, dict) else None
    if txn:
        read_view = txn['read_view']
    else:
        tid = executor.db.mvcc.next_txn_id()
        read_view = executor.db.mvcc.create_readview(tid)
    where = executor._normalize_where(plan['where'])
    workers = int(getattr(executor, 'olap_parallel_workers', 0) or 0)

    # 延迟 import 引擎模块：默认关闭时 SQL 路径不会加载这里
    from .vectorized_engine import (
        VectorizedScan, VectorizedGroupBy, VectorizedAggregateSet)

    data, n_rows = _fetch_columns(tbl, plan['fetch_cols'], where, read_view)
    source = _ColumnSource(plan['fetch_cols'], data)

    if plan['group_cols']:
        # 分组：向量化引擎算 keys+聚合值，列名/顺序与行存 _group_rows 对齐
        aggs = [{'func': s['func'], 'column': s['column'],
                 'alias': s['text']}
                for s in plan['agg_specs']]
        hidden = [{'func': s['func'], 'column': s['column'],
                   'alias': s['text']}
                  for s in plan['order_aggs']]
        scan = VectorizedScan(source, plan['fetch_cols'])
        gb = VectorizedGroupBy(scan, plan['group_cols'], aggs + hidden,
                               workers=workers)
        rows = gb.execute()
        intermediate_cols = (
            list(plan['group_by_raw'])
            + [s['text'] for s in plan['agg_specs']]
            + [s['text'] for s in plan['order_aggs']])
        # 交给行存既有投影/排序/限行实现，保证列名/顺序/LIMIT 语义逐字一致
        return executor._project_rows(
            parsed, intermediate_cols, [list(r) for r in rows],
            session=session, read_view=read_view)

    # 无 GROUP BY：整表聚成一行（AggregateSet 需要流式 child，包一层 Scan）
    specs = [{'func': s['func'], 'column': s['column'], 'alias': s['text']}
             for s in plan['agg_specs']]
    agg_scan = VectorizedScan(source, plan['fetch_cols'])
    values = VectorizedAggregateSet(agg_scan, specs, workers=workers).execute()

    if plan['advanced']:
        # 行存走 _select_advanced（有别名 / IS NULL）：空表输入返回 0 行，
        # 列名走投影路径；直接复用 _project_rows 保持逐字节一致
        intermediate_cols = [s['text'] for s in plan['agg_specs']]
        rows = [list(values)] if n_rows > 0 else []
        return executor._project_rows(
            parsed, intermediate_cols, rows,
            session=session, read_view=read_view)

    # 行存走存储层 _aggregate：列名全部大写；LIMIT/OFFSET 按 _cmd_select
    # 的既有处理顺序复刻（见 tests/test_olap_wired.py 的对拍矩阵）
    out_cols = [str(c).upper().strip() for c in plan['raw_cols']]
    rows = [list(values)]
    limit = parsed.get('limit')
    offset = int(parsed.get('offset') or 0)
    if limit is not None and (int(limit) + offset):
        rows = rows[:int(limit) + offset]
    if offset:
        rows = rows[offset:]
        if limit is not None:
            rows = rows[:int(limit)]
    return {'type': 'resultset', 'columns': list(out_cols),
            'rows': [tuple(r) for r in rows]}
