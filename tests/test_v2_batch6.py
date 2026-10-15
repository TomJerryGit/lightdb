"""v2.0 Batch 6 测试 - Mesh/治理/ReAct/影子表/可观测性"""
import os
import sys
import json
import time
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class FakeConn:
    def __init__(self, tables=None):
        self.tables = tables or {}
        self.created = []
        self.dropped = []

    def query(self, sql, params=None):
        sql_l = sql.lower()
        if 'show create table' in sql_l:
            import re
            m = re.search(r'`?(\w+)`?', sql.split('TABLE')[-1])
            tname = m.group(1) if m else 'unknown'
            return ['Table', 'Create Table'], [(
                tname,
                f"CREATE TABLE `{tname}` (id INT, name VARCHAR(50)) "
                f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")]

        for tname, (cols, rows) in self.tables.items():
            if tname.lower() in sql_l:
                if 'count(*)' in sql_l:
                    return ['count'], [(len(rows),)]
                return list(cols), list(rows)
        return [], []

    def get_columns(self, t):
        for tname, (cols, rows) in self.tables.items():
            if tname == t:
                return [{'name': c, 'type': 'VARCHAR', 'key': '',
                          'null': 'YES'} for c in cols]
        return []

    def get_tables(self):
        """修正（E 类 #24）：连接接口的既有契约包含 get_tables()。

        ReActAgent._tool_list_tables 调 conn.get_tables()（agent_react.py:256），
        项目 DBConnection.get_tables（lightdb_studio/db/connection.py:129）与全仓
        20 处调用都按此签名；原桩漏了它，工具返回 {'error': ...} 才导致
        KeyError: 'tables' —— 是桩不完整，不是引擎缺陷。
        """
        return list(self.tables)

    def execute(self, sql, params=None):
        if 'create table' in sql.lower():
            self.created.append(sql)
        elif 'drop table' in sql.lower():
            self.dropped.append(sql)
        return 1, None

    def execute_many(self, sql, params):
        return len(params), None

    class Config:
        database = 'test'


FakeConn.config = FakeConn.Config


def test_data_mesh():
    """测试1: 数据 Mesh"""
    print("\n【测试1】数据 Mesh")
    from lightdb_studio.core.data_mesh import DataMeshManager

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.data_mesh as dm
    old = dm.MESH_FILE
    dm.MESH_FILE = Path(tmp) / 'mesh.json'

    try:
        mgr = DataMeshManager()

        # 创建域
        d1 = mgr.create_domain('用户中心', '用户相关数据', owner='alice')
        d2 = mgr.create_domain('交易中心', '订单交易数据', owner='bob')
        d3 = mgr.create_domain('BI', '报表分析', owner='carol')
        print(f"  ✅ 创建 3 个域")

        # 创建数据产品
        p1 = mgr.create_product(
            '用户主数据', '用户中心',
            output_type='table', output_ref='db.users',
            owner='alice', tier='critical',
            tags=['核心'], description='所有用户基础数据')
        p2 = mgr.create_product(
            '订单数据', '交易中心',
            output_type='table', output_ref='db.orders',
            owner='bob', tier='important')
        p3 = mgr.create_product(
            '销售报表', 'BI',
            output_type='view', output_ref='db.sales_report',
            owner='carol')
        print(f"  ✅ 创建 3 个产品")

        # 声明依赖
        mgr.update_product(p3.product_id, inputs=[
            {'product_id': p2.product_id, 'role': 'source'},
            {'product_id': p1.product_id, 'role': 'source'},
        ])

        # 创建契约
        c = mgr.create_contract(p1.product_id, p3.product_id,
                                 sla={'freshness': 3600})
        print(f"  ✅ 创建契约: {c.contract_id[:8]}")

        c2 = mgr.create_contract(p2.product_id, p3.product_id)
        print(f"  ✅ 创建契约 2")

        # 域地图
        domain_map = mgr.domain_map()
        print(f"  → 域地图: {len(domain_map['nodes'])} 节点, "
              f"{len(domain_map['edges'])} 边")
        for n in domain_map['nodes']:
            print(f"    {n['name']}: {n['product_count']} 产品, "
                  f"in={n['inflow']}, out={n['outflow']}")

        # 产品血缘
        lineage = mgr.product_lineage(p3.product_id)
        print(f"  → 销售报表上游: {len(lineage['upstream'])} 个")
        for u in lineage['upstream']:
            print(f"    {u['name']} ({u['domain']})")

        # 搜索
        results = mgr.search_products(keyword='用户')
        print(f"  → 搜索 '用户': {len(results)} 结果")

        results = mgr.search_products(tier='critical')
        print(f"  → 关键产品: {len(results)}")

        # 统计
        stats = mgr.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_domains'] == 3
        assert stats['total_products'] == 3
        assert stats['total_contracts'] == 2

    finally:
        dm.MESH_FILE = old


def test_federated_governance():
    """测试2: 联邦治理"""
    print("\n【测试2】联邦治理")
    import lightdb_studio.core.federated_gov as fg
    tmp = tempfile.mkdtemp()
    old = fg.GOV_FILE
    fg.GOV_FILE = Path(tmp) / 'gov.json'

    try:
        gov = fg.FederatedGovernance()

        # 添加策略
        gov.add_policy(
            '禁止生产 DROP', 'access',
            {'resource_pattern': 'prod.*',
             'deny_actions': ['DROP', 'TRUNCATE']},
            'connection', 'prod', priority=100,
            description='保护生产环境')
        gov.add_policy(
            '工作时间限制', 'access',
            {'resource_pattern': 'prod.*',
             'time_windows': [{'start_hour': 8, 'end_hour': 22}]},
            'global', '', priority=50)
        print(f"  ✅ 添加 2 条策略")

        # 检查 - 违反
        result = gov.check_policy({
            'user': 'alice',
            'action': 'DROP',
            'resource': 'prod.users',
            'connection': 'prod',
        })
        print(f"  → DROP prod.users:")
        print(f"    allowed: {result['allowed']}")
        for v in result['violations']:
            print(f"    违规: {v['detail']}")

        assert not result['allowed']

        # 检查 - 允许
        result = gov.check_policy({
            'user': 'alice',
            'action': 'SELECT',
            'resource': 'prod.users',
            'connection': 'prod',
        })
        print(f"  → SELECT prod.users: allowed={result['allowed']}")
        assert result['allowed']

        # 分类策略
        gov.add_policy(
            'PII 必须脱敏', 'classification',
            {'min_classification': 'pii', 'require_masking': True},
            'global', '', priority=80)

        result = gov.check_policy({
            'user': 'bob',
            'action': 'SELECT',
            'resource': 'db.users',
            'sensitivity': 'pii',
            'masked': False,
        })
        print(f"  → PII 未脱敏: allowed={result['allowed']}")
        assert not result['allowed']

        result = gov.check_policy({
            'user': 'bob',
            'action': 'SELECT',
            'resource': 'db.users',
            'sensitivity': 'pii',
            'masked': True,
        })
        print(f"  → PII 已脱敏: allowed={result['allowed']}")
        assert result['allowed']

        # 审计日志
        log = gov.get_audit_log(limit=10)
        print(f"  → 审计日志: {len(log)} 条")
        for l in log[:3]:
            print(f"    [{l['result']}] {l['user']} {l['action']} "
                  f"{l['resource']}")

        # 默认策略
        n = gov.add_default_policies()
        print(f"  → 添加 {n} 条默认策略")

        # 统计
        stats = gov.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_policies'] >= 5

    finally:
        fg.GOV_FILE = old


def test_react_agent():
    """测试3: ReAct Agent"""
    print("\n【测试3】ReAct Agent 2.0")
    from lightdb_studio.core.agent_react import ReActAgent, ToolSpec

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.agent_react as ar
    old = ar.MEMORY_FILE
    ar.MEMORY_FILE = Path(tmp) / 'memory.json'

    try:
        # 模拟连接
        conn = FakeConn({
            'users': (['id', 'name', 'age'],
                       [(1, 'Alice', 30), (2, 'Bob', 25)]),
        })

        def provider():
            return conn

        # 模拟 schema
        def schema():
            return ['users'], {'users': ['id', 'name', 'age']}

        agent = ReActAgent(
            conn_provider=provider,
            schema_provider=schema,
            confirm_callback=lambda msg: True,
        )

        # 工具数
        print(f"  → 已注册 {len(agent.tools)} 个工具")
        for name in agent.tools:
            t = agent.tools[name]
            danger = ' ⚠️' if t.dangerous else ''
            print(f"    - {name}{danger}: {t.description}")

        # 直接测试工具
        result = agent._tool_list_tables()
        print(f"  → list_tables: {result}")
        assert 'users' in result['tables']

        result = agent._tool_describe_table('users')
        print(f"  → describe_table: {len(result['columns'])} 列")

        result = agent._tool_execute_query(
            'SELECT * FROM users', limit=10)
        print(f"  → execute_query: {result['row_count']} 行")

        # 安全检查：拒绝写操作
        result = agent._tool_execute_query('DROP TABLE users')
        assert 'error' in result
        print(f"  ✅ 写操作被拒绝: {result['error']}")

        # ReAct 响应解析
        response = """**Thought**: 用户想知道有多少用户

**Action**: execute_query
**Parameters**:
```json
{"sql": "SELECT COUNT(*) FROM users"}
```"""
        parsed = agent._parse_react_response(response)
        assert parsed['type'] == 'action'
        assert parsed['tool'] == 'execute_query'
        print(f"  ✅ 解析 Action: tool={parsed['tool']}")

        # Answer 响应
        response2 = """**Thought**: 已经得到答案

**Answer**: 当前共有 2 个用户。"""
        parsed2 = agent._parse_react_response(response2)
        assert parsed2['type'] == 'answer'
        assert '2' in parsed2['content']
        print(f"  ✅ 解析 Answer")

        # 记忆
        agent.memory.add('如何查询用户数', 'SELECT COUNT(*) FROM users')
        hits = agent.memory.search('用户数')
        print(f"  → 记忆检索: {len(hits)} 条")
        assert len(hits) >= 1

        # 统计
        stats = agent.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_tools'] >= 9
        assert stats['dangerous_tools'] >= 1

    finally:
        ar.MEMORY_FILE = old


def test_shadow_table():
    """测试4: 影子表"""
    print("\n【测试4】影子表")
    import lightdb_studio.core.shadow_table as st
    import lightdb_studio.core.masking as mk

    tmp = tempfile.mkdtemp()
    old = st.SHADOW_FILE
    old_mask = mk.MASK_RULES_FILE
    st.SHADOW_FILE = Path(tmp) / 'shadow.json'
    mk.MASK_RULES_FILE = Path(tmp) / 'masking.json'

    try:
        # 模拟连接
        conn = FakeConn({
            'users': (['id', 'name', 'phone'],
                       [(1, 'Alice', '13812345678'),
                        (2, 'Bob', '13987654321')]),
        })

        def provider(name):
            return conn

        # 脱敏引擎
        masking = mk.MaskingEngine()
        masking.rules = []
        masking.rules.append(mk.MaskingRule(
            column_pattern='phone', strategy='phone'))

        mgr = st.ShadowTableManager(
            conn_provider=provider, masking_engine=masking)

        # 创建影子表
        shadow = mgr.create_shadow(
            'users', 'test', sample_size=100,
            ttl_hours=1,
            masked_columns=['phone'],
            created_by='alice')
        print(f"  ✅ 创建影子表: {shadow.name}")
        print(f"    源: {shadow.source_table}")
        print(f"    行数: {shadow.row_count}")
        print(f"    脱敏: {shadow.masked_columns}")
        print(f"    剩余: {shadow.remaining_hours():.2f} 小时")

        assert shadow.source_table == 'users'
        assert shadow.row_count == 2
        assert 'phone' in shadow.masked_columns

        # 结构已创建
        assert len(conn.created) >= 1
        print(f"  ✅ CREATE TABLE 已执行")

        # 列表
        active = mgr.list_active()
        assert len(active) == 1
        print(f"  → 活跃影子表: {len(active)}")

        # 删除
        assert mgr.drop_shadow(shadow.shadow_id)
        assert len(conn.dropped) >= 1
        print(f"  ✅ DROP TABLE 已执行")

        # 统计
        stats = mgr.stats()
        print(f"  → 统计: {stats}")

    finally:
        st.SHADOW_FILE = old
        mk.MASK_RULES_FILE = old_mask


def test_observability():
    """测试5: 数据可观测性"""
    print("\n【测试5】数据可观测性")
    from lightdb_studio.core.observability import ObservabilityEngine

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.observability as ob
    old = ob.OBS_FILE
    ob.OBS_FILE = Path(tmp) / 'obs.json'

    try:
        engine = ObservabilityEngine()

        # 记录追踪
        t1 = engine.start_trace(
            'query_users', 'query', user='alice',
            sql='SELECT * FROM users')
        engine.add_span(t1, 'parse', 2.5)
        engine.add_span(t1, 'execute', 15.3)
        time.sleep(0.01)
        engine.end_trace(t1, status='ok',
                          resources=['db.users'])

        t2 = engine.start_trace(
            'etl_sync', 'etl', user='system',
            source='orders', target='orders_summary')
        engine.add_span(t2, 'extract', 50)
        engine.add_span(t2, 'transform', 30)
        time.sleep(0.02)
        engine.end_trace(t2, status='error',
                          error='Connection timeout',
                          resources=['db.orders', 'db.orders_summary'])

        t3 = engine.start_trace(
            'api_call', 'api', user='external')
        time.sleep(0.01)
        engine.end_trace(t3, status='ok',
                          resources=['api/users_api'])

        print(f"  ✅ 记录 3 条追踪")

        # 查询
        traces = engine.recent_traces(limit=10)
        print(f"  → 最近追踪: {len(traces)}")
        for t in traces:
            print(f"    {t['name']} [{t['trace_type']}] "
                  f"{t['duration_ms']:.2f}ms {t['status']}")

        # 质量事件
        e1 = engine.record_event(
            'anomaly', 'db.users', 'null_ratio',
            value=0.5, threshold=0.2, severity='warning',
            detail='email 列空值率异常')
        print(f"  ✅ 记录事件: {e1.event_id[:8]}")

        e2 = engine.record_event(
            'breach', 'db.orders', 'row_count',
            value=0, threshold=1000, severity='critical',
            detail='订单表行数为 0')

        # 查询事件
        events = engine.get_events(resolved=False, limit=10)
        print(f"  → 未解决事件: {len(events)}")

        # 根因分析
        rca = engine.root_cause_analysis(e1.event_id)
        print(f"  → 根因分析:")
        print(f"    疑似追踪: {len(rca['suspected_traces'])}")
        print(f"    时间线: {len(rca['timeline'])} 项")
        print(f"    假设:")
        for h in rca['hypotheses']:
            print(f"      [{h['probability']*100:.0f}%] "
                  f"{h['hypothesis']}")

        # 解决
        engine.resolve_event(e1.event_id)
        events = engine.get_events(resolved=False)
        print(f"  → 解决后未处理: {len(events)}")
        assert len(events) == 1   # e2 还未解决

        # 统计
        stats = engine.stats()
        print(f"  → 统计:")
        print(f"    总追踪: {stats['total_traces']}")
        print(f"    错误率: {stats['error_rate']:.2%}")
        print(f"    未解决事件: {stats['unresolved_events']}")

        assert stats['total_traces'] == 3

    finally:
        ob.OBS_FILE = old


def main():
    print("=" * 60)
    print("  v2.0 Batch 6 测试 - Mesh/治理/ReAct/影子表/可观测性")
    print("=" * 60)

    tests = [
        ('数据 Mesh', test_data_mesh),
        ('联邦治理', test_federated_governance),
        ('ReAct Agent', test_react_agent),
        ('影子表', test_shadow_table),
        ('数据可观测性', test_observability),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ {name} 失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"  通过: {passed}  失败: {failed}")
    if failed == 0:
        print("  🎉 v2.0 Batch 6 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())