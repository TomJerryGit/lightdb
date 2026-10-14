"""v3.0 Batch 2 测试 - 核心路径性能优化"""
import os
import sys
import time
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_row_format_correctness():
    """测试1: 二进制行格式正确性"""
    print("\n【测试1】二进制行格式")
    from lightdb.storage.row_format import BinaryRowFormat

    cols = [
        {'name': 'id', 'type': 'INT'},
        {'name': 'name', 'type': 'VARCHAR'},
        {'name': 'age', 'type': 'INT'},
        {'name': 'score', 'type': 'DOUBLE'},
        {'name': 'active', 'type': 'BOOLEAN'},
    ]
    fmt = BinaryRowFormat(cols)

    # 正常行
    row = [1, 'Alice', 30, 95.5, True]
    data = fmt.encode(row)
    decoded = fmt.decode(data)
    assert decoded == row, f"不一致: {decoded}"
    print(f"  ✅ 正常行 (size={len(data)}B)")

    # 含 NULL
    row = [2, None, 25, None, False]
    data = fmt.encode(row)
    decoded = fmt.decode(data)
    assert decoded == row, f"NULL 不一致: {decoded}"
    print(f"  ✅ 含 NULL")

    # 长文本
    row = [3, 'x' * 1000, 20, 0.0, True]
    data = fmt.encode(row)
    decoded = fmt.decode(data)
    assert decoded[1] == 'x' * 1000
    print(f"  ✅ 长文本 (size={len(data)}B)")

    # 中文
    row = [4, '你好世界', 30, 88.8, True]
    data = fmt.encode(row)
    decoded = fmt.decode(data)
    assert decoded[1] == '你好世界'
    print(f"  ✅ 中文")

    print("  ✅ 所有编解码正确")


def test_row_format_performance():
    """测试2: 二进制 vs JSON 性能"""
    print("\n【测试2】性能对比")
    from lightdb.storage.row_format import BinaryRowFormat
    import json

    cols = [
        {'name': 'id', 'type': 'INT'},
        {'name': 'name', 'type': 'VARCHAR'},
        {'name': 'score', 'type': 'DOUBLE'},
    ]
    fmt = BinaryRowFormat(cols)

    rows = [[i, f'user_{i}', i * 1.5] for i in range(10000)]

    # JSON
    t0 = time.perf_counter()
    json_blobs = [json.dumps(r).encode() for r in rows]
    t1 = time.perf_counter()
    json_decoded = [json.loads(b.decode()) for b in json_blobs]
    t2 = time.perf_counter()
    json_enc = (t1 - t0) * 1000
    json_dec = (t2 - t1) * 1000

    # Binary
    t0 = time.perf_counter()
    bin_blobs = fmt.encode_many(rows)
    t1 = time.perf_counter()
    bin_decoded = fmt.decode_many(bin_blobs)
    t2 = time.perf_counter()
    bin_enc = (t1 - t0) * 1000
    bin_dec = (t2 - t1) * 1000

    print(f"  JSON:   编码 {json_enc:8.2f}ms  解码 {json_dec:8.2f}ms")
    print(f"  Binary: 编码 {bin_enc:8.2f}ms  解码 {bin_dec:8.2f}ms")
    print(f"  加速:   编码 {json_enc/bin_enc:5.2f}x  解码 {json_dec/bin_dec:5.2f}x")

    assert bin_enc < json_enc, "编码未加速"
    assert bin_dec < json_dec, "解码未加速"

    # 空间对比
    json_size = sum(len(b) for b in json_blobs)
    bin_size = sum(len(b) for b in bin_blobs)
    print(f"  空间:   JSON {json_size/1024:.1f}KB  "
          f"Binary {bin_size/1024:.1f}KB  "
          f"压缩 {json_size/bin_size:.2f}x")


def test_plan_cache():
    """测试3: 查询计划缓存"""
    print("\n【测试3】查询计划缓存")
    from lightdb.sql.plan_cache import PlanCache, CachedParser

    cache = PlanCache(max_size=10, ttl=60)

    # 简单 parser mock
    class FakeParser:
        def __init__(self):
            self.call_count = 0
        def parse(self, sql):
            self.call_count += 1
            return {'command': 'SELECT', 'table': 'users',
                    'where': {'column': 'id', 'op': '=', 'value': 1}}

    parser = FakeParser()
    cached_parser = CachedParser(parser, cache)

    # 第一次
    ast1 = cached_parser.parse('SELECT * FROM users WHERE id = 1')
    assert parser.call_count == 1
    print(f"  ✅ 首次解析")

    # 第二次（命中）
    ast2 = cached_parser.parse('SELECT * FROM users WHERE id = 1')
    assert parser.call_count == 1, "未命中缓存"
    print(f"  ✅ 缓存命中 (parser 调用数: {parser.call_count})")

    # 第三次（相同但空格不同）
    ast3 = cached_parser.parse('SELECT  *  FROM  users  WHERE  id = 1')
    assert parser.call_count == 1
    print(f"  ✅ 归一化后命中")

    # 表变更失效
    cache.invalidate_table('users')
    ast4 = cached_parser.parse('SELECT * FROM users WHERE id = 1')
    assert parser.call_count == 2
    print(f"  ✅ 表变更失效")

    # 统计
    stats = cache.stats()
    print(f"  → 缓存: {stats}")
    assert stats['size'] == 1


def test_predicate_pushdown():
    """测试4: 谓词下推"""
    print("\n【测试4】谓词下推")
    from lightdb.sql.predicate_pushdown import (
        PushdownOptimizer, build_predicate_set)

    # 构造计划
    plan = {
        'command': 'SELECT',
        'table': 'users',
        'where': {
            'and': [
                {'column': 'id', 'op': '>', 'value': 100},
                {'column': 'age', 'op': '<', 'value': 30},
            ]
        }
    }

    optimized = PushdownOptimizer.optimize(plan)
    assert 'pushdown' in optimized
    assert len(optimized['pushdown']) == 2
    print(f"  ✅ 下推 2 个条件")

    # 谓词过滤
    col_names = ['id', 'name', 'age']
    pred_set = build_predicate_set(optimized['where'] or
                                    {'and': optimized['pushdown']},
                                    col_names)

    rows = [
        [50, 'A', 25],
        [150, 'B', 35],
        [200, 'C', 28],
        [300, 'D', 22],
    ]
    matched = [r for r in rows if pred_set.match(r)]
    print(f"  → 过滤后: {len(matched)} 行")
    assert len(matched) == 2   # [200, 'C', 28] 和 [300, 'D', 22]


def test_batch_executor():
    """测试5: 批处理执行器"""
    print("\n【测试5】批处理执行器")
    from lightdb.sql.batch_executor import (
        BatchScan, BatchPipeline, VectorizedFilter, HAS_NUMPY)

    data = [[i, f'name_{i}', i % 100, i * 1.5]
            for i in range(50000)]
    col_names = ['id', 'name', 'age', 'score']

    def pred(row):
        return row[2] > 50 and row[3] < 50000

    # 逐行
    t0 = time.perf_counter()
    r1 = [r for r in data if pred(r)]
    t1 = time.perf_counter()
    row_time = t1 - t0

    # 批处理
    scanner = BatchScan(iter(data), batch_size=1024)
    pipeline = BatchPipeline(scanner).filter(pred)
    t0 = time.perf_counter()
    r2 = pipeline.execute()
    t1 = time.perf_counter()
    batch_time = t1 - t0

    assert len(r1) == len(r2)
    print(f"  → 逐行: {row_time*1000:.2f}ms ({len(r1)} 行)")
    print(f"  → 批处理: {batch_time*1000:.2f}ms ({len(r2)} 行)")
    print(f"  → 加速: {row_time/batch_time:.2f}x")

    # 向量化
    if HAS_NUMPY:
        scanner = BatchScan(iter(data), col_names, batch_size=4096)
        pipeline = BatchPipeline(scanner)
        pipeline.stages.append(
            VectorizedFilter(pipeline.stages[-1], 'age', '>', 50))
        pipeline.stages.append(
            VectorizedFilter(pipeline.stages[-1], 'score', '<', 50000))
        t0 = time.perf_counter()
        r3 = pipeline.execute()
        t1 = time.perf_counter()
        vec_time = t1 - t0
        assert len(r3) == len(r1)
        print(f"  → 向量化: {vec_time*1000:.2f}ms")
        print(f"  → 向量化加速: {row_time/vec_time:.2f}x")


def test_rw_lock():
    """测试6: 读写锁"""
    print("\n【测试6】读写锁")
    from lightdb.concurrency.rw_lock import RWLock

    lock = RWLock()
    counter = {'reads': 0, 'writes': 0}
    lock_errors = []

    def reader():
        try:
            for _ in range(1000):
                with lock.read():
                    counter['reads'] += 1
        except Exception as e:
            lock_errors.append(str(e))

    def writer():
        try:
            for _ in range(200):
                with lock.write():
                    counter['writes'] += 1
        except Exception as e:
            lock_errors.append(str(e))

    # 8 读 2 写
    threads = [threading.Thread(target=reader) for _ in range(8)]
    threads += [threading.Thread(target=writer) for _ in range(2)]

    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    t1 = time.perf_counter()

    assert not lock_errors, f"错误: {lock_errors}"
    assert counter['reads'] == 8000
    assert counter['writes'] == 400
    print(f"  ✅ 并发正确 (耗时 {t1-t0:.3f}s)")
    print(f"  → 读: {counter['reads']}, 写: {counter['writes']}")


def test_lockfree_mvcc():
    """测试7: 无锁 MVCC"""
    print("\n【测试7】无锁 MVCC")
    from lightdb.concurrency.lockfree_mvcc import TableMVCC

    table = TableMVCC()

    # 写入
    table.write(1, 1, ('Alice', 30))
    table.write(2, 1, ('Bob', 25))

    # 读（事务 2 可见）
    v = table.read(1, 2, set(), 1, 3)
    assert v == ('Alice', 30)
    print(f"  ✅ 读可见版本: {v}")

    # 事务 1 未提交读
    v = table.read(1, 3, active={1}, min_id=1, max_id=4)
    assert v is None
    print(f"  ✅ 活跃事务不可见")

    # 删除
    table.delete(1, 3)
    v = table.read(1, 2, set(), 1, 4)
    assert v is None, "删除后应不可见"
    print(f"  ✅ 删除后不可见")


def test_page_prefetch():
    """测试8: 页预读"""
    print("\n【测试8】页预读")
    from lightdb.storage.page_prefetch import AdaptivePrefetcher

    # Mock Buffer Pool
    class FakeBP:
        def __init__(self):
            self.pages_read = []
        def get_page(self, pid):
            self.pages_read.append(pid)
            return {'id': pid}

    bp = FakeBP()
    prefetcher = AdaptivePrefetcher(bp)

    # 顺序访问
    for i in range(1, 10):
        prefetcher.record(i)

    # 检查顺序得分
    assert prefetcher.seq_score >= 5, f"顺序得分不足: {prefetcher.seq_score}"
    print(f"  → 顺序得分: {prefetcher.seq_score}")

    # 随机访问
    prefetcher.record(1000)
    prefetcher.record(500)
    prefetcher.record(2000)
    assert prefetcher.seq_score < 5
    print(f"  → 随机后得分: {prefetcher.seq_score}")
    print("  ✅ 自适应预读正常")


def test_integration_benchmark():
    """测试9: 综合性能测试"""
    print("\n【测试9】综合性能测试")
    from lightdb.storage.row_format import BinaryRowFormat
    from lightdb.sql.batch_executor import BatchScan, BatchPipeline

    # 全链路：编解码 + 过滤
    cols = [
        {'name': 'id', 'type': 'INT'},
        {'name': 'name', 'type': 'VARCHAR'},
        {'name': 'value', 'type': 'INT'},
    ]
    fmt = BinaryRowFormat(cols)

    N = 50000
    rows = [[i, f'item_{i}', i % 1000] for i in range(N)]

    # 编码
    t0 = time.perf_counter()
    blobs = fmt.encode_many(rows)
    t1 = time.perf_counter()
    enc_time = t1 - t0

    # 解码
    t0 = time.perf_counter()
    decoded = fmt.decode_many(blobs)
    t1 = time.perf_counter()
    dec_time = t1 - t0

    # 过滤
    def pred(row):
        return row[2] > 500

    scanner = BatchScan(iter(decoded), batch_size=1024)
    pipeline = BatchPipeline(scanner).filter(pred)
    t0 = time.perf_counter()
    result = pipeline.execute()
    t1 = time.perf_counter()
    filter_time = t1 - t0

    total = enc_time + dec_time + filter_time
    print(f"  → 编码: {enc_time*1000:.2f}ms")
    print(f"  → 解码: {dec_time*1000:.2f}ms")
    print(f"  → 过滤: {filter_time*1000:.2f}ms")
    print(f"  → 总计: {total*1000:.2f}ms ({N/total:,.0f} rows/sec)")


def main():
    print("=" * 60)
    print("  v3.0 Batch 2 测试 - 核心路径性能优化")
    print("=" * 60)

    tests = [
        ('二进制行格式正确性', test_row_format_correctness),
        ('二进制性能', test_row_format_performance),
        ('查询计划缓存', test_plan_cache),
        ('谓词下推', test_predicate_pushdown),
        ('批处理执行器', test_batch_executor),
        ('读写锁', test_rw_lock),
        ('无锁 MVCC', test_lockfree_mvcc),
        ('页预读', test_page_prefetch),
        ('综合性能', test_integration_benchmark),
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
        print("  🎉 v3.0 Batch 2 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())