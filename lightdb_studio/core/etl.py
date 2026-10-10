"""
实时 ETL
- 可视化管道编排
- 转换算子：filter / map / aggregate / join
- 流式执行
"""
import json
import re
import threading
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Callable, Optional, Any


ETL_FILE = Path.home() / '.lightdb_studio' / 'etl_pipelines.json'


@dataclass
class ETLOperator:
    """ETL 算子"""
    op_type: str = "source"     # source / filter / map / aggregate / join / sink
    name: str = ""
    params: Dict = field(default_factory=dict)
    position_x: int = 0
    position_y: int = 0
    # 唯一 id
    op_id: str = ""


@dataclass
class ETLPipeline:
    name: str = ""
    description: str = ""
    operators: List[ETLOperator] = field(default_factory=list)
    connections: List[Dict] = field(default_factory=list)
    # {'from': op_id, 'to': op_id}
    schedule: str = "manual"    # manual / interval / cron
    interval_seconds: int = 3600
    enabled: bool = True
    # 状态
    last_run: float = 0
    last_status: str = ""
    run_count: int = 0
    error_count: int = 0
    created_at: float = 0
    updated_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


class ETLRunner:
    """ETL 执行器"""

    def __init__(self, conn_provider: Callable = None,
                 lake_engine=None):
        self.conn_provider = conn_provider
        self.lake_engine = lake_engine

    def run_pipeline(self, pipeline: ETLPipeline,
                     on_progress: Callable = None) -> Dict:
        """执行管道"""
        result = {
            'success': False,
            'steps': [],
            'rows_in': 0,
            'rows_out': 0,
            'error': '',
        }

        # 1. 构建算子图
        op_map = {op.op_id: op for op in pipeline.operators}
        if not op_map:
            result['error'] = "无算子"
            return result

        # 邻接表
        adj = {}
        in_deg = {}
        for op in pipeline.operators:
            adj.setdefault(op.op_id, [])
            in_deg.setdefault(op.op_id, 0)

        for conn in pipeline.connections:
            src = conn.get('from')
            dst = conn.get('to')
            if src in op_map and dst in op_map:
                adj[src].append(dst)
                in_deg[dst] = in_deg.get(dst, 0) + 1

        # 2. 拓扑排序
        from collections import deque
        queue = deque([oid for oid, deg in in_deg.items()
                       if deg == 0])
        order = []
        while queue:
            oid = queue.popleft()
            order.append(oid)
            for nxt in adj.get(oid, []):
                in_deg[nxt] -= 1
                if in_deg[nxt] == 0:
                    queue.append(nxt)

        # 3. 逐算子执行
        data_store = {}   # op_id -> (cols, rows)

        for i, oid in enumerate(order):
            op = op_map[oid]
            if on_progress:
                on_progress(i, len(order), op.name or op.op_type)

            try:
                cols, rows = self._execute_op(
                    op, adj.get(oid, []), data_store)
                data_store[oid] = (cols, rows)
                result['steps'].append({
                    'op': op.name or op.op_type,
                    'rows': len(rows),
                    'status': 'success',
                })

                if op.op_type == 'source':
                    result['rows_in'] += len(rows)
                if op.op_type == 'sink':
                    result['rows_out'] += len(rows)

            except Exception as e:
                result['steps'].append({
                    'op': op.name or op.op_type,
                    'status': 'failed',
                    'error': str(e),
                })
                result['error'] = str(e)
                return result

        result['success'] = True
        return result

    def _execute_op(self, op: ETLOperator,
                    next_ops: List[str],
                    data_store: Dict) -> tuple:
        """执行单个算子"""
        t = op.op_type
        p = op.params

        if t == 'source':
            return self._op_source(p)
        elif t == 'filter':
            return self._op_filter(p, data_store)
        elif t == 'map':
            return self._op_map(p, data_store)
        elif t == 'aggregate':
            return self._op_aggregate(p, data_store)
        elif t == 'join':
            return self._op_join(p, data_store)
        elif t == 'sort':
            return self._op_sort(p, data_store)
        elif t == 'limit':
            return self._op_limit(p, data_store)
        elif t == 'sink':
            return self._op_sink(p, data_store)
        else:
            raise ValueError(f"未知算子: {t}")

    def _op_source(self, p: Dict) -> tuple:
        """源：数据库查询 / 数据湖文件 / 虚拟表"""
        src_type = p.get('source_type', 'sql')

        if src_type == 'sql':
            conn_name = p.get('connection', '')
            sql = p.get('sql', '')
            conn = self.conn_provider(conn_name) \
                if self.conn_provider else None
            if not conn:
                raise RuntimeError(f"连接 {conn_name} 不可用")
            return conn.query(sql)

        elif src_type == 'lake':
            table_name = p.get('table', '')
            if not self.lake_engine:
                raise RuntimeError("数据湖引擎未初始化")
            return self.lake_engine.query(table_name)

        else:
            raise ValueError(f"未知源类型: {src_type}")

    def _op_filter(self, p: Dict, data_store: Dict) -> tuple:
        """过滤"""
        upstream = p.get('upstream', '')
        if upstream not in data_store:
            raise ValueError("上游无数据")
        cols, rows = data_store[upstream]
        condition = p.get('condition', '')

        if not condition:
            return cols, rows

        # 解析条件
        conditions = re.split(r'\s+AND\s+', condition,
                               flags=re.IGNORECASE)
        result = list(rows)
        for cond in conditions:
            m = re.match(
                r'[`"]?(\w+)[`"]?\s*'
                r'(=|!=|<>|>=|<=|>|<|LIKE|IN)\s*(.+?)$',
                cond.strip(), re.IGNORECASE)
            if not m:
                continue
            col, op, val = m.group(1), m.group(2).upper(), m.group(3)
            if col not in cols:
                continue
            ci = cols.index(col)

            if op == 'IN':
                # (a, b, c)
                inner = val.strip('() ')
                items = [x.strip().strip("'\"")
                         for x in inner.split(',')]
                result = [r for r in result if str(r[ci]) in items]
                continue

            val_str = val.strip().strip("'\"")
            try:
                val_num = float(val_str)
            except ValueError:
                val_num = None

            new_rows = []
            for r in result:
                v = r[ci]
                try:
                    if val_num is not None and \
                            isinstance(v, (int, float)):
                        cmp = val_num
                    else:
                        cmp = val_str
                    ok = self._compare(v, op, cmp)
                except (TypeError, ValueError):
                    ok = False
                if ok:
                    new_rows.append(r)
            result = new_rows

        return cols, result

    def _compare(self, v, op, cmp):
        if v is None:
            return False
        if op == '=': return v == cmp
        if op in ('!=', '<>'): return v != cmp
        if op == '>': return v > cmp
        if op == '<': return v < cmp
        if op == '>=': return v >= cmp
        if op == '<=': return v <= cmp
        if op == 'LIKE':
            pat = str(cmp).replace('%', '.*').replace('_', '.')
            return bool(re.match('^' + pat + '$', str(v)))
        return False

    def _op_map(self, p: Dict, data_store: Dict) -> tuple:
        """字段映射 / 表达式"""
        upstream = p.get('upstream', '')
        if upstream not in data_store:
            raise ValueError("上游无数据")
        cols, rows = data_store[upstream]

        mapping = p.get('mapping', {})  # {new_col: expr}
        if not mapping:
            return cols, rows

        new_cols = list(mapping.keys())
        new_rows = []
        for r in rows:
            row_dict = dict(zip(cols, r))
            new_row = []
            for new_col, expr in mapping.items():
                v = self._eval_expr(expr, row_dict)
                new_row.append(v)
            new_rows.append(new_row)

        return new_cols, new_rows

    def _eval_expr(self, expr: str, row: Dict):
        """简单表达式求值"""
        expr = expr.strip()

        # 直接列名
        if expr in row:
            return row[expr]

        # 数字
        try:
            if '.' in expr:
                return float(expr)
            return int(expr)
        except ValueError:
            pass

        # 字符串
        if expr.startswith("'") and expr.endswith("'"):
            return expr[1:-1]

        # 函数：UPPER(col), LOWER(col), ABS(col), CONCAT(...)
        m = re.match(r'UPPER\s*\(\s*(\w+)\s*\)', expr, re.IGNORECASE)
        if m:
            v = row.get(m.group(1))
            return str(v).upper() if v is not None else None

        m = re.match(r'LOWER\s*\(\s*(\w+)\s*\)', expr, re.IGNORECASE)
        if m:
            v = row.get(m.group(1))
            return str(v).lower() if v is not None else None

        m = re.match(r'ABS\s*\(\s*(\w+)\s*\)', expr, re.IGNORECASE)
        if m:
            v = row.get(m.group(1))
            return abs(v) if isinstance(v, (int, float)) else None

        m = re.match(r'LENGTH\s*\(\s*(\w+)\s*\)', expr, re.IGNORECASE)
        if m:
            v = row.get(m.group(1))
            return len(str(v)) if v is not None else 0

        m = re.match(r'CONCAT\s*\((.+)\)', expr, re.IGNORECASE | re.DOTALL)
        if m:
            parts = [p.strip() for p in m.group(1).split(',')]
            result = ''
            for p in parts:
                v = self._eval_expr(p, row)
                result += str(v) if v is not None else ''
            return result

        # 二元运算 col * 2 / col1 + col2
        m = re.match(r'(\w+)\s*([+\-*/])\s*(.+)', expr)
        if m:
            a = row.get(m.group(1))
            op = m.group(2)
            b_expr = m.group(3).strip()
            try:
                b = float(b_expr)
            except ValueError:
                b = row.get(b_expr)

            if a is None or b is None:
                return None
            try:
                if op == '+': return a + b
                if op == '-': return a - b
                if op == '*': return a * b
                if op == '/': return a / b if b != 0 else None
            except TypeError:
                return None

        return None

    def _op_aggregate(self, p: Dict, data_store: Dict) -> tuple:
        """聚合"""
        upstream = p.get('upstream', '')
        if upstream not in data_store:
            raise ValueError("上游无数据")
        cols, rows = data_store[upstream]

        group_by = p.get('group_by', [])
        aggs = p.get('aggregations', {})   # {alias: 'SUM(col)'}

        # 分组
        groups = {}
        for r in rows:
            key = tuple(
                r[cols.index(g)] for g in group_by
                if g in cols)
            groups.setdefault(key, []).append(r)

        new_cols = list(group_by) + list(aggs.keys())
        new_rows = []

        for key, group_rows in groups.items():
            row = list(key)
            for alias, expr in aggs.items():
                val = self._compute_agg(expr, group_rows, cols)
                row.append(val)
            new_rows.append(row)

        return new_cols, new_rows

    def _compute_agg(self, expr: str, rows: List,
                     cols: List) -> Any:
        m = re.match(
            r'(\w+)\s*\(\s*(\*|\w+)\s*\)', expr, re.IGNORECASE)
        if not m:
            return None
        fn = m.group(1).upper()
        arg = m.group(2)

        if fn == 'COUNT':
            if arg == '*':
                return len(rows)
            ci = cols.index(arg) if arg in cols else -1
            if ci < 0:
                return 0
            return sum(1 for r in rows if r[ci] is not None)

        if arg not in cols:
            return None
        ci = cols.index(arg)
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
        return None

    def _op_join(self, p: Dict, data_store: Dict) -> tuple:
        """JOIN"""
        left = p.get('left', '')
        right = p.get('right', '')
        if left not in data_store or right not in data_store:
            raise ValueError("上游无数据")

        lcols, lrows = data_store[left]
        rcols, rrows = data_store[right]

        on = p.get('on', [])   # [{'left': 'id', 'right': 'id'}, ...]
        join_type = p.get('join_type', 'INNER')

        # 简化：假设单键
        if not on:
            return lcols + rcols, [list(a) + list(b)
                                    for a in lrows for b in rrows]

        lk = on[0].get('left', '')
        rk = on[0].get('right', '')
        if lk not in lcols or rk not in rcols:
            raise ValueError("JOIN 列不存在")

        li = lcols.index(lk)
        ri = rcols.index(rk)

        hash_table = {}
        for r in rrows:
            hash_table.setdefault(r[ri], []).append(r)

        new_cols = lcols + rcols
        new_rows = []
        for lr in lrows:
            key = lr[li]
            matches = hash_table.get(key, [])
            if matches:
                for rr in matches:
                    new_rows.append(list(lr) + list(rr))
            elif join_type in ('LEFT', 'LEFT OUTER'):
                new_rows.append(list(lr) + [None] * len(rcols))

        return new_cols, new_rows

    def _op_sort(self, p: Dict, data_store: Dict) -> tuple:
        upstream = p.get('upstream', '')
        if upstream not in data_store:
            raise ValueError("上游无数据")
        cols, rows = data_store[upstream]
        by = p.get('by', '')
        desc = p.get('desc', False)
        if by not in cols:
            return cols, rows
        ci = cols.index(by)
        rows = sorted(rows, key=lambda r: (r[ci] is None, r[ci]),
                       reverse=desc)
        return cols, rows

    def _op_limit(self, p: Dict, data_store: Dict) -> tuple:
        upstream = p.get('upstream', '')
        if upstream not in data_store:
            raise ValueError("上游无数据")
        cols, rows = data_store[upstream]
        n = p.get('n', 100)
        return cols, rows[:n]

    def _op_sink(self, p: Dict, data_store: Dict) -> tuple:
        """写入目标"""
        upstream = p.get('upstream', '')
        if upstream not in data_store:
            raise ValueError("上游无数据")
        cols, rows = data_store[upstream]

        sink_type = p.get('sink_type', 'table')
        if sink_type == 'table':
            conn_name = p.get('connection', '')
            table = p.get('table', '')
            conn = self.conn_provider(conn_name) \
                if self.conn_provider else None
            if not conn:
                raise RuntimeError(f"连接 {conn_name} 不可用")

            if not rows:
                return cols, rows

            col_str = ', '.join(f"`{c}`" for c in cols)
            ph = ', '.join(['%s'] * len(cols))
            sql = f"INSERT INTO `{table}` ({col_str}) VALUES ({ph})"

            batch = 500
            for i in range(0, len(rows), batch):
                conn.execute_many(sql, rows[i:i + batch])

        return cols, rows


class ETLManager:
    """ETL 管理器"""

    def __init__(self, conn_provider=None, lake_engine=None):
        self.file = ETL_FILE
        self.conn_provider = conn_provider
        self.lake_engine = lake_engine
        self.pipelines: Dict[str, ETLPipeline] = {}
        self.runner = ETLRunner(conn_provider, lake_engine)
        self.history: List[Dict] = []
        self._running = False
        self._thread = None
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for name, pd in data.get('pipelines', {}).items():
                ops = [ETLOperator(**o)
                       for o in pd.pop('operators', [])]
                p = ETLPipeline(**pd)
                p.operators = ops
                self.pipelines[name] = p
            self.history = data.get('history', [])
        except Exception:
            pass

    def save(self):
        data = {
            'pipelines': {
                n: {**asdict(p),
                    'operators': [asdict(o) for o in p.operators]}
                for n, p in self.pipelines.items()
            },
            'history': self.history[-200:],
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def add_pipeline(self, p: ETLPipeline):
        self.pipelines[p.name] = p
        self.save()

    def update_pipeline(self, name: str, p: ETLPipeline):
        p.updated_at = time.time()
        self.pipelines[name] = p
        self.save()

    def remove_pipeline(self, name: str):
        if name in self.pipelines:
            del self.pipelines[name]
            self.save()

    def get(self, name: str) -> Optional[ETLPipeline]:
        return self.pipelines.get(name)

    def run_now(self, name: str) -> Dict:
        p = self.pipelines.get(name)
        if not p:
            return {'error': '管道不存在'}

        result = self.runner.run_pipeline(p)
        p.last_run = time.time()
        p.last_status = 'success' if result['success'] else 'failed'
        p.run_count += 1
        if not result['success']:
            p.error_count += 1

        self.history.append({
            'pipeline': name,
            'ts': time.time(),
            'status': p.last_status,
            'rows_in': result.get('rows_in', 0),
            'rows_out': result.get('rows_out', 0),
            'error': result.get('error', ''),
        })
        self.save()
        return result

    def start_scheduler(self):
        """启动调度循环"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scheduler_loop, daemon=True, name='ETLScheduler')
        self._thread.start()

    def stop_scheduler(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _scheduler_loop(self):
        last_run = {}
        while self._running:
            now = time.time()
            for name, p in self.pipelines.items():
                if not p.enabled:
                    continue
                if p.schedule != 'interval':
                    continue
                last = last_run.get(name, 0)
                if now - last >= p.interval_seconds:
                    try:
                        self.run_now(name)
                    except Exception:
                        pass
                    last_run[name] = now
            time.sleep(1)

    def stats(self) -> dict:
        return {
            'total_pipelines': len(self.pipelines),
            'enabled': sum(1 for p in self.pipelines.values() if p.enabled),
            'total_runs': sum(p.run_count for p in self.pipelines.values()),
            'total_errors': sum(p.error_count
                                 for p in self.pipelines.values()),
        }