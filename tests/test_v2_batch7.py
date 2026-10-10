"""v2.0 Batch 7 测试 - 多云/分布式/自治/编织/数字孪生"""
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

    def query(self, sql, params=None):
        sql_l = sql.lower()
        for tname, (cols, rows) in self.tables.items():
            if tname.lower() in sql_l:
                if 'count(*)' in sql_l:
                    return ['count'], [(len(rows),)]
                return list(cols), list(rows)
        if 'show status' in sql_l:
            return ['Variable_name', 'Value'], [
                ('Threads_connected', '5'),
                ('Threads_running', '2'),
                ('Questions', '1000'),
            ]
        if 'show processlist' in sql_l:
            return ['Id', 'User', 'Time', 'State'], [
                (1, 'root', '30', 'Query'),
                (2, 'root', '120', 'Query'),
            ]
        return [], []

    def get_columns(self, t):
        for tname, (cols, rows) in self.tables.items():
            if tname == t:
                return [{'name': c, 'type': 'VARCHAR', 'key': '',
                          'null': 'YES'} for c in cols]
        return []

    def execute(self, sql, params=None):
        return 1, None

    def execute_many(self, sql, params):
        return len(params), None

    def switch_database(self, db):
        pass


def test_cloud():
    """测试1: 多云管理"""
    print("\n【测试1】多云管理")
    from lightdb_studio.core.cloud import CloudManager

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.cloud as cm
    old = cm.CLOUD_FILE
    cm.CLOUD_FILE = Path(tmp) / 'cloud.json'

    try:
        mgr = CloudManager()

        # 注册节点
        n1 = mgr.register_node(
            'aws-prod-1', 'aws', 'us-east-1',
            host='10.0.1.1', instance_type='db.r5.large',
            role='primary', vcpu=8, memory_gb=32)
        n2 = mgr.register_node(
            'aws-replica-1', 'aws', 'us-east-1',
            instance_type='db.t3.medium', role='replica')
        n3 = mgr.register_node(
            'gcp-analytics', 'gcp', 'us-central1',
            instance_type='db-n1-standard-2', role='primary')
        n4 = mgr.register_node(
            'local-dev', 'onprem', 'local',
            instance_type='local', role='primary')
        print(f"  ✅ 注册 4 个节点")

        # 成本
        print(f"  → 各节点月成本:")
        total_monthly = 0
        for n in mgr.nodes.values():
            c = n.monthly_cost()
            total_monthly += c
            print(f"    {n.name}: ${c:.2f}/月 "
                  f"({n.provider_info()['icon']})")
        assert total_monthly > 0

        # 记录成本
        for _ in range(5):
            mgr._record_cost(n1)
            mgr._record_cost(n2)

        summary = mgr.cost_summary(hours=24)
        print(f"  → 成本汇总: ${summary['total']:.2f}")
        print(f"  → 月度预测: ${summary['monthly_forecast']:.2f}")

        # 优化建议
        # 手动模拟低利用率
        n3.cpu_usage = 0.1
        n3.memory_usage = 0.2
        suggestions = mgr.cost_optimization_suggestions()
        print(f"  → 优化建议: {len(suggestions)} 条")
        for s in suggestions[:3]:
            print(f"    [{s['type']}] {s['message']} "
                  f"(省 ${s['potential_saving']:.2f}/月)")

        # 拓扑
        topo = mgr.topology()
        print(f"  → 拓扑: {len(topo['nodes'])} 节点, "
              f"{len(topo['edges'])} 边")

        # 统计
        stats = mgr.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_nodes'] == 4

    finally:
        cm.CLOUD_FILE = old


def test_distributed():
    """测试2: 分布式查询"""
    print("\n【测试2】分布式查询")
    from lightdb_studio.core.distributed import DistributedEngine

    # 模拟三个分片连接
    shard1 = FakeConn({'users': (['id', 'name'],
                                  [(1, 'A'), (2, 'B')])})
    shard2 = FakeConn({'users': (['id', 'name'],
                                  [(3, 'C'), (4, 'D')])})
    shard3 = FakeConn({'users': (['id', 'name'],
                                  [(5, 'E')])})

    def provider(name):
        return {'s1': shard1, 's2': shard2, 's3': shard3}.get(name)

    engine = DistributedEngine(conn_provider=provider)

    # 构建计划：3 个分片扫描 + 1 个 UNION 合并
    plan = engine.build_plan(
        'SELECT * FROM users',
        [
            {'node_id': 'n1', 'connection': 's1',
             'sql': 'SELECT * FROM users',
             'type': 'scan', 'output_to': 'f_merge'},
            {'node_id': 'n2', 'connection': 's2',
             'sql': 'SELECT * FROM users',
             'type': 'scan', 'output_to': 'f_merge'},
            {'node_id': 'n3', 'connection': 's3',
             'sql': 'SELECT * FROM users',
             'type': 'scan', 'output_to': 'f_merge'},
        ])

    print(f"  ✅ 构建计划: {len(plan.fragments)} 个分片")

    # 手动加合并分片
    from lightdb_studio.core.distributed import QueryFragment
    merge_frag = QueryFragment(
        fragment_id='f_merge',
        sql='SELECT * FROM UNION ALL',
        fragment_type='final',
        depends_on=[f.fragment_id for f in plan.fragments],
    )
    plan.fragments.append(merge_frag)

    # 执行
    result = engine.execute(plan)
    print(f"  → 执行状态: {result.status}")
    print(f"  → 总耗时: {result.total_elapsed_ms:.2f}ms")
    print(f"  → 分片数: {len(result.fragments)}")
    for f in result.fragments:
        print(f"    {f.fragment_id}: {f.status} "
              f"({f.row_count} 行, {f.elapsed_ms:.2f}ms)")

    assert result.status == 'success'
    assert len(result.final_rows) == 5
    print(f"  ✅ 合并得到 {len(result.final_rows)} 行")

    # 分布式事务
    txn = engine.begin_distributed_txn()
    print(f"  → 事务: {txn['txn_id']}")

    prep = engine.prepare_2pc(txn, [
        {'connection': 's1', 'sql': "INSERT INTO users VALUES (10, 'X')"},
        {'connection': 's2', 'sql': "INSERT INTO users VALUES (11, 'Y')"},
    ])
    print(f"  → 准备阶段: {prep.get('prepared')} 个参与者")
    assert prep['ok']

    commit = engine.commit_2pc(txn)
    print(f"  → 提交: {len(commit['results'])} 结果")

    # 统计
    print(f"  → 已记录 {len(engine.plans)} 个计划")


def test_autonomy():
    """测试3: 智能自治"""
    print("\n【测试3】智能自治")
    from lightdb_studio.core.autonomy import AutonomyEngine

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.autonomy as am
    old = am.AUTO_FILE
    am.AUTO_FILE = Path(tmp) / 'auto.json'

    try:
        conn = FakeConn()

        def provider(name='default'):
            return conn

        engine = AutonomyEngine(conn_provider=provider)

        # 添加默认规则
        n = engine.add_default_rules()
        print(f"  ✅ 添加 {n} 条默认规则")
        for r in engine.rules.values():
            print(f"    {r.name}: {r.metric} {r.condition} "
                  f"{r.threshold} ({r.mode})")

        # 自定义规则
        engine.add_rule(
            '连接数告警', 'connections', '>', 100,
            'notify', {'message': '连接数过高'},
            mode='suggest')

        # 评估-正常
        actions = engine.evaluate({'connections': 50})
        print(f"  → 正常指标: 触发 {len(actions)} 个动作")
        assert len(actions) == 0

        # 评估-触发
        actions = engine.evaluate({'connections': 150})
        print(f"  → 超阈值: 触发 {len(actions)} 个动作")
        for a in actions:
            print(f"    [{a.action_type}] {a.description}")
        assert len(actions) >= 1

        # 冷却
        actions = engine.evaluate({'connections': 200})
        print(f"  → 冷却中: 触发 {len(actions)} 个动作（应为 0）")

        # 趋势分析
        for i in range(20):
            engine.metrics_buffer['cpu_usage'].append(
                (time.time(), 0.5 + i * 0.02))
        trends = engine.analyze_trends()
        print(f"  → 趋势分析: {len(trends)} 项")
        for t in trends:
            print(f"    {t['metric']}: {t['trend']} "
                  f"({t['change_pct']:.1f}%)")

        # 预测
        issues = engine.predict_issues()
        print(f"  → 预测问题: {len(issues)} 项")

        # 统计
        stats = engine.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_rules'] >= 6

    finally:
        am.AUTO_FILE = old


def test_data_fabric():
    """测试4: 数据编织"""
    print("\n【测试4】数据编织")
    import lightdb_studio.core.data_fabric as df
    tmp = tempfile.mkdtemp()
    old = df.FABRIC_FILE
    df.FABRIC_FILE = Path(tmp) / 'fabric.json'

    try:
        fabric = df.DataFabric()

        # 注册节点
        r1 = fabric.register_peer(
            '对方公司 A', 'OrgA',
            allowed_products=['users', 'orders'],
            data_sovereignty='本地留存',
            pii_handling='masked')
        r2 = fabric.register_peer(
            '合作方 B', 'OrgB',
            allowed_products=['*'],
            data_sovereignty='允许跨境',
            pii_handling='encrypted')

        print(f"  ✅ 注册 2 个节点")
        print(f"    A: peer_id={r1['peer_id']}, secret 只显示一次")
        assert r1['secret']

        # 创建交换契约
        ex = fabric.create_exchange(
            r1['peer_id'], r2['peer_id'],
            product_id='db.users',
            mode='pull',
            data_residency='producer',
            ttl_hours=24 * 7)
        print(f"  ✅ 创建交换: {ex.exchange_id}")
        print(f"    TTL: 7 天, 加密密钥: {ex.encryption_key[:16]}...")

        # 访问请求（无审批）
        check = fabric.check_access(
            r1['peer_id'], 'db.users', 'SELECT')
        print(f"  → 无审批: allowed={check['allowed']}")
        assert not check['allowed']

        # 发起请求
        req = fabric.request_access(
            r1['peer_id'], 'db.users', 'SELECT',
            purpose='用户分析')
        print(f"  ✅ 发起请求: {req.request_id}")

        # 审批
        fabric.approve_request(req.request_id, 'admin',
                                reason='已审核通过', ttl_hours=1)
        print(f"  ✅ 审批通过")

        # 再次检查
        check = fabric.check_access(
            r1['peer_id'], 'db.users', 'SELECT')
        print(f"  → 审批后: allowed={check['allowed']}")
        assert check['allowed']

        # 数据主权限制
        r3 = fabric.register_peer(
            '受限组织', 'OrgC',
            data_sovereignty='禁止跨境 transfer foreign')
        check = fabric.check_access(
            r3['peer_id'], 'db.users', 'SELECT')
        print(f"  → 数据主权限制: {check['reason']}")
        assert not check['allowed']

        # 加密测试
        original = '{"name": "Alice", "age": 30}'
        encrypted = fabric.encrypt_payload(original, ex.exchange_id)
        print(f"  → 加密: {encrypted['ciphertext'][:40]}...")
        decrypted = fabric.decrypt_payload(encrypted, ex.exchange_id)
        print(f"  → 解密: {decrypted['data']}")
        assert decrypted['data'] == original

        # 统计
        stats = fabric.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_peers'] == 3

    finally:
        df.FABRIC_FILE = old


def test_digital_twin():
    """测试5: 数字孪生"""
    print("\n【测试5】数字孪生")
    import lightdb_studio.core.digital_twin as dt
    tmp = tempfile.mkdtemp()
    old = dt.TWIN_FILE
    dt.TWIN_FILE = Path(tmp) / 'twin.json'

    try:
        # 模拟连接
        conn = FakeConn({
            'users': (['id', 'name'], [(1, 'A'), (2, 'B')]),
            'orders': (['id', 'amount'], [(1, 100), (2, 200), (3, 300)]),
        })

        def provider(name):
            return conn

        mgr = dt.DigitalTwinManager(conn_provider=provider)

        # 创建孪生
        twin = mgr.create_twin(
            '生产环境', 'prod', 'testdb',
            sync_mode='interval', sync_interval=3600)
        print(f"  ✅ 创建孪生: {twin.name}")

        # 拍快照 1
        snap1 = mgr.take_snapshot(twin.twin_id, '初始状态')
        print(f"  ✅ 快照 1: {snap1.snapshot_id}")
        print(f"    {snap1.total_tables} 表, "
              f"{snap1.total_rows} 行")

        # 模拟变化
        conn.tables['users'] = (['id', 'name'],
                                 [(1, 'A'), (2, 'B'), (3, 'C'), (4, 'D')])
        conn.tables['new_table'] = (['id'], [(1,)])

        # 拍快照 2
        time.sleep(0.1)
        snap2 = mgr.take_snapshot(twin.twin_id, '变化后')
        print(f"  ✅ 快照 2: {snap2.snapshot_id}")

        # 对比
        diff = mgr.compare(twin.twin_id)
        print(f"  → 对比:")
        print(f"    严重度: {diff.severity}")
        print(f"    新增表: {diff.tables_added}")
        print(f"    变更表: {[c['table'] for c in diff.tables_changed]}")
        for ch in diff.tables_changed:
            if 'rows' in ch['changes']:
                r = ch['changes']['rows']
                print(f"      {ch['table']}: {r['old']} → {r['new']} "
                      f"({r['delta']:+d}, {r['pct']:+.1f}%)")

        assert 'new_table' in diff.tables_added
        assert any(c['table'] == 'users'
                    for c in diff.tables_changed)

        # 变更预演
        preview = mgr.preview_change(
            twin.twin_id,
            "UPDATE users SET name = 'X' WHERE id = 1")
        print(f"  → 预演 UPDATE:")
        print(f"    影响: {preview['estimated_impact']}")

        # 危险操作预演
        preview2 = mgr.preview_change(
            twin.twin_id, "DROP TABLE users")
        print(f"  → 预演 DROP:")
        for w in preview2['warnings']:
            print(f"    {w}")
        assert not preview2['dry_run_safe']

        # 实时对比
        live = mgr.live_diff(twin.twin_id)
        print(f"  → 实时对比: {len(live['changes'])} 项变化")
        print(f"    {live['summary']}")

        # 统计
        stats = mgr.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_twins'] == 1
        assert stats['total_snapshots'] == 2

    finally:
        dt.TWIN_FILE = old


def main():
    print("=" * 60)
    print("  v2.0 Batch 7 测试 - 多云/分布式/自治/编织/数字孪生")
    print("=" * 60)

    tests = [
        ('多云管理', test_cloud),
        ('分布式查询', test_distributed),
        ('智能自治', test_autonomy),
        ('数据编织', test_data_fabric),
        ('数字孪生', test_digital_twin),
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
        print("  🎉 v2.0 Batch 7 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())