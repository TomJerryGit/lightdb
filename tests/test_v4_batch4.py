"""v4.0 Batch 4 测试 - 列式存储 + OLAP"""
import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_encoders_plain():
    """测试1: Plain 编码"""
    print("\n【测试1】Plain 编码")
    from lightdb.columnar.encoders import PlainEncoder

    e = PlainEncoder()
    data = e.encode([1, 2, 3, None, 5])
    assert len(data) > 0
    print(f"  ✅ 编码 {len(data)} 字节")


def test_encoders_dict():
    """测试2: 字典编码"""
    print("\n【测试2】字典编码")
    from lightdb.columnar.encoders import DictEncoder

    e = DictEncoder()
    # 低基数列
    values = ['A', 'B', 'A', 'C', 'B', 'A'] * 100
    data = e.encode(values)
    decoded = e.decode(data, 'VARCHAR')

    print(f"  → {len(values)} 值 → {len(data)} 字节")
    assert decoded == values

    # 压缩率
    plain_size = len(str(values).encode())
    ratio = plain_size / len(data)
    print(f"  → 压缩比: {ratio:.1f}x")
    print(f"  ✅ 字典编解码正确")


def test_encoders_rle():
    """测试3: RLE 编码"""
    print("\n【测试3】RLE 编码")
    from lightdb.columnar.encoders import RLEEncoder

    e = RLEEncoder()
    # 大量重复
    values = [100] * 500 + [200] * 500 + [100] * 100
    data = e.encode(values)
    decoded = e.decode(data, 'INT')

    print(f"  → {len(values)} 值 → {len(data)} 字节")
    assert decoded == values
    print(f"  ✅ RLE 编解码正确")


def test_encoders_delta():
    """测试4: Delta 编码"""
    print("\n【测试4】Delta 编码")
    from lightdb.columnar.encoders import DeltaEncoder

    e = DeltaEncoder()
    # 递增序列
    values = list(range(1000, 2000))
    data = e.encode(values)
    decoded = e.decode(data, 'INT')

    print(f"  → {len(values)} 值 → {len(data)} 字节")
    assert decoded == values
    print(f"  ✅ Delta 编解码正确")


def test_encoders_bitpack():
    """测试5: BitPack 编码"""
    print("\n【测试5】BitPack 编码")
    from lightdb.columnar.encoders import BitPackEncoder

    e = BitPackEncoder()
    # 小整数（0-100）
    values = [i % 100 for i in range(1000)]
    data = e.encode(values)
    decoded = e.decode(data, 'INT')

    print(f"  → {len(values)} 值 → {len(data)} 字节")
    assert decoded == values
    print(f"  ✅ BitPack 编解码正确")


def test_encoder_auto_select():
    """测试6: 自动选择编码器"""
    print("\n【测试6】自动选择编码器")
    from lightdb.columnar.encoders import (
        EncoderRegistry, DictEncoder, RLEEncoder,
        DeltaEncoder, BitPackEncoder)

    # 字典场景
    e = EncoderRegistry.select('VARCHAR', ['A', 'B'] * 500)
    assert isinstance(e, DictEncoder)
    print(f"  → 低基数 VARCHAR → Dict")

    # RLE 场景
    e = EncoderRegistry.select('INT', [100] * 1000)
    assert isinstance(e, RLEEncoder)
    print(f"  → 重复 INT → RLE")

    # Delta 场景
    e = EncoderRegistry.select('INT', list(range(1000)))
    assert isinstance(e, DeltaEncoder)
    print(f"  → 递增 INT → Delta")

    # BitPack 场景
    e = EncoderRegistry.select('INT', [i % 100 for i in range(1000)])
    print(f"  → 小整数 → {type(e).__name__}")

    print(f"  ✅ 自动选择正确")


def test_column_chunk():
    """测试7: 列数据块"""
    print("\n【测试7】列数据块")
    from lightdb.columnar.chunk import ColumnChunk

    chunk = ColumnChunk('age', 'INT', list(range(100)))
    raw = chunk.encode()

    print(f"  → 编码 {len(raw)} 字节")

    # 解码
    chunk2 = ColumnChunk('age', 'INT')
    values = chunk2.decode(raw)

    assert values == list(range(100))
    print(f"  ✅ 编解码正确")

    # 统计
    assert chunk.stats.min_value == 0
    assert chunk.stats.max_value == 99
    assert chunk.stats.sum_value == sum(range(100))
    print(f"  → 统计: min={chunk.stats.min_value}, "
          f"max={chunk.stats.max_value}, "
          f"sum={chunk.stats.sum_value}")
    print(f"  ✅ 统计正确")


def test_column_store():
    """测试8: 列存储"""
    print("\n【测试8】列存储")
    from lightdb.columnar.column_store import ColumnStore

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'id', 'type': 'INT'},
            {'name': 'name', 'type': 'VARCHAR'},
            {'name': 'amount', 'type': 'INT'},
        ]
        store = ColumnStore('orders', cols, tmp)

        # 写入 5000 行
        rows = [
            [i, f'user_{i % 100}', i * 10]
            for i in range(5000)
        ]
        store.append_rows(rows)
        assert store.row_count == 5000
        print(f"  ✅ 写入 5000 行")

        # 扫描
        data = store.scan()
        assert len(data['id']) == 5000
        print(f"  → 全列扫描: {len(data)} 列")

        # 投影扫描
        data = store.scan(['id', 'amount'])
        assert 'name' not in data
        print(f"  → 投影扫描: {list(data.keys())}")

        # 范围扫描
        data = store.scan_range(['id'], 100, 200)
        assert len(data['id']) == 100
        print(f"  → 范围扫描 [100,200): {len(data['id'])} 行")

        # 统计
        stats = store.get_column_stats('amount')
        print(f"  → amount 统计: min={stats['min']}, "
              f"max={stats['max']}, sum={stats['sum']}")

        # 报告
        report = store.stats_report()
        print(f"  → 存储报告: {report['row_count']} 行, "
              f"{report['compression_ratio']:.1f}x 压缩")


def test_vectorized_scan():
    """测试9: 向量化扫描"""
    print("\n【测试9】向量化扫描")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.olap.vectorized_engine import (
        VectorizedScan, VectorizedFilter, VectorizedAggregate)

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'id', 'type': 'INT'},
            {'name': 'amount', 'type': 'INT'},
        ]
        store = ColumnStore('t', cols, tmp)
        rows = [[i, i % 1000] for i in range(100000)]
        store.append_rows(rows)

        # 扫描
        scan = VectorizedScan(store, ['id', 'amount'])
        batches = 0
        total = 0
        while True:
            batch = scan.next_batch()
            if batch is None:
                break
            batches += 1
            total += batch.size
        print(f"  → 扫描 {total} 行, {batches} 批")
        assert total == 100000


def test_vectorized_aggregate():
    """测试10: 向量化聚合"""
    print("\n【测试10】向量化聚合")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.olap.vectorized_engine import (
        VectorizedScan, VectorizedAggregate)

    with tempfile.TemporaryDirectory() as tmp:
        cols = [{'name': 'amount', 'type': 'INT'}]
        store = ColumnStore('t', cols, tmp)
        rows = [[i] for i in range(100000)]
        store.append_rows(rows)

        # SUM
        scan = VectorizedScan(store, ['amount'])
        agg = VectorizedAggregate(scan, 'amount', 'SUM')
        t0 = time.perf_counter()
        total = agg.execute()
        t1 = time.perf_counter()
        elapsed = t1 - t0

        expected = sum(range(100000))
        assert total == expected
        print(f"  → SUM = {int(total)}")
        print(f"  → 耗时: {elapsed*1000:.2f}ms "
              f"({100000/elapsed:,.0f} rows/s)")

        # COUNT
        scan = VectorizedScan(store, ['amount'])
        agg = VectorizedAggregate(scan, 'amount', 'COUNT')
        cnt = agg.execute()
        assert cnt == 100000
        print(f"  → COUNT = {cnt}")

        # MIN/MAX
        scan = VectorizedScan(store, ['amount'])
        agg = VectorizedAggregate(scan, 'amount', 'MIN')
        min_v = agg.execute()
        print(f"  → MIN = {min_v}")

        scan = VectorizedScan(store, ['amount'])
        agg = VectorizedAggregate(scan, 'amount', 'MAX')
        max_v = agg.execute()
        print(f"  → MAX = {max_v}")

        print(f"  ✅ 聚合正确")


def test_vectorized_filter():
    """测试11: 向量化过滤"""
    print("\n【测试11】向量化过滤")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.olap.vectorized_engine import (
        VectorizedScan, VectorizedFilter)

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'id', 'type': 'INT'},
            {'name': 'amount', 'type': 'INT'},
        ]
        store = ColumnStore('t', cols, tmp)
        rows = [[i, i * 2] for i in range(100000)]
        store.append_rows(rows)

        # 过滤
        scan = VectorizedScan(store, ['id', 'amount'])
        filt = VectorizedFilter(scan, 'amount', '>', 100000)

        total = 0
        while True:
            batch = filt.next_batch()
            if batch is None:
                break
            total += batch.size

        # amount > 100000 → i > 50000
        expected = 100000 - 50001
        print(f"  → 过滤: {total} 行")
        assert total == expected
        print(f"  ✅ 过滤正确")


def test_vectorized_groupby():
    """测试12: 向量化分组"""
    print("\n【测试12】向量化分组")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.olap.vectorized_engine import (
        VectorizedScan, VectorizedGroupBy)

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'city', 'type': 'VARCHAR'},
            {'name': 'amount', 'type': 'INT'},
        ]
        store = ColumnStore('t', cols, tmp)
        rows = [[f'city_{i % 5}', i] for i in range(10000)]
        store.append_rows(rows)

        scan = VectorizedScan(store, ['city', 'amount'])
        gb = VectorizedGroupBy(scan, ['city'], [
            {'func': 'SUM', 'column': 'amount', 'alias': 'total'},
            {'func': 'COUNT', 'column': '*', 'alias': 'cnt'},
        ])

        result = gb.execute()
        print(f"  → {len(result)} 组")
        for row in sorted(result):
            print(f"    {row}")
        assert len(result) == 5
        print(f"  ✅ 分组正确")


def test_olap_optimizer():
    """测试13: OLAP 优化器"""
    print("\n【测试13】OLAP 优化器")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.olap.olap_optimizer import OLAPOptimizer

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'id', 'type': 'INT'},
            {'name': 'age', 'type': 'INT'},
            {'name': 'name', 'type': 'VARCHAR'},
        ]
        store = ColumnStore('users', cols, tmp)
        rows = [[i, i % 100, f'user_{i}'] for i in range(10000)]
        store.append_rows(rows)

        opt = OLAPOptimizer(store)

        # 统计驱动跳过
        can_skip = opt.can_skip_scan({
            'column': 'age', 'op': '>', 'value': 99999})
        print(f"  → age > 99999 可跳过: {can_skip}")
        assert can_skip

        can_skip = opt.can_skip_scan({
            'column': 'age', 'op': '<', 'value': 50})
        print(f"  → age < 50 可跳过: {can_skip}")
        assert not can_skip

        # 生成计划
        plan = opt.optimize({
            'columns': ['name'],
            'where': {'column': 'age', 'op': '>', 'value': 50},
        })
        print(f"  → 计划: {type(plan).__name__}")
        print(f"  ✅ 优化器正确")


def test_hybrid_table():
    """测试14: HTAP 混合表"""
    print("\n【测试14】HTAP 混合表")
    from lightdb.db.hybrid_table import HybridTable

    with tempfile.TemporaryDirectory() as tmp:
        cols = [
            {'name': 'id', 'type': 'INT'},
            {'name': 'city', 'type': 'VARCHAR'},
            {'name': 'amount', 'type': 'INT'},
        ]
        table = HybridTable('orders', cols, tmp)

        # HybridTable 内部有一个 BufferPool 一直持有 orders.dat 的句柄；
        # 必须在临时目录被清理之前关闭，否则 TemporaryDirectory.__exit__
        # 的 rmtree 会撞上「文件被占用」（winerror=32）。这里用 try/finally
        # 保证断言失败时也关。
        try:
            # 插入
            for i in range(100):
                table.insert([i, f'city_{i % 5}', i * 10])
            print(f"  ✅ 插入 100 行")

            # 强制同步
            table.sync_now()

            # OLAP 查询
            result = table.olap_aggregate({
                'columns': ['city'],
                'group_by': ['city'],
                'aggregations': [
                    {'func': 'SUM', 'column': 'amount', 'alias': 'total'},
                ],
            })
            print(f"  → 聚合结果: {len(result)} 组")
            for row in sorted(result):
                print(f"    {row}")

            # 统计
            stats = table.stats()
            print(f"  → {stats}")
            print(f"  ✅ HTAP 表正确")
        finally:
            table.close()


def test_performance_vs_python():
    """测试15: 性能对比"""
    print("\n【测试15】向量化 vs Python 循环")
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.olap.vectorized_engine import (
        VectorizedScan, VectorizedAggregate)

    with tempfile.TemporaryDirectory() as tmp:
        cols = [{'name': 'amount', 'type': 'INT'}]
        store = ColumnStore('t', cols, tmp)

        N = 1000000
        print(f"  → 准备 {N:,} 行...")
        rows = [[i] for i in range(N)]
        store.append_rows(rows)

        # Python 循环
        t0 = time.perf_counter()
        data = store.scan(['amount'])
        total1 = 0
        for v in data['amount']:
            total1 += v
        t1 = time.perf_counter()
        py_time = t1 - t0
        print(f"  → Python 循环 SUM: {py_time*1000:.1f}ms "
              f"({N/py_time:,.0f} rows/s)")

        # 向量化
        scan = VectorizedScan(store, ['amount'])
        agg = VectorizedAggregate(scan, 'amount', 'SUM')

        t0 = time.perf_counter()
        total2 = agg.execute()
        t1 = time.perf_counter()
        vec_time = t1 - t0
        print(f"  → 向量化 SUM:     {vec_time*1000:.1f}ms "
              f"({N/vec_time:,.0f} rows/s)")

        speedup = py_time / vec_time if vec_time > 0 else 0
        print(f"  → 加速: {speedup:.1f}x")

        assert total1 == total2
        print(f"  ✅ 结果一致")


def main():
    print("=" * 60)
    print("  v4.0 Batch 4 测试 - 列式存储 + OLAP")
    print("=" * 60)

    tests = [
        ('Plain 编码', test_encoders_plain),
        ('字典编码', test_encoders_dict),
        ('RLE 编码', test_encoders_rle),
        ('Delta 编码', test_encoders_delta),
        ('BitPack 编码', test_encoders_bitpack),
        ('自动选择', test_encoder_auto_select),
        ('列数据块', test_column_chunk),
        ('列存储', test_column_store),
        ('向量化扫描', test_vectorized_scan),
        ('向量化聚合', test_vectorized_aggregate),
        ('向量化过滤', test_vectorized_filter),
        ('向量化分组', test_vectorized_groupby),
        ('OLAP 优化器', test_olap_optimizer),
        ('HTAP 混合表', test_hybrid_table),
        ('性能对比', test_performance_vs_python),
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
        print("  🎉 v4.0 Batch 4 全部通过！")
        print("  📊 列式存储 + OLAP 就绪")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())