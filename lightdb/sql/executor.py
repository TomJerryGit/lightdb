"""
SQL 执行器 - 结构化结果（修复版）

修复:
- __init__ 初始化 self.optimizer（原 bug: AttributeError）
- 加 start_event_scheduler / stop_event_scheduler
- 加 stats_manager
"""
import time
from typing import Dict

from ..logger import logger
from ..db.information_schema import InformationSchema


class Executor:

    def __init__(self, db):
        self.db = db
        self.info_schema = InformationSchema(db)
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
        for row in values_list:
            tbl.insert(row)
            n += 1

        self.stats['rows'] += n
        return self._ok(
            affected=n, message=f"{n} row(s) inserted")

    def _cmd_select(self, p, s):
        table = p['table']

        # information_schema
        if table.lower().startswith('information_schema.'):
            _, name = table.split('.', 1)
            cols, rows = self.info_schema.query(name)
            return self._rs(cols, rows)
        if table.upper() in ('TABLES', 'COLUMNS', 'SCHEMATA'):
            cols, rows = self.info_schema.query(table)
            return self._rs(cols, rows)

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

        cols, rows = tbl.select(
            p['columns'], p.get('where'),
            p.get('order_by'), p.get('limit'),
            read_view=rv)
        return self._rs(cols, rows)

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