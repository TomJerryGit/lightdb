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
from .parser import group_by_violation_message


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

    # ★ GROUP BY 严格模式开关（逃生口，与 executor.Executor.strict_group_by 同名同义）
    #   None（默认）-> 环境变量 LIGHTDB_STRICT_GROUP_BY 决定，未设置即**严格**
    #   False       -> 关闭严格模式：非分组键裸列恢复「组内第一行」的旧行为
    strict_group_by = None

    def _strict_group_by_on(self) -> bool:
        """解析本执行器的严格模式开关（委托给 parser.resolve_strict_group_by）

        用 **实例** 属性读开关：`Executor` 在创建计划执行器时会把
        `Executor.strict_group_by` 原样赋到实例上（逃生口必须能传下来，
        类属性读法会让实例级关闭失效）。
        """
        from .parser import resolve_strict_group_by
        return resolve_strict_group_by(self.strict_group_by)

    def _new_sub_executor(self):
        """子查询用的执行器：**继承本实例的严格模式开关**

        逃生口（`Executor.strict_group_by = False`）由调用方原样传到这里，
        再传给子查询执行器；漏传的话子查询会退回默认的严格模式，
        用户看到的现象是「关不掉」。
        """
        sub = PlanExecutor(self.db, read_view=self.read_view)
        sub.strict_group_by = self.strict_group_by
        return sub

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

    @staticmethod
    def _norm_ident(text) -> str:
        """列标识符归一化：去引号/限定符、小写（`u.City` -> `city`）"""
        return (str(text).strip().strip('`"')
                .rsplit('.', 1)[-1].strip().lower())

    @staticmethod
    def _strip_select_alias(expr) -> str:
        """`dept AS d` -> `dept`（与 executor._split_alias 同一形态）"""
        return re.sub(r'\s+AS\s+[`"]?\w+[`"]?\s*$', '',
                      str(expr).strip(), flags=re.IGNORECASE).strip()

    @classmethod
    def _is_group_key(cls, expr, group_cols) -> bool:
        """`expr` 是否是分组键（别名 / 限定名 / 大小写都不影响判定）"""
        want = cls._norm_ident(cls._strip_select_alias(expr))
        return any(cls._norm_ident(g) == want for g in (group_cols or []))

    def _resolve_col(self, cols, name) -> int:
        """'t1.id' 或 'id' -> 列索引

        标识符按 SQL 惯例**不区分大小写**匹配（原实现区分大小写）：
        GROUP BY 的输出列名会被统一成大写
        （`_exec_GroupBy` 的 `out_cols = [c.upper() ...]`），而 HAVING 里
        写的是用户原来的 `u.id`；区分大小写时 `_resolve_col` 抛
        ValueError，又会被 `_eval_condition` 的
        `except ValueError: return False` 吞掉 —— 实测
        `... GROUP BY u.id HAVING u.id = 1` 与 `HAVING u.id IN (1)`
        都静默返回 0 行（正确结果是 [(1, 2)]），是 JOIN 计划路径上的
        静默错答。大小写不敏感后两者都算对；真歧义仍显式报错。
        """
        if not isinstance(name, str):
            raise ValueError(f"列名必须是字符串: {name!r}")
        name = name.strip().strip('`"')

        if name in cols:
            return cols.index(name)

        # 按 '.' 拆最后一段匹配（大小写不敏感）
        low = name.lower()
        matches = [
            i for i, c in enumerate(cols)
            if isinstance(c, str)
            and (c.lower() == low or c.lower().endswith('.' + low))
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"列 '{name}' 歧义，请加表名前缀")
        raise ValueError(f"列 '{name}' 不存在")

    # ==================== 条件求值（唯一定义） ====================

    def _eval_condition(self, row, cols, where, agg_values=None,
                        strict=False):
        """求值 WHERE/HAVING 条件

        ★ 只保留这一份实现（原代码中重复定义两次）

        agg_values: HAVING 语境下预先算好的聚合值 {'COUNT(*)': 3, ...}。
        GROUP BY 之后行里只剩输出列，HAVING 引用的聚合表达式可能不在
        SELECT 列表里，因此在分组时统一算好再传进来。

        strict: **连接后过滤（JOIN ON 回退路径）**用。此时条件里的列名
        必须全部解析得到：`_resolve_col` 失败不能再 `return False`
        （那会把「列写错」静默变成"条件为假"→ 空集），必须显式报错。
        无论 strict 与否，**带限定符**的列（`u.nosuch`）解析不到都报错
        （见 `_missing_col_result`）；只有裸列名保留既有的「判假」语义，
        因为本引擎允许 `WHERE name = Alice` 这种不写引号的字面量写法。
        """
        if where is None:
            return True
        if not isinstance(where, dict):
            return True

        # NOT (...)：整体取反（三值语义下 UNKNOWN 取反仍是 UNKNOWN）
        if isinstance(where.get('not'), dict):
            inner = self._eval_condition(
                row, cols, where['not'], agg_values, strict=strict)
            if inner is None:
                return None
            return not inner

        if 'and' in where:
            return all(self._eval_condition(row, cols, c, agg_values,
                                            strict=strict)
                       for c in where['and'])
        if 'or' in where:
            parts = where['or']
            # 空 OR 列表 = 常量假（执行器改写子查询时用这个表示）
            if not parts:
                return False
            return any(self._eval_condition(row, cols, c, agg_values,
                                            strict=strict)
                       for c in parts)

        op = where.get('op')

        # ---------- 表达式谓词（标量函数 / 算术）----------
        # `WHERE LOWER(u.name) = 'a'`、`WHERE o.amt * 2 > 100`、
        # `HAVING SUM(v) * 2 > 100`：左右两侧交给 parser 的共用求值器，
        # 列解析走本类的 `_resolve_col`（JOIN 结果的 `u.id` 限定名 / 歧义报错
        # 都在那里）。**必须有这个分支**：否则 `{'expr_pred': ...}` 会落到
        # 下面「col_name is None → return True」，把整个谓词静默丢掉，
        # 返回全部连接行 —— 静默错答。
        pred = where.get('expr_pred')
        if isinstance(pred, dict):
            from .parser import eval_expr_predicate
            pos = 'HAVING' if agg_values is not None else 'WHERE'
            return eval_expr_predicate(
                pred, self._expr_resolver(cols, row, pos), agg_values, pos)

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
                return self._missing_col_result(
                    where.get('column'), agg_values, strict, 'WHERE')
            val = row[ci]
            sub = where['subquery']
            # 集合上下文只允许**一列**：多列在 SQL 标准里是行值（tuple）
            # 比较，本引擎不支持 → 显式报错。原实现 `_exec_subquery_values`
            # 用 `{row[0] for row in rows}` 只保留第一列，把
            # `u.id IN (SELECT uid, amt ...)` 静默算成
            # `u.id IN (SELECT uid ...)`，JOIN 路径上返回看似正确的错行集。
            # 判定分两层：静态投影宽度（解析期，空结果集也拦得住）+
            # 子查询结果的实际列数（兜住 `SELECT *` 这类静态判不出的形态）。
            from .executor import Executor
            Executor._guard_in_subquery_width(sub, 'JOIN 查询里的')
            res_cols, values = self._exec_subquery_values(sub)
            Executor._guard_in_subquery_width(
                sub, 'JOIN 查询里的', res_cols=res_cols)
            return self._in_semantics(val, values, neg=(op == 'NOT IN'))

        # ---------- IN (v1, v2, ...) ----------
        if op in ('IN', 'NOT IN') and 'values' in where:
            try:
                ci = self._resolve_col(cols, where['column'])
            except ValueError:
                # HAVING 语境 / JOIN ON / 限定名解析不到 → 显式报错
                return self._missing_col_result(
                    where.get('column'), agg_values, strict, 'WHERE')
            val = row[ci]
            # 字面量列表里的 NULL 同样走三值语义；带限定名的项是列引用
            vals = self._resolve_in_values(where['values'], row, cols)
            return self._in_semantics(val, vals, neg=(op == 'NOT IN'))

        # ---------- 普通比较 ----------
        col_name = where.get('column')
        if col_name is None:
            # 带 subquery 但没有左值的节点 = WHERE/HAVING 里的标量（相关）
            # 子查询比较 `(SELECT ...) > 1`。计划执行器没有逐外层行折叠
            # 外层列的能力，取不到行数语义（多行该报错、0 行该是 NULL），
            # 原实现直接 `return True` 把整个谓词静默丢掉（实测
            # `JOIN ... WHERE (SELECT ...) = 1` 返回全部连接行）。
            # 与投影位置（下面 _exec_Project）一致，显式报错，绝不静默
            # 给出可能错误的结果。
            if 'subquery' in where:
                raise ValueError(
                    "JOIN 查询的 WHERE/HAVING 里暂不支持标量子查询比较"
                    f"（op={where.get('op')}）：该谓词需要逐行折叠外层列，"
                    "计划执行路径没有这个能力")
            return True
        try:
            ci = self._resolve_col(cols, col_name)
        except ValueError:
            # HAVING 语境（agg_values is not None，见 `_exec_GroupBy`）里
            # 解析不到列时显式报错：GROUP BY 的输出行只有分组键 + 聚合值，
            # `HAVING <未投影的裸列> ...`（例如
            # `SELECT u.id, COUNT(*) ... GROUP BY u.id HAVING u.city IN ('BJ')`）
            # 原来静默判假、整个分组被丢掉 —— 用户看到的是「没有分组」。
            return self._missing_col_result(col_name, agg_values, strict,
                                            'WHERE')

        l = row[ci]
        r = where.get('value')
        r = self._comparison_rhs(r, where, row, cols, strict)

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

    def _missing_col_result(self, col_name, agg_values, strict, context):
        """条件里的列在当前行解析不到时的统一处理（**绝不静默给空集**）

        原来三处都是 `return False`（"条件为假"）—— 用户看到的是「查不到
        任何行」，分不清是「确实没有匹配」还是「列名写错了 / 忘了加表前缀」。

        * HAVING 语境（GROUP BY 之后只剩分组键与聚合值）→ 报错并说明；
        * JOIN ON 回退路径（strict）→ 报错（ON 里引用不存在的列）；
        * **带限定符**的列（`u.nosuch` / `x.id`，绝不可能是字符串字面量）
          → 报错，指出表名/别名或列不在这个查询里；
        * 其余（裸列名解析不到）保持既有「判假」行为：本引擎允许
          `WHERE name = Alice` 这种不写引号的字面量写法，收紧会改坏它。
        """
        if agg_values is not None:
            raise ValueError(
                f"HAVING 里的列 '{col_name}' 在分组结果里取不到："
                f"GROUP BY 之后只保留分组键与聚合值，"
                f"请把该列加入 GROUP BY / SELECT，或改用聚合表达式")
        if strict:
            raise ValueError(
                f"JOIN ON 条件里的列 '{col_name}' 在连接结果里解析不到"
                f"（列不存在或需要表名前缀）")
        if isinstance(col_name, str) and '.' in col_name.strip().strip('`"'):
            raise ValueError(
                f"JOIN 查询的 {context} 里列 '{col_name}' 在连接结果里"
                f"解析不到：列不存在，或该表名/别名不在这个查询里")
        return False

    def _comparison_rhs(self, r, where, row, cols, strict=False):
        """比较右侧取值：`value_column` 标记的**列引用**在本行取列值

        `WHERE o.user_id = u.id` 的右侧是列引用，但 AST 里它的编码与字面量
        一样（`{'column','op','value'}`）。求值器原来无条件把 `value` 当
        字面量 → 执行 `row[o.user_id] == 'u.id'` → 恒不成立 →
        **静默返回空集**（本次报告的主症状）。

        * 带 `value_column` 且能在本行解析到列 → 取该列的值比较；
        * 解析不到且**带限定符**（`u.nosuch`）或 `strict`（JOIN ON 回退
          路径）→ 显式报错，绝不静默判假；
        * 其余（裸标识符解析不到）→ 保持历史行为当字符串字面量
          （`WHERE name = Alice` 这种不写引号的写法在既有测试里存在）。
        """
        if not where.get('value_column') or not isinstance(r, str):
            return r
        try:
            rj = self._resolve_col(cols, r)
        except ValueError:
            if strict or '.' in r.strip().strip('`"'):
                raise ValueError(
                    f"列 '{r}' 在连接结果里解析不到"
                    f"（列不存在或需要表名前缀）")
            return r
        return row[rj] if rj < len(row) else None

    def _resolve_in_values(self, values, row, cols):
        """IN 列表求值：带限定名的项是**列引用**，取该行对应列的值

        `WHERE o.user_id IN (u.id)` 原来被当成字面量集合 {'u.id'} →
        恒不命中 → 静默空集（与 `a = b` 同一类吞错）。带点的项只可能是
        列引用：解析不到就显式报错；裸标识符 / 字面量保持原样。
        """
        out = []
        for v in (values or []):
            if isinstance(v, str) and '.' in v.strip().strip('`"'):
                try:
                    vi = self._resolve_col(cols, v)
                except ValueError:
                    raise ValueError(
                        f"IN 列表里的列 '{v}' 在连接结果里解析不到"
                        f"（列不存在或需要表名前缀）")
                out.append(row[vi] if vi < len(row) else None)
                continue
            out.append(None if str(v).upper() == 'NULL' else v)
        return out

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
        """执行子查询，返回 (结果列名, 第一列的值的集合)

        原来只返回第一列的集合（`{row[0] for row in rows}`），列数信息在
        这里被丢弃，调用方无法判断「多列子查询」——这正是 IN 在 JOIN 路径
        上静默截断成第一列的根源。现在把结果列名一并返回，由调用方用
        `Executor._guard_in_subquery_width` 判定多列并显式报错。
        注意：执行失败不再吞成空集合（`return set()` 会让 `c IN (...)` 变成
        恒假、`c NOT IN (...)` 变成恒真，是另一种静默错答），改为向上抛出，
        由 `Executor.execute` 统一转成错误结果。
        """
        if parsed is None:
            return [], set()
        opt = self._get_sub_optimizer()
        plan = opt.optimize(parsed)
        sub_exec = self._new_sub_executor()
        try:
            cols, rows = sub_exec.execute(plan)
        except Exception as e:
            raise ValueError(f"子查询执行失败: {e}")
        return (list(cols or []),
                {row[0] for row in rows if row})

    def _eval_subquery_exists(self, parsed) -> bool:
        """EXISTS 子查询（当前简化：不关联外层列）

        TODO: 支持相关子查询时，需要把外层行注入子执行器

        注意：子查询**执行失败**不再吞成 False。原实现
        `except Exception: return False` 会把「子查询算不出来」静默变成
        「EXISTS 为假」，于是 `WHERE EXISTS (...)` 返回 0 行、
        `NOT EXISTS` 返回全部行 —— 两者都是看似正常的错答案（同类静默
        降级）。改为向上抛出，由 `Executor.execute` 转成显式错误结果。
        """
        if parsed is None:
            return False
        opt = self._get_sub_optimizer()
        plan = opt.optimize(parsed)
        sub_exec = self._new_sub_executor()
        try:
            _, rows = sub_exec.execute(plan)
        except Exception as e:
            raise ValueError(f"EXISTS 子查询执行失败: {e}")
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

    # ==================== 标量表达式（JOIN 计划路径） ====================

    def _expr_resolver(self, cols, row, position=''):
        """表达式求值用的列解析回调（复用 `_resolve_col` 的报错与歧义语义）

        `_resolve_col` 的末段匹配方向是「列名以 .name 结尾」，而 Sort 位于
        Project 之上时列名已被压成短名（`u.name` → `name`），限定名反向
        解析不到；GROUP BY 的输出列名会带上 `AS 别名`（`U.CITY AS C`）。
        这两种情况在这里补一层「末段 / 别名」匹配，**真歧义仍然显式报错**。
        """
        def resolve(name):
            try:
                i = self._resolve_col(cols, name)
            except ValueError:
                i = self._resolve_loose(cols, name)
                if i is None:
                    raise
            if i >= len(row):
                raise ValueError(
                    f"{position} 里的列 '{name}' 在当前行里取不到值")
            return row[i]
        return resolve

    @staticmethod
    def _resolve_loose(cols, name):
        """宽松列解析：按「末段」或「AS 别名」匹配；歧义返回 None 由调用方报错"""
        want = str(name).strip().strip('`"').rsplit('.', 1)[-1].lower()
        if not want:
            return None
        hits = []
        for i, c in enumerate(cols):
            text = str(c).strip().strip('`"')
            m = re.search(r'\s+AS\s+[`"]?(\w+)[`"]?\s*$', text, re.IGNORECASE)
            last = (m.group(1) if m else text.rsplit('.', 1)[-1]).lower()
            if last == want:
                hits.append(i)
        if len(hits) > 1:
            raise ValueError(f"列 '{name}' 歧义，请加表名前缀")
        return hits[0] if hits else None

    def _eval_expr_in_row(self, node, cols, row, agg_values=None,
                          position=''):
        from .parser import eval_expr_node
        return eval_expr_node(node, self._expr_resolver(cols, row, position),
                              agg_values, position)

    @staticmethod
    def _check_expr_functions(node, position=''):
        """静态校验表达式里的函数名（不支持的**显式报错并回列支持清单**）

        与 `Executor._check_expr_functions` 同一份语义：`REVERSE(name)` /
        `NOW()` 这类名字在这里抛 ValueError（文案由 parser 统一给出），
        绝不静默返回 NULL，也绝不把函数名当成列名。

        **已注册的存储函数名**（`CREATE FUNCTION f(...)`）同样合法：
        查询走 `parser.is_stored_function_name`（执行器构造时登记的钩子），
        JOIN 查询的投影/WHERE 里调用存储函数因此不会被误判；真正的调用由
        计划执行器的 `_eval_expr_in_row` 走 `Executor` 那条求值路径完成。
        """
        from .parser import (is_stored_function_name,
                             unsupported_function_message)
        if not isinstance(node, dict):
            return
        kind = node.get('kind')
        if kind == 'func':
            name = node.get('name')
            if not is_stored_function_name(name):
                raise ValueError(unsupported_function_message(name))
            for a in (node.get('args') or []):
                PlanExecutor._check_expr_functions(a, position)
        elif kind == 'arith':
            PlanExecutor._check_expr_functions(node.get('left'), position)
            PlanExecutor._check_expr_functions(node.get('right'), position)
        elif kind == 'between':
            for key in ('operand', 'low', 'high'):
                PlanExecutor._check_expr_functions(node.get(key), position)

    @staticmethod
    def _row_agg_values(cols, row):
        """分组结果行上可用的聚合值映射（供 ORDER BY 表达式取聚合值）"""
        from .parser import row_agg_values_map
        return row_agg_values_map(cols, row)

    def _exec_Filter(self, node):
        cols, rows = self.execute(node.children[0])
        return cols, [r for r in rows
                      if self._eval_condition(r, cols, node.condition)]

    def _exec_Project(self, node):
        cols, rows = self.execute(node.children[0])

        from .parser import (parse_scalar_expr, expr_has_func,
                             expr_has_between, _split_select_alias)

        target_idx = []
        raw_cols = []
        consts = []
        exprs = []
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
                try:
                    i = self._resolve_col(cols, c)
                except ValueError:
                    expr_text, alias = _split_select_alias(c)
                    # `u.id AS uid`（纯列别名）：解析别名左侧的列
                    if alias:
                        try:
                            i2 = self._resolve_col(cols, expr_text)
                        except ValueError:
                            i2 = None
                        if i2 is not None:
                            target_idx.append(i2)
                            raw_cols.append(str(alias))
                            continue
                    # 标量函数 / 含函数或嵌套的算术（`LOWER(u.name)`、
                    # `o.amt * 2 + 1`）：逐行用表达式求值器算
                    fnode = parse_scalar_expr(expr_text)
                    if fnode is None or not (
                            expr_has_func(fnode)
                            or expr_has_between(fnode) or alias):
                        raise
                    self._check_expr_functions(fnode, '投影')
                    target_idx.append(-2)
                    raw_cols.append(str(alias or expr_text))
                    exprs.append((len(target_idx) - 1, fnode))
                    continue
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
            for pos, node_e in exprs:
                out[pos] = self._eval_expr_in_row(node_e, cols, r, None, '投影')
            out_rows.append(out)
        return short_cols, out_rows

    def _exec_Sort(self, node):
        """ORDER BY：**任意个键**、逐键独立 ASC/DESC

        Sort 在计划里位于 Project / GroupBy **之上**，因此这里的 `cols` 是
        投影之后的名字（`u.city` 被压成 `city`、聚合表达式压成 `COUNT(*)`
        或 `AS 别名`）。逐键解析顺序：

        ① 列序号（`ORDER BY 2`）→ 输出列下标；
        ② 精确 / 末段 / `AS 别名` 匹配的列名（`_resolve_col` → `_resolve_loose`）；
        ③ 标量表达式（`LENGTH(name)`、`o.amt * 2`）→ 逐行求值。

        NULL 定义：**NULL 视为最大值** —— 升序排最后、降序排最前，逐键一致。
        多键用「从最后一个键往前逐个稳定排序」实现字典序。

        任何键都解析不到时**显式报错**（报出第几个键）：原实现是
        `except: ValueError` 后**静默不排序**，`ORDER BY o.amt DESC` 会因为
        Project 把 `o.amt` 压成 `amt` 而解析失败 → 返回一份顺序错误的结果。
        """
        cols, rows = self.execute(node.children[0])
        keys = getattr(node, 'keys', None) or [(node.column, node.desc)]
        specs = []
        for pos, item in enumerate(keys):
            text, desc = (item if isinstance(item, (list, tuple))
                          else (item, getattr(node, 'desc', False)))
            t = str(text).strip()
            if re.match(r'^\d+$', t):
                k = int(t)
                if k < 1 or k > len(cols):
                    raise ValueError(
                        f"ORDER BY 第 {pos + 1} 个排序键是列序号 {k}，"
                        f"超出输出列范围（1..{len(cols)}）")
                specs.append(('idx', k - 1, bool(desc)))
                continue
            try:
                ci = self._resolve_col(cols, t)
            except ValueError:
                # Sort 位于 Project **之上**，此时列名已被压成短名
                # （`o.amt` 变成 `amt`），而 ORDER BY 里通常还写着限定名。
                # `_resolve_col` 的后缀匹配方向是「cols 里的名字以 .name
                # 结尾」，正好反了，于是 `JOIN ... ORDER BY o.amt` 解析失败
                # → 曾**静默不排序**（实测 `ORDER BY o.amt DESC LIMIT 2` 仍
                # 返回 [(1,10),(1,20)]）。这里按「列名末段 / AS 别名」再解析。
                ci = self._resolve_loose(cols, t)
            if ci is not None:
                specs.append(('idx', ci, bool(desc)))
                continue
            # ORDER BY 是**表达式**（`ORDER BY LENGTH(name) DESC`）：按行算出
            # 排序键。原行为是「解析不到就静默不排序」—— 静默错答。
            from .parser import parse_scalar_expr
            snode = parse_scalar_expr(self._strip_select_alias(t))
            if snode is None or snode.get('kind') == 'col':
                raise ValueError(
                    f"ORDER BY 第 {pos + 1} 个排序键 '{t}' 无法解析："
                    f"它既不是输出列名 / 投影别名 / 列序号，也不是可求值的"
                    f"标量表达式（该键不会被静默忽略）")
            self._check_expr_functions(snode, 'ORDER BY')
            specs.append(('expr', snode, bool(desc)))

        if not specs:
            return cols, rows

        # 先算出每个键在每一行上的取值（'expr' 键逐行求值）
        values = []
        for kind, spec, desc in specs:
            if kind == 'idx':
                vals = [r[spec] if spec < len(r) else None for r in rows]
            else:
                vals = [self._eval_expr_in_row(
                    spec, cols, r, self._row_agg_values(cols, r), 'ORDER BY')
                    for r in rows]
            values.append((vals, desc))

        # 从最后一个键往前逐个稳定排序 → 字典序多键 + 逐键方向；
        # `(值 is None, 值)` 把 NULL 当最大值（ASC 排最后、DESC 排最前）。
        idx = list(range(len(rows)))
        for vals, desc in reversed(values):
            idx.sort(key=lambda i, v=vals: (v[i] is None, v[i]),
                     reverse=desc)
        return cols, [rows[i] for i in idx]

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
        """无 GROUP BY 的全表聚合

        ★ 严格模式：整表聚成一组时 SELECT 列表里只能有聚合函数与常量，
        裸列（`SELECT dept, SUM(salary) FROM emp`）原来会静默取第一行。
        """
        cols, rows = self.execute(node.children[0])
        if self._strict_group_by_on():
            self._check_group_by_columns(
                node.columns, [], implicit=True)
        result = []
        for col_expr in node.columns:
            result.append(self._eval_agg_expr(col_expr, cols, rows))
        # 输出列名保留 SELECT 列的**原文大小写**（MySQL 语义）。
        # 曾用 `.upper()`，于是 `u.city` -> `U.CITY`、`cnt` -> `CNT`，
        # 与 MySQL 的列标签行为不符。
        out_cols = [str(c).strip() for c in node.columns]
        return out_cols, [result]

    def _check_group_by_columns(self, columns, group_cols, implicit=False,
                                having=None):
        """严格模式下的 GROUP BY 校验（计划路径的**运行期兜底**）

        与 `executor._check_strict_group_by` 共用 `parser.validate_group_by`
        这一份实现（文案逐字一致）；计划路径可能由内部代码直接构造，
        不经过 `_cmd_select`，所以这里必须再兜一次（SELECT 投影 /
        HAVING / ORDER BY 三个位置都在同一份校验里）。

        implicit=True 表示 group_cols 是优化器为了「无 GROUP BY 的聚合查询」
        自动补出来的裸列（见 `optimizer.optimize` 的 `group_cols = non_agg`），
        它**不是**用户写的分组键，否则第 6 条验收（无 GROUP BY 混用聚合与
        非聚合列）会在计划路径上被静默放行。
        """
        from .parser import validate_group_by, resolve_strict_group_by
        validate_group_by(
            {'command': 'SELECT', 'columns': list(columns or []),
             'group_by': list(group_cols or []) or None,
             'having': having,
             'implicit_group': bool(implicit)},
            strict=resolve_strict_group_by(self.strict_group_by))

    def _exec_GroupBy(self, node):
        cols, rows = self.execute(node.children[0])
        group_cols = node.group_cols or []
        agg_cols = node.agg_cols or cols

        # ★ 严格模式兜底（同一份静态校验，报错发生在构建任何分组行之前）
        if self._strict_group_by_on():
            self._check_group_by_columns(
                agg_cols, group_cols,
                implicit=bool(getattr(node, 'implicit_group', False)),
                having=node.having)

        # 解析分组键：纯列名走下标记；函数/算术表达式（`GROUP BY UPPER(dept)`）
        # 逐行求值。原实现对解析不到的键**静默跳过**，group_idx 变空后
        # 整表聚成一组（`GROUP BY UPPER(dept)` 静默返回 1 行）—— 静默错答。
        from .parser import parse_scalar_expr, expr_has_agg
        group_getters = []
        for gc in group_cols:
            try:
                group_getters.append(('idx', self._resolve_col(cols, gc)))
                continue
            except ValueError:
                pass
            gnode = parse_scalar_expr(self._strip_select_alias(gc))
            if gnode is None or expr_has_agg(gnode):
                raise ValueError(
                    f"GROUP BY 列 '{gc}' 不存在，也无法解析成分组表达式")
            self._check_expr_functions(gnode, 'GROUP BY')
            group_getters.append(('expr', gnode))

        def key_of(row):
            parts = []
            for kind, spec in group_getters:
                if kind == 'idx':
                    parts.append(self._hashable(row[spec]))
                else:
                    parts.append(self._hashable(
                        self._eval_expr_in_row(spec, cols, row, None,
                                               'GROUP BY')))
            return tuple(parts)

        # 分组
        if group_getters:
            groups = defaultdict(list)
            for r in rows:
                groups[key_of(r)].append(r)
        else:
            groups = {(): rows}

        # 同上：保留原文大小写（聚合列标签不应被转成大写）
        out_cols = [str(c).strip() for c in agg_cols]
        out_rows = []
        # HAVING 里的聚合表达式可能不在 SELECT 列表里（例如
        # `SELECT city FROM users GROUP BY city HAVING COUNT(*) > 5`），
        # 分组时必须先把它们算出来，否则求值时拿不到
        having_aggs = self._collect_having_aggs(node.having)
        for gkey, grows in groups.items():
            row = self._build_group_row(
                agg_cols, cols, gkey, grows, group_cols)
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
        """收集 HAVING 条件里出现的聚合表达式（保持出现顺序、去重）

        含表达式谓词（`HAVING SUM(v) * 2 > 100`）里的聚合 —— 文本由
        `render_scalar_expr` 规范渲染，与 `_eval_condition` 取值的查找键一致。
        """
        from .parser import expr_pred_agg_texts
        found = []
        if isinstance(having, dict):
            if 'agg' in having:
                a = str(having['agg']).strip()
                if a and a not in found:
                    found.append(a)
            if isinstance(having.get('expr_pred'), dict):
                for a in expr_pred_agg_texts(having['expr_pred']):
                    if a not in found:
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

    def _build_group_row(self, agg_cols, src_cols, gkey, grows,
                         group_cols=None):
        """根据聚合表达式列表构建一行

        ★ 本次收紧（等价 MySQL ONLY_FULL_GROUP_BY）：非分组键、且未被聚合
        的裸列**显式报错**，不再取「组内第一行」。

        旧行为（记录在此，便于对照）：非聚合列一律用 `grows[0][ci]` 当代表
        —— 对**分组键**列是安全的（组内取值恒等），对**非分组键**的裸列
        （`SELECT city, name, COUNT(*) ... GROUP BY city`）则是任取一行；
        同一份 SQL 换个子代理/换个物理顺序就可能换一组答案。

        允许的三种形态：
        ① 分组键本身（含 `u.city` 限定名与 `dept AS d` 别名形态）
           —— 组内取值恒等，取第一行安全；
        ② 聚合函数作用于任意列（`SUM(o.amount)`）；
        ③ 与分组无关的常量/字面量（`SELECT 1 ... GROUP BY k`），直接输出常量。

        逃生口：`PlanExecutor.strict_group_by = False`（或环境变量
        LIGHTDB_STRICT_GROUP_BY=0）时保留旧行为。
        """
        strict_on = self._strict_group_by_on()
        row = []
        # 用 key 里可迭代出来的值填充非聚合列（旧行为的兜底路径）
        key_iter = iter(gkey)
        for expr in agg_cols:
            text = str(expr).strip()
            if self._is_agg_expr(text):
                row.append(self._eval_agg_expr(text, src_cols, grows))
                continue

            # ③ 常量投影（`SELECT 1`）：与分组无关，直接输出常量
            lit = _literal_of(self._strip_select_alias(text))
            if lit is not _NOT_LITERAL:
                row.append(lit)
                continue

            # ①② 分组键（别名/限定名归一化后按名字精确解析）
            if self._is_group_key(text, group_cols):
                try:
                    ci = self._resolve_col(
                        src_cols, self._strip_select_alias(text))
                except ValueError:
                    ci = None
                if ci is not None and grows:
                    row.append(grows[0][ci])
                else:
                    row.append(next(key_iter, None))
                continue

            if strict_on:
                # 非分组键裸列：显式报错（含列名与修复办法）
                raise ValueError(group_by_violation_message(
                    self._strip_select_alias(text)))
            # 逃生口：旧行为（组内第一行）
            try:
                ci = self._resolve_col(
                    src_cols, self._strip_select_alias(text))
                row.append(grows[0][ci] if grows else None)
            except (ValueError, IndexError):
                row.append(next(key_iter, None))
        return row

    @classmethod
    def _is_agg_expr(cls, expr):
        """是否是聚合表达式（含 `ABS(SUM(v))` / `ROUND(AVG(v), 1)` 这类嵌套）

        先剥掉 `AS 别名` 再判定 —— `ROUND(AVG(o.amount), 1) AS av` 整段
        解析不出表达式，不剥别名就会漏判成「非聚合」，落到 Project 上。
        """
        if not isinstance(expr, str):
            return False
        e = cls._strip_select_alias(expr).strip()
        up = e.upper()
        if any(
            up.startswith(fn + '(')
            for fn in ('COUNT', 'SUM', 'AVG', 'MIN', 'MAX',
                       'STDDEV', 'STDDEV_POP', 'STDDEV_SAMP',
                       'VARIANCE', 'VAR_POP', 'VAR_SAMP',
                       'TOTAL', 'GROUP_CONCAT')
        ):
            return True
        from .parser import parse_scalar_expr, expr_has_agg
        node = parse_scalar_expr(e)
        return bool(node is not None and expr_has_agg(node))

    def _eval_agg_expr(self, expr, cols, rows):
        """求值单个聚合表达式

        参数是**表达式**（`SUM(salary * 2)`、`COUNT(ABS(v))`）或函数名是
        存储层不支持的聚合（`ROUND(AVG(v), 1)`、`STDDEV`、`GROUP_CONCAT`）时，
        交给 `parser.eval_agg_expr_over_rows` 逐行求值后再聚合；
        其余形态走下面的原有实现（结果完全相同）。
        """
        expr = str(expr).strip()
        from .parser import (parse_scalar_expr, expr_has_agg,
                             eval_agg_expr_over_rows)
        node = parse_scalar_expr(self._strip_select_alias(expr))
        if node is not None and expr_has_agg(node):
            def resolve_row(row, name):
                i = self._resolve_col(cols, name)
                if i >= len(row):
                    raise ValueError(
                        f"列 '{name}' 在当前行里取不到值（聚合表达式）")
                return row[i]
            return eval_agg_expr_over_rows(
                node, rows, resolve_row, 'GROUP BY/HAVING',
                strict=self._strict_group_by_on())

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

    def _resolve_join_keys(self, lcols, rcols, lkey, rkey):
        """等值连接的键解析（`ON` 两侧谁写在左边都可以）

        `ON u.id = o.uid` 与 `ON o.uid = u.id` 语义完全等价，但 AST 里保留的
        是 SQL 文本顺序。原实现一律把 `on['left']` 当作**左输入**的列：反过来
        写时（JOIN 进来的表写在左边）左键在左输入里解析不到 → `li/ri` 变成
        None → **静默退化成笛卡尔积**（实测 `FROM users u JOIN orders o
        ON o.uid=u.id` 返回 3×3=9 行，其上的 WHERE / GROUP BY / 子查询
        全部跟着算错）。

        这里按「原顺序 → 交换一次」两轮解析；两轮都失败才返回 (None, None)。
        调用方**不能**把 (None, None) 当成「没有 ON」：那会把 ON 丢掉，
        必须改走 `_exec_join_with_on_filter`（笛卡尔积 + ON 连接后过滤）
        或显式报错。
        """
        for lk, rk in ((lkey, rkey), (rkey, lkey)):
            if not lk or not rk:
                continue
            try:
                return (self._resolve_col(lcols, lk),
                        self._resolve_col(rcols, rk))
            except (ValueError, AttributeError):
                continue
        return None, None

    # ==================== JOIN（ON 连接后过滤） ====================

    def _join_on_filter(self, node):
        """连接节点上挂的 ON 条件树（没有则 None）"""
        f = getattr(node, 'on_filter', None)
        return f if isinstance(f, dict) else None

    def _exec_join_with_on_filter(self, node, lcols, lrows, rcols, rrows):
        """ON 不能用作等值连接键时的**标准语义**执行：笛卡尔积 + ON 过滤

        SQL 标准里 `ON` 就是连接后的过滤条件（笛卡尔积再按 ON 过滤），
        所以「ON 不可用作等值键」不该丢掉 ON，而应该退化成
        「嵌套循环 + 逐候选行求值 ON」：

        * 自比较 / 只引用单侧 / 常量 / 非等值 ON 全部走这里；
        * 没有 ON（真正的 CROSS JOIN 或 `JOIN t` 无 ON）时等价于笛卡尔积；
        * ON 引用了连接结果里不存在的列 → `strict=True` **显式报错**
          （原来会被 `except ValueError: return False` 吞成"条件为假"，
          整个结果静默变成空集）；
        * `LEFT/RIGHT JOIN` 未匹配时按 SQL 语义补 NULL（ON 是连接条件、
          不是外层 WHERE，未匹配的左侧行必须保留）。

        这就是本次修复的核心：原实现在 `_resolve_join_keys` 返回
        (None, None) 时直接产出笛卡尔积并**把 ON 丢掉**。
        """
        out_cols = lcols + rcols
        on_filter = self._join_on_filter(node)
        jt = (node.join_type or 'INNER').upper()
        out_rows = []
        matched_right = [False] * len(rrows)

        for lrow in lrows:
            matched = False
            for j, rrow in enumerate(rrows):
                cand = lrow + rrow
                if on_filter is not None and not self._eval_condition(
                        cand, out_cols, on_filter, strict=True):
                    continue
                out_rows.append(cand)
                matched = True
                matched_right[j] = True
            if not matched and jt in ('LEFT', 'LEFT OUTER'):
                out_rows.append(lrow + [None] * len(rcols))

        if jt in ('RIGHT', 'RIGHT OUTER'):
            for j, rrow in enumerate(rrows):
                if not matched_right[j]:
                    out_rows.append([None] * len(lcols) + rrow)

        return out_cols, out_rows

    def _exec_HashJoin(self, node):
        lcols, lrows = self.execute(node.left)
        rcols, rrows = self.execute(node.right)

        li, ri = self._resolve_join_keys(
            lcols, rcols, node.left_key, node.right_key)

        # 等值键解析失败 -> **不能丢 ON**：按标准语义笛卡尔积 + ON 过滤
        # （原来这里直接吐笛卡尔积，ON 被静默丢弃）
        if li is None or ri is None:
            return self._exec_join_with_on_filter(
                node, lcols, lrows, rcols, rrows)

        out_cols = lcols + rcols

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
            li, ri = self._resolve_join_keys(
                lcols, rcols, node.left_key, node.right_key)

        # 没有可用的等值键（无 ON / 常量 ON / 只引用单侧 / 非等值 ON）：
        # 交给统一的「ON 连接后过滤」实现（含 LEFT/RIGHT 补 NULL）
        if li is None or ri is None:
            return self._exec_join_with_on_filter(
                node, lcols, lrows, rcols, rrows)

        jt = (node.join_type or 'INNER').upper()
        out_rows = []
        matched_right = [False] * len(rrows)

        for lrow in lrows:
            matched = False
            for j, rrow in enumerate(rrows):
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
        on_filter = self._join_on_filter(node)
        # `CROSS JOIN` 本身没有 ON；带 ON 的形态（`JOIN t ON ...` 无可用
        # 等值键）也复用同一条「ON 连接后过滤」路径
        if on_filter is not None:
            return self._exec_join_with_on_filter(
                node, lcols, lrows, rcols, rrows)
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