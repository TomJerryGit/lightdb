"""
分布式查询
- 分布式执行计划
- 结果集跨节点合并
- 分布式事务（简化 2PC）
"""
import json
import threading
import time
import uuid
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Callable, Optional, Any


@dataclass
class QueryFragment:
    """查询分片"""
    fragment_id: str = ""
    node_id: str = ""
    node_name: str = ""
    connection: str = ""
    sql: str = ""
    # 依赖：等待其他 fragment 完成
    depends_on: List[str] = field(default_factory=list)
    # 类型：scan / aggregate / join / final
    fragment_type: str = "scan"
    # 状态
    status: str = "pending"     # pending / running / done / failed
    result_columns: List[str] = field(default_factory=list)
    result_rows: List[List] = field(default_factory=list)
    row_count: int = 0
    elapsed_ms: float = 0
    error: str = ""
    # 输出到哪个 fragment
    output_to: str = ""

    def __post_init__(self):
        if not self.fragment_id:
            self.fragment_id = f"f_{uuid.uuid4().hex[:8]}"


@dataclass
class DistributedPlan:
    """分布式执行计划"""
    plan_id: str = ""
    original_sql: str = ""
    fragments: List[QueryFragment] = field(default_factory=list)
    # 执行模式
    mode: str = "parallel"       # parallel / sequential
    # 状态
    status: str = "pending"
    start_time: float = 0
    end_time: float = 0
    # 结果
    final_columns: List[str] = field(default_factory=list)
    final_rows: List[List] = field(default_factory=list)
    total_elapsed_ms: float = 0
    error: str = ""

    def __post_init__(self):
        if not self.plan_id:
            self.plan_id = f"plan_{uuid.uuid4().hex[:8]}"


class DistributedEngine:
    """分布式查询引擎"""

    def __init__(self, cloud_mgr=None, conn_provider=None):
        self.cloud_mgr = cloud_mgr
        self.conn_provider = conn_provider
        self.plans: Dict[str, DistributedPlan] = {}

    def _get_conn(self, name: str):
        if not self.conn_provider:
            return None
        return self.conn_provider(name)

    # ==================== 计划生成 ====================

    def build_plan(self, sql: str,
                   fragment_rules: List[Dict]) -> DistributedPlan:
        """
        根据规则构建分布式计划
        fragment_rules: [
            {
                'node_id': 'xxx',
                'sql': 'SELECT ... FROM shard1',
                'type': 'scan',
                'output_to': 'f_agg',
            },
            ...
        ]
        """
        plan = DistributedPlan(original_sql=sql)
        for r in fragment_rules:
            f = QueryFragment(
                node_id=r.get('node_id', ''),
                node_name=r.get('node_name', ''),
                connection=r.get('connection', ''),
                sql=r.get('sql', ''),
                fragment_type=r.get('type', 'scan'),
                depends_on=r.get('depends_on', []),
                output_to=r.get('output_to', ''),
            )
            plan.fragments.append(f)
        return plan

    # ==================== 执行 ====================

    def execute(self, plan: DistributedPlan,
                on_progress: Callable = None) -> DistributedPlan:
        """执行分布式计划"""
        plan.status = 'running'
        plan.start_time = time.time()

        # 按依赖拓扑排序
        ordered = self._topological_sort(plan.fragments)

        # 并行执行无依赖的
        completed = {}
        while ordered:
            # 找出所有依赖已满足的
            ready = [f for f in ordered
                     if all(d in completed for d in f.depends_on)]
            if not ready:
                plan.status = 'failed'
                plan.error = '依赖环或缺失'
                plan.end_time = time.time()
                return plan

            # 并行执行就绪的
            threads = []
            results = {}

            def _run(f):
                self._execute_fragment(f, completed, results)

            for f in ready:
                t = threading.Thread(target=_run, args=(f,))
                t.start()
                threads.append((t, f))

            for t, f in threads:
                t.join(timeout=120)

            # 收集结果
            for f in ready:
                ordered.remove(f)
                completed[f.fragment_id] = f

                if on_progress:
                    on_progress(f)

                if f.status == 'failed':
                    plan.status = 'failed'
                    plan.error = f"分片 {f.fragment_id} 失败: {f.error}"

        plan.end_time = time.time()
        plan.total_elapsed_ms = (plan.end_time - plan.start_time) * 1000

        if plan.status != 'failed':
            plan.status = 'success'
            # 收集最终结果
            self._merge_results(plan)

        self.plans[plan.plan_id] = plan
        return plan

    def _topological_sort(self, fragments: List[QueryFragment]) -> List[QueryFragment]:
        """拓扑排序"""
        fmap = {f.fragment_id: f for f in fragments}
        result = []
        visited = set()

        def visit(f):
            if f.fragment_id in visited:
                return
            visited.add(f.fragment_id)
            for dep in f.depends_on:
                if dep in fmap:
                    visit(fmap[dep])
            result.append(f)

        for f in fragments:
            visit(f)
        return result

    def _execute_fragment(self, frag: QueryFragment,
                          completed: Dict, results: Dict):
        """执行单个分片"""
        frag.status = 'running'
        t0 = time.time()

        # 特殊类型：聚合/合并
        if frag.fragment_type in ('aggregate', 'join', 'final'):
            # 从依赖的 fragment 拿数据
            input_rows = []
            input_cols = []
            for dep in frag.depends_on:
                dep_frag = completed.get(dep)
                if dep_frag and dep_frag.result_rows:
                    input_rows.extend(dep_frag.result_rows)
                    if not input_cols:
                        input_cols = dep_frag.result_columns

            # 执行本地 SQL（对输入数据的聚合）
            try:
                merged = self._apply_local_transform(
                    frag, input_cols, input_rows)
                frag.result_columns = merged['columns']
                frag.result_rows = merged['rows']
                frag.row_count = len(merged['rows'])
                frag.status = 'done'
            except Exception as e:
                frag.status = 'failed'
                frag.error = str(e)

            frag.elapsed_ms = (time.time() - t0) * 1000
            results[frag.fragment_id] = frag
            return

        # 普通扫描：直接执行 SQL
        conn = self._get_conn(frag.connection)
        if not conn:
            frag.status = 'failed'
            frag.error = f"连接 {frag.connection} 不可用"
            frag.elapsed_ms = (time.time() - t0) * 1000
            results[frag.fragment_id] = frag
            return

        try:
            cols, rows = conn.query(frag.sql)
            frag.result_columns = cols
            frag.result_rows = [list(r) for r in rows]
            frag.row_count = len(rows)
            frag.status = 'done'
        except Exception as e:
            frag.status = 'failed'
            frag.error = str(e)

        frag.elapsed_ms = (time.time() - t0) * 1000
        results[frag.fragment_id] = frag

    def _apply_local_transform(self, frag: QueryFragment,
                               input_cols: List[str],
                               input_rows: List[List]) -> Dict:
        """
        应用本地转换（聚合 / 合并）
        frag.sql 描述转换逻辑
        """
        sql = frag.sql.strip().upper()

        # 判断是否是聚合
        import re
        m = re.search(r'(\w+)\s*\(\s*(\*|\w+)\s*\)', sql, re.IGNORECASE)

        if 'UNION ALL' in sql or 'UNION' in sql:
            # 合并结果
            return {
                'columns': input_cols,
                'rows': input_rows,
            }

        if m:
            fn = m.group(1).upper()
            arg = m.group(2)

            if fn == 'COUNT':
                return {
                    'columns': ['count'],
                    'rows': [[len(input_rows)]],
                }
            elif fn in ('SUM', 'AVG', 'MIN', 'MAX') and arg in input_cols:
                ci = input_cols.index(arg)
                vals = [r[ci] for r in input_rows
                        if r[ci] is not None]
                if not vals:
                    return {'columns': [fn.lower()], 'rows': [[None]]}
                if fn == 'SUM':
                    v = sum(vals)
                elif fn == 'AVG':
                    v = sum(vals) / len(vals)
                elif fn == 'MIN':
                    v = min(vals)
                elif fn == 'MAX':
                    v = max(vals)
                return {'columns': [fn.lower()], 'rows': [[v]]}

        # 默认：合并
        return {
            'columns': input_cols,
            'rows': input_rows,
        }

    def _merge_results(self, plan: DistributedPlan):
        """合并所有 frag 的最终结果"""
        # 找到没有下游依赖的 fragment
        downstream_ids = set()
        for f in plan.fragments:
            for d in f.depends_on:
                downstream_ids.add(d)

        final_frags = [f for f in plan.fragments
                        if f.fragment_id not in downstream_ids]

        if not final_frags:
            # 全部合并
            all_cols = []
            all_rows = []
            for f in plan.fragments:
                if f.result_rows:
                    all_cols = f.result_columns
                    all_rows.extend(f.result_rows)
            plan.final_columns = all_cols
            plan.final_rows = all_rows
            return

        # 取最后一个
        last = final_frags[-1]
        plan.final_columns = last.result_columns
        plan.final_rows = last.result_rows

    # ==================== 分布式事务 ====================

    def begin_distributed_txn(self) -> Dict:
        """开启分布式事务（2PC）"""
        txn_id = f"dtxn_{uuid.uuid4().hex[:8]}"
        return {
            'txn_id': txn_id,
            'participants': [],
            'status': 'preparing',
            'started_at': time.time(),
        }

    def prepare_2pc(self, txn: Dict,
                    statements: List[Dict]) -> Dict:
        """
        两阶段提交：准备阶段
        statements: [{'connection': 'db1', 'sql': '...'}]
        """
        prepared = []
        for s in statements:
            conn = self._get_conn(s['connection'])
            if not conn:
                return {
                    'ok': False,
                    'error': f"连接 {s['connection']} 不可用",
                }
            try:
                # 模拟准备（实际应开启事务）
                prepared.append({
                    'connection': s['connection'],
                    'sql': s['sql'],
                    'prepared': True,
                })
            except Exception as e:
                return {'ok': False, 'error': str(e)}

        txn['prepared'] = prepared
        txn['status'] = 'prepared'
        return {'ok': True, 'txn': txn, 'prepared': len(prepared)}

    def commit_2pc(self, txn: Dict) -> Dict:
        """提交阶段"""
        results = []
        for p in txn.get('prepared', []):
            conn = self._get_conn(p['connection'])
            if not conn:
                results.append({
                    'connection': p['connection'],
                    'status': 'failed',
                    'error': '连接不可用',
                })
                continue
            try:
                rows, err = conn.execute(p['sql'])
                if err:
                    results.append({
                        'connection': p['connection'],
                        'status': 'failed',
                        'error': err,
                    })
                else:
                    results.append({
                        'connection': p['connection'],
                        'status': 'committed',
                        'rows': rows,
                    })
            except Exception as e:
                results.append({
                    'connection': p['connection'],
                    'status': 'failed',
                    'error': str(e),
                })

        txn['status'] = 'committed'
        txn['committed_at'] = time.time()
        return {'ok': True, 'results': results}

    def rollback_2pc(self, txn: Dict) -> Dict:
        """回滚"""
        txn['status'] = 'rolled_back'
        txn['rolled_back_at'] = time.time()
        return {'ok': True}