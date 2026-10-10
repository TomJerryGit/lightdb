"""v2.0 Batch 4 测试 - 虚拟化/联邦/数据湖/ETL/API"""
import os
import sys
import json
import time
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class FakeConn:
    """模拟连接"""
    def __init__(self, tables_data=None):
        self.tables_data = tables_data or {}

    def query(self, sql, params=None):
        sql_l = sql.lower()
        # 匹配表名
        for tname, (cols, rows) in self.tables_data.items():
            if tname.lower() in sql_l:
                # 简单过滤 WHERE
                if 'where' in sql_l:
                    import re
                    m = re.search(
                        r'where\s+(\w+)\s*=\s*[\'"]?([^\s\'"]+)',
                        sql_l)
                    if m:
                        col, val = m.group(1), m.group(2)
                        if col in cols:
                            ci = cols.index(col)
                            try:
                                val_num = float(val)
                            except ValueError:
                                val_num = val
                            rows = [r for r in rows
                                    if str(r[ci]) == str(val)
                                    or r[ci] == val_num]
                return list(cols), list(rows)
        return [], []

    def get_columns(self, t):
        for tname, (cols, rows) in self.tables_data.items():
            if tname == t:
                return [{'name': c, 'type': 'VARCHAR'} for c in cols]
        return []

    def execute_many(self, sql, params):
        return len(params), None


def test_virtualization():
    """测试1: 数据虚拟化"""
    print("\n【测试1】数据虚拟化")
    from lightdb_studio.core.virtualization import (
        VirtualizationEngine, VirtualTable, VirtualColumn)

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.virtualization as vm
    old = vm.VIRTUAL_FILE
    vm.VIRTUAL_FILE = Path(tmp) / 'virt.json'

    try:
        conn = FakeConn({
            'users': (['id', 'name', 'age'],
                       [(1, 'Alice', 30), (2, 'Bob', 25),
                        (3, 'Carol', 28)]),
        })

        def provider(name):
            return conn if name in ('prod', 'db1') else None

        engine = VirtualizationEngine(conn_provider=provider)

        # 创建虚拟表
        vt = engine.create_table(
            'adult_users',
            sources=[{
                'connection': 'prod',
                'sql': 'SELECT * FROM users WHERE age >= 18',
                'alias': 'u',
            }],
            description='成年用户视图')
        print(f"  ✅ 创建虚拟表: {vt.name}")

        # 查询
        cols, rows = engine.execute('adult_users')
        print(f"  → 查询结果: {cols}")
        for r in rows:
            print(f"    {r}")
        assert len(rows) == 3

        # 过滤
        cols, rows = engine.execute('adult_users', where='age > 26')
        print(f"  → WHERE age > 26: {len(rows)} 行")
        assert len(rows) == 2

        # LIMIT
        cols, rows = engine.execute('adult_users', limit=2)
        assert len(rows) == 2
        print(f"  ✅ LIMIT 生效")

        # 多源 + 定义
        engine.create_table(
            'adult_names',
            sources=[{
                'connection': 'prod',
                'sql': 'SELECT * FROM users',
                'alias': 'u',
            }],
            definition='SELECT name FROM {u} WHERE age >= 28')
        cols, rows = engine.execute('adult_names')
        print(f"  → 定义投影: {cols} {rows}")
        assert 'name' in cols

        # 统计
        stats = engine.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_tables'] == 2

    finally:
        vm.VIRTUAL_FILE = old


def test_federation():
    """测试2: 联邦查询"""
    print("\n【测试2】联邦查询")
    from lightdb_studio.core.federation import (
        FederationEngine, SourceFragment, JoinCondition)

    # 两个不同的连接
    db_a = FakeConn({
        'users': (['id', 'name', 'city_id'],
                   [(1, 'Alice', 10), (2, 'Bob', 20)]),
    })
    db_b = FakeConn({
        'cities': (['id', 'city_name'],
                    [(10, 'Beijing'), (20, 'Shanghai'),
                     (30, 'Guangzhou')]),
    })

    def provider(name):
        return db_a if name == 'db_a' else db_b

    engine = FederationEngine(conn_provider=provider)

    # 联邦 JOIN
    fragments = [
        SourceFragment(connection='db_a',
                        sql='SELECT * FROM users', alias='u'),
        SourceFragment(connection='db_b',
                        sql='SELECT * FROM cities', alias='c'),
    ]
    joins = [
        JoinCondition(left_alias='u', left_col='city_id',
                       right_alias='c', right_col='id',
                       join_type='LEFT'),
    ]

    cols, rows = engine.execute(fragments, joins)
    print(f"  → JOIN 结果: {cols}")
    for r in rows:
        print(f"    {r}")

    assert len(rows) == 2
    assert 'u.name' in cols
    assert 'c.city_name' in cols
    print("  ✅ 跨库 JOIN 成功")

    # 投影
    cols2, rows2 = engine.execute(
        fragments, joins, select_columns=['name', 'city_name'])
    print(f"  → 投影: {cols2} {rows2}")
    assert len(cols2) == 2

    # WHERE
    cols3, rows3 = engine.execute(
        fragments, joins, where='u.name = \'Alice\'')
    print(f"  → WHERE: {len(rows3)} 行")
    assert len(rows3) == 1

    # SQL 解析
    parsed = engine.parse_federated_sql(
        "SELECT u.id, u.name, c.city_name FROM users u "
        "LEFT JOIN cities c ON u.city_id = c.id "
        "WHERE u.age > 18 LIMIT 10",
        alias_to_conn={'u': 'db_a', 'c': 'db_b'})
    print(f"  → 解析: {len(parsed['fragments'])} 源, "
          f"{len(parsed['joins'])} JOIN")
    assert len(parsed['fragments']) == 2
    assert len(parsed['joins']) == 1


def test_data_lake():
    """测试3: 数据湖"""
    print("\n【测试3】数据湖")
    from lightdb_studio.core.data_lake import DataLakeEngine

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.data_lake as dl
    old = dl.LAKE_FILE
    dl.LAKE_FILE = Path(tmp) / 'lake.json'

    try:
        # 创建测试 CSV
        csv_path = os.path.join(tmp, 'products.csv')
        with open(csv_path, 'w', encoding='utf-8') as f:
            f.write("id,name,price,stock\n")
            f.write("1,Apple,5.5,100\n")
            f.write("2,Banana,3.0,200\n")
            f.write("3,Orange,4.5,150\n")

        engine = DataLakeEngine()
        t = engine.register_file('products', csv_path, 'csv')
        print(f"  ✅ 注册 CSV: {t.name}")

        # Schema 推断
        print(f"  → Schema: {[(c['name'], c['type']) for c in t.columns]}")
        assert len(t.columns) == 4
        type_map = {c['name']: c['type'] for c in t.columns}
        assert type_map['id'] == 'INT'
        assert type_map['price'] == 'DOUBLE'
        assert type_map['name'] == 'VARCHAR'

        # 查询
        cols, rows = engine.query('products')
        print(f"  → 查询: {len(rows)} 行")
        assert len(rows) == 3

        # WHERE
        cols, rows = engine.query('products', where='price > 4')
        print(f"  → WHERE price > 4: {len(rows)} 行")
        assert len(rows) == 2

        # LIMIT
        cols, rows = engine.query('products', limit=1)
        assert len(rows) == 1
        print("  ✅ LIMIT 生效")

        # JSON 文件
        json_path = os.path.join(tmp, 'config.json')
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump([
                {'key': 'theme', 'value': 'dark'},
                {'key': 'lang', 'value': 'zh_CN'},
            ], f)
        engine.register_file('config', json_path, 'json')
        cols, rows = engine.query('config')
        print(f"  → JSON: {cols} {rows}")
        assert len(rows) == 2

        # 统计
        stats = engine.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_tables'] == 2

    finally:
        dl.LAKE_FILE = old


def test_etl():
    """测试4: 实时 ETL"""
    print("\n【测试4】实时 ETL")
    from lightdb_studio.core.etl import (
        ETLManager, ETLPipeline, ETLOperator)

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.etl as em
    old = em.ETL_FILE
    em.ETL_FILE = Path(tmp) / 'etl.json'

    try:
        # 模拟源连接
        src_conn = FakeConn({
            'orders': (['id', 'user_id', 'amount', 'status'],
                        [(1, 100, 500, 'paid'),
                         (2, 100, 300, 'paid'),
                         (3, 200, 800, 'pending'),
                         (4, 100, 200, 'paid')]),
        })
        tgt_calls = []

        class TargetConn:
            def query(self, sql, params=None):
                return [], []
            def execute_many(self, sql, params):
                tgt_calls.append((sql, len(params)))
                return len(params), None

        tgt_conn = TargetConn()

        def provider(name):
            if name == 'src':
                return src_conn
            if name == 'tgt':
                return tgt_conn
            return None

        mgr = ETLManager(conn_provider=provider)

        # 构建管道：源 → 过滤 → 聚合 → 目标
        pipe = ETLPipeline(
            name='paid_summary',
            description='已支付订单汇总',
            operators=[
                ETLOperator(op_id='src', op_type='source',
                             name='读订单',
                             params={
                                 'source_type': 'sql',
                                 'connection': 'src',
                                 'sql': 'SELECT * FROM orders',
                             }),
                ETLOperator(op_id='flt', op_type='filter',
                             name='过滤已支付',
                             params={
                                 'upstream': 'src',
                                 'condition': "status = 'paid'",
                             }),
                ETLOperator(op_id='agg', op_type='aggregate',
                             name='按用户聚合',
                             params={
                                 'upstream': 'flt',
                                 'group_by': ['user_id'],
                                 'aggregations': {
                                     'total': 'SUM(amount)',
                                     'cnt': 'COUNT(*)',
                                 },
                             }),
                ETLOperator(op_id='sink', op_type='sink',
                             name='写入',
                             params={
                                 'upstream': 'agg',
                                 'sink_type': 'table',
                                 'connection': 'tgt',
                                 'table': 'user_summary',
                             }),
            ],
            connections=[
                {'from': 'src', 'to': 'flt'},
                {'from': 'flt', 'to': 'agg'},
                {'from': 'agg', 'to': 'sink'},
            ],
        )
        mgr.add_pipeline(pipe)
        print(f"  ✅ 创建管道: {pipe.name}")

        # 执行
        result = mgr.run_now('paid_summary')
        print(f"  → 执行结果:")
        for s in result['steps']:
            print(f"    {s['op']}: {s.get('rows', 0)} 行 - "
                  f"{s['status']}")
        assert result['success'], result.get('error')
        print(f"  → rows_in={result['rows_in']}, "
              f"rows_out={result['rows_out']}")

        # 目标收到数据
        assert len(tgt_calls) >= 1
        print(f"  ✅ 目标收到 {len(tgt_calls)} 批次")

        # 统计
        stats = mgr.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_pipelines'] == 1
        assert stats['total_runs'] == 1

    finally:
        em.ETL_FILE = old


def test_data_api():
    """测试5: 数据服务 API"""
    print("\n【测试5】数据服务 API")
    from lightdb_studio.core.data_api import DataAPIManager

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.data_api as dm
    old = dm.API_FILE
    dm.API_FILE = Path(tmp) / 'apis.json'

    try:
        conn = FakeConn({
            'users': (['id', 'name', 'age', 'city'],
                       [(1, 'Alice', 30, 'Beijing'),
                        (2, 'Bob', 25, 'Shanghai'),
                        (3, 'Carol', 35, 'Beijing')]),
        })

        def provider(name):
            return conn if name == 'prod' else None

        mgr = DataAPIManager(conn_provider=provider)

        # 发布 API
        api = mgr.publish_table(
            name='users_api',
            connection='prod',
            table='users',
            description='用户查询接口',
            query_params=[
                {'name': 'city', 'type': 'string',
                 'where': "city = {value}"},
                {'name': 'min_age', 'type': 'int',
                 'where': "age >= {value}"},
            ],
        )
        print(f"  ✅ 发布 API: {api.name} -> {api.path}")
        assert len(api.columns) == 4

        # 创建密钥
        key = mgr.create_key('测试密钥', rate_limit=100)
        print(f"  ✅ 生成密钥: {key.key[:20]}...")

        # 调用 - 无密钥
        r = mgr.call('users_api')
        assert not r['ok']
        assert r['code'] == 401
        print(f"  ✅ 无密钥拒绝: {r['error']}")

        # 调用 - 有效密钥
        r = mgr.call('users_api', api_key=key.key)
        assert r['ok']
        print(f"  → 全部查询: {r['count']} 行")

        # 参数过滤
        r = mgr.call('users_api',
                      params={'city': 'Beijing'},
                      api_key=key.key)
        print(f"  → city=Beijing: {r['count']} 行")
        for row in r['rows']:
            print(f"    {row}")
        assert r['count'] == 2

        # 多参数
        r = mgr.call('users_api',
                      params={'city': 'Beijing', 'min_age': '31'},
                      api_key=key.key)
        print(f"  → city=Beijing, min_age>=31: {r['count']} 行")
        assert r['count'] == 1

        # 无效密钥
        r = mgr.call('users_api', api_key='invalid_key')
        assert not r['ok']
        assert r['code'] == 401
        print("  ✅ 无效密钥拒绝")

        # 限流
        small_key = mgr.create_key('限流密钥', rate_limit=2)
        for _ in range(2):
            mgr.call('users_api', api_key=small_key.key)
        r = mgr.call('users_api', api_key=small_key.key)
        assert r['code'] == 429
        print(f"  ✅ 限流生效: {r['error']}")

        # 调用日志
        logs = mgr.get_recent_calls(5)
        print(f"  → 最近 {len(logs)} 次调用")

        # 统计
        stats = mgr.stats()
        print(f"  → 统计: {stats}")

    finally:
        dm.API_FILE = old


def main():
    print("=" * 60)
    print("  v2.0 Batch 4 测试 - 虚拟化/联邦/数据湖/ETL/API")
    print("=" * 60)

    tests = [
        ('数据虚拟化', test_virtualization),
        ('联邦查询', test_federation),
        ('数据湖', test_data_lake),
        ('实时 ETL', test_etl),
        ('数据服务 API', test_data_api),
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
        print("  🎉 v2.0 Batch 4 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())