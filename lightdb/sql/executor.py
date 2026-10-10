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
        return self.execute(parsed, session or {})

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

        tbl = self.db.get_table(table)

        txn = s.get('txn')
        if txn:
            rv = txn['read_view']
        else:
            tid = self.db.mvcc.next_txn_id()
            rv = self.db.mvcc.create_readview(tid)

        # GROUP BY / DISTINCT / 窗口函数：都要在拿到行之后再加工
        # （原实现的 DISTINCT 只在解析器里记录，执行路径从未使用，属于静默错答）
        if (p.get('windows') or p.get('group_by')
                or p.get('distinct')):
            return self._select_advanced(p, tbl, read_view=rv)

        cols, rows = tbl.select(
            p['columns'], p.get('where'),
            p.get('order_by'), p.get('limit'),
            read_view=rv)
        return self._rs(cols, rows)

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

    def _select_advanced(self, p, src, read_view=None):
        """GROUP BY / 窗口函数 / 普通投影的统一执行路径

        分组与窗口都必须先拿到「全部列的行」，因此流程固定为：
        取基础行 → （可选）分组聚合 → （可选）计算窗口 → 投影 → DISTINCT/ORDER BY/LIMIT。
        """
        base_cols, rows = self._base_rows_and_cols(
            src, p.get('where'), read_view)
        if p.get('group_by'):
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
            if agg_eval:
                # 复用存储层的聚合实现（Table._aggregate）
                vals += list(_StorageTable._aggregate(
                    src, groups[key], agg_eval))
            out_rows.append(vals)
        return keys + agg_cols, out_rows

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
        for kind, text in projection:
            if kind == 'win':
                continue
            expr, alias = self._split_alias(str(text))
            out_cols.append(alias or expr)
            idx.append(self._col_index_of(base_cols, str(text)))

        out_rows = [[r[i] if 0 <= i < len(r) else None for i in idx]
                    for r in rows]
        return self._finish_select(p, out_cols, out_rows)

    def _select_from_relation(self, p, s, rel):
        """从 CTE 内存关系里查询"""
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
            if col in out_cols:
                i = out_cols.index(col)
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