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

        # FROM 子查询：parser 能解析，但执行器尚未实现
        if not table:
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
        if has_sub:
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
            p.get('order_by'), p.get('limit'),
            read_view=rv)
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

                neg = (op == 'NOT IN')
                if neg:
                    # NOT IN 空集合恒真（含 NULL 左值，语义上也是真）
                    if not values:
                        return None, True
                    return {'and': [{'column': column, 'op': '!=',
                                     'value': v} for v in values]}, False
                if not values:
                    return self._ALWAYS_FALSE, False
                return self._eq_conditions(column, values), False

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
        递归 CTE 只保证能解析，不保证结果正确。
        """
        saved = dict(self._cte_relations)
        try:
            for cte in parsed.get('ctes') or []:
                sub = cte.get('query')
                if sub is None:
                    return self._err(
                        1064, f"CTE {cte['name']} 无法解析")
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
        if p.get('group_by') or p.get('having'):
            base_cols, rows = self._group_rows(p, src, base_cols, rows)
        if p.get('windows'):
            return self._apply_windows(p, base_cols, rows)
        return self._project_rows(p, base_cols, rows)

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

        out_cols, idx = [], []
        consts = []
        for kind, text in projection:
            if kind == 'win':
                continue
            expr, alias = self._split_alias(str(text))
            out_cols.append(alias or self._strip_qualifier(expr))
            is_lit, val = self._literal_value(expr)
            if is_lit:
                # 字面量列（`SELECT 1 FROM orders`）只出现在常量投影里
                idx.append(-1)
                consts.append((len(idx) - 1, val))
            else:
                idx.append(self._col_index_of(base_cols, str(text)))

        out_rows = []
        for r in rows:
            out = [r[i] if 0 <= i < len(r) else None for i in idx]
            for pos, val in consts:
                out[pos] = val
            out_rows.append(out)
        return self._finish_select(p, out_cols, out_rows)

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
        if self._contains_subquery(where):
            try:
                where, _ = self._rewrite_subqueries(
                    where, s, rel.name, p.get('table_alias'))
            except Exception as e:
                return self._err(1105, f"子查询求值失败: {e}")
            return self._select_advanced(p, rel, where=where)
        return self._select_advanced(p, rel)

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

        limit = p.get('limit')
        if limit:
            out_rows = out_rows[:int(limit)]
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