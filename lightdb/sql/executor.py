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
        """列名 -> 下标

        精确匹配失败后再按「列名末段」唯一匹配一次：JOIN 结果物化成的内存
        关系列名带限定前缀（`u.id` / `o.amt`），而存储层的 `_aggregate` /
        `_match` 只认裸列名（`amt`）。末段歧义时仍然报错，绝不任取一列。
        """
        for i, c in enumerate(self.cols):
            if c['name'] == name:
                return i
        want = str(name).strip().strip('`"').rsplit('.', 1)[-1].lower()
        hits = [i for i, c in enumerate(self.cols)
                if str(c['name']).strip().strip('`"')
                .rsplit('.', 1)[-1].lower() == want]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise KeyError(f"列 {name} 歧义，请加表名前缀")
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

    # ★ GROUP BY 严格模式开关（逃生口，与 MySQL 的 ONLY_FULL_GROUP_BY 等价）
    #   None（默认）-> 由环境变量 LIGHTDB_STRICT_GROUP_BY 决定，未设置即**严格**
    #   False       -> 关闭严格模式，恢复旧行为（非分组键裸列取「组内第一行」）
    #   True        -> 强制严格
    # 用法（类属性，对所有实例生效）：
    #   from lightdb.sql.executor import Executor
    #   Executor.strict_group_by = False        # 旧行为
    # 或进程级：set LIGHTDB_STRICT_GROUP_BY=0
    strict_group_by = None

    def _check_strict_group_by(self, p):
        """GROUP BY 严格校验（静态，见 `parser.validate_group_by`）

        校验只吃解析后的 AST，不需要任何行数据，因此挂在 `_cmd_select`
        的最前面：单表 / JOIN / CTE / FROM 子查询 / UNION 各分支都在那里
        汇合，报错必然发生在真正取数之前（不会先算一半再失败）。

        统一路径（`_group_rows`）与计划路径（`plan_executor._exec_GroupBy`）
        还会各自再兜一次底，兜底用的是同一份实现，报错信息完全一致。
        """
        from .parser import validate_group_by, resolve_strict_group_by
        validate_group_by(
            p, strict=resolve_strict_group_by(self.strict_group_by))

    def execute(self, parsed: Dict, session: Dict) -> Dict:
        start = time.time()
        # 记录"当前会话"：投影列表里的标量子查询需要把折叠后的子查询
        # 重新交给 self.execute 执行（复用事务读视图 / CTE 关系）。
        # 有些内部路径（FROM 子查询物化、JOIN 计划执行）拿不到 session，
        # 由这里兜底，避免子查询在无事务上下文里重新执行。
        prev_session = getattr(self, '_active_session', None)
        self._active_session = session
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
        finally:
            self._active_session = prev_session

    def _use_session(self, session):
        """取本次执行使用的会话（显式传参优先，其次 execute() 记录的会话）"""
        if isinstance(session, dict):
            return session
        active = getattr(self, '_active_session', None)
        return active if isinstance(active, dict) else {}

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
        # ★ GROUP BY 严格校验放在最前面（静态判定，见 _check_strict_group_by）
        self._check_strict_group_by(p)
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

        # 外层引用无法无歧义解析（外层未起别名、子查询里又有同名表被别名
        # 遮蔽）时显式报错：这种形态原来会被判成"非相关子查询"，一次性
        # 求值成空集后静默返回 0 行，属于会误导使用者的错答。
        amb = self._ambiguous_outer_ref(
            p.get('where'), table, p.get('table_alias'))
        if amb:
            return self._err(1062, self._ambiguous_outer_ref_message(
                amb, table, p.get('table_alias')))

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
        # 必须走逐行求值路径；非相关 EXISTS / IN 子查询仍走原来的
        # 「求值 + 改写」路径（它们是一次性集合语义，改写后能继续下推）。
        # **标量比较谓词**（`(SELECT ...) = 5`）同样必须逐外层行求值：
        # 它需要按行数语义收敛（0 行 → NULL、1 行 1 列 → 取值、
        # 多行/多列 → 显式报错），一次性改写无法表达这套语义。
        # 原实现让这种谓词掉进 _rewrite_subqueries 的兜底分支，把 subquery
        # 直接丢掉，产出没有左值的条件后报出误导性的 err 1146「缺少:
        # column」；逐行路径按折叠值缓存，非相关标量子查询只执行一次。
        correlated = has_sub and (
            self._where_has_correlated(where, table, p.get('table_alias'))
            or self._where_has_scalar_subquery(where))
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
        # 投影列表里的算术表达式（`SELECT salary*2 AS d`）：存储层只认
        # 列名，直接下推会报「列 salary*2 不存在」，必须走投影路径由
        # _project_rows 的 _parse_arith / _eval_arith 求值
        arith = any(self._arith_projection_expr(c) is not None
                    for c in (p.get('columns') or []) if c != '*')
        # 投影列表里的标量子查询（`SELECT id, (SELECT COUNT(*) ...) AS cnt`）
        # ——必须走 _project_rows 的逐行求值路径，否则要么被存储层当未知列
        # （视有无点号）静默填 NULL，要么直接报"列不存在"。
        proj_sub = any(
            self._projection_subquery_ast(self._split_alias(str(c))[0])[0]
            is not None
            for c in (p.get('columns') or []) if c != '*')
        # 投影里的标量函数（`SELECT LOWER(name)`）：存储层 select 用
        # `\w+\s*\(` 判断"是否有聚合"，于是把标量函数也交给 _aggregate
        # —— 非聚合项取「第一行的值」，实测 `SELECT id, LOWER(name) FROM
        # users` 静默返回 1 行 (1, None)（NULL + 丢行），是同类错答。
        # 这里强制走投影路径，由 `_projection_item_plan` 显式报「列不存在」
        # （引擎没有标量函数求值能力），不再静默给出错答。
        scalar_func = any(
            self._is_scalar_function_projection(c)
            for c in (p.get('columns') or []) if c != '*')
        # WHERE 里的 NOT (...)：存储层 _match 只认 and/or/column+op+value，
        # 遇到 {'not': ...} 直接 KeyError，必须走统一求值路径
        has_not = self._where_has_not(p.get('where'))
        has_null = self._where_has_is_null(p.get('where'))
        # 归一化后仍残留 negate 标记（LIKE 取反等存储层无法表达的形态）：
        # `_base_rows_and_cols` 会在取到行之后自己做三值过滤
        if self._where_has_negate(self._normalize_where(where)):
            has_not = True
        if (p.get('windows') or p.get('group_by') or p.get('having')
                or p.get('distinct') or qualified or literal or has_sub
                or arith or proj_sub or scalar_func or has_not or has_null):
            return self._select_advanced(
                p, tbl, read_view=rv, where=where, session=s)
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
        """WHERE 条件里是否包含 EXISTS / IN (SELECT ...) / 标量子查询"""
        if not isinstance(where, dict):
            return False
        if 'subquery' in where:
            return True
        inner = where.get('not')
        if isinstance(inner, dict) and cls._contains_subquery(inner):
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
            # NOT (...) 里也可能藏着聚合（`HAVING NOT (COUNT(*) > 1)`）
            inner = having.get('not')
            if isinstance(inner, dict):
                for a in cls._collect_agg_exprs(inner):
                    if a not in found:
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
          子查询返回**多于 1 列**时显式报错（行值 IN 不支持，绝不只认
          第一列 —— 见 `_guard_in_subquery_width`）。
        * col NOT IN (SELECT ...)：同样取集合后改写成
          `col != v1 AND col != v2 AND ...`；空集合时 NOT IN 恒真 → None。

        相关子查询（引用外层列）：本方法没有外层行可折叠，遇到直接报错，
        避免"看起来有结果其实是错答案"。真正需要逐外层行求值的场景走
        `_select_correlated` → `_eval_correlated_predicate`（外层列常量折叠
        + 复用正常执行路径）。
        """
        if not isinstance(where, dict):
            return where, False
        if depth > 8:
            raise ValueError("子查询嵌套过深（>8 层）")

        # NOT (...)：整体取反。恒真/恒假的判定要跟着翻过来。
        if 'not' in where:
            inner = where['not']
            if self._contains_subquery(inner):
                rc, always_true = self._rewrite_subqueries(
                    inner, session, outer_table, outer_alias, depth)
                if rc is None or always_true:
                    return self._ALWAYS_FALSE, False
                if rc == self._ALWAYS_FALSE:
                    return None, True
            return {'not': rc if rc is not None else self._ALWAYS_FALSE}, False

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
                    # 左值不是单列（行值 IN `(a,b) IN (...)` 之类）：
                    # 本引擎不支持行值比较，显式报错，绝不猜一个左值。
                    raise ValueError(
                        "IN / NOT IN 的左侧必须是单列：本引擎不支持行值"
                        "（多列）比较，无法安全推断出参与比较的列")
                # 集合上下文同样只允许**一列**：多列一律显式报错（静态
                # 列数 + 实际列数双判定，空结果集也拦得住），绝不静默
                # 只取每行第一列 —— 见 `_guard_in_subquery_width`。
                self._guard_in_subquery_width(
                    sub, 'WHERE/HAVING 里的', res_cols=res.get('columns'),
                    rows=rows)
                # IN 是**集合语义**（允许多行），每行取唯一一列做候选值；
                # 多列形态已在上一行被显式拒绝（行值 IN 不支持）。
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

        # 兜底：带 subquery 但不是 EXISTS/IN，也不是标量比较（正常情况
        # 下列子查询已在 _cmd_select / _select_from_relation 里被路由到
        # 逐行求值路径，走不到这里）。原实现把 subquery 直接丢掉，产出
        # `{'op': '=', 'value': '10'}` 这种没有左值的条件，下游报
        # 「缺少: 'column'」（误导性错误），还可能把条件静默放过。
        # 显式报错，绝不静默丢条件。
        if isinstance(sub, dict):
            raise ValueError(
                f"无法求值的子查询谓词（op={op}）：该形态没有一次性求值的语义")
        # 结构上不像已知形态，原样返回（保持旧行为）
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
        """在列名列表里定位，支持 `t.col` 与 `col AS x` 形式

        JOIN 路径产出的列名带限定前缀（`u.id` / `o.amt`），因此精确匹配全部
        失败后，再按「列名末段」唯一匹配一次（`amt` → `o.amt`）。末段歧义
        （`u.id` 与 `o.id` 并存时的裸 `id`）仍返回 -1，由调用方显式报错，
        绝不随便挑一列 —— 与计划执行器 `_resolve_col` 的语义保持一致，
        这样 JOIN 的后置处理路径与纯计划路径对裸列名的解析结果相同。
        """
        if name == '*':
            return -1
        expr, _ = Executor._split_alias(name)
        candidates = [name, expr]
        if '.' in expr:
            candidates.append(expr.rsplit('.', 1)[1])
        for c in candidates:
            if c in cols:
                return cols.index(c)
        want = str(expr or name).strip().strip('`"').rsplit('.', 1)[-1].lower()
        if not want:
            return -1
        hits = [i for i, c in enumerate(cols)
                if isinstance(c, str)
                and str(c).strip().strip('`"').rsplit('.', 1)[-1].lower()
                == want]
        if len(hits) == 1:
            return hits[0]
        return -1

    def _base_rows_and_cols(self, src, where, read_view=None):
        """取「全部列 + 过滤后的行」；src 可以是 Table 或 _CTERelation

        条件里若残留 `{'negate': ...}`（NOT 作用在 LIKE 这类存储层没有
        取反运算符的形态上），先取全部行再用执行器自己的统一求值器过滤
        —— 三值语义（NOT UNKNOWN 仍是 UNKNOWN）也由它负责，
        不会因为 `NOT (c LIKE ...)` 把 NULL 行错误地放行。
        """
        if isinstance(where, dict) and 'negate' in where \
                and isinstance(where['negate'], dict):
            target = where['negate']
            if isinstance(src, _CTERelation):
                cols = list(src.col_names)
                rows = [list(r) for r in src.rows]
            else:
                cols, rows = src.select(['*'], None, None, None, read_view)
                cols, rows = list(cols), [list(r) for r in rows]
            kept = []
            for r in rows:
                # 三值逻辑：NOT 只在「内层确定为假」时为真；
                # 内层为 None（UNKNOWN，例如 `NULL LIKE 'A%'`）时不通过，
                # 与 `is False` 的判定天然一致
                if self._eval_condition(r, cols, target) is False:
                    kept.append(r)
            return cols, kept
        if isinstance(src, _CTERelation):
            rows = [list(r) for r in src.rows]
            if where:
                rows = [r for r in rows
                        if _StorageTable._match(src, r, where)]
            return list(src.col_names), rows
        cols, rows = src.select(['*'], where, None, None, read_view)
        return list(cols), [list(r) for r in rows]

    def _select_advanced(self, p, src, read_view=None, where=_UNSET,
                         session=None):
        """GROUP BY / 窗口函数 / 普通投影的统一执行路径

        分组与窗口都必须先拿到「全部列的行」，因此流程固定为：
        取基础行 → （可选）分组聚合 → （可选）计算窗口 → 投影 → DISTINCT/ORDER BY/LIMIT。

        where: WHERE 子查询求值改写后的条件。默认 _UNSET 表示"调用方没改写"，
        此时用 p['where']；显式传 None 表示"条件已被改写成恒真、不再需要过滤"。
        session: 投影里的标量子查询要重新执行（折叠后）子查询，需要会话
        （事务读视图）；拿不到时 `_project_rows` 会退回 `_active_session`。
        """
        src_where = p.get('where') if where is _UNSET else where
        base_cols, rows = self._base_rows_and_cols(
            src, self._normalize_where(src_where), read_view)
        if (p.get('group_by') or p.get('having')
                or self._has_bare_aggregate(p)):
            base_cols, rows = self._group_rows(
                p, src, base_cols, rows, session=session,
                read_view=read_view)
        if p.get('windows'):
            return self._apply_windows(p, base_cols, rows, session=session,
                                       read_view=read_view)
        return self._project_rows(p, base_cols, rows, session=session,
                                  read_view=read_view,
                                  outer_name=p.get('table'),
                                  outer_alias=p.get('table_alias'))

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

    def _normalize_where(self, where, strip=True):
        """把 WHERE 条件里的限定列名归一化（`u.city` -> `city`）

        存储层的 Table._match 只认裸列名；只改 column 字段，
        value 原样保留（字符串常量里也可能含点）。

        `NOT (...)` 存储层完全不认识（`_match` 只认 and/or/column+op+value，
        遇到 {'not':...} 会 KeyError），因此这里把它改写成等价的德摩根形式：
            NOT (A AND B)  ->  OR(NOT A, NOT B)
            NOT (A OR  B)  ->  AND(NOT A, NOT B)
            NOT (col = v)  ->  col != v
        叶子比较一律**先取反再交给存储层**（not op），
        这样 `_base_rows_and_cols` 传给存储层的条件永远是它能认的形态，
        既避免了 KeyError，也让 NOT 条件能享受存储层的索引/下推，
        不必把整表读进内存再过滤。

        strip=False：**JOIN 后置处理路径**里保留限定列名不动。连接结果的
        列名本身带前缀（`u.id` / `o.amt`），一旦归一化成裸名就会歧义
        （`id` 同时属于两张表）甚至解析失败；该路径不经过存储层
        `_match`（行已经在内存里），因此不需要归一化。
        """
        if not isinstance(where, dict):
            return where
        if isinstance(where.get('not'), dict):
            return self._negate_condition(
                self._normalize_where(where['not'], strip))
        if 'and' in where:
            return {'and': [self._normalize_where(c, strip)
                            for c in where['and']]}
        if 'or' in where:
            return {'or': [self._normalize_where(c, strip)
                           for c in where['or']]}
        out = dict(where)
        if strip and isinstance(out.get('column'), str):
            out['column'] = self._strip_qualifier(out['column'])
        return out

    # 比较运算符的取反表（>= / <= 的取反要保留等号）
    _NOT_OP = {
        '=': '!=', '!=': '=', '<': '>=', '>': '<=',
        '<=': '>', '>=': '<',
    }

    @classmethod
    def _negate_condition(cls, where):
        """对**已归一化**的条件取反（德摩根律），产出存储层认识的条件"""
        if not isinstance(where, dict):
            return where
        if 'and' in where:
            return {'or': [cls._negate_condition(c) for c in where['and']]}
        if 'or' in where:
            return {'and': [cls._negate_condition(c) for c in where['or']]}
        if isinstance(where.get('not'), dict):
            return cls._negate_condition(where['not'])
        op = where.get('op')
        val = where.get('value')
        if 'column' in where and op in cls._NOT_OP:
            # `NOT (c IS NULL)` 即 `c IS NOT NULL`：value=None 保持不变，
            # 只翻转运算符（=' -> '!=、'!=' -> '='），NULL 语义自动对齐
            return {'column': where.get('column'),
                    'op': cls._NOT_OP[op],
                    'value': val}
        if 'agg' in where and op in cls._NOT_OP:
            return {'agg': where.get('agg'), 'op': cls._NOT_OP[op],
                    'value': val}
        if op in ('IN', 'NOT IN'):
            # IN 列表 / IN (子查询) 直接翻转 IN ↔ NOT IN：三值语义由
            # _in_predicate / _rewrite_in_lists 负责，含 NULL 时同样正确
            return {'column': where.get('column'),
                    'op': 'NOT IN' if op == 'IN' else 'IN',
                    'values': where.get('values'),
                    'subquery': where.get('subquery')}
        # 其它形态（LIKE 等）存储层没有取反运算符：打上 negate 标记，
        # 由 `_base_rows_and_cols` 取全部行后用统一求值器做三值过滤
        return {'negate': dict(where)}

    def _where_has_qualified(self, where) -> bool:
        """WHERE 里是否出现 `u.city` 这类限定列名"""
        if not isinstance(where, dict):
            return False
        if 'not' in where:
            return self._where_has_qualified(where['not'])
        if 'and' in where:
            return any(self._where_has_qualified(c)
                       for c in where['and'])
        if 'or' in where:
            return any(self._where_has_qualified(c)
                       for c in where['or'])
        col = where.get('column')
        return isinstance(col, str) and '.' in col

    @classmethod
    def _where_has_not(cls, where) -> bool:
        """WHERE 里是否出现 `NOT (...)` 节点

        存储层 `Table._match` 只认 and/or/column+op+value，遇到
        `{'not': ...}` 会直接 KeyError（外层看起来像"表不存在"）。
        只要出现 NOT 就让查询走统一求值路径 `_eval_condition`。
        """
        if not isinstance(where, dict):
            return False
        if isinstance(where.get('not'), dict):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._where_has_not(c):
                    return True
        return False

    @classmethod
    def _where_has_negate(cls, where) -> bool:
        """条件里是否残留 negate 标记（存储层无法表达的 NOT 取反）

        例如 `NOT (name LIKE 'A%')`：LIKE 没有取反运算符，_negate_condition
        只能标成 {'negate': {...}}，由 _base_rows_and_cols 用执行器自己的
        三值求值器过滤。
        """
        if not isinstance(where, dict):
            return False
        if isinstance(where.get('negate'), dict):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._where_has_negate(c):
                    return True
        return False

    @classmethod
    def _where_has_is_null(cls, where) -> bool:
        """WHERE 里是否出现字面量 NULL 比较（`col IS NULL` / `IS NOT NULL`）

        解析器把 IS [NOT] NULL 编码成 value=None 的 =/!= 条件。存储层
        `Table._match` 虽然能求值，但 `_find_pks` 会用
        `coerce(None, col_type)` 走索引/主键快路径，结果把 NULL 当成
        默认值（`amount IS NULL` 返回 0 行、`IS NOT NULL` 返回全部行）。
        因此含字面量 NULL 的条件统一走执行器的统一求值路径。
        """
        if not isinstance(where, dict):
            return False
        if 'not' in where:
            return cls._where_has_is_null(where['not'])
        if 'value' in where and where.get('value') is None \
                and 'column' in where:
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._where_has_is_null(c):
                    return True
        return False

    @classmethod
    def _arith_projection_expr(cls, text):
        """投影项是否是算术表达式（可带 AS 别名），是则返回表达式文本

        `salary*2 AS d` → 'salary*2'；`id` → None；`2*salary` → '2*salary'。
        要求至少一侧是列名，纯常量算式（`SELECT 1+2`）不在此列（走常量投影）。
        """
        expr, _ = cls._split_alias(str(text))
        spec = cls._parse_arith(expr)
        if spec is None:
            return None
        left, _, right = spec
        if not (cls._is_plain_literal(left) and cls._is_plain_literal(right)):
            return expr.strip()
        return None

    @staticmethod
    def _is_plain_literal(tok) -> bool:
        """操作数是否是数字字面量（`2` / `-1.5`）"""
        return bool(re.match(r'^-?\d+(?:\.\d+)?$', str(tok)))

    def _group_rows(self, p, src, base_cols, rows, session=None,
                    read_view=None, extra_names=None):
        """按 GROUP BY 分组并计算聚合列，返回 (分组后的列名, 分组后的行)

        原实现把 group_by 完全忽略，`SELECT dept, COUNT(*) ... GROUP BY dept`
        会退化成「全表一行」，属于静默错答。

        session / read_view：HAVING 里的子查询需要（重新）执行子查询，
        必须沿用同一个事务读视图；拿不到时由 `_use_session` 兜底。
        extra_names：JOIN 后置处理路径传入的连接表名/别名，供 HAVING 里的
        子查询折叠使用（见 `_eval_having_with_subquery`）。
        """
        # ★ 严格模式运行期兜底：非分组键裸列在下面「分组后只剩 keys+聚合值」
        # 的形态里会被静默丢掉（随后 `_project_rows` 报「列不存在」，完全
        # 看不出是 GROUP BY 的问题）。这里先按同一份静态校验显式报错，
        # 保证两条执行路径的报错信息逐字一致；逃生口见 strict_group_by。
        from .parser import validate_group_by, resolve_strict_group_by
        validate_group_by(
            p, strict=resolve_strict_group_by(self.strict_group_by))

        keys = [str(k).strip() for k in (p.get('group_by') or [])]
        kidx = [self._col_index_of(base_cols, k) for k in keys]
        if any(i < 0 for i in kidx):
            raise KeyError(f"GROUP BY 列不存在: {keys}")

        agg_cols = [str(c).strip() for c in (p.get('columns') or [])
                    if re.match(r'\w+\s*\(', str(c).strip())]
        # 计算用归一化后的表达式，输出列名保留用户原样（含表别名）
        agg_eval = [self._strip_qualifier(c) for c in agg_cols]

        having = p.get('having')
        # HAVING 里的聚合同样要归一化限定符：`HAVING SUM(o.amount) > 25`
        # 里的 `SUM(o.amount)` 交给存储层 `_aggregate` 前必须变成
        # `SUM(amount)`，否则报「列 SUM(o.amount) 不存在」。
        # 归一化前后用同一套键查 agg_vals：eval_cols 存归一化名，
        # having_aggs 保留原样，下面按归一化名建 extra 映射。
        having_aggs = self._collect_agg_exprs(having)
        having_aggs_eval = [self._strip_qualifier(a) for a in having_aggs]
        eval_cols = list(agg_eval)
        for a in having_aggs_eval:
            if a not in eval_cols:
                eval_cols.append(a)
        # HAVING 里有子查询 → 每个分组行单独求值（分组键作为外层行折叠），
        # 结果按折叠值缓存；没有子查询时仍走原来的 _eval_condition 快路径
        having_has_sub = self._contains_subquery(having)
        having_cache = {}

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
                # 此时 SELECT 输出里没有这一列，必须由 agg_vals 提供。
                # 键用用户原样的表达式（_eval_condition 按原样查表）
                extra = {a: agg_vals.get(e)
                         for a, e in zip(having_aggs, having_aggs_eval)}
                hcols = list(keys) + list(eval_cols)
                if having_has_sub:
                    # GROUP BY 之后的行只剩分组键 + 聚合值：子查询里的
                    # 外层引用只能折叠成「分组键」的取值（引用别的列要
                    # 显式报错，见 _eval_having_with_subquery）
                    if not self._eval_having_with_subquery(
                            vals, hcols, having, extra, p, session,
                            read_view, having_cache, extra_names):
                        continue
                elif not self._eval_condition(vals, hcols, having, extra):
                    continue
            out_rows.append(vals)
        return keys + agg_cols, out_rows

    def _eval_having_with_subquery(self, row, cols, where, agg_values, p,
                                   session, read_view, cache,
                                   extra_names=None):
        """HAVING 里带子查询的条件求值（逐分组行）

        HAVING 在 GROUP BY 之后求值，此时「外层行」是分组行：列只有
        分组键 + 聚合值。因此：

        * `{'and'/'or'/'not'}` 递归处理；
        * 不含子查询的叶子节点交给 `_eval_condition`（聚合谓词靠
          agg_values 取聚合值）；
        * 含子查询的叶子节点：
          - 相关（引用外层/分组键）→ `_eval_correlated_predicate`，
            把分组键取值折叠成字面量后执行子查询；
          - 非相关 → `_eval_outer_where` 的非相关分支（EXISTS/IN 一次性
            求值；标量比较走 `_eval_scalar_subquery`，整体只算一次）；
        * 引用不到分组键的列（例如 `HAVING (SELECT ... WHERE o.uid = u.id)`
          而 GROUP BY 只有 city）→ 显式报错。原实现直接 `return True`，
          把 HAVING 条件静默丢掉（实测所有分组都保留）。

        缓存：按折叠用到的分组键取值缓存子查询结果（同值分组共享一次
        执行），因此分组很多时也不会重复跑子查询。

        extra_names：JOIN 后置处理路径传入的连接表名/别名（外层行由多表
        连接而来），相关判定与折叠都要把它们算作外层限定符。
        """
        if not isinstance(where, dict):
            return True
        # NOT (...)：三值逻辑下 NOT UNKNOWN 仍是 UNKNOWN → 该分组不通过
        if 'not' in where and isinstance(where['not'], dict):
            inner = self._eval_having_with_subquery(
                row, cols, where['not'], agg_values, p, session,
                read_view, cache, extra_names)
            if inner is None:
                return None
            return not inner
        if 'and' in where:
            return all(self._eval_having_with_subquery(
                row, cols, c, agg_values, p, session, read_view, cache,
                extra_names)
                for c in where['and'])
        if 'or' in where:
            parts = where['or']
            if not parts:
                return False
            return any(self._eval_having_with_subquery(
                row, cols, c, agg_values, p, session, read_view, cache,
                extra_names)
                for c in parts)

        sub = where.get('subquery')
        if not isinstance(sub, dict):
            # 普通列比较 / 聚合谓词：沿用统一求值器
            return self._eval_condition(row, cols, where, agg_values)

        outer_name = p.get('table')
        outer_alias = p.get('table_alias')
        session = self._use_session(session)
        correlated = (
            self._is_correlated(sub, outer_name, outer_alias, extra_names)
            or self._sub_owner_is_outer(sub, outer_name, outer_alias,
                                        extra_names))
        if correlated:
            # 先做「外层引用能否在分组行里取到值」的前置检查：取不到时
            # 给出比折叠失败更明确的信息（GROUP BY 之后只保留分组键）
            names = self._fold_outer_names(sub, outer_name, outer_alias,
                                           extra_names)
            missing = [
                c for c in self._outer_ref_columns(sub, names)
                if self._outer_column_value(cols, row, c) is _MISSING]
            if missing:
                raise ValueError(
                    f"HAVING 里的相关子查询引用了 '{missing[0]}'，"
                    f"但 GROUP BY 之后只保留分组键与聚合值，取不到该列："
                    f"请把该列加入 GROUP BY，或改成引用分组键")
            return self._eval_correlated_predicate(
                row, cols, where, session, outer_name, outer_alias,
                cache, read_view, extra_names)
        # 非相关子查询：EXISTS / IN 走集合语义，标量比较按行数语义收敛，
        # 两者都按子查询缓存（整体只求值一次）。`_eval_outer_where` 里还会
        # 做一次「外层引用藏在表达式里」的残留检查（命中即显式报错）。
        return self._eval_outer_where(
            row, cols, where, session, outer_name, outer_alias,
            cache, read_view, extra_names)

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

        # NOT (...)：整体取反（可嵌套，可与 AND/OR 组合）
        if 'not' in where and isinstance(where['not'], dict):
            return not self._eval_condition(
                row, cols, where['not'], agg_values)

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

        # ---------- IN / NOT IN（集合上下文）----------
        # 字面量列表 `c IN (1,2,NULL)`：按三值语义求值。
        # **原实现没有这个分支**，条件会掉进下面「r = where.get('value')
        # → None → return op == '!='」的普通比较逻辑；op 是 'IN' 于是恒返回
        # False —— 实测 `... GROUP BY uid HAVING uid IN (1,2)` 静默返回 0 个
        # 分组（WHERE 里的字面量列表在 `_cmd_select` / `_select_from_relation`
        # 已被 `_rewrite_in_lists` 改写成 and/or 链，因此只有 HAVING 位置
        # 暴露了这个静默错答）。
        # 子查询形态（`{'subquery': ...}`）不该走到这里：WHERE 由
        # `_rewrite_subqueries` / `_eval_outer_where` 处理，HAVING 由
        # `_eval_having_with_subquery` 处理。真走到这里说明路由有缺口，
        # 显式报错，绝不静默判假（否则会变成「查不到行」这种错误答案）。
        if op in ('IN', 'NOT IN'):
            if isinstance(where.get('subquery'), dict):
                raise ValueError(
                    "IN / NOT IN 的子查询谓词没有走子查询求值路径："
                    "该条件在此处无法安全求值，绝不静默判假")
            if 'values' not in where:
                raise ValueError(
                    "IN / NOT IN 谓词缺少值列表（也没带子查询）：无法求值")
            if not isinstance(where.get('column'), str) \
                    or not where.get('column'):
                raise ValueError(
                    "IN / NOT IN 的左值必须是单列：本引擎不支持行值"
                    "（多列）比较，无法安全推断出参与比较的列")
            ci = self._col_index_of(cols, where['column'])
            if ci < 0:
                # 末段歧义（JOIN 结果里 `u.id` / `o.id` 并存）→ 显式报错
                self._raise_if_ambiguous_col(cols, where['column'])
                if agg_values is not None:
                    raise ValueError(
                        f"HAVING 里的列 '{where['column']}' 在分组结果里"
                        f"取不到：GROUP BY 之后只保留分组键与聚合值，"
                        f"请把该列加入 GROUP BY / SELECT，或改用聚合表达式")
                return False
            lv = row[ci] if ci < len(row) else None
            vals = [None if (v is None or str(v).upper() == 'NULL') else v
                    for v in (where.get('values') or [])]
            return bool(self._in_predicate(lv, vals, neg=(op == 'NOT IN')))

        col = where.get('column')
        if not isinstance(col, str):
            return True
        i = self._col_index_of(cols, col)
        if i < 0:
            # 末段歧义（JOIN 结果里 `u.id` 与 `o.id` 并存时的裸 `id`）必须
            # 显式报错：静默返回"条件为假"会把整个结果集变成空集，用户看不出
            # 是「没有匹配行」还是「列名有歧义」。
            self._raise_if_ambiguous_col(cols, col)
            # HAVING 语境（agg_values is not None）取不到列时显式报错：
            # GROUP BY 之后行里只有分组键 + 聚合值，`HAVING <裸列> ...`
            # （例如 `SELECT id, COUNT(*) ... GROUP BY id HAVING city IN ('BJ')`）
            # 原来静默判假、整个分组被丢掉；正确做法是要求用户把该列放进
            # GROUP BY/SELECT 或改用聚合表达式。
            if agg_values is not None:
                raise ValueError(
                    f"HAVING 里的列 '{col}' 在分组结果里取不到："
                    f"GROUP BY 之后只保留分组键与聚合值，"
                    f"请把该列加入 GROUP BY / SELECT，或改用聚合表达式")
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

    @staticmethod
    def _raise_if_ambiguous_col(cols, name):
        """列名末段歧义时显式报错（`u.id` / `o.id` 并存时的裸 `id`）

        与计划执行器 `_resolve_col` 的「列 'x' 歧义，请加表名前缀」保持一致：
        多表连接的结果行必须用限定名引用同名列，绝不任取一侧。
        """
        want = str(name).strip().strip('`"').rsplit('.', 1)[-1].lower()
        hits = [c for c in cols
                if isinstance(c, str)
                and str(c).strip().strip('`"').rsplit('.', 1)[-1].lower()
                == want]
        if len(hits) > 1:
            raise ValueError(f"列 '{name}' 歧义，请加表名前缀")

    def _project_rows(self, p, base_cols, rows, session=None, read_view=None,
                      outer_name=None, outer_alias=None, extra_names=None):
        """把行按 SELECT 列表投影成输出列

        投影项支持：`*` / 普通列（含表限定名）/ 常量 / 二元算术表达式 /
        **标量子查询 `(SELECT ...)`**。

        标量子查询实现方式（与 WHERE 里的相关谓词共用同一套机制）：
        逐个外层行把子查询 AST 里的「外层限定名.列」折叠成该行的字面量
        （`_fold_outer_literals`），折叠后的子查询已不相关，交给正常执行
        路径求值（因此内层 JOIN / GROUP BY / HAVING / 嵌套子查询都能用）；
        结果按「折叠用到的外层列取值」缓存，相同外层行只算一次。非相关的
        标量子查询整体只求值一次，所有行共用结果。

        extra_names：外层行来自多表连接（JOIN 后置处理路径）时，连接进来的
        表名/别名 —— 子查询里引用连接表的列也能被折叠。

        语义边界（全部显式处理，绝不静默填 NULL）：
        * 子查询 0 行 → 聚合补空集值（COUNT→0，SUM/AVG/MIN/MAX→NULL），
          非聚合 → NULL（SQL 标准语义）；
        * 子查询 >1 行 → 抛 ValueError（标量子查询返回了多行）；
        * 子查询 >1 列 → 抛 ValueError（标量子查询返回了多列）；
        * 未知列 / 无法求值的投影表达式 → 抛 ValueError，不再降级成 NULL
          （原实现用 `('col', -1)` 的哨兵把所有认不出来的投影项静默输出
          NULL，`SELECT u.uid FROM users u` 会返回一堆 None 而不是报错）。
        """
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

        session = self._use_session(session)
        if outer_name is None:
            outer_name = p.get('table')
        if outer_alias is None:
            outer_alias = p.get('table_alias')
        sub_cache = {}

        out_cols, plan_items = [], []
        for kind, text in projection:
            if kind == 'win':
                continue
            item = self._projection_item_plan(str(text), base_cols)
            out_cols.append(item[2])
            plan_items.append(item)

        out_rows = []
        for r in rows:
            out = []
            for item in plan_items:
                out.append(self._eval_projection_item(
                    item, r, base_cols, session, read_view,
                    outer_name, outer_alias, sub_cache, extra_names))
            out_rows.append(out)
        return self._finish_select(p, out_cols, out_rows)

    def _projection_item_plan(self, text, base_cols):
        """单个投影项 -> (kind, spec, 输出列名)

        kind ∈ {'const','col','arith','sub'}。认不出来的形态抛 ValueError：
        投影是用户直接看得到的结果，宁可变报错也不能安静地输出 NULL。
        """
        expr, alias = self._split_alias(str(text))
        sub, bad = self._projection_subquery_ast(expr)
        if bad:
            raise ValueError(f"投影表达式无法求值: {expr}（{bad}）")
        if sub is not None:
            # 输出列名保留子查询原文（不要做限定符归一化，否则列名会被改写）
            return ('sub', sub, alias or expr.strip())
        label = alias or self._strip_qualifier(expr)
        is_lit, val = self._literal_value(expr)
        if is_lit:
            # 字面量列（`SELECT 1 FROM orders`）只出现在常量投影里
            return ('const', val, label)
        i = self._col_index_of(base_cols, str(text))
        if i >= 0:
            return ('col', i, label)
        arith = self._parse_arith(expr)
        if arith is not None:
            # 简单算术表达式（`SELECT n + 1`）：递归 CTE 的迭代步长
            # 常见写法，原实现会当成未知列静默投影成 NULL
            for tok in (arith[0], arith[2]):
                if self._is_plain_literal(tok):
                    continue
                if self._col_index_of(base_cols, tok) < 0:
                    # 操作数解析不到时 `_eval_arith` 会返回 NULL：静默填 NULL，
                    # 显式报错更安全
                    raise ValueError(
                        f"列 '{tok}' 不存在（算术表达式 {expr} 的列操作数）")
            return ('arith', arith, label)
        raise ValueError(f"列 '{expr}' 不存在")

    def _eval_projection_item(self, item, row, base_cols, session, read_view,
                              outer_name, outer_alias, sub_cache,
                              extra_names=None):
        """求值一个投影项在当前行上的取值"""
        kind, spec, label = item
        if kind == 'const':
            return spec
        if kind == 'col':
            if 0 <= spec < len(row):
                return row[spec]
            raise ValueError(f"投影列 '{label}' 在当前行里取不到值")
        if kind == 'arith':
            return self._eval_arith(spec, base_cols, row)
        return self._eval_projection_scalar(
            spec, row, base_cols, session, read_view,
            outer_name, outer_alias, sub_cache, extra_names)

    # ---- 投影列表里的标量子查询 ----

    # 投影项以 `(SELECT` 开头即视为标量子查询（是否括号正好包住整段再校验）
    _SCALAR_SUB_LEAD = re.compile(r'^\(\s*SELECT\b', re.IGNORECASE)

    @classmethod
    def _projection_subquery_ast(cls, expr):
        """把投影项 `(SELECT ...)` 解析成内层 AST

        返回 (AST 或 None, 不支持的原因或 None)：
        * 不是以 `(SELECT` 开头 → (None, None)；
        * 括号正好包住整段且能解析成 SELECT → (AST, None)；
        * 以 `(SELECT` 开头但括号不匹配整段（`(SELECT 1) + 1`）或解析失败
          → (None, 原因文本)，由调用方显式报错。
        """
        t = str(expr or '').strip()
        if not cls._SCALAR_SUB_LEAD.match(t):
            return None, None
        end = cls._scan_matching_paren(t, 0)
        if end is None or t[end + 1:].strip():
            return None, "标量子查询必须独占一个投影项（不能参与运算）"
        try:
            from .parser import parser as _parser
            ast = _parser.parse(t[1:end].strip())
        except SyntaxError as exc:
            return None, f"标量子查询语法错误: {exc}"
        except Exception as exc:
            return None, f"标量子查询无法解析: {exc}"
        if not isinstance(ast, dict) or ast.get('command') != 'SELECT':
            return None, "标量子查询无法解析成 SELECT"
        return ast, None

    @staticmethod
    def _scan_matching_paren(text: str, start: int):
        """返回 text[start] == '(' 对应右括号的下标（跳过字符串字面量）"""
        depth = 0
        i = start
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "'":
                i += 1
                while i < n:
                    if text[i] == "'":
                        if i + 1 < n and text[i + 1] == "'":
                            i += 2
                            continue
                        break
                    i += 1
            elif ch == '"':
                i += 1
                while i < n and text[i] != '"':
                    i += 1
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return None

    @staticmethod
    def _scalar_static_width(sub):
        """标量子查询的静态列数（无法判断时返回 None，例如 `SELECT *`）"""
        proj = sub.get('projection')
        if not isinstance(proj, list) or not proj:
            proj = [('col', c) for c in (sub.get('columns') or [])]
        items = [t for k, t in proj if k != 'win']
        if any(str(t).strip() == '*' for t in items):
            return None
        return len(items)

    @staticmethod
    def _in_subquery_arity_error(pos_text, n):
        """IN / NOT IN 子查询多列时的报错文本（静态/动态判定共用同一份措辞）"""
        pos = str(pos_text or '').strip()
        head = f"{pos} " if pos else ""
        return (
            f"{head}IN / NOT IN 的子查询只能返回一列，"
            f"当前返回了 {n} 列："
            f"多列子查询的 IN 在 SQL 标准里是**行值（tuple）比较**，"
            f"本引擎不支持行值比较，因此显式报错"
            f"（绝不只取第一列参与比较，那会给出看似正确、实际错误的行）")

    @staticmethod
    def _guard_in_subquery_width(sub, pos_text="", res_cols=None, rows=None):
        """IN / NOT IN（集合上下文）的子查询列数守卫：多于 1 列一律报错

        背景：`c IN (SELECT a, b FROM t)` 在 SQL 标准里等价于行值比较
        `c IN ((a1,b1),(a2,b2),...)`，左值必须是行值；左值是单列时它本身
        就是语义错误。原实现在集合上下文里对每行 `r[0]` 取值，把
        `id IN (SELECT uid, amt FROM orders)` 静默算成
        `id IN (SELECT uid FROM orders)` —— 返回的是**看起来对**的行集，
        属于最典型的静默错答。标量上下文（`_scalar_from_rows`）上一轮已经
        收紧为「多列报错」，这里把集合上下文对齐到同一纪律。

        静态 + 动态双保险（缺一不可）：
        * **静态**：解析期就能数出投影项数（`_scalar_static_width`），
          因此**结果集为空**也拦得住（`IN (SELECT a, b ... WHERE ...)`
          命中 0 行时不能让多列悄悄通过）；
        * **动态**：拿到结果后再按实际列数（结果列名个数，其次首行长度）
          判一次，兜住静态判不出的形态（`SELECT *`）。

        pos_text：出错位置前缀（如 'WHERE 里的'），拼进报错信息。
        res_cols：结果集列名列表（`res['columns']`），可为 None。
        rows：结果行列表，可为 None。
        """
        static_w = Executor._scalar_static_width(sub)
        if static_w is not None and static_w > 1:
            raise ValueError(
                Executor._in_subquery_arity_error(pos_text, static_w))
        if res_cols is not None:
            n = len(res_cols)
        elif rows:
            n = len(rows[0]) if rows[0] else 0
        else:
            n = 0
        if n > 1:
            raise ValueError(
                Executor._in_subquery_arity_error(pos_text, n))

    def _eval_projection_scalar(self, sub, row, cols, session, read_view,
                                outer_name, outer_alias, cache,
                                extra_names=None):
        """求值投影里的标量子查询（对当前外层行）

        相关子查询 → 折叠外层列后执行并按折叠值缓存；
        非相关子查询 → 整体执行一次并缓存。
        多行/多列/无法求值一律抛 ValueError（显式报错，不静默填 NULL）。

        extra_names：外层行来自多表连接（JOIN 后置处理路径）时，
        连接进来的表名/别名同样可以折叠。
        """
        # 外层限定名被内层同名表遮蔽时无法无歧义解析：显式报错
        amb = self._ambiguous_outer_ref(
            {'subquery': sub}, outer_name, outer_alias)
        if amb:
            raise ValueError(self._ambiguous_outer_ref_message(
                amb, outer_name, outer_alias))

        static_w = self._scalar_static_width(sub)
        if static_w is not None and static_w > 1:
            raise ValueError(
                f"标量子查询返回了多列（{static_w} 列）："
                f"投影里的标量子查询只能返回一列")

        correlated = bool(outer_name) and (
            self._is_correlated(sub, outer_name, outer_alias, extra_names)
            or self._sub_owner_is_outer(sub, outer_name, outer_alias,
                                        extra_names))

        if correlated:
            names = self._fold_outer_names(sub, outer_name, outer_alias,
                                           extra_names)
            if not names:
                raise ValueError(
                    "投影里的相关标量子查询无法求值：外层引用无法安全折叠"
                    "（请检查外层限定名是否被子查询里的同名表遮蔽）")
            key = ('psub', id(sub), self._fold_cache_key(
                sub, row, cols, outer_name, outer_alias, extra_names))
            if key not in cache:
                # `_fold_and_run` 现在返回 (列名, 行)；投影是标量上下文，
                # 列数语义仍统一由 `_scalar_from_rows` 收敛
                _res_cols, folded_rows = self._fold_and_run(
                    sub, row, cols, session, outer_name, outer_alias,
                    read_view, extra_names)
                cache[key] = folded_rows
            rows = cache[key]
        else:
            # 疑似非相关：确认里面没有「藏在表达式里的外层引用」，
            # 否则会被当成非相关子查询一次性求值后静默给出错值
            self._guard_residual_outer_ref(sub, outer_name, outer_alias,
                                           extra_names)
            key = ('psub-nc', id(sub))
            if key not in cache:
                res = self.execute(sub, session)
                if not isinstance(res, dict) or res.get('type') == 'error':
                    raise ValueError(
                        "标量子查询执行失败: "
                        f"{(res or {}).get('message', '未知错误')}")
                cache[key] = [tuple(r) for r in (res.get('rows') or [])]
            rows = cache[key]

        # 行数/列数语义统一交给 _scalar_from_rows（与 WHERE/HAVING 同一份
        # 实现）：0 行 → 空集聚合值/NULL、1 行 1 列 → 取值、
        # 多行/多列 → 显式报错。绝不再取第一行/第一列。
        return self._scalar_from_rows(sub, rows, '投影里的')

    def _scalar_from_rows(self, sub, rows, where_text=""):
        """把子查询的结果行按**标量语义**收敛成一个值（统一实现）

        WHERE / HAVING / 投影三个位置的标量上下文共用这一份语义：

        * 多于 1 行 → 抛 ValueError（显式报错，绝不取第一行）；
        * 0 行 → 空集聚合值（COUNT→0，SUM/AVG/MIN/MAX→NULL），
          非聚合投影 → NULL（SQL 标准语义，这是正确的 NULL）；
        * 1 行但多于 1 列 → 抛 ValueError（绝不取第一列）；
        * 1 行 1 列 → 返回该值。

        静态列数可判（`SELECT a, b` / `SELECT *` 之外的形态）时先报多列，
        这样即使某个外层行恰好匹配 0 行也能显式报出多列子查询。
        where_text 只用于把出错位置写进信息（例如 'WHERE/HAVING 里的'）。
        """
        pos = str(where_text or '')
        static_w = self._scalar_static_width(sub)
        if static_w is not None and static_w > 1:
            raise ValueError(
                f"{pos}标量子查询返回了多列（{static_w} 列）："
                f"标量上下文只允许返回 1 列")
        if len(rows) > 1:
            raise ValueError(
                f"{pos}标量子查询返回了多行（{len(rows)} 行）："
                f"标量上下文最多只允许返回 1 行")
        if not rows or not rows[0]:
            return self._empty_agg_scalar(sub)
        if len(rows[0]) > 1:
            raise ValueError(
                f"{pos}标量子查询返回了多列（{len(rows[0])} 列）："
                f"标量上下文只允许返回 1 列")
        return rows[0][0]

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

    @classmethod
    def _is_scalar_function_projection(cls, text) -> bool:
        """投影项是否是「标量函数调用」（形如 LOWER(name) / NOW()）

        只认函数名不是存储层支持的 5 个聚合函数的 `名字(` 开头形态；
        子查询项（`(SELECT ...)`）与 `COUNT(*) OVER ()` 都不会命中。
        """
        expr, _ = cls._split_alias(str(text))
        m = re.match(r'^(\w+)\s*\(', expr.strip())
        return bool(m) and m.group(1).upper() not in cls._AGG_FUNCS

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
            # 外层限定名被内层同名表遮蔽 → 显式报错（原来静默返回空集）
            amb = self._ambiguous_outer_ref(
                where, rel.name, p.get('table_alias'))
            if amb:
                return self._err(1062, self._ambiguous_outer_ref_message(
                    amb, rel.name, p.get('table_alias')))
            if self._where_has_correlated(where, rel.name,
                                          p.get('table_alias')) \
                    or self._where_has_scalar_subquery(where):
                return self._select_correlated(
                    p, s, rel, self._rv_of(s), rel.name)
            try:
                where, _ = self._rewrite_subqueries(
                    where, s, rel.name, p.get('table_alias'))
            except Exception as e:
                return self._err(1105, f"子查询求值失败: {e}")
            return self._select_advanced(p, rel, where=where, session=s)
        return self._select_advanced(p, rel, session=s)

    def _select_no_from(self, p, s):
        """FROM-less SELECT（`SELECT 1 AS n`）

        递归 CTE 的 base 分支常写成 `SELECT 1 AS n`，解析器现在允许没有
        FROM；执行时用「一行的空关系」承接，常量投影直接算出结果行。
        """
        return self._select_advanced(p, _CTERelation('', [], [[]]), session=s)

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
        inner = where.get('not')
        if isinstance(inner, dict) and self._where_has_correlated(
                inner, outer_table, outer_alias):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if self._where_has_correlated(c, outer_table, outer_alias):
                    return True
        return False

    @classmethod
    def _where_has_scalar_subquery(cls, where) -> bool:
        """条件里是否存在「标量子查询比较」节点（`(SELECT ...) <op> v`）

        与 EXISTS / IN 不同，这类谓词没有一次性求值的语义：每个外层行
        都要把子查询结果按行数/列数收敛（0 行 → NULL、1 行 1 列 → 取值、
        多行或多列 → 显式报错），因此必须走 `_select_correlated` 的
        逐行求值路径（`_eval_outer_where` → `_eval_correlated_predicate`
        / `_eval_scalar_subquery`）。漏判会让条件走一次性改写路径，
        原实现会把 subquery 静默丢掉。
        """
        if not isinstance(where, dict):
            return False
        if 'subquery' in where and where.get('op') not in (
                'EXISTS', 'NOT EXISTS', 'IN', 'NOT IN'):
            return True
        inner = where.get('not')
        if isinstance(inner, dict) and cls._where_has_scalar_subquery(inner):
            return True
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                if cls._where_has_scalar_subquery(c):
                    return True
        return False

    def _is_correlated(self, sub, outer_table, outer_alias=None,
                       extra_names=None) -> bool:
        """判断子查询是否相关（引用了外层表的列）

        判据：
        1. 列名限定符等于外层表名/别名，且不是子查询自己的表/别名；
        2. WHERE 里的裸列名在子查询表里不存在、却在外层表里存在。
        `SELECT 1 FROM orders` 这类自洽子查询一律放行。

        extra_names：外层行来自多表连接（JOIN 后置处理路径）时，连接进来的
        表名/别名同样是外层限定符 —— 漏掉会让「引用连接表」的子查询被误判成
        非相关、一次性求值（实测会静默返回空集/0 行）。
        """
        if not outer_table or not isinstance(sub, dict):
            return False
        outers = {str(outer_table).lower()}
        if outer_alias:
            outers.add(str(outer_alias).lower())
        for n in (extra_names or ()):
            if n:
                outers.add(str(n).lower())

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

        # 本层 WHERE 里的**嵌套子查询**也可能引用外层列。`inner_names`
        # 已经包含本层与所有嵌套层自己的表/别名（见方法开头的收集），
        # 因此这里只判"限定符等于外层表名/别名、又不在这个集合里"。
        inner_nested = self._nested_subqueries(sub.get('where'))
        scope = set(inner_names)
        for nested in inner_nested:
            for key in ('table', 'table_alias'):
                if nested.get(key):
                    scope.add(str(nested[key]).lower())
            for j in (nested.get('joins') or []):
                for key in ('table', 'alias'):
                    if j.get(key):
                        scope.add(str(j[key]).lower())
        for nested in inner_nested:
            for c in self._nested_direct_columns(nested):
                owner = self._qualified_owner(c)
                if owner and owner in outers and owner not in scope:
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

        for c in self._nested_direct_columns(sub, local_only=True):
            c = str(c).strip().strip('`"')
            if '.' in c:
                continue
            if c.lower() not in inner_cols and c.lower() in outer_cols:
                return True
        return False

    def _nested_direct_columns(self, sub, local_only=False) -> list:
        """收集 `sub` 本层 WHERE 直接引用的列 + 其嵌套子查询里的列引用

        只用于相关子查询判定：递归进嵌套子查询是必要的（嵌套层也可能
        引用最外层列），但判定时要配合 *_scope() 排除嵌套层自己的表/别名。
        local_only=True 时只看本层 WHERE 直接引用的列，不再往嵌套层递归
        —— 裸列名判据必须这样做：嵌套层的裸列属于嵌套层自己的表，
        拿它去比对本层表的列名会产生假相关（实测
        `... FROM orders WHERE user_id IN (SELECT id FROM users ...)` 里的
        `id` 会被误判成外层 users.id）。
        """
        out = list(self._referenced_columns(sub.get('where')))
        if local_only:
            return out
        for nested in self._nested_subqueries(sub.get('where')):
            out.extend(self._subquery_ast_columns(nested))
        return out

    def _nested_subqueries(self, where, out=None) -> list:
        """收集条件树里出现的所有嵌套子查询 AST（不含自身）"""
        if out is None:
            out = []
        if not isinstance(where, dict):
            return out
        sub = where.get('subquery')
        if isinstance(sub, dict):
            out.append(sub)
        for key in ('and', 'or'):
            for c in (where.get(key) or []):
                self._nested_subqueries(c, out)
        inner = where.get('not')
        if isinstance(inner, dict):
            self._nested_subqueries(inner, out)
        return out

    def _select_correlated(self, p, s, src, read_view, outer_name,
                           extra_names=None):
        """相关子查询的执行路径

        不能像非相关子查询那样「一次求值 + 改写条件」，因为每个外层行的
        真值都不同。这里先一次性取出「全部列的行」（只扫一遍表），再逐行
        用作用域求值器判断条件；子查询的内层行也按子查询缓存，因此单表
        相关子查询不会每行重新打开表。

        extra_names：外层行由多表连接而来时（JOIN 后置处理路径）传入的连接
        表名/别名列表；非 None 同时表示「多表行」，此时普通比较的列名保留
        限定前缀、按连接结果的列名精确解析。
        """
        outer_alias = p.get('table_alias')
        base_cols, rows = self._base_rows_and_cols(src, None, read_view)
        cache = {}
        kept = []
        for r in rows:
            if self._eval_outer_where(r, base_cols, p.get('where'), s,
                                      outer_name, outer_alias, cache,
                                      read_view, extra_names):
                kept.append(r)
        rows = kept

        if (p.get('group_by') or p.get('having')
                or self._has_bare_aggregate(p)):
            base_cols, rows = self._group_rows(
                p, src, base_cols, rows, session=s, read_view=read_view,
                extra_names=extra_names)
        if p.get('windows'):
            return self._apply_windows(p, base_cols, rows, session=s,
                                       read_view=read_view,
                                       extra_names=extra_names)
        return self._project_rows(p, base_cols, rows, session=s,
                                  read_view=read_view,
                                  outer_name=outer_name,
                                  outer_alias=outer_alias,
                                  extra_names=extra_names)

    def _eval_outer_where(self, row, cols, where, s, outer_name,
                          outer_alias, cache, read_view, extra_names=None):
        """外层行上的条件求值（含相关/非相关子查询谓词）

        返回值：True / False / **None（三值逻辑的 UNKNOWN）**。
        UNKNOWN 在调用方（`_select_correlated` 的 `if`、and/or 的 all/any）
        里按「不通过」处理，与 SQL 的行过滤语义一致；只有 `NOT` 需要显式
        区分：`NOT UNKNOWN` 仍是 UNKNOWN，**不能**变成真。

        extra_names：外层行来自多表连接（JOIN 后置处理路径）时，连接进来的
        表名/别名列表 —— 相关子查询要能折叠这些表的列。该参数同时充当
        「多表行」标志：普通比较的列名不做限定符归一化（见 `_normalize_where`）。
        """
        if where is None or not isinstance(where, dict):
            return True
        # NOT (...)：整体取反（可与 AND/OR 组合、可嵌套）。
        # 三值逻辑：NOT UNKNOWN 仍是 UNKNOWN（该行不通过），不能取反成真。
        if 'not' in where and isinstance(where['not'], dict):
            inner = self._eval_outer_where(
                row, cols, where['not'], s, outer_name, outer_alias,
                cache, read_view, extra_names)
            if inner is None:
                return None
            return not inner
        if 'and' in where:
            return all(self._eval_outer_where(
                row, cols, c, s, outer_name, outer_alias, cache, read_view,
                extra_names)
                for c in where['and'])
        if 'or' in where:
            parts = where['or']
            if not parts:
                return False
            return any(self._eval_outer_where(
                row, cols, c, s, outer_name, outer_alias, cache, read_view,
                extra_names)
                for c in parts)

        sub = where.get('subquery')
        op = where.get('op')
        if isinstance(sub, dict):
            # 相关性判定要"宽进"：`_is_correlated` 覆盖裸列/限定列两种判据，
            # `_sub_owner_is_outer` 兜底"子查询里带 JOIN"等漏判形态。
            # 任一命中就走逐外层行求值（外层列常量折叠）路径；漏判会走去
            # 重/标量分支 → 静默给出错误结果（实测 EXISTS 恒为空）。
            correlated = (self._is_correlated(sub, outer_name, outer_alias,
                                              extra_names)
                          or self._sub_owner_is_outer(
                              sub, outer_name, outer_alias, extra_names))
            if correlated:
                return self._eval_correlated_predicate(
                    row, cols, where, s, outer_name, outer_alias,
                    cache, read_view, extra_names)
            # 疑似非相关：再确认一次里面没有「藏在表达式里的外层引用」
            # （`x.uid = MIN(u.id)` 这类识别不到的相关引用），命中即报错，
            # 绝不当成非相关子查询一次性求值后静默返回 0 行。
            self._guard_residual_outer_ref(sub, outer_name, outer_alias,
                                           extra_names)
            if op not in ('EXISTS', 'NOT EXISTS', 'IN', 'NOT IN'):
                # 非相关标量子查询：可以一次性求值（结果按子查询缓存）
                sv = self._eval_scalar_subquery(sub, s, cache)
                rv = where.get('value')
                if self._is_column_ref(rv):
                    rv = self._scoped_value(rv, row, cols, None)
                    if rv is _MISSING:
                        return False
                if sv is None and rv is not None:
                    # 标量是 NULL（0 行 → NULL）→ 比较结果是 UNKNOWN，
                    # 不是 False：AND/OR 里表现相同，但 NOT 下必须保持
                    # UNKNOWN（不能变成真）。见 `_eval_outer_where` 的
                    # NOT 分支。
                    return None
                return self._compare_scoped(sv, op, rv)
            return self._eval_plain_subquery_predicate(
                row, cols, where, s, cache, outer_name, outer_alias,
                extra_names)
        # 普通比较 / LIKE / IS NULL（编码成 value=None 的 =/!=）/ 聚合谓词
        return self._eval_plain_condition(
            row, cols, self._normalize_where(where, strip=not extra_names))

    def _eval_plain_condition(self, row, cols, where):
        """不含子查询的普通条件求值（三值语义，含 `negate` 标记）

        `_normalize_where` 对 LIKE 这类存储层没有取反运算符的形态会打上
        `{'negate': ...}` 标记。`_eval_condition` 不认识它，遇到会顺着
        「column 不是字符串 → return True」的分支把条件**静默丢掉**
        （实测 `NOT (name LIKE 'A%')` 被判成恒真）。这里把 negate 标记按
        三值语义就地展开后再交给 `_eval_condition`。
        """
        if isinstance(where, dict):
            if isinstance(where.get('negate'), dict):
                inner = self._eval_plain_condition(row, cols, where['negate'])
                return None if inner is None else (not inner)
            if 'and' in where:
                return all(self._eval_plain_condition(row, cols, c)
                           for c in where['and'])
            if 'or' in where:
                parts = where['or']
                if not parts:
                    return False
                return any(self._eval_plain_condition(row, cols, c)
                           for c in parts)
        return self._eval_condition(row, cols, where)

    def _eval_plain_subquery_predicate(self, row, cols, where, s, cache,
                                       outer_name=None, outer_alias=None,
                                       extra_names=None):
        """相关 WHERE 里混进来的非相关子查询（结果按子查询缓存）"""
        sub = where['subquery']
        op = where['op']
        self._guard_residual_outer_ref(sub, outer_name, outer_alias,
                                       extra_names)
        key = ('sub', id(sub))
        if key not in cache:
            res = self.execute(sub, s)
            if not isinstance(res, dict) or res.get('type') == 'error':
                raise ValueError(
                    f"子查询执行失败: {(res or {}).get('message', '')}")
            cache[key] = (list(res.get('columns') or []),
                          [tuple(r) for r in (res.get('rows') or [])])
        res_cols, rows = cache[key]

        if op == 'EXISTS':
            return len(rows) > 0
        if op == 'NOT EXISTS':
            return len(rows) == 0
        # IN / NOT IN 是**集合语义**（允许多行），但只允许**一列**：
        # 多列子查询在 SQL 标准里是行值（tuple）比较，本引擎不支持，
        # 因此显式报错 —— 原实现对每行取 r[0]，把
        # `id IN (SELECT uid, amt ...)` 静默算成 `id IN (SELECT uid ...)`，
        # 返回看似正确、实际错误的行集（本会话消灭的静默错答）。
        # 静态列数（解析期）+ 实际列数（结果列名/首行长度）双判定，
        # 空结果集也拦得住，见 `_guard_in_subquery_width`。
        self._guard_in_subquery_width(
            sub, 'WHERE/HAVING 里的', res_cols=res_cols, rows=rows)
        if not isinstance(where.get('column'), str) or not where.get('column'):
            raise ValueError(
                "IN / NOT IN 的左侧必须是单列：本引擎不支持行值"
                "（多列）比较，无法安全推断出参与比较的列")
        vals = [r[0] for r in rows if r]
        lv = self._scoped_value(where.get('column'), row, cols, None)
        return self._in_predicate(lv, vals, neg=(op == 'NOT IN'))

    def _eval_correlated_predicate(self, row, cols, where, s, outer_name,
                                   outer_alias, cache, read_view,
                                   extra_names=None):
        """相关子查询谓词求值：外层列常量折叠 + 复用正常执行路径

        实现方式（通用做法，能直接复用 JOIN / GROUP BY / HAVING / 窗口 /
        嵌套子查询等全部既有能力）：

        1. 把子查询 AST 里出现的「外层别名.列」全部替换成该外层行的
           字面量（WHERE / HAVING / SELECT 列表 / JOIN ON 都要处理）；
        2. 折叠后的子查询已经**不相关**，交给 `self.execute` 正常执行；
        3. 按折叠后的结果判定谓词真值：
           * EXISTS  → 行数 > 0
           * NOT EXISTS → 行数 == 0
           * IN / NOT IN → 取投影第一列做三值语义判定
           * 标量比较（`(SELECT COUNT(*)...) > 1`）→ 取第一行第一列比较

        缓存：同一子查询对象按「折叠用的外层列取值元组」缓存结果，
        因此相同外层行只算一次、取值相同的外层行共享结果，
        20 行规模也是瞬时完成。

        无法安全折叠的形态（子查询 JOIN ON 里引用外层列、折叠后仍残留
        外层引用、FROM 子查询）显式报错，不静默错答。
        """
        sub = where['subquery']
        op = where['op']
        cache_key = ('corr', id(sub), self._fold_cache_key(
            sub, row, cols, outer_name, outer_alias, extra_names))
        if cache_key in cache:
            res_cols, rows = cache[cache_key]
        else:
            res_cols, rows = self._fold_and_run(
                sub, row, cols, s, outer_name, outer_alias, read_view,
                extra_names)
            cache[cache_key] = (res_cols, rows)

        if op == 'EXISTS':
            return len(rows) > 0
        if op == 'NOT EXISTS':
            return len(rows) == 0

        if op in ('IN', 'NOT IN'):
            # IN / NOT IN（子查询形态）：集合语义，允许多行，但只允许
            # **一列**。多列在 SQL 标准里是行值比较，本引擎不支持 →
            # 显式报错。原实现取 r[0]，把多列子查询静默截断成第一列
            # （`id IN (SELECT uid, amt ...)` 算成 `id IN (SELECT uid ...)`），
            # 是典型静默错答，这里按 `_guard_in_subquery_width` 收紧
            # （静态列数 + 实际列数双判定，空结果集同样报错）。
            self._guard_in_subquery_width(
                sub, 'WHERE/HAVING 里的', res_cols=res_cols, rows=rows)
            if not isinstance(where.get('column'), str) \
                    or not where.get('column'):
                raise ValueError(
                    "IN / NOT IN 的左侧必须是单列：本引擎不支持行值"
                    "（多列）比较，无法安全推断出参与比较的列")
            vals = [r[0] for r in rows if r]
            lv = self._scoped_value(where.get('column'), row, cols, None)
            return self._in_predicate(lv, vals, neg=(op == 'NOT IN'))

        # 标量比较：按标量行数/列数语义收敛后再比较（统一走
        # _scalar_from_rows）。注意空结果：SQL 里 `(SELECT COUNT(*) ...)`
        # 即使没有匹配行也返回 0（聚合作用于空集），而 SUM/AVG/MIN/MAX
        # 返回 NULL，因此空集要按投影的聚合函数名补出正确的空集值，
        # 否则 `(SELECT COUNT(*) ...) = 0` 会把 0 误判成 NULL 而漏掉整行。
        # 原实现无条件取 `rows[0][0]`：多行静默取第一行、多列静默取第一列，
        # 属于会给出错误结果的静默降级（WHERE 位置的主验收点）。
        sv = self._scalar_from_rows(sub, rows, 'WHERE/HAVING 里的')
        rv = where.get('value')
        if self._is_column_ref(rv):
            rv = self._scoped_value(rv, row, cols, None)
            if rv is _MISSING:
                return False
        elif sv is not None and not isinstance(rv, type(sv)):
            try:
                if isinstance(sv, (int, float)) and isinstance(rv, str):
                    rv = float(rv) if '.' in rv else int(rv)
            except (TypeError, ValueError):
                pass
        if sv is None and rv is not None:
            # 标量是 NULL（0 行 → NULL，或空集上的 SUM/AVG/MIN/MAX）
            # → 比较结果是三值逻辑的 UNKNOWN（该行不通过），但**不是 False**：
            # NOT 下必须保持 UNKNOWN（见 `_eval_outer_where` 的 NOT 分支）。
            return None
        return self._compare_scoped(sv, op, rv)

    @staticmethod
    def _empty_agg_scalar(sub):
        """空结果集上的标量子查询取值（聚合作用于空集的 SQL 语义）

        COUNT → 0，其它聚合（SUM/AVG/MIN/MAX）→ NULL；
        投影不是聚合函数时也返回 NULL。

        已知偏差（本次刻意不改，仅显式记录）：本引擎对空集不产出任何行，
        因此「0 行」有两种来源——① 无 GROUP BY 的裸聚合作用于空集
        （SQL 定义为 COUNT=0，这个补值是正确的）；② 带 GROUP BY/HAVING
        且没有任何分组存活（SQL 标准下整个标量子查询是 NULL，即使投影是
        COUNT）。这里对两种情况一律按「COUNT → 0」补值，第二种情况下
        `(SELECT COUNT(*) ... GROUP BY g HAVING ...) = 0` 会命中、
        而 PostgreSQL/SQLite/MySQL 下应为 NULL（UNKNOWN）不命中。
        收紧这一点会同时改变**投影位置**已锁定的语义
        （tests/test_v4_sql_features.py 中 `... GROUP BY o.user_id
        HAVING COUNT(*) >= 1) AS g` 对无订单用户的期望值 0），
        属于上一轮已决策的范围，因此留待统一决策；WHERE/HAVING 位置
        与投影位置保持一致，不单独偏离。
        """
        cols = sub.get('columns') or []
        expr = str(cols[0]).strip() if cols else ''
        m = re.match(r'^(\w+)\s*\(', expr)
        if m and m.group(1).upper() == 'COUNT':
            return 0
        return None

    def _eval_scalar_subquery(self, sub, s, cache):
        """非相关标量子查询求值（结果按子查询缓存）

        与相关路径共用 `_scalar_from_rows` 的行数/列数语义：
        0 行 → 空集聚合值（COUNT→0，其余→NULL）、1 行 1 列 → 取值、
        多行/多列 → 显式报错。原实现是
        `cache[key] = rows[0][0] if (rows and rows[0]) else None`
        ——多行静默取第一行、多列静默取第一列、空集上的 COUNT 还会
        错成 NULL，三重静默错答。
        """
        key = ('scalar', id(sub))
        if key not in cache:
            res = self.execute(sub, s)
            if not isinstance(res, dict) or res.get('type') == 'error':
                raise ValueError(
                    f"子查询执行失败: {(res or {}).get('message', '')}")
            rows = [tuple(r) for r in (res.get('rows') or [])]
            cache[key] = self._scalar_from_rows(sub, rows, 'WHERE/HAVING 里的')
        return cache[key]

    # ---- 外层列常量折叠 ----

    @staticmethod
    def _is_column_ref(text) -> bool:
        """文本是否可能是列引用（`u.id` / `id`；排除数字与字符串字面量）"""
        t = str(text or '').strip().strip('`"')
        if not t or not re.match(r'^[\w.]+$', t):
            return False
        if re.match(r'^-?\d+(?:\.\d+)?$', t):
            return False
        return True

    @classmethod
    def _render_literal(cls, value) -> str:
        """把 Python 值渲染成 SQL 字面量文本（供折叠后的 SQL 文本重新解析）"""
        if value is None:
            return 'NULL'
        if isinstance(value, bool):
            return '1' if value else '0'
        if isinstance(value, (int, float)):
            return repr(value)
        return "'" + str(value).replace("'", "''") + "'"

    @classmethod
    def _ast_literal(cls, value):
        """把 Python 值折叠进 AST 的 value 字段（不是 SQL 文本！）

        解析器存进 AST 的比较右侧是**去掉引号**的原始值
        （`name = 'BJ'` → value='BJ'、`n = 1` → value='1'），折叠必须与之一致。
        原来这里一律用 `_render_literal`（SQL 文本），字符串会被带上引号，
        折叠后的条件变成 `u2.city = "'BJ'"`（value 里含引号）→ 永远匹配不上，
        实测 `WHERE u2.city = u.city` 静默返回 0 行。数值本来就是文本形态，
        保持 repr()，NULL 保持 'NULL'（与解析器/求值器的 NULL 编码一致）。
        """
        if value is None:
            return 'NULL'
        if isinstance(value, bool):
            return '1' if value else '0'
        if isinstance(value, (int, float)):
            return repr(value)
        return value

    def _outer_column_value(self, cols, row, name):
        """在外层行里取 `name` 的值；取不到返回 _MISSING

        先按精确名 / 去限定符名找列；都找不到时再按「列名末段」匹配，
        因为外层列名本身可能带限定符（`GROUP BY u.city` 之后外层列名是
        `u.city`，而子查询里引用的是 `u.city` 或裸 `city` 两种写法）。
        匹配不上仍返回 _MISSING，由调用方显式报错（不静默当 NULL）。
        """
        if not isinstance(name, str) or not name.strip():
            return _MISSING
        t = name.strip().strip('`"')
        cands = [t]
        if '.' in t:
            cands.append(t.rsplit('.', 1)[1])
        for cand in cands:
            i = self._col_index_of(cols, cand)
            if 0 <= i < len(row):
                return row[i]
        want = t.rsplit('.', 1)[-1].lower()
        for i, c in enumerate(cols):
            if not isinstance(c, str):
                continue
            if c.strip().strip('`"').rsplit('.', 1)[-1].lower() == want:
                if 0 <= i < len(row):
                    return row[i]
        return _MISSING

    def _sub_owner_is_outer(self, sub, outer_name, outer_alias,
                            extra_names=None) -> bool:
        """粗判：子查询 AST 里是否有「外层限定名」引用

        比 `_is_correlated` 宽松（不做裸列名/表列比对），用于兜底识别
        `_is_correlated` 可能漏掉的相关引用（例如子查询里带了 JOIN）。
        必须排除**子查询及其嵌套子查询自己的表/别名**：自洽子查询里出现
        与外层同名的表（`... IN (SELECT ... FROM users WHERE city='BJ')`
        而外层也叫 users）否则会被误判成相关引用。

        extra_names：外层行来自多表连接（JOIN 后置处理路径）时，
        连接进来的表名/别名同样是可引用的外层限定符。
        """
        outers = set()
        for n in (outer_name, outer_alias):
            if n:
                outers.add(str(n).lower())
        for n in (extra_names or ()):
            if n:
                outers.add(str(n).lower())
        if not outers:
            return False
        inner = self._sub_scope_names(sub)
        if inner & outers:
            outers -= inner
            if not outers:
                return False
        for c in self._subquery_ast_columns(sub):
            owner = self._qualified_owner(c)
            if owner and owner in outers:
                return True
        return False

    def _sub_scope_names(self, sub) -> set:
        """子查询（含其 WHERE 里的嵌套子查询）自己声明的表名/别名集合"""
        names = set()
        if not isinstance(sub, dict):
            return names
        for key in ('table', 'table_alias'):
            if sub.get(key):
                names.add(str(sub[key]).lower())
        for j in (sub.get('joins') or []):
            for key in ('table', 'alias'):
                if j.get(key):
                    names.add(str(j[key]).lower())
        for nested in self._nested_subqueries(sub.get('where')):
            for key in ('table', 'table_alias'):
                if nested.get(key):
                    names.add(str(nested[key]).lower())
            for j in (nested.get('joins') or []):
                for key in ('table', 'alias'):
                    if j.get(key):
                        names.add(str(j[key]).lower())
        return names

    # ---- 外层引用被内层同名表遮蔽（无法无歧义解析） ----

    @staticmethod
    def _sub_binding_names(sub):
        """子查询里的 FROM 项 -> (可见引用名集合, 表名集合)

        * 可见名：写了别名就用别名（SQL 里别名会遮蔽表名），否则用表名；
        * 表名集合：FROM/JOIN 项实际指向的表名（FROM 子查询记其别名）。

        两者的差集就是「被别名遮蔽掉的表名」：这种名字在子查询里
        既可能是外层引用、又对应一张内层表，引擎无法无歧义解析。
        """
        visible, tables = set(), set()
        if not isinstance(sub, dict):
            return visible, tables
        items = [(sub.get('table'), sub.get('table_alias'))]
        for j in (sub.get('joins') or []):
            items.append((j.get('table'), j.get('alias')))
        for t, a in items:
            if t:
                tables.add(str(t).lower())
            if a:
                visible.add(str(a).lower())
            elif t:
                visible.add(str(t).lower())
        return visible, tables

    def _all_nested_subqueries(self, where, out=None) -> list:
        """条件树里所有（含嵌套层）的子查询 AST（按出现顺序去重）"""
        if out is None:
            out = []
        for sub in self._nested_subqueries(where):
            if any(s is sub for s in out):
                continue
            out.append(sub)
            for key in ('where', 'having'):
                if isinstance(sub.get(key), dict):
                    self._all_nested_subqueries(sub[key], out)
        return out

    def _ambiguous_outer_ref(self, where, outer_table, outer_alias=None):
        """找出「外层限定名被内层同名表遮蔽」的引用，返回限定符；无则 None

        典型形态（实测原来静默返回空集）：

            SELECT id FROM users WHERE EXISTS (
              SELECT 1 FROM orders o JOIN users u2 ON o.user_id = u2.id
              WHERE o.user_id = users.id)

        外层没起别名时，`users.id` 既可能是外层 users、又对应子查询里
        被别名 u2 遮蔽的那张 users——引擎的作用域判定（`_is_correlated` /
        `_sub_scope_names`）会把它算成**内层**引用，于是整个子查询被当成
        非相关、一次性求值成空集，静默返回 0 行。这里显式报错，要求给
        外层表起别名（起别名后名字不再冲突，正常用法不受影响）。
        """
        outers = {str(n).lower() for n in (outer_table, outer_alias) if n}
        if not isinstance(where, dict) or not outers:
            return None
        for sub in self._all_nested_subqueries(where):
            if not isinstance(sub, dict):
                continue
            visible, tables = self._sub_binding_names(sub)
            hidden = tables - visible          # 被别名遮蔽掉的表名
            conflict = outers & hidden
            if not conflict:
                continue
            for c in self._subquery_ast_columns(sub):
                owner = self._qualified_owner(c)
                if owner and owner in conflict:
                    return owner
        return None

    @staticmethod
    def _ambiguous_outer_ref_message(owner, outer_table, outer_alias=None):
        """外层引用无法无歧义解析时的报错信息"""
        return (
            f"子查询里的 '{owner}.' 无法无歧义解析：外层表 "
            f"'{outer_table}' 与子查询里被别名遮蔽的同名表 '{owner}' 冲突。"
            f"请给外层表起别名（例如 FROM {outer_table} {owner[:1]}x），"
            f"并在子查询里用该别名引用外层列")

    def _fold_outer_literals(self, sub, row, cols, outer_names,
                             depth=0):
        """把子查询 AST 里的外层列引用替换成外层行的字面量

        outer_names：可以安全折叠的限定符集合（来自 _fold_outer_names）。
        返回折叠后的新 AST（不改原对象，避免污染缓存）。已知限制：
        * 子查询 JOIN ON 里引用外层列 → 抛 ValueError（连接键折叠不安全）；
        * 嵌套子查询里的外层引用 → 抛 ValueError（本实现只折叠一层）。
        """
        if depth > 6:
            raise ValueError("相关子查询嵌套过深（>6 层）")
        if not isinstance(sub, dict):
            return sub

        def fold_value(v):
            """比较右侧 / 聚合右值：是外层列引用就换字面量

            注意用 `_ast_literal`（AST 值）而不是 `_render_literal`（SQL 文本）：
            折叠结果是直接喂给执行器的 AST，字符串不能带引号。
            """
            if (isinstance(v, str) and self._is_column_ref(v)
                    and self._qualified_owner(v) in outer_names):
                got = self._outer_column_value(cols, row, v)
                if got is _MISSING:
                    raise ValueError(
                        f"相关子查询折叠失败：外层列 {v} 解析不到")
                return self._ast_literal(got)
            return v

        def fold_node(node, d):
            if not isinstance(node, dict):
                return node
            out = dict(node)
            for key in ('and', 'or'):
                if isinstance(out.get(key), list):
                    out[key] = [fold_node(c, d) for c in out[key]]
            if 'not' in out:
                out['not'] = fold_node(out['not'], d)
            inner_sub = out.get('subquery')
            if isinstance(inner_sub, dict):
                # 嵌套子查询：它引用的外层列同样折叠成字面量，
                # 然后整个嵌套子查询走正常执行路径（可继续嵌套）
                out['subquery'] = self._fold_outer_literals(
                    inner_sub, row, cols, outer_names, d + 1)
            col = out.get('column')
            if isinstance(col, str) and '.' in col and \
                    self._qualified_owner(col) in outer_names:
                got = self._outer_column_value(cols, row, col)
                if got is _MISSING:
                    raise ValueError(
                        f"相关子查询折叠失败：外层列 {col} 解析不到")
                # 折叠后没有任何比较能留在这一层，直接给常量真
                return None
            if 'value' in out:
                out['value'] = fold_value(out['value'])
            return out

        out = dict(sub)
        # SELECT 列表：`SELECT u.x + 1 AS y` → 常量表达式文本
        # （折叠后用正常路径重新解析，因此标量聚合子查询也能算对）
        cols_out = []
        for c in (sub.get('columns') or []):
            text = str(c)
            for owner in sorted(outer_names, key=len, reverse=True):
                pattern = r'\b' + re.escape(owner) + r'\.(\w+)'

                def _sub(m, _o=owner):
                    got = self._outer_column_value(
                        cols, row, f'{_o}.{m.group(1)}')
                    return (m.group(0) if got is _MISSING
                            else self._render_literal(got))
                text = re.sub(pattern, _sub, text, flags=re.IGNORECASE)
            cols_out.append(text)
        out['columns'] = cols_out
        # 投影顺序：窗口函数项按原样保留（内层窗口不引用外层列）
        if isinstance(sub.get('projection'), list):
            out['projection'] = [
                (k, t) if k != 'col' else (k, str(t))
                for k, t in sub['projection']]

        for key in ('where', 'having'):
            if isinstance(out.get(key), dict):
                folded = fold_node(out[key], 0)
                out[key] = None if folded is None else folded

        new_joins = []
        for j in (sub.get('joins') or []):
            nj = dict(j)
            on = nj.get('on')
            if isinstance(on, dict):
                # JOIN ON 里的外层列引用同样折叠成字面量，交给 JOIN 路径
                # 正常执行（例如 `JOIN users u2 ON u2.id = o.user_id AND
                # u2.id = u.id` → `... AND u2.id = 1`）
                nj['on'] = fold_node(on, 0)
            new_joins.append(nj)
        if new_joins:
            out['joins'] = new_joins
        return out

    def _fold_outer_names(self, sub, outer_name, outer_alias,
                          extra_names=None):
        """收集「可以安全折叠」的外层限定符集合

        只认限定符等于外层表名/别名、且子查询自己没有同名表/别名的引用，
        避免把子查询内部的表别名（例如自连接的 u2）误当外层引用。

        extra_names：外层行由**多张表连接**而来时（JOIN 后置处理路径），
        除 `outer_name` / `outer_alias` 之外还可以折叠的连接表名/别名。
        """
        outers = set()
        for n in (outer_name, outer_alias):
            if n:
                outers.add(str(n).lower())
        for n in (extra_names or ()):
            if n:
                outers.add(str(n).lower())
        inner_names = set()
        for key in ('table', 'table_alias'):
            if sub.get(key):
                inner_names.add(str(sub[key]).lower())
        for j in (sub.get('joins') or []):
            for key in ('table', 'alias'):
                if j.get(key):
                    inner_names.add(str(j[key]).lower())
        return {o for o in outers if o not in inner_names}

    def _outer_ref_columns(self, sub, names, out=None) -> list:
        """收集子查询（含嵌套子查询）里引用外层限定符的列名

        只看 `where` 会漏掉 `value` 位置的引用（`WHERE o.uid = u.id` 里的
        `u.id` 是 value），也漏掉嵌套子查询里的引用。漏掉会让
        `_fold_cache_key` 返回空键 → 所有外层行共用第一行的结果，
        实测表现为"最后一行的答案串到所有行"这类静默错答。
        """
        if out is None:
            out = []
        if not isinstance(sub, dict):
            return out

        def consider(text):
            if (isinstance(text, str) and self._is_column_ref(text)
                    and self._qualified_owner(text) in names):
                key = str(text).strip().strip('`"').lower()
                if key not in out:
                    out.append(key)

        consider(sub.get('column'))
        consider(sub.get('value'))
        for key in ('and', 'or'):
            for c in (sub.get(key) or []):
                self._outer_ref_columns(c, names, out)
        if isinstance(sub.get('not'), dict):
            self._outer_ref_columns(sub['not'], names, out)
        # 本节点可能是一个条件节点（带 subquery 键指向子查询 AST），
        # 也可能本身就是子查询 AST。两种都要继续往下走。
        nested = sub.get('subquery')
        if isinstance(nested, dict):
            self._outer_ref_columns(nested, names, out)
        for key in ('where', 'having'):
            if isinstance(sub.get(key), dict):
                self._outer_ref_columns(sub[key], names, out)
        for j in (sub.get('joins') or []):
            on = j.get('on')
            if isinstance(on, dict):
                self._outer_ref_columns(on, names, out)
        return out

    def _fold_cache_key(self, sub, row, cols, outer_name, outer_alias,
                        extra_names=None):
        """按「折叠用到的外层列取值」构造缓存键（同值外层行共享结果）

        必须覆盖子查询（含嵌套层）里出现的**所有**外层列引用：
        漏一个就会让取值不同的外层行命中同一条缓存，产生静默错答。
        """
        names = self._fold_outer_names(sub, outer_name, outer_alias,
                                       extra_names)
        items = []
        for c in self._outer_ref_columns(sub, names):
            val = self._outer_column_value(cols, row, c)
            items.append((c, self._fold_key_value(val)))
        items.sort(key=lambda kv: kv[0])
        return tuple(items)

    @classmethod
    def _fold_key_value(cls, value):
        """缓存键里的单值归一化（list/dict 等不可比较的值转成字符串）"""
        if value is _MISSING:
            return ('__missing__',)
        if isinstance(value, (int, float, str, bytes, type(None), bool)):
            return (type(value).__name__, value)
        return ('repr', repr(value))

    def _fold_and_run(self, sub, row, cols, s, outer_name, outer_alias,
                      read_view, extra_names=None):
        """折叠外层列 → 正常执行 → 返回 (结果列名, 结果行列表)

        结果列名一并返回，是为了让 IN / NOT IN 的集合上下文能按**实际
        列数**判定多列子查询（空结果集也要拦得住：`SELECT *` 这类静态
        判不出列数的形态只能靠结果列名）。
        """
        names = self._fold_outer_names(sub, outer_name, outer_alias,
                                       extra_names)
        if not names:
            raise ValueError(
                "相关子查询的外层引用无法安全折叠（找不到外层限定符）")
        folded = self._fold_outer_literals(
            sub, row, cols, names)
        # 折叠后必须不再相关：否则说明还有本实现没覆盖到的外层引用
        if self._is_correlated(folded, outer_name, outer_alias,
                               extra_names) or \
                self._sub_owner_is_outer(folded, outer_name, outer_alias,
                                         extra_names):
            raise ValueError(
                "相关子查询的外层引用无法安全折叠（折叠后仍残留外层引用）")
        leftover = self._leftover_outer_pattern(folded, names)
        if leftover:
            # 例如 `WHERE o.uid = u.id, o.amount > 5`（逗号当 AND 用）：
            # 解析器把整段塞进了 value，折叠后既不是字面量、又含外层引用，
            # 直接执行会静默给出 0 行。这种形态没有安全语义 → 显式报错。
            raise ValueError(
                f"相关子查询无法安全折叠：条件里还残留外层引用 '{leftover}'"
                f"（该形态无法求值，请检查 WHERE 条件是否缺少 AND/OR）")
        res = self.execute(folded, s)
        if not isinstance(res, dict) or res.get('type') == 'error':
            raise ValueError(
                f"相关子查询执行失败: {(res or {}).get('message', '未知错误')}")
        return (list(res.get('columns') or []),
                [tuple(r) for r in (res.get('rows') or [])])

    def _leftover_outer_pattern(self, node, outer_names):
        """折叠后的 AST 里是否还残留 `外层名.列` 文本（返回该文本或 None）"""
        if not isinstance(node, dict):
            return None
        for key in ('column', 'value'):
            v = node.get(key)
            if isinstance(v, str):
                for owner in outer_names:
                    if re.search(r'\b' + re.escape(owner) + r'\.\w+',
                                 v, re.IGNORECASE):
                        return v
        for key in ('and', 'or'):
            for c in (node.get(key) or []):
                got = self._leftover_outer_pattern(c, outer_names)
                if got:
                    return got
        if isinstance(node.get('not'), dict):
            got = self._leftover_outer_pattern(node['not'], outer_names)
            if got:
                return got
        if isinstance(node.get('subquery'), dict):
            got = self._leftover_outer_pattern(node['subquery'], outer_names)
            if got:
                return got
        for key in ('where', 'having'):
            if isinstance(node.get(key), dict):
                got = self._leftover_outer_pattern(node[key], outer_names)
                if got:
                    return got
        for j in (node.get('joins') or []):
            if isinstance(j.get('on'), dict):
                got = self._leftover_outer_pattern(j['on'], outer_names)
                if got:
                    return got
        return None

    def _residual_outer_ref(self, sub, outer_name, outer_alias,
                            extra_names=None):
        """「疑似非相关」的子查询里是否还藏有外层引用（返回该文本或 None）

        `_is_correlated` / `_sub_owner_is_outer` 的判据只认 `别名.列` 这种
        **纯列引用**（正则 `[\\w.]+`）。真值藏在表达式里的外层引用
        （`WHERE x.uid = MIN(u.id)`、`WHERE x.amt = u.amt + 1`）识别不到，
        会被当成非相关子查询一次性执行：内层解析不到该列 → 要么报「列不存在」，
        要么被存储层当成字符串比较后**静默返回 0 行**。这里做一次文本级残留
        检查，命中就显式报错（见 `_guard_residual_outer_ref`）。

        计算外层限定符时要扣掉**子查询及其嵌套子查询**自己声明的表名/别名，
        否则 `... FROM orders WHERE uid IN (SELECT id FROM users WHERE
        users.city='BJ')` 这类自洽子查询会被误判（users 只是嵌套层自己的表）。
        """
        names = set()
        for n in (outer_name, outer_alias):
            if n:
                names.add(str(n).lower())
        for n in (extra_names or ()):
            if n:
                names.add(str(n).lower())
        names -= self._sub_scope_names(sub)
        if not names:
            return None
        return self._leftover_outer_pattern(sub, names)

    def _guard_residual_outer_ref(self, sub, outer_name, outer_alias,
                                  extra_names=None):
        """非相关子查询里仍残留外层引用 → 显式报错（绝不静默给错结果）"""
        got = self._residual_outer_ref(sub, outer_name, outer_alias,
                                       extra_names)
        if got:
            raise ValueError(
                f"子查询里的 '{got}' 无法安全折叠：本引擎只支持把"
                f"「别名.列」形式的纯列引用折叠成外层行的取值，"
                f"不支持在外层引用上套聚合/函数（该形态无法求值）")

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

    def _apply_windows(self, p, base_cols, rows, session=None,
                       read_view=None, extra_names=None):
        """计算窗口列并按投影顺序拼装结果行

        非窗口投影项复用 `_projection_item_plan` / `_eval_projection_item`：
        原来这里用 `_col_index_of` 取不到列就静默输出 None，同样属于
        「静默填 NULL」，现在认不出来的投影项会显式报错。

        extra_names：JOIN 后置处理路径传入的连接表名/别名（见 `_project_rows`）。
        """
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

        session = self._use_session(session)
        outer_name = p.get('table')
        outer_alias = p.get('table_alias')
        sub_cache = {}

        out_cols, plan_items = [], []
        for kind, text in projection:
            if kind == 'win':
                out_cols.append(text['alias'])
                plan_items.append(('win', id(text), text['alias']))
                continue
            item = self._projection_item_plan(str(text), base_cols)
            out_cols.append(item[2])
            plan_items.append(item)

        out_rows = []
        for ri, r in enumerate(rows):
            out = []
            for item in plan_items:
                if item[0] == 'win':
                    out.append(computed[item[1]][ri])
                    continue
                out.append(self._eval_projection_item(
                    item, r, base_cols, session, read_view,
                    outer_name, outer_alias, sub_cache, extra_names))
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
        """多表 JOIN

        两条路径：

        1. **纯计划路径**（既有行为，零改动）：查询里没有需要逐连接行求值的
           子查询时，整条查询交给优化器 + 计划执行器（Filter/Project/
           GroupBy/Sort/Limit 都在计划里）。
        2. **后置处理路径**：WHERE 里有标量子查询比较 / 引用连接表的子查询、
           HAVING 里有子查询、或投影列表里有标量子查询时，计划执行器只负责
           「FROM + JOIN（含 ON）」，产出带限定列名的连接结果；随后由执行器
           逐连接行把子查询折叠求值（`_fold_and_run` + `_scalar_from_rows`，
           与单表相关子查询完全同一套机制与缓存）。

        为什么不让计划执行器只处理一半条件：`A OR (SELECT ...) > 1` 这种形态
        只摘掉子查询那一半会让真值表变错（要么全放行、要么丢太多行），
        因此只要 WHERE 里出现必须在执行器里求值的子查询，整个 WHERE 都改由
        执行器逐行求值（`_eval_plain_condition` 保留限定列名精确解析）。
        """
        from .plan_executor import PlanExecutor

        txn = s.get('txn')
        if txn:
            rv = txn['read_view']
        else:
            tid = self.db.mvcc.next_txn_id()
            rv = self.db.mvcc.create_readview(tid)

        if self.optimizer is None:
            return self._err(1105, "优化器未初始化")

        join_names = self._join_qualifiers(p)
        if self._join_needs_row_eval(p, join_names):
            return self._exec_join_query_with_subqueries(
                p, s, rv, join_names)

        plan = self.optimizer.optimize(p)
        self._last_plan = plan

        executor = PlanExecutor(self.db, read_view=rv)
        # ★ 逃生口要能传到计划路径：本实例的严格模式开关原样传给计划执行器，
        # 否则 `Executor.strict_group_by = False` 在 JOIN 查询上不生效。
        # 只在显式表态（非 None）时传递，避免用 None 覆盖掉
        # `PlanExecutor.strict_group_by` 的类属性设置。
        if self.strict_group_by is not None:
            executor.strict_group_by = self.strict_group_by
        cols, rows = executor.execute(plan)
        return self._rs(cols, rows)

    def _join_qualifiers(self, p) -> list:
        """JOIN 查询里所有可作外层限定符的名字（别名优先，其次表名）

        计划执行器的 SeqScan 用别名（没有别名则用表名）作列前缀，因此连接
        结果的列名是 `u.id` / `o.uid`；这份名单同时用于「相关判定」和
        「外层列折叠」，漏一个就会把引用连接表的子查询误判成非相关。
        """
        names = []
        items = [(p.get('table'), p.get('table_alias'))]
        for j in (p.get('joins') or []):
            items.append((j.get('table'), j.get('alias')))
        for t, a in items:
            for n in (a, t):
                if n and str(n).lower() not in [x.lower() for x in names]:
                    names.append(str(n))
        return names

    def _join_needs_row_eval(self, p, join_names) -> bool:
        """JOIN 查询里是否存在「必须逐连接行求值」的子查询

        * WHERE 里的标量比较（`(SELECT ...) > 1`）：行数语义要逐行收敛；
        * WHERE 里引用任一连接表的相关子查询（EXISTS / IN / 标量）：
          计划执行器没有逐行折叠外层列的能力（原来会静默返回空集/0 行）；
        * HAVING 里的任何子查询（分组键折叠必须逐分组做）；
        * 投影列表里的标量子查询。
        """
        where = p.get('where')
        if self._where_has_scalar_subquery(where):
            return True
        for sub in self._all_nested_subqueries(where):
            if self._is_correlated(sub, p.get('table'),
                                   p.get('table_alias'), join_names) or \
                    self._sub_owner_is_outer(
                        sub, p.get('table'), p.get('table_alias'),
                        join_names):
                return True
        if self._contains_subquery(p.get('having')):
            return True
        return any(
            self._projection_subquery_ast(self._split_alias(str(c))[0])[0]
            is not None
            for c in (p.get('columns') or []) if c != '*')

    def _exec_join_query_with_subqueries(self, p, s, rv, join_names):
        """JOIN 查询的后置处理路径：先算连接，再逐连接行处理子查询

        1. 计划执行器只算 `FROM ... JOIN ... ON ...`（投影成 `*`，列名保留
           限定前缀 `u.id` / `o.uid`）；
        2. 结果物化成内存关系，交给 `_select_correlated` 的逐行路径：
           WHERE（含标量/相关子查询）→ GROUP BY/HAVING（含标量子查询）→
           投影（含标量子查询）→ DISTINCT/ORDER BY/LIMIT+OFFSET。
        """
        from .plan_executor import PlanExecutor

        join_p = dict(p)
        join_p.update({
            'columns': ['*'], 'projection': None, 'where': None,
            'having': None, 'group_by': None, 'order_by': None,
            'limit': None, 'offset': None, 'distinct': False,
            'windows': [],
        })
        plan = self.optimizer.optimize(join_p)
        self._last_plan = plan
        executor = PlanExecutor(self.db, read_view=rv)
        # ★ 同 _exec_join_query：严格模式开关随实例传递（逃生口）
        if self.strict_group_by is not None:
            executor.strict_group_by = self.strict_group_by
        cols, rows = executor.execute(plan)
        rel = _CTERelation('join', cols, rows)
        return self._select_correlated(
            p, s, rel, rv, p.get('table'), extra_names=join_names)

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

        # ★ 与 _cmd_select 同一份静态校验：非法 SQL 在 EXPLAIN 阶段就报错，
        # 不要等真正执行时才发现（MySQL 的 ONLY_FULL_GROUP_BY 同样如此）
        self._check_strict_group_by(inner)
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