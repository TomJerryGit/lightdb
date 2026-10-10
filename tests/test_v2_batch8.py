"""v2.0 Batch 8 测试 - AI微调/NLB/产品运营/全栈可观测"""
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
                if 'show index' in sql_l:
                    return ['Table', 'Key_name', 'Column_name'], []
                return list(cols), list(rows)
        return [], []


def test_ai_finetune():
    """测试1: AI 微调管理"""
    print("\n【测试1】AI 微调管理")
    from lightdb_studio.core.ai_finetune import (
        DatasetBuilder, FineTuneManager, TrainingSample)

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.ai_finetune as ft
    old_dir = ft.FINETUNE_DIR
    ft.FINETUNE_DIR = Path(tmp) / 'finetune'

    try:
        builder = DatasetBuilder()

        # 从历史构建
        history = [
            {'question': '查询用户', 'sql': 'SELECT * FROM users',
             'feedback': 1},
            {'question': '统计订单', 'sql': 'SELECT COUNT(*) FROM orders',
             'feedback': 1},
            {'question': '差评样本', 'sql': 'DROP TABLE x',
             'feedback': -1},
            {'question': '列出前10', 'sql': 'SELECT * FROM users LIMIT 10',
             'feedback': 1},
        ]

        def schema():
            return ['users', 'orders'], {
                'users': ['id', 'name'],
                'orders': ['id', 'amount'],
            }

        samples = builder.build_from_history(history, schema)
        print(f"  ✅ 从历史构建 {len(samples)} 条样本")
        assert len(samples) == 3   # 排除差评

        # 保存数据集
        info = builder.save_dataset('test_ds', samples)
        print(f"  → 训练集: {info['train_samples']} 条")
        print(f"  → 验证集: {info['val_samples']} 条")
        assert Path(info['train_path']).exists()

        # 校验
        validation = builder.validate_dataset(info['train_path'])
        print(f"  → 校验: {'通过' if validation['ok'] else '失败'}")
        assert validation['ok']

        # 微调任务
        mgr = FineTuneManager()
        job = mgr.create_job(
            base_model='gpt-4o-mini',
            dataset_path=info['train_path'],
            method='lora', epochs=2, lora_r=8)
        print(f"  ✅ 创建微调任务: {job.job_id}")

        # 模拟训练
        mgr.start_job(job.job_id, simulator=True)
        # 等待完成
        for _ in range(50):
            time.sleep(0.1)
            j = mgr.get_job(job.job_id)
            if j.status in ('completed', 'failed'):
                break

        j = mgr.get_job(job.job_id)
        print(f"  → 任务状态: {j.status}")
        print(f"  → 进度: {j.progress * 100:.0f}%")
        print(f"  → 最终 loss: {j.loss_history[-1]:.4f}")
        print(f"  → 评估分: {j.eval_score:.1f}")
        assert j.status == 'completed'
        assert len(j.loss_history) > 0

        # 统计
        stats = mgr.stats()
        print(f"  → 统计: {stats}")

    finally:
        ft.FINETUNE_DIR = old_dir


def test_sql_auto_optimizer():
    """测试2: SQL 自动优化"""
    print("\n【测试2】SQL 端到端自动优化")
    from lightdb_studio.core.sql_auto_optimizer import SQLAutoOptimizer
    import lightdb_studio.core.catalog as cm

    tmp = tempfile.mkdtemp()
    old = cm.CATALOG_FILE
    cm.CATALOG_FILE = Path(tmp) / 'catalog.json'

    try:
        # 建目录
        cat = cm.CatalogEngine()
        cat.index_table('db', 'users', 'prod', [
            {'name': 'id', 'type': 'INT', 'key': 'PRI'},
            {'name': 'name', 'type': 'VARCHAR'},
            {'name': 'email', 'type': 'VARCHAR'},
            {'name': 'bio', 'type': 'TEXT'},
        ], row_count=10000)

        conn = FakeConn({'users': (
            ['id', 'name', 'email', 'bio'],
            [(1, 'A', 'a@x.com', 'text')])})

        opt = SQLAutoOptimizer(conn=conn, catalog=cat)

        # 测试复杂 SQL
        sql = ("SELECT * FROM users "
                "WHERE id NOT IN (SELECT id FROM blacklist) "
                "AND name LIKE '%alice%'")

        result = opt.optimize(sql, execute=False)
        print(f"  → 原始评分: {result.original_score}")
        print(f"  → 优化后评分: {result.optimized_score}")
        print(f"  → 优化步骤: {len(result.steps)}")
        for s in result.steps:
            print(f"    [{s.step_type}] {s.description}")
            if s.reason:
                print(f"      原因: {s.reason[:60]}")

        print(f"  → 优化后 SQL:")
        print(f"    {result.optimized_sql[:200]}")

        print(f"  → 索引建议: {len(result.index_suggestions)}")
        for s in result.index_suggestions:
            print(f"    {s['sql']}")

        # 报告
        report = opt.format_report(result)
        print(f"  → 报告长度: {len(report)} 字符")
        assert 'SQL 优化报告' in report

        assert result.original_score < 100
        assert result.optimized_score > result.original_score

    finally:
        cm.CATALOG_FILE = old


def test_nlb():
    """测试3: 自然语言 BI"""
    print("\n【测试3】自然语言 BI")
    import lightdb_studio.core.catalog as cm
    from lightdb_studio.core.nlb import NaturalLanguageBI

    tmp = tempfile.mkdtemp()
    old = cm.CATALOG_FILE
    cm.CATALOG_FILE = Path(tmp) / 'catalog.json'

    try:
        # 建目录
        cat = cm.CatalogEngine()
        cat.index_table('db', 'orders', 'prod', [
            {'name': 'id', 'type': 'INT', 'key': 'PRI'},
            {'name': 'amount', 'type': 'DECIMAL'},
            {'name': 'status', 'type': 'VARCHAR'},
            {'name': 'created_at', 'type': 'DATETIME'},
        ], row_count=5000)
        cat.update_table_meta('db', 'orders',
                              business_name='订单表',
                              domain='交易中心',
                              tags=['核心'])

        # 模拟连接
        conn = FakeConn({'orders': (
            ['count'], [(5000,)])})

        nlb = NaturalLanguageBI(conn=conn, catalog=cat)

        # 测试 1: 计数
        answer = nlb.ask("订单表有多少条记录")
        print(f"  → Q: 订单表有多少条记录")
        print(f"    SQL: {answer.sql}")
        print(f"    意图: count, 洞察: {answer.insight}")
        assert 'COUNT' in answer.sql.upper()

        # 测试 2: 分布
        answer = nlb.ask("订单表的分布情况")
        print(f"  → Q: 订单表的分布情况")
        print(f"    SQL: {answer.sql}")
        print(f"    图表: {answer.chart.get('type')}")
        assert answer.chart.get('type') == 'pie' or \
               'GROUP BY' in answer.sql.upper()

        # 测试 3: 趋势
        answer = nlb.ask("订单表的近期趋势")
        print(f"  → Q: 订单表的近期趋势")
        print(f"    SQL: {answer.sql}")
        print(f"    图表: {answer.chart.get('type')}")
        assert answer.chart.get('type') == 'line'

        # 测试 4: 追问
        answer = nlb.ask("订单表有多少条")
        print(f"  → 追问建议: {answer.follow_ups}")

        # 数据故事
        story = nlb.export_data_story([answer])
        print(f"  → 数据故事: {len(story)} 字符")
        assert '数据故事' in story

    finally:
        cm.CATALOG_FILE = old


def test_product_ops():
    """测试4: 数据产品运营"""
    print("\n【测试4】数据产品运营")
    import lightdb_studio.core.data_mesh as dm
    from lightdb_studio.core.product_ops import ProductOpsEngine

    tmp = tempfile.mkdtemp()
    old = dm.MESH_FILE
    dm.MESH_FILE = Path(tmp) / 'mesh.json'

    try:
        # 构建 Mesh
        mesh = dm.DataMeshManager()
        mesh.create_domain('交易中心', owner='alice')
        mesh.create_domain('BI', owner='bob')

        p1 = mesh.create_product(
            '订单数据', '交易中心',
            output_type='table', output_ref='db.orders',
            owner='alice', tags=['核心'],
            description='订单主数据')
        p2 = mesh.create_product(
            '销售报表', 'BI',
            output_type='view', output_ref='db.report',
            owner='bob')
        p3 = mesh.create_product(
            '临时分析表', 'BI',
            output_type='table', output_ref='db.tmp')

        # 声明依赖
        mesh.update_product(p2.product_id, inputs=[
            {'product_id': p1.product_id, 'role': 'source'}])

        # 模拟使用
        p1.query_count = 1500
        p1.consumer_count = 8
        p1.last_used = time.time()

        p2.query_count = 200
        p2.consumer_count = 3

        # p3 从未使用

        ops = ProductOpsEngine(mesh_mgr=mesh)

        # 健康度
        print(f"  → 健康度:")
        for pid, p in mesh.products.items():
            h = ops.compute_health(pid)
            print(f"    {p.name}: {h.overall}/100")
            print(f"      使用={h.dimensions.get('usage', 0)}, "
                  f"质量={h.dimensions.get('quality', 0)}, "
                  f"文档={h.dimensions.get('documentation', 0)}")
            if h.issues:
                for i in h.issues[:2]:
                    print(f"      ⚠️ {i}")

        # 价值评估
        print(f"  → 价值评估:")
        for pid, p in mesh.products.items():
            va = ops.assess_value(pid)
            print(f"    {p.name}: {va.total_value} ({va.tier})")

        # 报告
        report = ops.generate_report()
        print(f"  → 报告长度: {len(report)}")
        assert '数据产品运营报告' in report

        # 统计
        stats = ops.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_products'] == 3

    finally:
        dm.MESH_FILE = old


def test_fullstack_observability():
    """测试5: 全栈可观测"""
    print("\n【测试5】全栈可观测")
    from lightdb_studio.core.fullstack_obs import (
        FullStackObservability, LinkedEvent)

    obs = FullStackObservability()

    # 添加事件
    base_time = time.time()

    # 场景：订单表出问题
    obs.add_quality_event({
        'event_id': 'q1',
        'event_type': 'anomaly',
        'resource': 'db.orders',
        'detail': '订单表数据异常',
        'severity': 'critical',
        'detected_at': base_time,
    })

    # 相关联的失败 trace
    obs.add_trace_event({
        'trace_id': 't1',
        'name': 'etl_orders',
        'status': 'error',
        'start_time': base_time + 5,
        'resources': ['db.orders'],
        'error': 'Connection timeout',
    })

    # 相关的告警
    obs.add_alert({
        'rule_name': '订单表异常',
        'message': '订单表数据异常',
        'fired_at': base_time + 10,
        'severity': 'critical',
    })

    # 另一个无关事件
    obs.add_trace_event({
        'trace_id': 't2',
        'name': 'query_users',
        'status': 'ok',
        'start_time': base_time + 20,
        'resources': ['db.users'],
    })

    print(f"  ✅ 添加 4 个事件")

    # 关联分析
    groups = obs.correlate(window_seconds=600)
    print(f"  → 关联组: {len(groups)}")

    for g in groups:
        print(f"    组 {g.group_id} [{g.severity}]")
        print(f"      根因: {g.root_cause_hint}")
        print(f"      事件数: {len(g.events)}")
        print(f"      影响表: {g.affected_tables}")

    # 至少应有 1 个组（订单相关）
    assert len(groups) >= 1
    assert any('orders' in str(g.affected_tables) for g in groups)

    # 获取组列表
    group_dicts = obs.get_groups()
    print(f"  → 组列表: {len(group_dicts)}")

    # 解决
    if groups:
        obs.resolve_group(groups[0].group_id)
        print(f"  ✅ 已解决组 {groups[0].group_id}")

    # 端到端 SLO
    slo = obs.compute_end_to_end_slo('product_x', 24)
    print(f"  → 端到端 SLO: 合规率={slo['compliance']:.2%}")

    # 统计
    stats = obs.stats()
    print(f"  → 统计: {stats}")
    assert stats['total_events'] == 4

    # 事件关联逻辑验证
    e1 = LinkedEvent(event_type='test', table='db.users')
    e2 = LinkedEvent(event_type='test', table='db.users')
    assert obs._is_related(e1, e2)
    print("  ✅ 事件关联判定正确")

    e3 = LinkedEvent(event_type='test', table='db.other')
    assert not obs._is_related(e1, e3)
    print("  ✅ 无关事件判定正确")


def main():
    print("=" * 60)
    print("  v2.0 Batch 8 测试 - AI微调/NLB/产品运营/全栈可观测")
    print("=" * 60)

    tests = [
        ('AI 微调管理', test_ai_finetune),
        ('SQL 自动优化', test_sql_auto_optimizer),
        ('自然语言 BI', test_nlb),
        ('数据产品运营', test_product_ops),
        ('全栈可观测', test_fullstack_observability),
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
        print("  🎉 v2.0 Batch 8 全部通过！")
        print("  🏁 v2.0 全部完成（8 个批次）！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())