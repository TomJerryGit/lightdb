"""
基于计划的执行器（火山模型 / 物化列表）

修复历史:
- v4.0.1: 删除重复的 _eval_condition 定义（后者覆盖前者导致 IN/EXISTS 失效）
- v4.0.1: 支持所有 PlanNode 类型（Distinct/GroupBy/Union/SubqueryScan/Window/CTEScan）
- v4.0.1: 缓存 Optimizer / StatsManager，避免子查询反复构造
- v4.0.1: 修正 GroupBy 的 HAVING 求值上下文
- v4.0.1: 修正 AVG/STDDEV 等聚合在空集时的返回
- v4.0.1: IN 常量列表用类型感知比较
- v4.0.1: 未知节点给出明确错误而非静默返回空
"""
import re
from collections import defaultdict

from .plan import (
    SeqScan, IndexScan, Filter, Project, Sort, Limit, Aggregate, Explain,
    HashJoin, NestedLoopJoin, CrossJoin,
    Distinct, GroupBy, Union, SubqueryScan, ValueList,
    Window, CTEScan, CTEDefine, RecursiveCTE,
)


# 常量投影的识别（`SELECT 1 FROM orders`）——用于无法用 None 表示的字面量
_NOT_LITERAL = object()


def _literal_of(text):
    """识别 SQL 字面量；不是字面量时返回 _NOT_LITERAL"""
    t = str(text or '').strip()
    if not t:
        return _NOT_LITERAL
    if re.match(r'^-?\d+$', t):
        return int(t)
    if re.match(r'^-?\d+\.\d+$', t):
        return float(t)
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        return t[1:-1].replace("''", "'")
    if t.upper() == 'NULL':
        return None
    return _NOT_LITERAL


class PlanExecutor:
    """计划执行器

    对外接口:
        execute(node) -> (columns: list[str], rows: list[list])
    """

    def __init__(self, db, read_view=None):
        self.db = db
        self.read_view = read_view
        # ★ 缓存子查询用的优化器
        self._sub_optimizer = None
        # CTE 上下文（WITH 语句物化结果）
        self._cte_cache = {}

    # ==================== 入口 ====================

    def execute(self, node):
        if node is None:
            return [], []

        method = getattr(self, f'_exec_{type(node).__name__}', None)
        if method is None:
            # ★ 明确报错，而不是静默返回空
            raise NotImplementedError(
                f"不支持的节点: {type(node).__name__}")
        return method(node)

    # ==================== 列解析 ====================

    def _resolve_col(self, cols, name) -> int:
        """'t1.id' 或 'id' -> 列索引"""
        if not isinstance(name, str):
            raise ValueError(f"列名必须是字符串: {name!r}")
        name = name.strip().strip('`"')

        if name in cols:
            return cols.index(name)

        # 尝试按 '.' 拆最后一段匹配
        suffix = '.' + name
        matches = [
            i for i, c in enumerate(cols)
            if c == name or c.endswith(suffix)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"列 '{name}' 歧义，请加表名前缀")
        raise ValueError(f"列 '{name}' 不存在")

    # ==================== 条件求值（唯一定义） ====================

    def _eval_condition(self, row, cols, where, agg_values=None):
        """求值 WHERE/HAVING 条件

        ★ 只保留这一份实现（原代码中重复定义两次）

        agg_values: HAVING 语境下预先算好的聚合值 {'COUNT(*)': 3, ...}。
        GROUP BY 之后行里只剩输出列，HAVING 引用的聚合表达式可能不在
        SELECT 列表里，因此在分组时统一算好再传进来。
        """
        if where is None:
            return True
        if not isinstance(where, dict):
            return True

        # NOT (...)：整体取反（三值语义下 UNKNOWN 取反仍是 UNKNOWN）
        if isinstance(where.get('not'), dict):
            inner = self._eval_condition(
                row, cols, where['not'], agg_values)
            if inner is None:
                return None
            return not inner

        if 'and' in where:
            return all(self._eval_condition(row, cols, c, agg_values)
                       for c in where['and'])
        if 'or' in where:
            parts = where['or']
            # 空 OR 列表 = 常量假（执行器改写子查询时用这个表示）
            if not parts:
                return False
            return any(self._eval_condition(row, cols, c, agg_values)
                       for c in parts)

        op = where.get('op')

        # ---------- 聚合谓词（HAVING）----------
        if 'agg' in where:
            key = str(where.get('agg', '')).strip()
            if agg_values is not None and key in agg_values:
                l = agg_values[key]
            else:
                l = self._eval_agg_expr(key, cols, [row])
            r = where.get('value')
            if l is None:
                return op == '=' and r is None
            if r is None:
                return op == '!='
            # 数值列 + 字符串字面量（`HAVING COUNT(*) > '1'`）也要能比
            l, r = self._coerce_pair(l, r, op)
            if op == '=':  return l == r
            if op == '!=': return l != r
            if op == '<':  return l < r
            if op == '>':  return l > r
            if op == '<=': return l <= r
            if op == '>=': return l >= r
            return False

        # ---------- EXISTS ----------
        if op in ('EXISTS', 'NOT EXISTS'):
            sub = where.get('subquery')
            exists = self._eval_subquery_exists(sub)
            return exists if op == 'EXISTS' else (not exists)

        # ---------- IN (SELECT ...) ----------
        if op in ('IN', 'NOT IN') and 'subquery' in where:
            try:
                ci = self._resolve_col(cols, where['column'])
            except ValueError:
                return False
            val = row[ci]
            values = self._exec_subquery_values(where['subquery'])
            return self._in_semantics(val, values, neg=(op == 'NOT IN'))

        # ---------- IN (v1, v2, ...) ----------
        if op in ('IN', 'NOT IN') and 'values' in where:
            try:
                ci = self._resolve_col(cols, where['column'])
            except ValueError:
                return False
            val = row[ci]
            # 字面量列表里的 NULL 同样走三值语义
            vals = [None if str(v).upper() == 'NULL' else v
                    for v in where['values']]
            return self._in_semantics(val, vals, neg=(op == 'NOT IN'))

        # ---------- 普通比较 ----------
        col_name = where.get('column')
        if col_name is None:
            return True
        try:
            ci = self._resolve_col(cols, col_name)
        except ValueError:
            return False

        l = row[ci]
        r = where.get('value')

        if l is None:
            return op == '=' and r is None
        if r is None:
            return op == '!='

        # 类型兼容
        l, r = self._coerce_pair(l, r, op)

        if op == '=':  return l == r
        if op == '!=': return l != r
        if op == '<':  return l < r
        if op == '>':  return l > r
        if op == '<=': return l <= r
        if op == '>=': return l >= r
        if op == 'LIKE':
            pat = str(r).replace('%', '.*').replace('_', '.')
            return bool(re.match('^' + pat + '$', str(l)))
        return False

    @classmethod
    def _in_semantics(cls, value, values, neg=False):
        """IN / NOT IN 的 NULL 三值语义（简化为真/假）

        * 左值为 NULL → UNKNOWN → 不通过；
        * NOT IN 且候选含 NULL → UNKNOWN → 不通过（SQL 标准）；
        * 否则按类型感知等值匹配。
        """
        if value is None:
            return False
        has_null = any(v is None for v in values)
        if neg and has_null:
            return False
        hit = any(v is not None and cls._value_eq(value, v) for v in values)
        return (not hit) if neg else hit

    @staticmethod
    def _value_eq(a, b):
        """类型感知的等值比较"""
        if a is None or b is None:
            return a is None and b is None
        if type(a) is type(b):
            return a == b
        try:
            if isinstance(a, (int, float)):
                return float(a) == float(b)
            if isinstance(b, (int, float)):
                return float(a) == float(b)
        except (ValueError, TypeError):
            pass
        return str(a) == str(b)

    @staticmethod
    def _coerce_pair(l, r, op):
        """将 l 和 r 转为可比较的同类型"""
        if isinstance(l, bool) or isinstance(r, bool):
            return l, r
        try:
            if isinstance(l, int) and isinstance(r, str):
                if op == 'LIKE':
                    return l, r
                r = int(r) if '.' not in r else float(r)
            elif isinstance(l, float) and isinstance(r, str):
                r = float(r)
            elif isinstance(l, str) and isinstance(r, (int, float)):
                return l, str(r)
        except (ValueError, TypeError):
            pass
        return l, r

    # ==================== 子查询 ====================

    def _get_sub_optimizer(self):
        if self._sub_optimizer is None:
            from .optimizer import Optimizer
            from .stats import StatsManager
            try:
                stats_mgr = StatsManager(str(self.db.data_dir))
            except Exception:
                stats_mgr = None
            self._sub_optimizer = Optimizer(self.db, stats_mgr)
        return self._sub_optimizer

    def _exec_subquery_values(self, parsed):
        """执行子查询，返回第一列的值的集合"""
        if parsed is None:
            return set()
        opt = self._get_sub_optimizer()
        plan = opt.optimize(parsed)
        sub_exec = PlanExecutor(self.db, read_view=self.read_view)
        try:
            cols, rows = sub_exec.execute(plan)
        except Exception:
            return set()
        return {row[0] for row in rows if row}

    def _eval_subquery_exists(self, parsed) -> bool:
        """EXISTS 子查询（当前简化：不关联外层列）

        TODO: 支持相关子查询时，需要把外层行注入子执行器
        """
        if parsed is None:
            return False
        opt = self._get_sub_optimizer()
        plan = opt.optimize(parsed)
        sub_exec = PlanExecutor(self.db, read_view=self.read_view)
        try:
            _, rows = sub_exec.execute(plan)
        except Exception:
            return False
        return len(rows) > 0

    # ==================== 扫描 ====================

    def _exec_SeqScan(self, node):
        tbl = self.db.get_table(node.table)
        # 列前缀优先用表别名：`u.id` / `o.id` 这类同名限定列只有精确
        # 前缀才能被 _resolve_col 命中，否则会因后缀相同被判歧义
        prefix = getattr(node, 'alias', None) or node.table
        cols = [f"{prefix}.{c['name']}" for c in tbl.cols]

        rows = []
        for pk, pid, sid in tbl.bt.scan():
            page = tbl.bp.get_page(pid)
            if page is None:
                continue
            rb = page.get_row(sid)
            if rb is None:
                continue
            r = tbl._decode_row_full(rb)
            if r is None:
                continue
            row, xmin, xmax = r
            if self.read_view is not None and \
                    not self.read_view.is_visible(xmin, xmax):
                continue
            rows.append(list(row))

        if node.filter:
            rows = [r for r in rows
                    if self._eval_condition(r, cols, node.filter)]

        return cols, rows

    def _exec_IndexScan(self, node):
        """索引扫描

        当前实现走全扫 + filter（索引定位待未来引入 B+Tree range scan）
        """
        return self._exec_SeqScan(node)

    # ==================== 中间算子 ====================

    def _exec_Filter(self, node):
        cols, rows = self.execute(node.children[0])
        return cols, [r for r in rows
                      if self._eval_condition(r, cols, node.condition)]

    def _exec_Project(self, node):
        cols, rows = self.execute(node.children[0])

        target_idx = []
        raw_cols = []
        consts = []
        for c in node.columns:
            c = c.strip()
            if c == '*':
                target_idx.extend(range(len(cols)))
                raw_cols.extend(cols)
            else:
                val = _literal_of(c)
                if val is not _NOT_LITERAL:
                    # 常量投影（`SELECT 1 FROM orders`）：子查询里很常见，
                    # 原来会直接报「列 1 不存在」
                    target_idx.append(-1)
                    raw_cols.append(c)
                    consts.append((len(target_idx) - 1, val))
                    continue
                if c.lstrip('(').upper().startswith('SELECT'):
                    # 投影列表里的标量子查询：JOIN 计划执行器没有逐外层行
                    # 求值的能力。显式报错，不要落到 _resolve_col 的
                    # 「列不存在」上（错误信息会误导使用者）。
                    raise ValueError(
                        "JOIN 查询的投影列表里暂不支持标量子查询"
                        f"（SELECT 列表）: {c}")
                i = self._resolve_col(cols, c)
                target_idx.append(i)
                raw_cols.append(cols[i])
        # 输出短名（避免歧义时保留全名）
        seen = {}
        short_cols = []
        for c in raw_cols:
            short = c.split('.', 1)[-1] if '.' in c else c
            if short in seen:
                short_cols.append(c)
            else:
                short_cols.append(short)
                seen[short] = True

        out_rows = []
        for r in rows:
            out = [r[i] if i >= 0 else None for i in target_idx]
            for pos, val in consts:
                out[pos] = val
            out_rows.append(out)
        return short_cols, out_rows

    def _exec_Sort(self, node):
        cols, rows = self.execute(node.children[0])
        try:
            ci = self._resolve_col(cols, node.column)
            rows = sorted(
                rows,
                key=lambda r: (r[ci] is None, r[ci] if r[ci] is not None else 0),
                reverse=node.desc,
            )
        except ValueError:
            pass
        return cols, rows

    def _exec_Limit(self, node):
        cols, rows = self.execute(node.children[0])
        # OFFSET 先裁剪，再套 LIMIT（n is None 表示只偏移不截断）
        off = int(getattr(node, 'offset', 0) or 0)
        if off:
            rows = rows[off:]
        if node.n is None:
            return cols, rows
        return cols, rows[:node.n]

    def _exec_Distinct(self, node):
        cols, rows = self.execute(node.children[0])
        seen = set()
        out = []
        for r in rows:
            k = tuple(self._hashable(v) for v in r)
            if k not in seen:
                seen.add(k)
                out.append(r)
        return cols, out

    @staticmethod
    def _hashable(v):
        if isinstance(v, (list, dict)):
            return str(v)
        return v

    # ==================== 聚合 ====================

    def _exec_Aggregate(self, node):
        """无 GROUP BY 的全表聚合"""
        cols, rows = self.execute(node.children[0])
        result = []
        for col_expr in node.columns:
            result.append(self._eval_agg_expr(col_expr, cols, rows))
        out_cols = [c.upper().strip() for c in node.columns]
        return out_cols, [result]

    def _exec_GroupBy(self, node):
        cols, rows = self.execute(node.children[0])
        group_cols = node.group_cols or []
        agg_cols = node.agg_cols or cols

        # 解析分组键
        group_idx = []
        for gc in group_cols:
            try:
                group_idx.append(self._resolve_col(cols, gc))
            except ValueError:
                pass

        # 分组
        if group_idx:
            groups = defaultdict(list)
            for r in rows:
                key = tuple(self._hashable(r[i]) for i in group_idx)
                groups[key].append(r)
        else:
            groups = {(): rows}

        out_cols = [c.upper().strip() for c in agg_cols]
        out_rows = []
        # HAVING 里的聚合表达式可能不在 SELECT 列表里（例如
        # `SELECT city FROM users GROUP BY city HAVING COUNT(*) > 5`），
        # 分组时必须先把它们算出来，否则求值时拿不到
        having_aggs = self._collect_having_aggs(node.having)
        for gkey, grows in groups.items():
            row = self._build_group_row(agg_cols, cols, gkey, grows)
            out_rows.append(row)

        # HAVING
        if node.having:
            filtered = []
            for idx, grows in enumerate(groups.values()):
                agg_values = {
                    a: self._eval_agg_expr(a, cols, grows)
                    for a in having_aggs
                }
                if self._eval_condition(
                        out_rows[idx], out_cols, node.having, agg_values):
                    filtered.append(out_rows[idx])
            out_rows = filtered

        return out_cols, out_rows

    @classmethod
    def _collect_having_aggs(cls, having) -> list:
        """收集 HAVING 条件里出现的聚合表达式（保持出现顺序、去重）"""
        found = []
        if isinstance(having, dict):
            if 'agg' in having:
                a = str(having['agg']).strip()
                if a and a not in found:
                    found.append(a)
            # NOT (...) 里也可能藏着聚合（`HAVING NOT (COUNT(*) > 1)`）
            inner = having.get('not')
            if isinstance(inner, dict):
                for a in cls._collect_having_aggs(inner):
                    if a not in found:
                        found.append(a)
            for key in ('and', 'or'):
                for c in (having.get(key) or []):
                    for a in cls._collect_having_aggs(c):
                        if a not in found:
                            found.append(a)
        return found

    def _build_group_row(self, agg_cols, src_cols, gkey, grows):
        """根据聚合表达式列表构建一行"""
        row = []
        # 用 key 里可迭代出来的值填充非聚合列
        key_iter = iter(gkey)
        for expr in agg_cols:
            expr = expr.strip()
            if self._is_agg_expr(expr):
                row.append(self._eval_agg_expr(expr, src_cols, grows))
            else:
                # 分组列：从 key 里取（如果取不到，从第一行取）
                try:
                    ci = self._resolve_col(src_cols, expr)
                    # 找 key 中对应的索引
                    if gkey:
                        # 因为 group_idx 与 group_cols 顺序一致
                        # 我们用第一行的值作为代表
                        row.append(grows[0][ci] if grows else None)
                    else:
                        row.append(next(key_iter, None))
                except (ValueError, IndexError):
                    row.append(next(key_iter, None))
        return row

    @staticmethod
    def _is_agg_expr(expr):
        if not isinstance(expr, str):
            return False
        e = expr.strip().upper()
        return any(
            e.startswith(fn + '(')
            for fn in ('COUNT', 'SUM', 'AVG', 'MIN', 'MAX',
                       'STDDEV', 'STDDEV_POP', 'STDDEV_SAMP',
                       'VARIANCE', 'VAR_POP', 'VAR_SAMP')
        )

    def _eval_agg_expr(self, expr, cols, rows):
        """求值单个聚合表达式"""
        expr = expr.strip()
        m = re.match(r'(\w+)\s*\(\s*(\*|[^)]+)\s*\)',
                     expr, re.IGNORECASE)
        if not m:
            try:
                ci = self._resolve_col(cols, expr)
                return rows[0][ci] if rows else None
            except ValueError:
                return None

        fn = m.group(1).upper()
        arg = m.group(2).strip()

        if fn == 'COUNT':
            if arg == '*':
                return len(rows)
            try:
                ci = self._resolve_col(cols, arg)
            except ValueError:
                return 0
            return sum(1 for r in rows if r[ci] is not None)

        try:
            ci = self._resolve_col(cols, arg)
        except ValueError:
            return None
        vals = [r[ci] for r in rows if r[ci] is not None]
        if not vals:
            return None

        if fn == 'SUM':
            try:
                return sum(vals)
            except TypeError:
                return None
        if fn == 'AVG':
            try:
                return sum(vals) / len(vals)
            except TypeError:
                return None
        if fn == 'MIN':
            try:
                return min(vals)
            except TypeError:
                return None
        if fn == 'MAX':
            try:
                return max(vals)
            except TypeError:
                return None
        if fn in ('STDDEV', 'STDDEV_POP'):
            import math
            m_ = sum(vals) / len(vals)
            return math.sqrt(
                sum((v - m_) ** 2 for v in vals) / len(vals))
        if fn == 'STDDEV_SAMP':
            import math
            if len(vals) < 2:
                return None
            m_ = sum(vals) / len(vals)
            return math.sqrt(
                sum((v - m_) ** 2 for v in vals) / (len(vals) - 1))
        if fn in ('VARIANCE', 'VAR_POP'):
            m_ = sum(vals) / len(vals)
            return sum((v - m_) ** 2 for v in vals) / len(vals)
        if fn == 'VAR_SAMP':
            if len(vals) < 2:
                return None
            m_ = sum(vals) / len(vals)
            return sum((v - m_) ** 2 for v in vals) / (len(vals) - 1)
        return None

    # ==================== JOIN ====================

    def _exec_HashJoin(self, node):
        lcols, lrows = self.execute(node.left)
        rcols, rrows = self.execute(node.right)

        try:
            li = self._resolve_col(lcols, node.left_key)
            ri = self._resolve_col(rcols, node.right_key)
        except (ValueError, AttributeError):
            li = ri = None

        out_cols = lcols + rcols

        # 无等值键 -> 笛卡尔积
        if li is None or ri is None:
            out_rows = []
            for lr in lrows:
                for rr in rrows:
                    out_rows.append(lr + rr)
            return out_cols, out_rows

        # 构建哈希表
        hash_table = defaultdict(list)
        for idx, r in enumerate(rrows):
            hash_table[r[ri]].append(idx)

        out_rows = []
        matched_right = [False] * len(rrows)
        jt = (node.join_type or 'INNER').upper()

        for lrow in lrows:
            key = lrow[li]
            indices = hash_table.get(key, [])
            if indices:
                for idx in indices:
                    out_rows.append(lrow + rrows[idx])
                    matched_right[idx] = True
            elif jt in ('LEFT', 'LEFT OUTER'):
                out_rows.append(lrow + [None] * len(rcols))

        # RIGHT JOIN 补未匹配右表
        if jt in ('RIGHT', 'RIGHT OUTER'):
            for idx, r in enumerate(rrows):
                if not matched_right[idx]:
                    out_rows.append([None] * len(lcols) + r)

        return out_cols, out_rows

    def _exec_NestedLoopJoin(self, node):
        lcols, lrows = self.execute(node.left)
        rcols, rrows = self.execute(node.right)
        out_cols = lcols + rcols

        li = ri = None
        if node.left_key and node.right_key:
            try:
                li = self._resolve_col(lcols, node.left_key)
                ri = self._resolve_col(rcols, node.right_key)
            except ValueError:
                li = ri = None

        jt = (node.join_type or 'INNER').upper()
        out_rows = []
        matched_right = [False] * len(rrows)

        for lrow in lrows:
            matched = False
            for j, rrow in enumerate(rrows):
                if li is not None and ri is not None:
                    if lrow[li] != rrow[ri]:
                        continue
                out_rows.append(lrow + rrow)
                matched = True
                matched_right[j] = True
            if not matched and jt in ('LEFT', 'LEFT OUTER'):
                out_rows.append(lrow + [None] * len(rcols))

        if jt in ('RIGHT', 'RIGHT OUTER'):
            for j, rrow in enumerate(rrows):
                if not matched_right[j]:
                    out_rows.append([None] * len(lcols) + rrow)

        return out_cols, out_rows

    def _exec_CrossJoin(self, node):
        lcols, lrows = self.execute(node.left)
        rcols, rrows = self.execute(node.right)
        out_rows = [lr + rr for lr in lrows for rr in rrows]
        return lcols + rcols, out_rows

    # ==================== UNION ====================

    def _exec_Union(self, node):
        cols1, rows1 = self.execute(node.children[0])
        cols2, rows2 = self.execute(node.children[1])
        out_rows = rows1 + rows2
        if not node.all:
            seen = set()
            uniq = []
            for r in out_rows:
                k = tuple(self._hashable(v) for v in r)
                if k not in seen:
                    seen.add(k)
                    uniq.append(r)
            out_rows = uniq
        return cols1, out_rows

    # ==================== 子查询扫描 ====================

    def _exec_SubqueryScan(self, node):
        cols, rows = self.execute(node.children[0])
        alias = node.alias
        new_cols = [f"{alias}.{c.split('.', 1)[-1]}" for c in cols]
        return new_cols, rows

    def _exec_ValueList(self, node):
        return ['value'], [[v] for v in (node.values or [])]

    # ==================== EXPLAIN ====================

    def _exec_Explain(self, node):
        from .plan import print_plan
        lines = print_plan(node.children[0])
        return ['EXPLAIN'], [[l] for l in lines]

    # ==================== 窗口 / CTE（钩子） ====================

    def _exec_Window(self, node):
        """窗口函数执行（委托给 window.WindowExecutor）"""
        cols, rows = self.execute(node.children[0])
        try:
            from .window import WindowExecutor
            wexec = WindowExecutor(rows, cols, node.window_calls)
            return wexec.execute()
        except Exception as e:
            raise NotImplementedError(f"窗口函数执行失败: {e}")

    def _exec_CTEDefine(self, node):
        cols, rows = self.execute(node.children[0])
        self._cte_cache[node.cte_name] = (cols, rows)
        return cols, rows

    def _exec_CTEScan(self, node):
        data = self._cte_cache.get(node.cte_name)
        if data is None:
            raise ValueError(f"CTE '{node.cte_name}' 未定义")
        return data

    def _exec_RecursiveCTE(self, node):
        # 递归 CTE：先执行 base，再迭代 recursive 直到不再增长
        base_cols, base_rows = self.execute(node.base_plan)
        all_rows = list(base_rows)
        seen = {tuple(self._hashable(v) for v in r) for r in base_rows}
        max_iter = 1000
        for _ in range(max_iter):
            self._cte_cache[node.cte_name] = (base_cols, all_rows)
            try:
                _, new_rows = self.execute(node.recursive_plan)
            except Exception:
                break
            added = 0
            for r in new_rows:
                k = tuple(self._hashable(v) for v in r)
                if k not in seen:
                    seen.add(k)
                    all_rows.append(r)
                    added += 1
            if added == 0:
                break
        return base_cols, all_rows