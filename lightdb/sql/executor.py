"""
SQL 执行器 - 结构化结果（修复版）

修复:
- __init__ 初始化 self.optimizer（原 bug: AttributeError）
- 加 start_event_scheduler / stop_event_scheduler
- 加 stats_manager
"""
import re
import time
from typing import Dict

from ..logger import logger
from ..db.information_schema import InformationSchema
from ..db.table import Table as _StorageTable


# 哨兵：区分"调用方没传 where"与"where 被改写成恒真(None)"
_UNSET = object()

# 哨兵：作用域里解析不到某个列引用
_MISSING = object()


class _CTERelation:
    """WITH 子句物化出来的内存关系

    只实现 Table._match / Table._aggregate 需要的接口（cols / _col_idx），
    因此 WHERE 与聚合可以直接复用存储层的实现，而不必创建真实表文件
    （真实表还会因为主键唯一性要求而无法承载有重复值的 CTE 结果）。
    """

    def __init__(self, name, columns, rows):
        self.name = name
        self.col_names = [str(c) for c in (columns or [])]
        self.rows = [list(r) for r in (rows or [])]
        self.cols = [
            {'name': n, 'type': self._guess_type(i)}
            for i, n in enumerate(self.col_names)
        ]

    def _guess_type(self, i):
        vals = [r[i] for r in self.rows
                if i < len(r) and r[i] is not None]
        if vals and all(isinstance(v, bool) for v in vals):
            return 'INT'
        if vals and all(isinstance(v, int) for v in vals):
            return 'INT'
        if vals and all(isinstance(v, (int, float)) for v in vals):
            return 'DOUBLE'
        return 'VARCHAR'

    def _col_idx(self, name: str) -> int:
        for i, c in enumerate(self.cols):
            if c['name'] == name:
                return i
        raise KeyError(f"列 {name} 不存在")


class Executor:

    def __init__(self, db):
        self.db = db
        self.info_schema = InformationSchema(db)
        # CTE 注册表：名字 -> _CTERelation（仅在一次语句执行期间有效）
        self._cte_relations = {}
        self.stats = {
            'queries': 0,
            'rows': 0,
            'commits': 0,
        }

        # ★ 修复：初始化 optimizer 和 stats_manager
        try:
            from .optimizer import Optimizer
            from .stats import StatsManager
            self.stats_manager = StatsManager(
                str(db.data_dir))
            self.optimizer = Optimizer(
                db, self.stats_manager)
        except Exception as e:
            logger.warn(f"初始化 optimizer 失败: {e}")
            self.optimizer = None
            self.stats_manager = None

        # 兼容别名：测试与部分调用方用的是 stats_mgr
        self.stats_mgr = self.stats_manager

        # ★ 事件调度器（可选）
        self._event_scheduler = None

    # ==================== 主入口 ====================

    def execute(self, parsed: Dict, session: Dict) -> Dict:
        start = time.time()
        try:
            if isinstance(parsed, dict) and parsed.get('ctes'):
                result = self._execute_with_ctes(parsed, session)
            else:
                result = self._dispatch(parsed, session)
            self.stats['queries'] += 1
            elapsed = time.time() - start
            if elapsed > 0.5:
                logger.warn(
                    f"慢查询 {elapsed:.3f}s: "
                    f"{parsed.get('command')}")
                try:
                    logger.slow(
                        sql=str(parsed)[:500],
                        elapsed=elapsed,
                        user=session.get('user', ''),
                        threshold=0.5)
                except Exception:
                    pass
            return result
        except KeyError as e:
            return self._err(1146, f"表不存在: {e}")
        except ValueError as e:
            return self._err(1062, str(e))
        except Exception as e:
            logger.error(f"执行错误: {e}")
            return self._err(1105, str(e))

    def execute_sql(self, sql: str, session: Dict = None) -> Dict:
        """便捷方法：直接执行 SQL 字符串"""
        from .parser import parser
        parsed = parser.parse(sql)
        if parsed is None:
            return self._err(1064, f"无法解析 SQL: {sql[:80]}")
        # 注意：不能用 `session or {}` —— 空 dict 是 falsy，会新建一个字典，
        # 调用方传进来的会话就丢了（BEGIN 写的 s['txn'] 传不回去，
        # 导致 BEGIN/ROLLBACK 各自变成 autocommit，回滚静默失效）。
        return self.execute(
            parsed, session if session is not None else {})

    def _dispatch(self, p: Dict, s: Dict) -> Dict:
        cmd = p['command']
        handler = getattr(
            self,
            f'_cmd_{cmd.replace(" ", "_").lower()}',
            None)
        if handler:
            return handler(p, s)
        return self._err(1064, f"不支持的语句: {cmd}")

    # ==================== 结果构造 ====================

    def _ok(self, affected: int = 0,
            message: str = "") -> Dict:
        return {
            'type': 'ok',
            'affected': affected,
            'message': message,
        }

    def _rs(self, cols, rows) -> Dict:
        return {
            'type': 'resultset',
            'columns': [str(c) for c in cols],
            'rows': [tuple(r) for r in rows],
        }

    def _err(self, code: int, msg: str) -> Dict:
        return {
            'type': 'error',
            'code': code,
            'message': msg,
        }

    # ==================== DDL ====================

    def _cmd_create_table(self, p, s):
        self.db.create_table(p['table'], p['columns'])
        return self._ok(
            message=f"Table '{p['table']}' created")

    def _cmd_drop_table(self, p, s):
        self.db.drop_table(p['table'])
        return self._ok(
            message=f"Table '{p['table']}' dropped")

    def _cmd_analyze(self, p, s):
        """ANALYZE [TABLE] t —— 收集统计信息

        解析器一直能解析 ANALYZE，但执行器没有对应 handler（返回 1064），
        导致 stats 永远为空、优化器只能一直选全表扫描。
        """
        if self.stats_manager is None:
            return self._err(1105, "统计管理器不可用")
        tbl = self.db.get_table(p['table'])
        st = self.stats_manager.analyze_table(tbl)
        try:
            self.stats_manager.save()
        except Exception as e:
            logger.warn(f"统计信息落盘失败: {e}")
        n = getattr(st, 'row_count', None)
        if n is None:
            n = tbl.row_count
        return self._ok(
            message=f"Table '{p['table']}' analyzed "
                    f"({n} rows)")

    # ==================== DML ====================

    def _cmd_insert(self, p, s):
        tbl = self.db.get_table(p['table'])
        values_list = p['values']

        # 指定列 → 补全
        if p.get('columns'):
            col_map = {
                c['name']: i
                for i, c in enumerate(tbl.cols)
            }
            expanded = []
            for v in values_list:
                row = [None] * len(tbl.cols)
                for i, cn in enumerate(p['columns']):
                    if cn in col_map:
                        row[col_map[cn]] = (
                            v[i] if i < len(v) else None)
                expanded.append(row)
            values_list = expanded

        n = 0
        # 必须把事务号传下去：原实现无条件走 txn_id=0（autocommit），
        # 导致 BEGIN; INSERT; ROLLBACK 无法回滚，且插入的锁与事务无关。
        txn = s.get('txn')
        tid = txn['txn_id'] if txn else 0
        rv = txn['read_view'] if txn else None
        for row in values_list:
            tbl.insert(row, txn_id=tid, read_view=rv)
            n += 1

        self.stats['rows'] += n
        return self._ok(
            affected=n, message=f"{n} row(s) inserted")

    def _cmd_select(self, p, s):
        table = p['table']

        # WHERE 里的字面量 IN 列表（`id IN (1,2,NULL)`）解析出来是
        # {'values': [...]}，存储层的 _match / 统一投影都不认这种形态
        # （会 KeyError 或恒假）。先统一改写成 and/or 链，并在这里处理
        # 三值逻辑（列表含 NULL 时 NOT IN 整体为 UNKNOWN）。
        if self._contains_in_list(p.get('where')):
            p = dict(p)
            p['where'] = self._rewrite_in_lists(p['where'])

        # FROM-less SELECT（`SELECT 1 AS n`）：递归 CTE 的 base 分支常见写法
        if not table:
            if p.get('no_from'):
                return self._select_no_from(p, s)
            # FROM (子查询) 别名：物化成内存关系后走「以内存关系为数据源」
            # 的既有路径（与 CTE 同一条路径）
            sub = p.get('subquery')
            if isinstance(sub, dict) and sub.get('sql'):
                return self._select_from_subquery(p, s, sub)
            return self._err(1105, "暂不支持 FROM 子查询")

        # information_schema
        if table.lower().startswith('information_schema.'):
            _, name = table.split('.', 1)
            cols, rows = self.info_schema.query(name)
            return self._rs(cols, rows)
        if table.upper() in ('TABLES', 'COLUMNS', 'SCHEMATA'):
            cols, rows = self.info_schema.query(table)
            return self._rs(cols, rows)

        # CTE：WITH 物化出来的内存关系（优先于真实表）
        rel = self._cte_relations.get(table.lower())
        if rel is not None:
            return self._select_from_relation(p, s, rel)

        # 有 JOIN → 用计划执行器
        joins = p.get('joins') or []
        if joins:
            return self._exec_join_query(p, s)

        # 单表
        if self.optimizer is not None:
            try:
                plan = self.optimizer.optimize(p)
                self._last_plan = plan
            except Exception:
                self._last_plan = None

        # WHERE 里的子查询条件（IN/NOT IN/EXISTS）先在执行器侧求值，
        # 改写成存储层认识的条件再下推（db/table.py 的 _match 只认
        # and/or/column+op+value，不认 subquery）
        has_sub = self._contains_subquery(p.get('where'))
        where = p.get('where')
        # 相关子查询不能一次性求值改写（它的真值依赖外层行），
        # 必须走逐行求值路径；非相关子查询仍走原来的「求值 + 改写」
        correlated = has_sub and self._where_has_correlated(
            where, table, p.get('table_alias'))
        if has_sub and not correlated:
            try:
                where, _ = self._rewrite_subqueries(
                    where, s, table, p.get('table_alias'))
            except Exception as e:
                return self._err(1105, f"子查询求值失败: {e}")

        tbl = self.db.get_table(table)

        txn = s.get('txn')
        if txn:
            rv = txn['read_view']
        else:
            tid = self.db.mvcc.next_txn_id()
            rv = self.db.mvcc.create_readview(tid)

        if correlated:
            return self._select_correlated(p, s, tbl, rv, table)

        # GROUP BY / DISTINCT / 窗口函数 / 带限定列名的单表查询：
        # 都要在拿到行之后再加工
        # （原实现的 DISTINCT 只在解析器里记录，执行路径从未使用，属于静默错答）
        qualified = (
            any('.' in str(c) for c in (p.get('columns') or []))
            or self._where_has_qualified(p.get('where'))
            or bool(p.get('order_by')
                    and '.' in str(p['order_by'][0])))
        # 常量投影（`SELECT 1 FROM orders`）存储层认不了，必须走投影路径
        literal = any(self._literal_value(c)[0]
                      for c in (p.get('columns') or []) if c != '*')
        if (p.get('windows') or p.get('group_by') or p.get('having')
                or p.get('distinct') or qualified or literal or has_sub):
            return self._select_advanced(p, tbl, read_view=rv, where=where)

        cols, rows = tbl.select(
            p['columns'], where,
            p.get('order_by'),
            # 存储层只认 limit；OFFSET 靠多取 offset 行再切片实现
            (int(p['limit']) + int(p.get('offset') or 0))
            if p.get('limit') is not None else None,
            read_view=rv)
        off = int(p.get('offset') or 0)
        if off:
            rows = rows[off:]
            if p.get('limit') is not None:
                rows = rows[:int(p['limit'])]
        return self._rs(cols, rows)

    # ==================== 子查询条件求值（WHERE） ====================

    # 常量条件的内部表示（沿用工程既有约定）：
    #   恒假 -> {'or': []}（空 or 列表，_match 的 any([]) / _eval_condition
    #          的 any([]) 都是 False）
    #   恒真 -> None（条件被就地删除；带 filter 的路径也都把 None 当"无过滤"）
    _ALWAYS_FALSE = {'or': []}

    @classmethod
    def _contains_subquery(cls, where) -> bool:
        """WHERE 条件里是否包含 EXISTS / IN (SELECT ...)"""
        if not isinstance(where, dict):
            return False
        if 'subquery' in where:
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._contains_subquery(c):
                    return True
        return False

    @staticmethod
    def _eq_conditions(column, values):
        """column IN (v1,v2,...) -> {'or': [{'column':c,'op':'=','value':v}, ...]}

        存储层的 _match 只有单值比较，没有 IN，因此必须展开成等值 or 链。
        """
        return {'or': [{'column': column, 'op': '=', 'value': v}
                       for v in values]}

    @classmethod
    def _collect_agg_exprs(cls, having) -> list:
        """收集 HAVING 里的聚合表达式（形如 COUNT(*) / SUM(amount)）"""
        found = []
        if isinstance(having, dict):
            if 'agg' in having:
                a = str(having['agg']).strip()
                if a and a not in found:
                    found.append(a)
            for key in ('and', 'or'):
                for c in (having.get(key) or []):
                    for a in cls._collect_agg_exprs(c):
                        if a not in found:
                            found.append(a)
        return found

    def _rewrite_subqueries(self, where, session, outer_table=None,
                            outer_alias=None, depth=0):
        """把带子查询的条件改写成可下推的条件

        返回值 (条件, 是否常量真)：

        * EXISTS / NOT EXISTS：先执行一次子查询，非空即真（沿用
          plan_executor._eval_subquery_exists 的语义），再改写成常量
          —— 真 -> (None, True)，假 -> ({'or': []}, False)。
        * col IN (SELECT ...)：执行子查询取第一列的值集合 v，改写成
          `col = v1 OR col = v2 OR ...`；空集合时 IN 恒假 → {'or': []}。
        * col NOT IN (SELECT ...)：同样取集合后改写成
          `col != v1 AND col != v2 AND ...`；空集合时 NOT IN 恒真 → None。

        相关子查询（引用外层列）不在支持范围内，遇到直接报错，
        避免"看起来有结果其实是错答案"。
        """
        if not isinstance(where, dict):
            return where, False
        if depth > 8:
            raise ValueError("子查询嵌套过深（>8 层）")

        if 'and' in where:
            out = []
            for c in where['and']:
                rc, always_true = self._rewrite_subqueries(
                    c, session, outer_table, outer_alias, depth)
                if rc is None or always_true:
                    continue          # 恒真：AND 里可以直接去掉
                out.append(rc)
            if not out:
                return None, True
            if len(out) == 1:
                return out[0], False
            return {'and': out}, False

        if 'or' in where:
            out = []
            for c in where['or']:
                rc, always_true = self._rewrite_subqueries(
                    c, session, outer_table, outer_alias, depth)
                if rc is None or always_true:
                    return None, True  # 有一项恒真 → 整体恒真
                out.append(rc)
            if not out:
                return self._ALWAYS_FALSE, False
            if len(out) == 1:
                return out[0], False
            return {'or': out}, False

        op = where.get('op')
        sub = where.get('subquery')

        if 'subquery' in where and sub is not None:
            self._check_correlated(sub, outer_table, outer_alias)
            res = self.execute(sub, session)
            if not isinstance(res, dict) or res.get('type') == 'error':
                msg = (res or {}).get('message', '未知错误')
                raise ValueError(f"子查询执行失败: {msg}")
            rows = res.get('rows') or []

            if op in ('EXISTS', 'NOT EXISTS'):
                exists = len(rows) > 0
                if op == 'NOT EXISTS':
                    exists = not exists
                if exists:
                    return None, True
                return self._ALWAYS_FALSE, False

            if op in ('IN', 'NOT IN'):
                column = where.get('column')
                if not isinstance(column, str) or not column:
                    raise ValueError("IN 子查询缺少左值列")
                values, seen = [], set()
                for r in rows:
                    if not r:
                        continue
                    k = (type(r[0]).__name__, r[0])
                    if k in seen:
                        continue
                    seen.add(k)
                    values.append(r[0])

                # NULL 三值语义：
                #   结果集含 NULL 时 `c NOT IN (...)` 整体为 UNKNOWN
                #   → 恒不通过（原实现会写成 `c != NULL` 之类的错误条件）；
                #   `c IN (...)` 里 NULL 匹配不了任何值（连 NULL 左值也不行）
                #   → 直接把 NULL 从等值链里丢掉。
                has_null = any(v is None for v in values)
                non_null = [v for v in values if v is not None]

                neg = (op == 'NOT IN')
                if neg:
                    if has_null:
                        return self._ALWAYS_FALSE, False
                    # NOT IN 空集合恒真（含 NULL 左值，语义上也是真）
                    if not non_null:
                        return None, True
                    if len(non_null) == 1:
                        return {'column': column, 'op': '!=',
                                'value': non_null[0]}, False
                    return {'and': [{'column': column, 'op': '!=',
                                     'value': v} for v in non_null]}, False
                if not non_null:
                    return self._ALWAYS_FALSE, False
                if len(non_null) == 1:
                    return {'column': column, 'op': '=',
                            'value': non_null[0]}, False
                return self._eq_conditions(column, non_null), False

        # 兜底：结构上不像已知形态，原样返回（保持旧行为）
        rw = dict(where)
        rw.pop('subquery', None)
        return rw, False

    @staticmethod
    def _referenced_columns(where, out=None):
        """递归收集条件里引用到的列名（含有子查询）

        比较条件的右侧也可能是列（`orders.user_id = users.id`），
        带点号又没被引号包起来的一律按限定列名收集，供相关子查询检测用。
        """
        if out is None:
            out = []
        if not isinstance(where, dict):
            return out
        col = where.get('column')
        if isinstance(col, str) and col:
            out.append(col)
        val = where.get('value')
        if (isinstance(val, str) and '.' in val
                and not val.startswith(("'", '"'))
                and re.match(r'^[\w.]+$', val)):
            out.append(val)
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                Executor._referenced_columns(c, out)
        if isinstance(where.get('subquery'), dict):
            Executor._referenced_columns(where['subquery'], out)
        return out

    def _subquery_ast_columns(self, sub, root=True) -> list:
        """取子查询 AST 里出现的所有列引用（SELECT/WHERE/HAVING/JOIN ON）"""
        if not isinstance(sub, dict):
            return []
        cmds = sub.get('segments')
        if cmds:
            out = []
            for c in cmds:
                out.extend(self._subquery_ast_columns(c, root=False))
            return out

        cols = []
        for c in (sub.get('columns') or []):
            for part in str(c).split(','):
                part = part.strip()
                if not part or part == '*' or '(' in part:
                    continue
                cols.append(part)
        cols.extend(self._referenced_columns(sub.get('where')))
        cols.extend(self._referenced_columns(sub.get('having')))
        for j in (sub.get('joins') or []):
            for k in (j.get('on') or {}).values():
                if isinstance(k, str):
                    cols.append(k)
        return cols

    @staticmethod
    def _qualified_owner(text) -> str:
        """`users.id` -> 'users'；裸列名 -> ''"""
        t = str(text or '').strip().strip('`"')
        if '.' in t and re.match(r'^[\w.]+$', t):
            return t.rsplit('.', 1)[0].lower()
        return ''

    def _check_correlated(self, sub, outer_table, outer_alias=None):
        """相关子查询检测：子查询引用了外层表的列就直接报错

        判据：
        1. 列名限定符等于外层表名、且不是子查询自己的表/别名 —— 外层引用；
        2. WHERE 里的裸列名在子查询表里不存在，却在外层表里存在 —— 外层引用。
        （`SELECT 1 FROM orders`、`SELECT user_id FROM orders WHERE id > 0`
        这类自洽子查询一律放行。）
        """
        if not outer_table:
            return
        outers = {str(outer_table).lower()}
        if outer_alias:
            outers.add(str(outer_alias).lower())

        # 子查询自身可用的限定符（表名 + 别名 + JOIN 进来的表）
        inner_names = set()
        for key in ('table', 'table_alias'):
            if sub.get(key):
                inner_names.add(str(sub[key]).lower())
        for j in (sub.get('joins') or []):
            for key in ('table', 'alias'):
                if j.get(key):
                    inner_names.add(str(j[key]).lower())

        for c in self._subquery_ast_columns(sub):
            owner = self._qualified_owner(c)
            # 限定符是外层表名/别名，而子查询自己并没有同名表/别名 → 外层引用
            if owner and owner in outers and owner not in inner_names:
                raise ValueError(
                    f"不支持相关子查询（引用了外层列 {c}）")

        inner = sub.get('table')
        inner_cols = set()
        if inner:
            try:
                inner_tbl = self.db.get_table(inner)
                inner_cols = {col['name'].lower() for col in inner_tbl.cols}
            except KeyError:
                pass

        outer_cols = set()
        try:
            outer_tbl = self.db.get_table(outer_table)
            outer_cols = {col['name'].lower() for col in outer_tbl.cols}
        except KeyError:
            pass

        if not inner_cols or not outer_cols:
            return

        # 裸列名（不带限定符）只在子查询表里找不到、只在外层表里存在时，
        # 才可以断定是外层引用 —— 避免把 `SELECT id FROM orders` 这种
        # 同名自洽子查询误判成相关子查询。只看 WHERE：
        # SELECT 列表里的裸列名属于投影，不是条件。
        for c in self._referenced_columns(sub.get('where')):
            c = str(c).strip().strip('`"')
            if '.' in c:
                continue
            if c.lower() not in inner_cols and c.lower() in outer_cols:
                raise ValueError(
                    f"不支持相关子查询（列 {c} 只在子查询外的表里存在）")

    # ==================== CTE / 窗口函数 ====================

    def _execute_with_ctes(self, parsed, session):
        """物化 WITH 子句，然后执行主体语句

        CTE 存为内存关系并逐个求值，因此后面的 CTE 可以引用前面的 CTE。
        递归 CTE（`WITH RECURSIVE t AS (base UNION [ALL] recursive)`）
        走迭代定点路径，见 _run_recursive_cte。
        """
        saved = dict(self._cte_relations)
        try:
            for cte in parsed.get('ctes') or []:
                sub = cte.get('query')
                if sub is None:
                    return self._err(
                        1064, f"CTE {cte['name']} 无法解析")
                if cte.get('recursive') and self._is_recursive_body(sub):
                    res = self._run_recursive_cte(cte, session)
                else:
                    res = self.execute(sub, session)
                if res.get('type') == 'error':
                    return res
                self._cte_relations[cte['name'].lower()] = _CTERelation(
                    cte['name'],
                    res.get('columns') or [],
                    res.get('rows') or [])
            body = {k: v for k, v in parsed.items() if k != 'ctes'}
            return self.execute(body, session)
        finally:
            self._cte_relations = saved

    # 递归 CTE 的最大迭代轮数（防止 `UNION ALL` 写错导致死循环）
    MAX_CTE_ITERATIONS = 1000

    @staticmethod
    def _is_recursive_body(query) -> bool:
        """递归体的形态：`base UNION [ALL] recursive`（两个分支）"""
        return (isinstance(query, dict)
                and query.get('command') == 'UNION'
                and len(query.get('segments') or []) == 2)

    def _run_recursive_cte(self, cte, session):
        """迭代求解递归 CTE

        第 0 轮先算 base 分支；之后每一轮把 CTE 名绑定为「上一轮新增的行」
        再求值递归分支并追加，直到某轮没有新增行为止。
        `UNION` 需要跨全局去重，`UNION ALL` 原样追加（只按递归分支返回空
        来终止，因此必须依赖迭代上限兜底）。
        """
        name = cte['name']
        query = cte['query']
        segments = query['segments']
        flags = query.get('unions_all') or []
        union_all = bool(flags[0]) if flags else False

        # 第 0 轮：base 分支（此时 CTE 还不能引用自己）
        res = self.execute(segments[0], session)
        if not isinstance(res, dict) or res.get('type') == 'error':
            return self._err(
                1105, f"递归 CTE {name} 的 base 分支失败: "
                      f"{(res or {}).get('message', '未知错误')}")
        cols = res.get('columns') or []
        rows = [list(r) for r in (res.get('rows') or [])]
        seen = set(self._row_key(r) for r in rows)

        prev = rows
        iterations = 0
        while prev:
            if iterations >= self.MAX_CTE_ITERATIONS:
                return self._err(
                    1105, f"递归 CTE {name} 超过最大迭代次数 "
                          f"({self.MAX_CTE_ITERATIONS})，可能存在无限递归")
            iterations += 1

            # 关键：绑定「上一轮新增的行」，而不是全部累积结果
            self._cte_relations[name.lower()] = _CTERelation(
                name, cols, prev)
            rres = self.execute(segments[1], session)
            if not isinstance(rres, dict) or rres.get('type') == 'error':
                return self._err(
                    1105, f"递归 CTE {name} 的递归分支失败: "
                          f"{(rres or {}).get('message', '未知错误')}")
            produced = [list(r) for r in (rres.get('rows') or [])]
            if not produced:
                break

            if union_all:
                new = produced
            else:
                new = []
                for r in produced:
                    k = self._row_key(r)
                    if k not in seen:
                        seen.add(k)
                        new.append(r)
                if not new:
                    break

            rows.extend(new)
            prev = new

        return self._rs(cols, rows)

    @staticmethod
    def _row_key(row):
        """行去重用的键（list/dict 不可哈希，先转成可哈希形态）"""
        return tuple(
            v if isinstance(v, (int, float, str, bytes, type(None), bool))
            else str(v)
            for v in row)

    @staticmethod
    def _split_alias(text: str):
        """把 `expr AS alias` / `expr alias` 拆成 (expr, alias)"""
        m = re.match(r'^(.+?)\s+AS\s+[`"]?(\w+)[`"]?$',
                     text, re.IGNORECASE)
        if m:
            return m.group(1).strip(), m.group(2)
        return text.strip(), None

    @staticmethod
    def _col_index_of(cols, name: str) -> int:
        """在列名列表里定位，支持 `t.col` 与 `col AS x` 形式"""
        if name == '*':
            return -1
        expr, _ = Executor._split_alias(name)
        candidates = [name, expr]
        if '.' in expr:
            candidates.append(expr.rsplit('.', 1)[1])
        for c in candidates:
            if c in cols:
                return cols.index(c)
        return -1

    def _base_rows_and_cols(self, src, where, read_view=None):
        """取「全部列 + 过滤后的行」；src 可以是 Table 或 _CTERelation"""
        if isinstance(src, _CTERelation):
            rows = [list(r) for r in src.rows]
            if where:
                rows = [r for r in rows
                        if _StorageTable._match(src, r, where)]
            return list(src.col_names), rows
        cols, rows = src.select(['*'], where, None, None, read_view)
        return list(cols), [list(r) for r in rows]

    def _select_advanced(self, p, src, read_view=None, where=_UNSET):
        """GROUP BY / 窗口函数 / 普通投影的统一执行路径

        分组与窗口都必须先拿到「全部列的行」，因此流程固定为：
        取基础行 → （可选）分组聚合 → （可选）计算窗口 → 投影 → DISTINCT/ORDER BY/LIMIT。

        where: WHERE 子查询求值改写后的条件。默认 _UNSET 表示"调用方没改写"，
        此时用 p['where']；显式传 None 表示"条件已被改写成恒真、不再需要过滤"。
        """
        src_where = p.get('where') if where is _UNSET else where
        base_cols, rows = self._base_rows_and_cols(
            src, self._normalize_where(src_where), read_view)
        if (p.get('group_by') or p.get('having')
                or self._has_bare_aggregate(p)):
            base_cols, rows = self._group_rows(p, src, base_cols, rows)
        if p.get('windows'):
            return self._apply_windows(p, base_cols, rows)
        return self._project_rows(p, base_cols, rows)

    # 存储层 Table._aggregate 支持的聚合函数（用于识别「无 GROUP BY 聚合」）
    _AGG_FUNCS = ('COUNT', 'SUM', 'AVG', 'MIN', 'MAX')

    @classmethod
    def _has_bare_aggregate(cls, p) -> bool:
        """SELECT 列表里有聚合函数、且没写 GROUP BY（整表聚成一组）

        内存关系（CTE / FROM 子查询）路径原先直接按行投影，`SELECT
        COUNT(*) FROM (SELECT ...) x` 会输出「每行一个 NULL」——静默错答。
        这里识别出来交给 `_group_rows` 的「零分组键」形态：key=() 只产生
        一个分组，再复用存储层 `_aggregate`。

        只认存储层支持的 5 个聚合函数，避免把标量函数
        （`SELECT LOWER(name) FROM cte`）误当成聚合。
        """
        if p.get('group_by') or p.get('windows'):
            return False
        for c in (p.get('columns') or []):
            s = str(c).strip()
            if s == '*':
                continue
            m = re.match(r'^(\w+)\s*\(', cls._split_alias(s)[0].strip())
            if m and m.group(1).upper() in cls._AGG_FUNCS:
                return True
        return False

    @staticmethod
    def _strip_qualifier(text: str) -> str:
        """去掉列名上的表限定符：`o.amount` -> `amount`

        存储层的 _col_idx / _aggregate 只认裸列名，因此带表别名的
        `SUM(o.amount)`、`GROUP BY u.city` 必须归一化后才能复用它们。
        """
        return re.sub(r'\b\w+\.(\w+)', r'\1', str(text))

    def _normalize_where(self, where):
        """把 WHERE 条件里的限定列名归一化（`u.city` -> `city`）

        存储层的 Table._match 只认裸列名；只改 column 字段，
        value 原样保留（字符串常量里也可能含点）。
        """
        if not isinstance(where, dict):
            return where
        if 'and' in where:
            return {'and': [self._normalize_where(c)
                            for c in where['and']]}
        if 'or' in where:
            return {'or': [self._normalize_where(c)
                           for c in where['or']]}
        out = dict(where)
        if isinstance(out.get('column'), str):
            out['column'] = self._strip_qualifier(out['column'])
        return out

    def _where_has_qualified(self, where) -> bool:
        """WHERE 里是否出现 `u.city` 这类限定列名"""
        if not isinstance(where, dict):
            return False
        if 'and' in where:
            return any(self._where_has_qualified(c)
                       for c in where['and'])
        if 'or' in where:
            return any(self._where_has_qualified(c)
                       for c in where['or'])
        col = where.get('column')
        return isinstance(col, str) and '.' in col

    def _group_rows(self, p, src, base_cols, rows):
        """按 GROUP BY 分组并计算聚合列，返回 (分组后的列名, 分组后的行)

        原实现把 group_by 完全忽略，`SELECT dept, COUNT(*) ... GROUP BY dept`
        会退化成「全表一行」，属于静默错答。
        """
        keys = [str(k).strip() for k in (p.get('group_by') or [])]
        kidx = [self._col_index_of(base_cols, k) for k in keys]
        if any(i < 0 for i in kidx):
            raise KeyError(f"GROUP BY 列不存在: {keys}")

        agg_cols = [str(c).strip() for c in (p.get('columns') or [])
                    if re.match(r'\w+\s*\(', str(c).strip())]
        # 计算用归一化后的表达式，输出列名保留用户原样（含表别名）
        agg_eval = [self._strip_qualifier(c) for c in agg_cols]

        having = p.get('having')
        having_aggs = self._collect_agg_exprs(having)
        eval_cols = list(agg_eval)
        for a in having_aggs:
            if a not in eval_cols:
                eval_cols.append(a)

        groups, order = {}, []
        for r in rows:
            key = tuple(r[i] for i in kidx)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(r)

        out_rows = []
        for key in order:
            vals = list(key)
            agg_vals = {}
            if eval_cols:
                # 复用存储层的聚合实现（Table._aggregate）
                agg_vals = dict(zip(
                    eval_cols,
                    list(_StorageTable._aggregate(
                        src, groups[key], eval_cols))))
                vals += list(agg_vals[a] for a in agg_eval)
            if having:
                # HAVING 的聚合值单独备好：可能不在 SELECT 列表里
                # （`SELECT city FROM users GROUP BY city HAVING COUNT(*)>5`），
                # 此时 SELECT 输出里没有这一列，必须由 agg_vals 提供
                extra = {a: agg_vals.get(a) for a in having_aggs}
                if not self._eval_condition(
                        vals, list(keys) + list(eval_cols), having, extra):
                    continue
            out_rows.append(vals)
        return keys + agg_cols, out_rows

    # ==================== 条件求值（统一路径） ====================

    def _eval_condition(self, row, cols, where, agg_values=None):
        """统一路径上的 WHERE/HAVING 条件求值

        只处理分组后还要再过滤的场景（HAVING），因此实现得比
        plan_executor._eval_condition 窄：and/or/agg/普通比较。
        """
        if where is None:
            return True
        if not isinstance(where, dict):
            return True

        if 'and' in where:
            return all(self._eval_condition(row, cols, c, agg_values)
                       for c in where['and'])
        if 'or' in where:
            parts = where['or']
            if not parts:
                return False
            return any(self._eval_condition(row, cols, c, agg_values)
                       for c in parts)

        op = where.get('op')
        if 'agg' in where:
            key = str(where.get('agg', '')).strip()
            # 分组时已经算好（normalize 过、位于 cols 里的聚合列也能按名取到）
            l = None
            if agg_values is not None and key in agg_values:
                l = agg_values[key]
            else:
                i = self._col_index_of(cols, key)
                if 0 <= i < len(row):
                    l = row[i]
            r = where.get('value')
            if l is None:
                return op == '=' and r is None
            if r is None:
                return op == '!='
            try:
                if isinstance(l, (int, float)) and isinstance(r, str):
                    r = float(r) if '.' in r else int(r)
            except (TypeError, ValueError):
                pass
            if op == '=':  return l == r
            if op == '!=': return l != r
            if op == '<':  return l < r
            if op == '>':  return l > r
            if op == '<=': return l <= r
            if op == '>=': return l >= r
            return False

        col = where.get('column')
        if not isinstance(col, str):
            return True
        i = self._col_index_of(cols, col)
        if i < 0:
            return False
        l = row[i] if i < len(row) else None
        r = where.get('value')
        if l is None:
            return op == '=' and r is None
        if r is None:
            return op == '!='
        try:
            if isinstance(l, (int, float)) and isinstance(r, str):
                r = float(r) if '.' in r else int(r)
        except (TypeError, ValueError):
            pass
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

    def _project_rows(self, p, base_cols, rows):
        """把行按 SELECT 列表投影成输出列"""
        projection = p.get('projection') or [
            ('col', c) for c in (p.get('columns') or ['*'])]

        if any(k == 'col' and str(t).strip() == '*'
               for k, t in projection):
            expanded = []
            for k, t in projection:
                if k == 'col' and str(t).strip() == '*':
                    expanded.extend(('col', c) for c in base_cols)
                else:
                    expanded.append((k, t))
            projection = expanded

        out_cols, plan_items = [], []
        for kind, text in projection:
            if kind == 'win':
                continue
            expr, alias = self._split_alias(str(text))
            out_cols.append(alias or self._strip_qualifier(expr))
            is_lit, val = self._literal_value(expr)
            if is_lit:
                # 字面量列（`SELECT 1 FROM orders`）只出现在常量投影里
                plan_items.append(('const', val))
                continue
            i = self._col_index_of(base_cols, str(text))
            if i >= 0:
                plan_items.append(('col', i))
                continue
            arith = self._parse_arith(expr)
            if arith is not None:
                # 简单算术表达式（`SELECT n + 1`）：递归 CTE 的迭代步长
                # 常见写法，原实现会当成未知列静默投影成 NULL
                plan_items.append(('arith', arith))
                continue
            plan_items.append(('col', -1))   # 未知列：保持旧的 None 行为

        out_rows = []
        for r in rows:
            out = []
            for kind, spec in plan_items:
                if kind == 'const':
                    out.append(spec)
                elif kind == 'col':
                    out.append(r[spec] if 0 <= spec < len(r) else None)
                else:
                    out.append(self._eval_arith(spec, base_cols, r))
            out_rows.append(out)
        return self._finish_select(p, out_cols, out_rows)

    # 简单的二元算术表达式：`col + 1` / `salary * 2` / `a - b` / `a / b`
    _ARITH_RE = re.compile(
        r'^\s*([\w.]+|-?\d+(?:\.\d+)?)\s*([+\-*/])\s*'
        r'([\w.]+|-?\d+(?:\.\d+)?)\s*$')

    @classmethod
    def _parse_arith(cls, text):
        """识别二元算术表达式，返回 (左操作数, 运算符, 右操作数)"""
        m = cls._ARITH_RE.match(str(text or ''))
        if not m:
            return None
        return (m.group(1), m.group(2), m.group(3))

    def _eval_arith(self, spec, cols, row):
        """求值二元算术表达式；操作数取不到或为 NULL 时返回 NULL"""
        left, op, right = spec

        def operand(tok):
            if re.match(r'^-?\d+(?:\.\d+)?$', str(tok)):
                return float(tok) if '.' in str(tok) else int(tok)
            i = self._col_index_of(cols, tok)
            if i < 0 or i >= len(row):
                return _UNSET
            return row[i]

        l, r = operand(left), operand(right)
        if l is _UNSET or r is _UNSET or l is None or r is None:
            return None
        try:
            if op == '+': return l + r
            if op == '-': return l - r
            if op == '*': return l * r
            if op == '/': return l / r if r != 0 else None
        except TypeError:
            return None
        return None

    @staticmethod
    def _literal_value(text):
        """识别 SQL 字面量，返回 (是否字面量, 值)

        子查询里常见的 `SELECT 1 FROM orders` / `SELECT 'x' FROM t`
        原来会被当成列名，报 `列 1 不存在`，导致 EXISTS 子查询直接失败
        （错误信息是 err(1146)，外面看起来还像"表不存在"）。
        """
        t = str(text or '').strip()
        if not t:
            return False, None
        if re.match(r'^-?\d+$', t):
            return True, int(t)
        if re.match(r'^-?\d+\.\d+$', t):
            return True, float(t)
        if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
            return True, t[1:-1].replace("''", "'")
        if t.upper() in ('NULL',):
            return True, None
        return False, None

    def _select_from_relation(self, p, s, rel):
        """从 CTE 内存关系里查询"""
        where = p.get('where')
        if self._contains_in_list(where):
            p = dict(p)
            p['where'] = self._rewrite_in_lists(p['where'])
            where = p['where']
        if self._contains_subquery(where):
            if self._where_has_correlated(where, rel.name,
                                          p.get('table_alias')):
                return self._select_correlated(
                    p, s, rel, self._rv_of(s), rel.name)
            try:
                where, _ = self._rewrite_subqueries(
                    where, s, rel.name, p.get('table_alias'))
            except Exception as e:
                return self._err(1105, f"子查询求值失败: {e}")
            return self._select_advanced(p, rel, where=where)
        return self._select_advanced(p, rel)

    def _select_no_from(self, p, s):
        """FROM-less SELECT（`SELECT 1 AS n`）

        递归 CTE 的 base 分支常写成 `SELECT 1 AS n`，解析器现在允许没有
        FROM；执行时用「一行的空关系」承接，常量投影直接算出结果行。
        """
        return self._select_advanced(p, _CTERelation('', [], [[]]))

    def _select_from_subquery(self, p, s, sub):
        """`FROM (子查询) 别名`：把子查询物化成内存关系再执行外层查询

        实现方式（方案 A，复用 CTE 机制）：

        1. 解析并执行内层子查询，拿到 (columns, rows)。内层可以含
           GROUP BY / 聚合 / 窗口函数 / 多层嵌套子查询（嵌套时内层又会
           递归走一遍本方法）；
        2. 用别名注册一个临时 `_CTERelation`（与 WITH 物化同一条路径，
           不落盘、不建真实表，因此子查询结果允许有重复值）；
        3. 外层查询的 table 指向该别名，交给 `_select_from_relation`
           —— WHERE / GROUP BY / HAVING / ORDER BY / LIMIT+OFFSET /
           限定列名（`e.salary`）都由这条已有路径处理，
           限定符会由 `_normalize_where` / `_strip_qualifier` 归一化；
        4. finally 里注销临时关系（若同名 CTE 已存在则还原），
           避免泄漏到下一次执行。

        已知限制：
        * `FROM (子查询) x JOIN t ON ...` 暂不支持（显式报错，不静默错答）；
        * 子查询里的 `ORDER BY` 只影响物化顺序，外层不写 ORDER BY 时
          依赖该顺序（本实现保持物化顺序，与 SQLite 行为一致）。
        """
        from .parser import parser as _parser

        if p.get('joins'):
            return self._err(1105, "FROM 子查询暂不支持再 JOIN")
        alias = str(sub.get('alias') or 'sub')
        try:
            inner = _parser.parse(str(sub.get('sql') or ''))
        except SyntaxError as e:
            return self._err(1064, f"FROM 子查询语法错误: {e}")
        if not isinstance(inner, dict):
            return self._err(
                1064, f"FROM 子查询无法解析: {str(sub.get('sql'))[:80]}")

        res = self.execute(inner, s)
        if not isinstance(res, dict) or res.get('type') == 'error':
            return res if isinstance(res, dict) else self._err(
                1105, "FROM 子查询执行失败")

        key = alias.lower()
        saved = self._cte_relations.get(key, _UNSET)
        self._cte_relations[key] = _CTERelation(
            alias, res.get('columns') or [], res.get('rows') or [])
        try:
            outer = dict(p)
            outer['table'] = alias
            outer['subquery'] = None
            return self._select_from_relation(
                outer, s, self._cte_relations[key])
        finally:
            if saved is _UNSET:
                self._cte_relations.pop(key, None)
            else:
                self._cte_relations[key] = saved

    def _rv_of(self, s):
        """取当前会话的读视图（没有事务就临时建一个）"""
        txn = s.get('txn')
        if txn:
            return txn['read_view']
        tid = self.db.mvcc.next_txn_id()
        return self.db.mvcc.create_readview(tid)

    # ==================== 字面量 IN 列表（三值逻辑） ====================

    @classmethod
    def _contains_in_list(cls, where) -> bool:
        """条件里是否含字面量 IN 列表（{'op':'IN','values':[...]}）"""
        if not isinstance(where, dict):
            return False
        if 'values' in where and where.get('op') in ('IN', 'NOT IN'):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._contains_in_list(c):
                    return True
        return False

    @classmethod
    def _rewrite_in_lists(cls, where):
        """把字面量 IN 列表改写成 and/or 比较链（带上 NULL 三值语义）

        * `c IN (v1, v2, NULL)`：NULL 匹配不了任何值（连 NULL 左值也不行，
          SQL 里 `NULL IN (...)` 是 UNKNOWN），所以直接丢掉 NULL 项；
        * `c NOT IN (..., NULL)`：SQL 标准下整体为 UNKNOWN → 恒不通过；
        * 空列表：IN 恒假、NOT IN 恒真（保持既有行为）。
        """
        if not isinstance(where, dict):
            return where
        if 'and' in where:
            return {'and': [cls._rewrite_in_lists(c) for c in where['and']]}
        if 'or' in where:
            return {'or': [cls._rewrite_in_lists(c) for c in where['or']]}
        if 'values' not in where or where.get('op') not in ('IN', 'NOT IN'):
            return where

        col = where.get('column')
        neg = where.get('op') == 'NOT IN'
        raw = list(where.get('values') or [])
        has_null = any(v is None or str(v).upper() == 'NULL' for v in raw)
        values = [v for v in raw
                  if not (v is None or str(v).upper() == 'NULL')]

        if neg:
            if has_null:
                return cls._ALWAYS_FALSE
            if not values:
                return None
            if len(values) == 1:
                return {'column': col, 'op': '!=', 'value': values[0]}
            return {'and': [{'column': col, 'op': '!=', 'value': v}
                            for v in values]}
        if not values:
            return cls._ALWAYS_FALSE
        if len(values) == 1:
            return {'column': col, 'op': '=', 'value': values[0]}
        return cls._eq_conditions(col, values)

    # ==================== 相关子查询（逐外层行求值） ====================

    def _where_has_correlated(self, where, outer_table, outer_alias) -> bool:
        """WHERE 里是否存在「引用了外层列」的子查询条件"""
        if not isinstance(where, dict):
            return False
        sub = where.get('subquery')
        if isinstance(sub, dict) and self._is_correlated(
                sub, outer_table, outer_alias):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if self._where_has_correlated(c, outer_table, outer_alias):
                    return True
        return False

    def _is_correlated(self, sub, outer_table, outer_alias=None) -> bool:
        """判断子查询是否相关（引用了外层表的列）

        判据：
        1. 列名限定符等于外层表名/别名，且不是子查询自己的表/别名；
        2. WHERE 里的裸列名在子查询表里不存在、却在外层表里存在。
        `SELECT 1 FROM orders` 这类自洽子查询一律放行。
        """
        if not outer_table or not isinstance(sub, dict):
            return False
        outers = {str(outer_table).lower()}
        if outer_alias:
            outers.add(str(outer_alias).lower())

        inner_names = set()
        for key in ('table', 'table_alias'):
            if sub.get(key):
                inner_names.add(str(sub[key]).lower())
        for j in (sub.get('joins') or []):
            for key in ('table', 'alias'):
                if j.get(key):
                    inner_names.add(str(j[key]).lower())

        for c in self._subquery_ast_columns(sub):
            owner = self._qualified_owner(c)
            if owner and owner in outers and owner not in inner_names:
                return True

        inner = sub.get('table')
        inner_cols = set()
        if inner:
            try:
                inner_tbl = self.db.get_table(inner)
                inner_cols = {col['name'].lower() for col in inner_tbl.cols}
            except KeyError:
                rel = self._cte_relations.get(str(inner).lower())
                if rel is not None:
                    inner_cols = {c.lower() for c in rel.col_names}
        outer_cols = set()
        try:
            outer_tbl = self.db.get_table(outer_table)
            outer_cols = {col['name'].lower() for col in outer_tbl.cols}
        except KeyError:
            rel = self._cte_relations.get(str(outer_table).lower())
            if rel is not None:
                outer_cols = {c.lower() for c in rel.col_names}

        if not inner_cols or not outer_cols:
            return False
        for c in self._referenced_columns(sub.get('where')):
            c = str(c).strip().strip('`"')
            if '.' in c:
                continue
            if c.lower() not in inner_cols and c.lower() in outer_cols:
                return True
        return False

    def _select_correlated(self, p, s, src, read_view, outer_name):
        """相关子查询的执行路径

        不能像非相关子查询那样「一次求值 + 改写条件」，因为每个外层行的
        真值都不同。这里先一次性取出「全部列的行」（只扫一遍表），再逐行
        用作用域求值器判断条件；子查询的内层行也按子查询缓存，因此单表
        相关子查询不会每行重新打开表。
        """
        outer_alias = p.get('table_alias')
        base_cols, rows = self._base_rows_and_cols(src, None, read_view)
        cache = {}
        kept = []
        for r in rows:
            if self._eval_outer_where(r, base_cols, p.get('where'), s,
                                      outer_name, outer_alias, cache,
                                      read_view):
                kept.append(r)
        rows = kept

        if (p.get('group_by') or p.get('having')
                or self._has_bare_aggregate(p)):
            base_cols, rows = self._group_rows(p, src, base_cols, rows)
        if p.get('windows'):
            return self._apply_windows(p, base_cols, rows)
        return self._project_rows(p, base_cols, rows)

    def _eval_outer_where(self, row, cols, where, s, outer_name,
                          outer_alias, cache, read_view):
        """外层行上的条件求值（含相关/非相关子查询谓词）"""
        if where is None or not isinstance(where, dict):
            return True
        if 'and' in where:
            return all(self._eval_outer_where(
                row, cols, c, s, outer_name, outer_alias, cache, read_view)
                for c in where['and'])
        if 'or' in where:
            parts = where['or']
            if not parts:
                return False
            return any(self._eval_outer_where(
                row, cols, c, s, outer_name, outer_alias, cache, read_view)
                for c in parts)

        sub = where.get('subquery')
        op = where.get('op')
        if isinstance(sub, dict) and op in ('EXISTS', 'NOT EXISTS',
                                            'IN', 'NOT IN'):
            if self._is_correlated(sub, outer_name, outer_alias):
                return self._eval_correlated_predicate(
                    row, cols, where, s, outer_name, outer_alias,
                    cache, read_view)
            return self._eval_plain_subquery_predicate(row, cols, where, s,
                                                       cache)
        # 普通比较 / LIKE / 聚合谓词
        return self._eval_condition(row, cols, self._normalize_where(where))

    def _eval_plain_subquery_predicate(self, row, cols, where, s, cache):
        """相关 WHERE 里混进来的非相关子查询（结果按子查询缓存）"""
        sub = where['subquery']
        op = where['op']
        key = ('sub', id(sub))
        if key not in cache:
            res = self.execute(sub, s)
            if not isinstance(res, dict) or res.get('type') == 'error':
                raise ValueError(
                    f"子查询执行失败: {(res or {}).get('message', '')}")
            cache[key] = [tuple(r) for r in (res.get('rows') or [])]
        rows = cache[key]

        if op == 'EXISTS':
            return len(rows) > 0
        if op == 'NOT EXISTS':
            return len(rows) == 0
        vals = [r[0] for r in rows if r]
        lv = self._scoped_value(where.get('column'), row, cols, None)
        return self._in_predicate(lv, vals, neg=(op == 'NOT IN'))

    def _eval_correlated_predicate(self, row, cols, where, s, outer_name,
                                   outer_alias, cache, read_view):
        """相关子查询谓词求值：把外层行作为作用域，过滤一遍内层行"""
        sub = where['subquery']
        op = where['op']

        # 明确不支持的相关形态：子查询内部还有 JOIN / 分组 / 聚合
        if sub.get('joins'):
            raise ValueError("不支持相关子查询里出现 JOIN")
        if sub.get('group_by') or sub.get('having'):
            raise ValueError("不支持相关子查询里出现 GROUP BY / HAVING")
        if any(re.match(r'\w+\s*\(', str(c).strip())
               for c in (sub.get('columns') or [])):
            raise ValueError("不支持相关子查询里引用外层列做聚合")

        key = ('corr', id(sub))
        if key not in cache:
            inner = sub.get('table')
            src = self._cte_relations.get(str(inner or '').lower())
            if src is None:
                src = self.db.get_table(inner)
            cache[key] = self._base_rows_and_cols(src, None, read_view)
        icols, irows = cache[key]

        outer = {
            'names': set(),
            'cols': cols,
            'row': row,
        }
        if outer_name:
            outer['names'].add(str(outer_name).lower())
        if outer_alias:
            outer['names'].add(str(outer_alias).lower())

        inner_where = sub.get('where')
        matched = [ir for ir in irows
                   if self._eval_scoped(ir, icols, inner_where, outer)]

        if op == 'EXISTS':
            return len(matched) > 0
        if op == 'NOT EXISTS':
            return len(matched) == 0

        # IN / NOT IN：取子查询投影的第一列
        proj = (sub.get('columns') or ['*'])[0]
        is_lit, lit = self._literal_value(str(proj).strip())
        if is_lit:
            idx = -1
        else:
            idx = self._col_index_of(icols, str(proj).strip())
        vals = []
        for ir in matched:
            vals.append(lit if is_lit else
                        (ir[idx] if 0 <= idx < len(ir) else None))
        lv = self._scoped_value(where.get('column'), row, cols, outer)
        return self._in_predicate(lv, vals, neg=(op == 'NOT IN'))

    def _eval_scoped(self, row, cols, where, outer):
        """内层行上的条件求值（列名可用外层行解析）

        支持 and/or 与普通比较（比较右侧也可以引用外层列，例如
        `WHERE o.user_id = u.id`），以及字面量 IN 列表；遇到聚合谓词或
        嵌套子查询时明确报错，避免静默给出错误真值。
        """
        if where is None or not isinstance(where, dict):
            return True
        if 'and' in where:
            return all(self._eval_scoped(row, cols, c, outer)
                       for c in where['and'])
        if 'or' in where:
            parts = where['or']
            return any(self._eval_scoped(row, cols, c, outer)
                       for c in parts) if parts else False
        if 'agg' in where:
            raise ValueError("不支持相关子查询里的聚合谓词")
        if isinstance(where.get('subquery'), dict):
            raise ValueError("不支持相关子查询里再嵌套子查询")

        op = where.get('op')
        if op in ('IN', 'NOT IN') and 'values' in where:
            lv = self._scoped_value(where.get('column'), row, cols, outer)
            vals = [None if str(v).upper() == 'NULL' else v
                    for v in (where.get('values') or [])]
            return self._in_predicate(lv, vals, neg=(op == 'NOT IN'))
        if op in ('EXISTS', 'NOT EXISTS', 'IN', 'NOT IN'):
            raise ValueError("不支持相关子查询里再嵌套子查询")

        lv = self._scoped_value(where.get('column'), row, cols, outer)
        rv = where.get('value')
        # 右侧可能是外层列引用（`o.user_id = u.id`）：能解析成列就当列用，
        # 否则保持原来的字面量语义
        if isinstance(rv, str) and '.' in rv:
            ov = self._scoped_value(rv, row, cols, outer)
            if ov is not _MISSING:
                rv = ov
        if lv is _MISSING:
            return False
        return self._compare_scoped(lv, op, rv)

    @staticmethod
    def _compare_scoped(l, op, r):
        """作用域比较（与 _eval_condition 的语义保持一致）"""
        if l is None:
            return op == '=' and r is None
        if r is None:
            return op == '!='
        try:
            if isinstance(l, (int, float)) and isinstance(r, str):
                r = float(r) if '.' in r else int(r)
        except (TypeError, ValueError):
            pass
        try:
            if op == '=':  return l == r
            if op == '!=': return l != r
            if op == '<':  return l < r
            if op == '>':  return l > r
            if op == '<=': return l <= r
            if op == '>=': return l >= r
            if op == 'LIKE':
                pat = str(r).replace('%', '.*').replace('_', '.')
                return bool(re.match('^' + pat + '$', str(l)))
        except TypeError:
            return False
        return False

    def _scoped_value(self, name, row, cols, outer):
        """按作用域解析列引用

        解析顺序：外层限定名 → 内层列（裸名或内层限定名）→ 外层裸列。
        外层限定名优先是必须的：内外层都有 `id` 时（users/orders），
        `u.id` 不能解析成内层的 `id`。解析不到返回 _MISSING。
        """
        t = str(name or '').strip().strip('`"')
        if not t:
            return _MISSING
        qual = ''
        if '.' in t and re.match(r'^[\w.]+$', t):
            qual = t.rsplit('.', 1)[0].lower()

        # 1) 外层限定名（`u.id`）
        if outer is not None and qual and qual in outer['names']:
            j = self._col_index_of(outer['cols'], t.rsplit('.', 1)[1])
            if 0 <= j < len(outer['row']):
                return outer['row'][j]
            return _MISSING

        # 2) 内层列（裸名或内层限定名）
        i = self._col_index_of(cols, t)
        if 0 <= i < len(row):
            return row[i]

        # 3) 外层裸列 / 其它限定名
        if outer is not None:
            j = self._col_index_of(outer['cols'],
                                   t.rsplit('.', 1)[1] if qual else t)
            if 0 <= j < len(outer['row']):
                return outer['row'][j]
        return _MISSING

    @staticmethod
    def _in_predicate(value, values, neg=False):
        """IN / NOT IN 的三值语义（简化为真/假）

        * 左值为 NULL / 解析不到 → UNKNOWN → 不通过；
        * NOT IN 且候选里含 NULL → UNKNOWN → 不通过；
        * 否则按等值匹配。
        """
        if value is _MISSING or value is None:
            return False
        has_null = any(v is None for v in values)
        if neg and has_null:
            return False
        hit = any(v is not None and Executor._value_eq(value, v)
                  for v in values)
        return (not hit) if neg else hit

    @staticmethod
    def _value_eq(a, b):
        """类型感知等值比较（数字与数字字符串视为相等）"""
        if a is None or b is None:
            return a is None and b is None
        if type(a) is type(b):
            return a == b
        try:
            if isinstance(a, (int, float)) and isinstance(b, str):
                b = float(b) if '.' in b else int(b)
            elif isinstance(b, (int, float)) and isinstance(a, str):
                a = float(a) if '.' in a else int(a)
        except (TypeError, ValueError):
            return False
        return a == b

    def _apply_windows(self, p, base_cols, rows):
        """计算窗口列并按投影顺序拼装结果行"""
        windows = p.get('windows') or []
        projection = p.get('projection') or [
            ('col', c) for c in (p.get('columns') or ['*'])]

        # 展开 SELECT *
        if any(k == 'col' and str(t).strip() == '*'
               for k, t in projection):
            expanded = []
            for k, t in projection:
                if k == 'col' and str(t).strip() == '*':
                    expanded.extend(('col', c) for c in base_cols)
                else:
                    expanded.append((k, t))
            projection = expanded

        computed = {}
        for w in windows:
            computed[id(w)] = self._compute_window(w, base_cols, rows)

        out_cols = []
        for kind, text in projection:
            if kind == 'win':
                out_cols.append(text['alias'])
            else:
                expr, alias = self._split_alias(str(text))
                out_cols.append(alias or expr)

        out_rows = []
        for ri in range(len(rows)):
            out = []
            for kind, text in projection:
                if kind == 'win':
                    out.append(computed[id(text)][ri])
                else:
                    i = self._col_index_of(base_cols, str(text))
                    out.append(rows[ri][i] if 0 <= i < len(rows[ri])
                               else None)
            out_rows.append(out)
        return self._finish_select(p, out_cols, out_rows)

    def _finish_select(self, p, out_cols, out_rows):
        """DISTINCT / ORDER BY / LIMIT（窗口计算之后才生效）"""
        if p.get('distinct'):
            seen, deduped = set(), []
            for r in out_rows:
                key = tuple(r)
                if key not in seen:
                    seen.add(key)
                    deduped.append(r)
            out_rows = deduped

        order_by = p.get('order_by')
        if order_by:
            col, desc = order_by
            key = self._strip_qualifier(col)
            i = None
            if col in out_cols:
                i = out_cols.index(col)
            elif key in out_cols:
                i = out_cols.index(key)
            if i is not None:
                out_rows.sort(key=lambda r: (r[i] is None, r[i]),
                              reverse=bool(desc))

        # LIMIT / OFFSET：都在 ORDER BY 之后、对最终结果裁剪
        offset = int(p.get('offset') or 0)
        limit = p.get('limit')
        if limit is not None:
            out_rows = out_rows[offset:offset + int(limit)]
        elif offset:
            out_rows = out_rows[offset:]
        return self._rs(out_cols, out_rows)

    def _compute_window(self, spec, base_cols, rows):
        """计算一个窗口函数在每行上的取值

        支持 ROW_NUMBER / RANK / DENSE_RANK / LAG / LEAD /
        SUM / AVG / COUNT / MIN / MAX；有 ORDER BY 时聚合为累计值，
        无 ORDER BY 时为整个分区的值。
        """
        func = spec.get('func', '').upper()
        args = spec.get('args') or []
        pidx = [self._col_index_of(base_cols, c)
                for c in (spec.get('partition_by') or [])]
        oidx = [(self._col_index_of(base_cols, c), d)
                for c, d in (spec.get('order_by') or [])]
        arg_idx = (self._col_index_of(base_cols, args[0])
                   if args else -1)
        offset = 1
        if len(args) > 1:
            try:
                offset = int(args[1])
            except (TypeError, ValueError):
                offset = 1

        total = len(rows)
        out = [None] * total

        groups = {}
        for i in range(total):
            key = tuple(rows[i][j] for j in pidx
                        if 0 <= j < len(rows[i]))
            groups.setdefault(key, []).append(i)

        for idxs in groups.values():
            # 稳定排序：从最后一个排序键往前排，可支持多列升降序混排
            for j, desc in reversed(oidx):
                idxs.sort(
                    key=lambda i, j=j: (rows[i][j] is None, rows[i][j]),
                    reverse=bool(desc))
            n = len(idxs)
            vals = [rows[i][arg_idx]
                    if 0 <= arg_idx < len(rows[i]) else None
                    for i in idxs]

            if func == 'ROW_NUMBER':
                for pos, i in enumerate(idxs):
                    out[i] = pos + 1
            elif func in ('RANK', 'DENSE_RANK'):
                prev = object()
                rank = dense = 0
                for pos, i in enumerate(idxs):
                    cur = tuple(rows[i][j] for j, _ in oidx
                                if 0 <= j < len(rows[i]))
                    if pos == 0 or cur != prev:
                        rank = pos + 1
                        dense += 1
                        prev = cur
                    out[i] = rank if func == 'RANK' else dense
            elif func in ('LAG', 'LEAD'):
                for pos, i in enumerate(idxs):
                    q = pos - offset if func == 'LAG' else pos + offset
                    out[i] = vals[q] if 0 <= q < n else None
            elif func in ('SUM', 'AVG', 'COUNT', 'MIN', 'MAX'):
                frame = spec.get('frame')
                if frame is not None:
                    # ★ 显式帧：ROWS 按物理行取帧；RANGE 只支持默认帧
                    # （UNBOUNDED PRECEDING → CURRENT ROW，CURRENT ROW
                    # 按 peer 组解释）。帧只在聚合类窗口函数上生效；
                    # ROW_NUMBER/RANK/DENSE_RANK/LAG/LEAD 按标准忽略帧。
                    peers = (self._peer_ends(idxs, rows, oidx)
                             if str(frame.get('type') or 'ROWS').upper()
                             == 'RANGE' else None)
                    for pos, i in enumerate(idxs):
                        lo, hi = self._frame_bounds(frame, pos, n, peers)
                        seg = vals[lo:hi + 1] if hi >= lo else []
                        out[i] = self._agg_over_frame(seg, func, arg_idx)
                    continue
                acc, seen_vals = None, []
                for pos, i in enumerate(idxs):
                    v = vals[pos]
                    if func == 'COUNT':
                        if arg_idx < 0 or v is not None:
                            acc = (acc or 0) + 1
                        out[i] = acc or 0
                        continue
                    if v is not None:
                        if func == 'SUM':
                            acc = v if acc is None else acc + v
                        elif func == 'AVG':
                            seen_vals.append(v)
                            acc = sum(seen_vals) / len(seen_vals)
                        elif func == 'MIN':
                            acc = v if acc is None else min(acc, v)
                        elif func == 'MAX':
                            acc = v if acc is None else max(acc, v)
                    out[i] = acc
                if not oidx:
                    # 无 ORDER BY：整个分区同一个值
                    final = out[idxs[-1]] if idxs else None
                    for i in idxs:
                        out[i] = final
            else:
                logger.warn(f"未实现的窗口函数: {func}")
        return out

    # ==================== 窗口帧（ROWS / RANGE 语义） ====================

    @staticmethod
    def _peer_ends(idxs, rows, oidx):
        """RANGE 帧用的 peer 边界表：每个位置所属「对等行组」的末位下标

        peer = 分区内 ORDER BY 值完全相同的行（SQL 的 RANGE 按值域取帧，
        CURRENT ROW 作为结束边界表示当前对等组的最后一行）。无 ORDER BY
        时整个分区互为 peer，因此末位恒为分区最后一行。
        返回值的下标是「分区内排序位置」，与 `_frame_bounds` 的 pos 对齐。
        """
        n = len(idxs)
        if not oidx:
            return [n - 1] * n
        keys = [tuple(rows[i][j] for j, _ in oidx
                      if 0 <= j < len(rows[i])) for i in idxs]
        out = [0] * n
        last = n - 1
        for pos in range(n - 1, -1, -1):
            if pos < n - 1 and keys[pos] != keys[pos + 1]:
                last = pos
            out[pos] = last
        return out

    @staticmethod
    def _frame_bounds(frame, pos, n, peers=None):
        """把窗口帧换算成当前分区内的下标闭区间 [lo, hi]

        ROWS：严格按「分区内排序位置」取行，不做值域合并。
        RANGE（仅 UNBOUNDED PRECEDING → CURRENT ROW）：CURRENT ROW 取
        当前 peer 组的末位（peers[pos]），即对等值行整组一起进帧。
        区间不裁剪到合法范围之外，因此越界时会返回 lo > hi（空帧），
        而不是错误地退化成边界那一行。

        边界形式：UNBOUNDED PRECEDING / <n> PRECEDING / CURRENT ROW /
        <n> FOLLOWING / UNBOUNDED FOLLOWING（已在解析器里校验过组合）。
        """
        ftype = str(frame.get('type') or 'ROWS').upper()

        def to_pos(bound):
            kind, k = bound
            if kind == 'UNBOUNDED PRECEDING':
                return 0
            if kind == 'UNBOUNDED FOLLOWING':
                return n - 1
            if kind == 'CURRENT ROW':
                if ftype == 'RANGE':
                    return peers[pos] if peers else pos
                return pos
            if ftype == 'RANGE':
                # 解析器已挡掉；这里是防御性检查，避免静默按 ROWS 取帧
                raise ValueError("RANGE 帧不支持数值偏移，请显式写成 ROWS")
            if kind == 'PRECEDING':
                return pos - k
            return pos + k          # FOLLOWING

        lo = max(0, to_pos(frame['start']))
        hi = min(n - 1, to_pos(frame['end']))
        return lo, hi

    @staticmethod
    def _agg_over_frame(seg, func, arg_idx):
        """在帧内的行上求聚合值（NULL 一律跳过，符合 SQL 语义）"""
        if func == 'COUNT':
            if arg_idx < 0:          # COUNT(*)：帧内所有行
                return len(seg)
            return sum(1 for v in seg if v is not None)
        nums = [v for v in seg if v is not None]
        if not nums:
            return None
        try:
            if func == 'SUM':
                return sum(nums)
            if func == 'AVG':
                return sum(nums) / len(nums)
            if func == 'MIN':
                return min(nums)
            if func == 'MAX':
                return max(nums)
        except TypeError:
            return None
        return None

    # ==================== UNION ====================

    def _cmd_union(self, p, s):
        """UNION / UNION ALL

        按左结合逐段合并：UNION ALL 直接追加；UNION 对「已累积的整份结果」
        去重（因此同一分支内部的重复行也会被去掉），保留首次出现的顺序。
        """
        segments = p.get('segments') or []
        unions_all = p.get('unions_all') or []
        out_cols = None
        result = []

        for i, seg in enumerate(segments):
            res = self.execute(seg, s)
            if isinstance(res, dict) and res.get('type') == 'error':
                return res

            cols = res.get('columns') or []
            if out_cols is None:
                out_cols = cols
            elif cols and len(cols) != len(out_cols):
                return self._err(1222, "UNION 各分支的列数不一致")

            rows = [tuple(r) for r in (res.get('rows') or [])]
            is_all = bool(unions_all[i - 1]) if (
                i > 0 and i - 1 < len(unions_all)) else False

            if i == 0 or is_all:
                result.extend(rows)
            else:
                merged = result + rows
                seen, deduped = set(), []
                for row in merged:
                    if row not in seen:
                        seen.add(row)
                        deduped.append(row)
                result = deduped

        return self._rs(out_cols or [], result)

    def _exec_join_query(self, p, s):
        """多表 JOIN"""
        from .plan_executor import PlanExecutor

        txn = s.get('txn')
        if txn:
            rv = txn['read_view']
        else:
            tid = self.db.mvcc.next_txn_id()
            rv = self.db.mvcc.create_readview(tid)

        if self.optimizer is None:
            return self._err(1105, "优化器未初始化")

        plan = self.optimizer.optimize(p)
        self._last_plan = plan

        executor = PlanExecutor(self.db, read_view=rv)
        cols, rows = executor.execute(plan)
        return self._rs(cols, rows)

    def _cmd_update(self, p, s):
        tbl = self.db.get_table(p['table'])
        txn = s.get('txn')
        tid = txn['txn_id'] if txn else 0
        rv = txn['read_view'] if txn else None
        n = tbl.update(
            p['set'], p.get('where'),
            txn_id=tid, read_view=rv)
        self.stats['rows'] += n
        return self._ok(
            affected=n, message=f"{n} row(s) updated")

    def _cmd_delete(self, p, s):
        tbl = self.db.get_table(p['table'])
        txn = s.get('txn')
        tid = txn['txn_id'] if txn else 0
        rv = txn['read_view'] if txn else None
        n = tbl.delete(
            p.get('where'), txn_id=tid, read_view=rv)
        self.stats['rows'] += n
        return self._ok(
            affected=n, message=f"{n} row(s) deleted")

    # ==================== SHOW ====================

    def _cmd_show_tables(self, p, s):
        cols = [f"Tables_in_{s.get('database', 'default')}"]
        rows = [
            (name,) for name in sorted(self.db.tables.keys())
        ]
        return self._rs(cols, rows)

    def _cmd_show_databases(self, p, s):
        return self._rs(
            ['Database'],
            [('default',), ('information_schema',)])

    def _cmd_show_columns(self, p, s):
        tbl = self.db.get_table(p['table'])
        cols = ['Field', 'Type', 'Null', 'Key',
                'Default', 'Extra']
        rows = []
        for c in tbl.cols:
            key = ('PRI' if c.get('primary')
                   else 'UNI' if c.get('unique') else '')
            null = 'NO' if c.get('notnull') else 'YES'
            extra = ('auto_increment'
                     if c.get('primary') else '')
            rows.append((
                c['name'], c['type'], null, key,
                None, extra))
        return self._rs(cols, rows)

    def _cmd_show_stats(self, p, s):
        """SHOW STATS - 表统计"""
        cols = ['table', 'rows', 'pages', 'btree_height']
        rows = []
        for name, tbl in self.db.tables.items():
            st = tbl.stats()
            rows.append((
                name, st['rows'], st['pages'],
                st['btree_height']))
        return self._rs(cols, rows)

    # ==================== 事务 ====================

    def _cmd_begin(self, p, s):
        txn = self.db.begin()
        s['txn'] = txn
        return self._ok(
            message=f"Transaction started (id={txn['txn_id']})")

    def _cmd_commit(self, p, s):
        txn = s.pop('txn', None)
        if txn:
            self.db.commit(txn)
            self.stats['commits'] += 1
            return self._ok(
                message=f"Committed (id={txn['txn_id']})")
        return self._ok(message="Committed")

    def _cmd_rollback(self, p, s):
        txn = s.pop('txn', None)
        if txn:
            self.db.abort(txn)
            return self._ok(
                message=f"Rolled back (id={txn['txn_id']})")
        return self._ok(message="Rolled back")

    def _cmd_use(self, p, s):
        s['database'] = p['database']
        return self._ok(
            message=f"Database changed to {p['database']}")

    def _cmd_explain(self, p, s):
        """EXPLAIN"""
        inner = p.get('inner')
        if not inner or self.optimizer is None:
            return self._err(1105, "EXPLAIN 无法解析")

        plan = self.optimizer.optimize(inner)
        try:
            cols, rows, _ = self.optimizer.explain(plan)
            return self._rs(cols, rows)
        except Exception as e:
            return self._err(1105, f"EXPLAIN 失败: {e}")

    # ==================== 事件调度器 ====================

    def start_event_scheduler(self):
        """启动事件调度器（可选）"""
        if self._event_scheduler is not None:
            return
        try:
            from ..sql.event import EventScheduler
            self._event_scheduler = EventScheduler(
                str(self.db.data_dir), self)
            self._event_scheduler.start()
            logger.info("事件调度器已启动")
        except ImportError:
            logger.debug("EventScheduler 未提供，跳过")
        except Exception as e:
            logger.warn(f"事件调度器启动失败: {e}")

    def stop_event_scheduler(self):
        """停止事件调度器"""
        if self._event_scheduler is None:
            return
        try:
            self._event_scheduler.stop()
        except Exception:
            pass
        self._event_scheduler = None

    # ==================== 统计 ====================

    def get_stats(self) -> dict:
        return dict(self.stats)

    def stats_report(self) -> dict:
        return {
            **self.stats,
            'tables': len(self.db.tables),
            'has_optimizer': self.optimizer is not None,
        }