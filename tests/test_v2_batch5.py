"""v2.0 Batch 5 测试 - 推荐/市场/契约/闭包/SLO"""
import os
import sys
import json
import time
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class FakeConn:
    def __init__(self, tables_data=None):
        self.tables_data = tables_data or {}

    def query(self, sql, params=None):
        sql_l = sql.lower()
        for tname, (cols, rows) in self.tables_data.items():
            if tname.lower() in sql_l:
                # 处理 COUNT/MIN/MAX/AVG
                if 'count(*)' in sql_l:
                    return ['count'], [(len(rows),)]
                if 'count(distinct' in sql_l:
                    return ['d', 't'], [(1, len(rows))]
                if 'where' in sql_l and 'is null' in sql_l:
                    return ['c'], [(0,)]
                if 'min(' in sql_l or 'max(' in sql_l or 'avg(' in sql_l:
                    if cols:
                        ci = 0
                        vals = [r[ci] for r in rows if r[ci] is not None]
                        if vals:
                            return ['v'], [(vals[0],)]
                        return ['v'], [(None,)]
                return list(cols), list(rows)
        return [], []

    def get_columns(self, t):
        for tname, (cols, rows) in self.tables_data.items():
            if tname == t:
                return [{'name': c, 'type': 'VARCHAR', 'key': '',
                         'null': 'YES'} for c in cols]
        return []


def test_recommender():
    """测试1: 智能推荐"""
    print("\n【测试1】智能推荐 2.0")
    from lightdb_studio.core.recommender import Recommender

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.recommender as rm
    old = rm.RECOMMEND_FILE
    rm.RECOMMEND_FILE = Path(tmp) / 'recommend.json'

    try:
        rec = Recommender()

        # 记录使用
        for _ in range(5):
            rec.record_usage('alice', 'users')
            rec.record_usage('alice', 'orders')
            rec.record_usage('bob', 'users')
            rec.record_usage('bob', 'orders')
            rec.record_usage('bob', 'products')
            rec.record_usage('carol', 'users')
            rec.record_usage('carol', 'products')

        # 协同过滤
        result = rec.recommend_tables_for_user('alice', 5)
        print(f"  → alice 推荐:")
        for r in result:
            print(f"    {r['table']} (score={r['score']:.2f})")
        assert any(r['table'] == 'products' for r in result)

        # 学习 SQL
        rec.learn_from_sql('alice',
                           'SELECT * FROM users JOIN orders ON users.id = orders.user_id')
        rec.learn_from_sql('bob',
                           'SELECT * FROM users JOIN orders ON users.id = orders.user_id')

        # JOIN 关联
        related = rec.related_tables('users', 5)
        print(f"  → users 关联表:")
        for r in related:
            print(f"    {r['table']} ({r['score']})")
        assert any(r['table'] == 'orders' for r in related)

        # 热门
        trending = rec.trending_tables(hours=24, top_n=5)
        print(f"  → 热门: {[t['table'] for t in trending]}")

        # 统计
        stats = rec.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_users'] >= 3

    finally:
        rm.RECOMMEND_FILE = old


def test_data_market():
    """测试2: 数据市场"""
    print("\n【测试2】数据市场")
    import lightdb_studio.core.data_market as dm
    tmp = tempfile.mkdtemp()
    old = dm.MARKET_FILE
    dm.MARKET_FILE = Path(tmp) / 'market.json'

    try:
        market = dm.DataMarket()

        # 发布
        l1 = market.publish('db.users', title='用户主表',
                             description='核心用户数据',
                             owner='alice', tags=['核心', '用户'])
        l2 = market.publish('db.orders', title='订单表',
                             description='订单交易数据',
                             owner='bob', tags=['核心', '交易'])
        l3 = market.publish('db.logs', title='日志表',
                             description='系统日志',
                             owner='admin', tags=['运维'])

        print(f"  ✅ 发布了 3 个资产")

        # 评分
        market.rate('db.users', 'bob', 4.5, '很好用')
        market.rate('db.users', 'carol', 5.0, '完美')
        market.rate('db.orders', 'alice', 4.0)

        users = market.listings['db.users']
        print(f"  → users 平均分: {users.avg_rating():.2f} "
              f"({users.rating_count()} 条)")

        # 订阅
        market.subscribe('db.users', 'alice')
        market.subscribe('db.users', 'bob')
        market.subscribe('db.orders', 'alice')
        print(f"  → users 订阅数: {len(users.subscribers)}")

        # 搜索
        results = market.search(keyword='用户')
        print(f"  → 搜索 '用户': {len(results)} 结果")

        results = market.search(tags=['核心'])
        print(f"  → 标签 核心: {len(results)} 结果")
        assert len(results) == 2

        # 推荐
        recs = market.recommend_to_user('alice', 5)
        print(f"  → 给 alice 推荐: {[r['table_key'] for r in recs]}")

        # 统计
        stats = market.stats()
        print(f"  → 统计: {stats}")
        assert stats['published'] == 3
        assert stats['total_ratings'] == 3

    finally:
        dm.MARKET_FILE = old


def test_data_contract():
    """测试3: 数据契约"""
    print("\n【测试3】数据契约")
    import lightdb_studio.core.data_contract as dc
    tmp = tempfile.mkdtemp()
    old = dc.CONTRACT_FILE
    dc.CONTRACT_FILE = Path(tmp) / 'contracts.json'

    try:
        # 源表
        conn = FakeConn({
            'users': (['id', 'name', 'email'],
                       [(1, 'a', 'a@x.com')]),
        })

        def provider(name):
            return conn if name == 'prod' else None

        mgr = dc.ContractManager(conn_provider=provider)

        # 创建契约
        c = mgr.create_contract(
            'db.users', connection='prod', table='users',
            name='用户表契约', owner='alice')
        print(f"  ✅ 创建契约: {c.name} v{c.current_version}")
        assert len(c.latest_version().columns) == 3

        # 添加消费者
        c.consumers = ['team_a', 'team_b']
        mgr.save()

        # 更新版本
        new_cols = [
            dc.ContractColumn(name='id', type='INT', required=True),
            dc.ContractColumn(name='name', type='VARCHAR'),
            dc.ContractColumn(name='email', type='VARCHAR'),
            dc.ContractColumn(name='age', type='INT'),
        ]
        mgr.update_contract(
            'db.users', '1.1.0', new_cols,
            note='添加 age 列', created_by='alice')

        c = mgr.get_contract('db.users')
        assert c.current_version == '1.1.0'
        assert len(c.latest_version().columns) == 4
        print(f"  ✅ 更新到 v{c.current_version}")

        # 变更记录
        assert len(c.change_history) == 1
        changes = c.change_history[0]['changes']
        print(f"  → 变更: {[ch['type'] for ch in changes]}")
        assert any(ch['type'] == 'column_added' for ch in changes)

        # 破坏性变更
        breaking_cols = [
            dc.ContractColumn(name='id', type='BIGINT', required=True),
            dc.ContractColumn(name='name', type='VARCHAR'),
            # 删除 email
            dc.ContractColumn(name='age', type='INT'),
        ]
        mgr.update_contract('db.users', '2.0.0', breaking_cols,
                             note='id 改 BIGINT, 删除 email')
        c = mgr.get_contract('db.users')
        changes = c.change_history[-1]['changes']
        breaking = [ch for ch in changes if ch.get('breaking')]
        print(f"  → 破坏性变更: {len(breaking)} 项")
        assert len(breaking) >= 1

        # 通知消费者
        notice = mgr.notify_consumers('db.users', changes)
        print(f"  → 通知: {notice['title']}")
        assert 'team_a' in notice['consumers']

        # 检查违约
        violations = mgr.check_violations(
            'db.users', 'prod', 'users')
        print(f"  → 违约检查: {len(violations)} 项")
        for v in violations:
            print(f"    {v.severity}: {v.detail}")

        # 统计
        stats = mgr.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_contracts'] == 1
        assert stats['total_versions'] == 3

    finally:
        dc.CONTRACT_FILE = old


def test_lineage_closure():
    """测试4: 血缘闭包"""
    print("\n【测试4】血缘闭包")
    import lightdb_studio.core.lineage_enhanced as lm
    import lightdb_studio.core.catalog as cm
    from lightdb_studio.core.lineage_closure import LineageClosure

    tmp = tempfile.mkdtemp()
    old_e = lm.ENHANCED_LINEAGE_FILE
    old_c = cm.CATALOG_FILE
    lm.ENHANCED_LINEAGE_FILE = Path(tmp) / 'lin.json'
    cm.CATALOG_FILE = Path(tmp) / 'cat.json'

    try:
        # 血缘
        lin = lm.EnhancedLineageStore()
        # orders.amount → order_summary.total
        lin.record_sql("INSERT INTO order_summary (user_id, total) "
                        "SELECT user_id, SUM(amount) FROM orders "
                        "GROUP BY user_id")
        # order_summary → bi_report
        lin.record_sql("INSERT INTO bi_report SELECT * FROM order_summary")

        # 目录
        cat = cm.CatalogEngine()
        cat.index_table('db', 'orders', 'prod', [
            {'name': 'id', 'type': 'INT'},
            {'name': 'user_id', 'type': 'INT'},
            {'name': 'amount', 'type': 'DECIMAL'},
        ], row_count=100000)
        cat.index_table('db', 'order_summary', 'prod', [
            {'name': 'user_id', 'type': 'INT'},
            {'name': 'total', 'type': 'DECIMAL'},
        ], row_count=1000)
        cat.update_table_meta('db', 'order_summary',
                               domain='数据仓库',
                               sensitivity='confidential')
        cat.index_table('db', 'bi_report', 'prod', [
            {'name': 'date', 'type': 'DATE'},
            {'name': 'total', 'type': 'DECIMAL'},
        ], row_count=100)
        cat.update_table_meta('db', 'bi_report',
                               domain='BI',
                               sensitivity='restricted')

        closure = LineageClosure(catalog=cat, lineage=lin)

        # 影响分析
        impact = closure.assess_impact('orders', 'amount')
        print(f"  → orders.amount 影响:")
        print(f"    风险: {impact['risk_level']} "
              f"(score={impact['risk_score']})")
        print(f"    P0: {impact['p0_count']}, "
              f"P1: {impact['p1_count']}, "
              f"P2: {impact['p2_count']}")
        for d in impact['downstream']:
            print(f"      [{d['level']}] {d['table']}.{d['column']} "
                  f"(深度 {d['depth']})")
        print(f"  → 建议:")
        for a in impact['advice']:
            print(f"      {a}")

        # 变更模拟
        sim = closure.simulate_change('orders', 'amount', 'drop')
        print(f"  → DROP amount 模拟:")
        print(f"    调整后风险: {sim['adjusted_level']} "
              f"({sim['adjusted_risk']})")
        for s in sim['scenarios']:
            print(f"      场景: {s}")

        # 测试生成
        tests = closure.generate_tests('orders', 'amount')
        print(f"  → 生成 {len(tests)} 个测试用例")

    finally:
        lm.ENHANCED_LINEAGE_FILE = old_e
        cm.CATALOG_FILE = old_c


def test_slo():
    """测试5: SLI/SLO"""
    print("\n【测试5】SLI/SLO")
    import lightdb_studio.core.slo as sm
    tmp = tempfile.mkdtemp()
    old = sm.SLO_FILE
    sm.SLO_FILE = Path(tmp) / 'slo.json'

    try:
        # 模拟连接
        conn = FakeConn({
            'users': (['id', 'name', 'email'],
                       [(1, 'a', 'a@x.com'), (2, 'b', None),
                        (3, 'c', 'c@x.com')]),
        })

        def provider(name):
            return conn

        mgr = sm.SLOManager(conn_provider=provider)

        # 定义 SLI
        mgr.add_sli(sm.SLI(
            name='users_null_email_ratio',
            display_name='用户邮箱空值率',
            metric='null_ratio',
            target_table='users',
            target_column='email',
            direction='lower_better',
        ))

        # 定义 SLO
        mgr.add_slo(sm.SLO(
            name='users_email_quality',
            sli_name='users_null_email_ratio',
            description='邮箱空值率 ≤ 20%',
            target=0.2,          # 目标：≤ 20%
            tolerance=0.1,
            window_hours=24,
            notify_on_breach=False,
        ))

        # 检查
        result = mgr.check_slo('users_email_quality')
        print(f"  → 检查结果:")
        print(f"    值: {result['value']}")
        print(f"    目标: {result['target']}")
        print(f"    状态: {result['status']}")
        print(f"    合规率: {result['compliance']:.2%}")

        assert result['status'] in ('ok', 'warning', 'breach')

        # 错误预算
        budget = mgr.error_budget('users_email_quality')
        print(f"  → 错误预算: 剩余 {budget['remaining']:.4f}")

        # 添加默认 SLI
        n_before = len(mgr.slis)
        mgr.add_default_slis(['users'])
        print(f"  → 添加默认 SLI: "
              f"{len(mgr.slis) - n_before} 个")

        # 统计
        stats = mgr.stats()
        print(f"  → 统计: {stats}")

    finally:
        sm.SLO_FILE = old


def main():
    print("=" * 60)
    print("  v2.0 Batch 5 测试 - 推荐/市场/契约/闭包/SLO")
    print("=" * 60)

    tests = [
        ('智能推荐', test_recommender),
        ('数据市场', test_data_market),
        ('数据契约', test_data_contract),
        ('血缘闭包', test_lineage_closure),
        ('SLI/SLO', test_slo),
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
        print("  🎉 v2.0 Batch 5 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())