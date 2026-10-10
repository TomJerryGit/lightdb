"""v2.0 Batch 1 测试 - 数据目录 + AI Agent + 列级血缘"""
import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_catalog_basic():
    """测试1: 数据目录基础"""
    print("\n【测试1】数据目录")

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.catalog as cm
    old = cm.CATALOG_FILE
    cm.CATALOG_FILE = Path(tmp) / 'catalog.json'

    try:
        cat = cm.CatalogEngine()

        # 索引表
        cat.index_table(
            'testdb', 'users', 'dev',
            columns_meta=[
                {'name': 'id', 'type': 'INT', 'key': 'PRI'},
                {'name': 'name', 'type': 'VARCHAR'},
                {'name': 'phone', 'type': 'VARCHAR'},
                {'name': 'password', 'type': 'VARCHAR'},
                {'name': 'salary', 'type': 'DECIMAL'},
            ],
            comment='用户表', row_count=10000)
        print(f"  ✅ 索引 users 表")

        # 敏感度推断
        meta = cat.get_table('testdb', 'users')
        sens_map = {c.name: c.sensitivity for c in meta.columns}
        print(f"  → 敏感度: {sens_map}")
        assert sens_map['phone'] == 'pii'
        assert sens_map['password'] == 'restricted'
        assert sens_map['salary'] == 'confidential'
        assert sens_map['id'] == 'internal'

        # 表级敏感度 = 最高
        print(f"  → 表级敏感度: {meta.sensitivity}")
        assert meta.sensitivity == 'restricted'

        # 更新元数据
        cat.update_table_meta(
            'testdb', 'users',
            business_name='用户主表',
            owner='张三',
            domain='用户中心',
            tags=['核心', '高频'])
        meta = cat.get_table('testdb', 'users')
        assert meta.business_name == '用户主表'
        assert meta.domain == '用户中心'
        assert '核心' in meta.tags
        print(f"  ✅ 元数据更新: {meta.business_name}")

        # 搜索
        results = cat.search('user')
        assert len(results) >= 1
        print(f"  ✅ 搜索 'user': {len(results)} 结果")

        results = cat.search('phone')
        assert len(results) >= 1
        print(f"  ✅ 搜索 'phone': {len(results)} 结果")

        # 过滤
        results = cat.search('', {'sensitivity': 'pii'})
        print(f"  ✅ 过滤 PII: {len(results)} 结果")

        results = cat.search('', {'domain': '用户中心'})
        print(f"  ✅ 过滤域: {len(results)} 结果")

        # 记录查询
        for i in range(5):
            cat.record_query('testdb', 'users', f'SELECT * FROM users WHERE id = {i}')
        meta = cat.get_table('testdb', 'users')
        assert meta.query_count == 5
        print(f"  ✅ 查询热度: {meta.query_count}")

        # 统计
        stats = cat.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_tables'] == 1

        # 敏感扫描
        sensitive = cat.scan_sensitive()
        print(f"  ✅ 敏感字段: {len(sensitive)} 个")
        for s in sensitive:
            print(f"    {s['table']}.{s['column']} [{s['label']}]")

    finally:
        cm.CATALOG_FILE = old


def test_catalog_search():
    """测试2: 目录搜索"""
    print("\n【测试2】目录搜索")
    import lightdb_studio.core.catalog as cm
    tmp = tempfile.mkdtemp()
    old = cm.CATALOG_FILE
    cm.CATALOG_FILE = Path(tmp) / 'catalog.json'

    try:
        cat = cm.CatalogEngine()

        # 添加多张表
        cat.index_table('db', 'users', 'dev', [
            {'name': 'id', 'type': 'INT', 'key': 'PRI'},
            {'name': 'username', 'type': 'VARCHAR'},
        ], row_count=1000)
        cat.update_table_meta('db', 'users',
                              business_name='用户表',
                              domain='用户中心', tags=['核心'])

        cat.index_table('db', 'orders', 'dev', [
            {'name': 'id', 'type': 'INT', 'key': 'PRI'},
            {'name': 'user_id', 'type': 'INT'},
            {'name': 'amount', 'type': 'DECIMAL'},
        ], row_count=5000)
        cat.update_table_meta('db', 'orders',
                              business_name='订单表',
                              domain='交易中心', tags=['核心'])

        cat.index_table('db', 'products', 'dev', [
            {'name': 'id', 'type': 'INT', 'key': 'PRI'},
            {'name': 'name', 'type': 'VARCHAR'},
            {'name': 'price', 'type': 'DECIMAL'},
        ], row_count=500)
        cat.update_table_meta('db', 'products',
                              business_name='商品表',
                              domain='商品中心')

        # 搜索表名
        r = cat.search('order')
        assert len(r) >= 1
        assert r[0]['table'].name == 'orders'
        print(f"  ✅ 表名 'order': {len(r)} 结果")

        # 搜索业务名
        r = cat.search('用户')
        assert len(r) >= 1
        print(f"  ✅ 业务名 '用户': {len(r)} 结果")

        # 搜索列名
        r = cat.search('amount')
        assert len(r) >= 1
        print(f"  ✅ 列名 'amount': {len(r)} 结果")

        # 搜索标签
        r = cat.search('核心')
        assert len(r) >= 2
        print(f"  ✅ 标签 '核心': {len(r)} 结果")

    finally:
        cm.CATALOG_FILE = old


def test_lineage_enhanced():
    """测试3: 列级血缘"""
    print("\n【测试3】列级血缘")
    import lightdb_studio.core.lineage_enhanced as lm
    tmp = tempfile.mkdtemp()
    old = lm.ENHANCED_LINEAGE_FILE
    lm.ENHANCED_LINEAGE_FILE = Path(tmp) / 'lineage_v2.json'

    try:
        store = lm.EnhancedLineageStore()

        # INSERT INTO ... SELECT
        sql = """
        INSERT INTO user_summary (user_id, order_count, total_amount)
        SELECT user_id, COUNT(*), SUM(amount) FROM orders GROUP BY user_id
        """
        store.record_sql(sql, 'dev')

        print(f"  → 记录 {len(store.edges)} 条列级血缘")
        assert len(store.edges) >= 1

        # 检查边
        for e in store.edges:
            print(f"    {e.from_table}.{e.from_column} → "
                  f"{e.to_table}.{e.to_column} ({e.transform})")

        # 影响分析
        impact = store.impact_analysis('orders')
        print(f"  → orders 影响分析:")
        for d in impact['direct']:
            print(f"    直接: {d['table']}.{d['column']}")

        # 上游追溯
        upstream = store.upstream_of_column('user_summary', 'total_amount')
        print(f"  → user_summary.total_amount 上游:")
        for u in upstream:
            print(f"    {u['table']}.{u['column']} (第 {u['level']} 层)")

        assert any(u['table'] == 'orders' for u in upstream)
        print(f"  ✅ 列级血缘追踪正确")

        # UPDATE 血缘
        sql2 = "UPDATE users SET full_name = name WHERE id = 1"
        store.record_sql(sql2, 'dev')
        print(f"  → 记录 UPDATE: {len(store.edges)} 条边")

        # 统计
        stats = store.stats()
        print(f"  → 统计: {stats}")

    finally:
        lm.ENHANCED_LINEAGE_FILE = old


def test_ai_agent_structure():
    """测试4: AI Agent 结构"""
    print("\n【测试4】AI Agent")

    from lightdb_studio.core.ai_agent import AIAgent, DiagnosisReport

    # 无连接场景
    agent = AIAgent(conn_getter=lambda: None)
    report = agent.diagnose()
    assert report.severity == 'critical'
    assert '无可用连接' in report.summary
    print(f"  ✅ 无连接诊断: {report.summary}")

    # 推荐功能（无 catalog）
    recs = agent.recommend_indexes('users')
    assert recs == []
    print(f"  ✅ 无目录时返回空")


def test_ai_agent_with_catalog():
    """测试5: AI Agent + 目录"""
    print("\n【测试5】AI Agent + 数据目录")
    import lightdb_studio.core.catalog as cm
    tmp = tempfile.mkdtemp()
    old = cm.CATALOG_FILE
    cm.CATALOG_FILE = Path(tmp) / 'catalog.json'

    try:
        cat = cm.CatalogEngine()

        # 索引一张表并添加查询历史
        cat.index_table('db', 'orders', 'dev', [
            {'name': 'id', 'type': 'INT', 'key': 'PRI'},
            {'name': 'user_id', 'type': 'INT'},
            {'name': 'amount', 'type': 'DECIMAL'},
            {'name': 'status', 'type': 'VARCHAR'},
        ], row_count=100000)

        # 添加历史查询
        queries = [
            'SELECT * FROM orders WHERE user_id = 123',
            'SELECT * FROM orders WHERE user_id = 456 AND status = \'paid\'',
            'SELECT * FROM orders WHERE status = \'pending\' ORDER BY id',
            'SELECT SUM(amount) FROM orders WHERE user_id = 789',
        ]
        for q in queries:
            cat.record_query('db', 'orders', q)

        from lightdb_studio.core.ai_agent import AIAgent
        agent = AIAgent(conn_getter=lambda: None, catalog=cat)
        recs = agent.recommend_indexes('orders')

        print(f"  → 索引推荐 ({len(recs)} 条):")
        for r in recs:
            print(f"    {r['column']}: {r['sql']}")
            print(f"      原因: {r['reason']}")

        # 应该推荐 user_id 和 status
        cols = [r['column'] for r in recs]
        assert 'user_id' in cols
        print(f"  ✅ 索引推荐正确")

    finally:
        cm.CATALOG_FILE = old


def main():
    print("=" * 60)
    print("  v2.0 Batch 1 测试 - 数据目录 + AI Agent + 列级血缘")
    print("=" * 60)

    tests = [
        ('数据目录基础', test_catalog_basic),
        ('目录搜索', test_catalog_search),
        ('列级血缘', test_lineage_enhanced),
        ('AI Agent 结构', test_ai_agent_structure),
        ('AI + 目录', test_ai_agent_with_catalog),
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
        print("  🎉 v2.0 Batch 1 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())