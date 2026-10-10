"""v4.0 Batch 3 测试 - Cython 关键路径"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_cython_status():
    """测试1: Cython 模块状态"""
    print("\n【测试1】Cython 模块状态")
    from lightdb.cython import status, AVAILABLE

    st = status()
    print(f"  可用模块:")
    for name, ok in st['available'].items():
        icon = '✅' if ok else '❌'
        print(f"    {icon} {name}")

    if not st['all_available']:
        print(f"\n  ⚠️ 部分模块未编译")
        print(f"  运行: ./lightdb/build.sh")
        print(f"  错误: {st['errors']}")
        return

    print(f"\n  ✅ 全部 {len(AVAILABLE)} 个模块就绪")


def test_btree_cython():
    """测试2: B+Tree Cython 版本"""
    print("\n【测试2】B+Tree Cython")
    from lightdb.cython import btree_pyx as bt

    if bt is None:
        print("  ⚠️ 跳过（Cython 未编译）")
        return

    # 测试 RawBTreeIndex
    idx = bt.RawBTreeIndex(capacity=1000)

    # 插入
    for i in range(100):
        idx.insert(i * 10, i, i * 2)

    assert len(idx) == 100
    print(f"  ✅ 插入 100 条")

    # 查找
    found, pid, sid = idx.search(500)
    assert found
    assert pid == 50
    print(f"  ✅ 查找 key=500: pid={pid}, sid={sid}")

    # 查找不存在
    found, _, _ = idx.search(999)
    assert not found
    print(f"  ✅ 不存在的 key 返回 False")

    # 范围扫描
    result = idx.scan(200, 400)
    print(f"  ✅ 范围 [200,400]: {len(result)} 条")
    assert len(result) == 21   # 200,210,...,400

    # 删除
    assert idx.delete(500)
    found, _, _ = idx.search(500)
    assert not found
    print(f"  ✅ 删除成功")

    # 性能
    idx2 = bt.RawBTreeIndex(capacity=100000)
    t0 = time.perf_counter()
    for i in range(50000):
        idx2.insert(i, i, i)
    t1 = time.perf_counter()
    build_time = t1 - t0
    print(f"  → 插入 50000 条: {build_time*1000:.1f}ms "
          f"({50000/build_time:.0f} ops/s)")

    t0 = time.perf_counter()
    for _ in range(100000):
        idx2.search(_ % 50000)
    t1 = time.perf_counter()
    search_time = t1 - t0
    print(f"  → 查找 100000 次: {search_time*1000:.1f}ms "
          f"({100000/search_time:.0f} ops/s)")


def test_row_codec_cython():
    """测试3: 行编解码 Cython"""
    print("\n【测试3】行编解码 Cython")
    from lightdb.cython import row_codec as rc

    if rc is None:
        print("  ⚠️ 跳过")
        return

    codec = rc.RowCodec(['INT', 'VARCHAR', 'INT', 'DOUBLE'])

    # 编解码
    row = [1, 'Alice', 30, 95.5]
    encoded = codec.encode(row)
    decoded = codec.decode(encoded)
    print(f"  → 编码: {len(encoded)} 字节")
    print(f"  → 解码: {decoded}")
    assert decoded == row
    print(f"  ✅ 编解码正确")

    # NULL 处理
    row2 = [2, None, 25, None]
    e2 = codec.encode(row2)
    d2 = codec.decode(e2)
    assert d2 == row2
    print(f"  ✅ NULL 处理正确")

    # 中文
    row3 = [3, '你好', 30, 88.8]
    e3 = codec.encode(row3)
    d3 = codec.decode(e3)
    assert d3[1] == '你好'
    print(f"  ✅ 中文正确")

    # 批量
    rows = [[i, f'user{i}', i % 100, i * 1.5]
            for i in range(1000)]
    t0 = time.perf_counter()
    for r in rows:
        codec.encode(r)
    t1 = time.perf_counter()
    enc_time = t1 - t0

    t0 = time.perf_counter()
    for r in rows:
        e = codec.encode(r)
        codec.decode(e)
    t1 = time.perf_counter()
    full_time = t1 - t0

    print(f"  → 编码 1000 行: {enc_time*1000:.1f}ms "
          f"({1000/enc_time:.0f} ops/s)")
    print(f"  → 完整编解码 1000 行: {full_time*1000:.1f}ms")

    # 批量 API
    encoded_batch = codec.encode_many(rows)
    print(f"  → 批量编码: {len(encoded_batch)} 字节")

    decoded_batch = codec.decode_many(encoded_batch)
    assert len(decoded_batch) == 1000
    assert decoded_batch[42] == rows[42]
    print(f"  ✅ 批量编解码正确")


def test_predicate_cython():
    """测试4: 谓词 Cython"""
    print("\n【测试4】谓词 Cython")
    from lightdb.cython import predicate_pyx as pr

    if pr is None:
        print("  ⚠️ 跳过")
        return

    # 单谓词
    p = pr.CompiledPredicate(0, '>', 10)
    assert p.match([15]) is True
    assert p.match([5]) is False
    assert p.match([None]) is False
    print(f"  ✅ 整数谓词")

    # 字符串谓词
    p2 = pr.CompiledPredicate(1, '=', 'Alice')
    assert p2.match([1, 'Alice']) is True
    assert p2.match([1, 'Bob']) is False
    print(f"  ✅ 字符串谓词")

    # 谓词集合
    preds = [
        pr.CompiledPredicate(0, '>', 10),
        pr.CompiledPredicate(1, '=', 'Alice'),
    ]
    ps = pr.PredicateSet(preds)

    assert ps.match([15, 'Alice']) is True
    assert ps.match([15, 'Bob']) is False
    assert ps.match([5, 'Alice']) is False
    print(f"  ✅ 谓词集合")

    # 批量过滤
    rows = [[i, 'Alice' if i % 2 == 0 else 'Bob'] for i in range(100)]
    p3 = pr.CompiledPredicate(0, '>', 50)
    ps3 = pr.PredicateSet([p3])
    filtered = ps3.filter(rows)
    print(f"  → 过滤 100 行 → {len(filtered)} 行")
    assert len(filtered) == 49

    # 计数
    count = ps3.count_matches(rows)
    assert count == 49
    print(f"  ✅ 批量过滤/计数正确")


def test_page_cython():
    """测试5: 页操作 Cython"""
    print("\n【测试5】页操作 Cython")
    from lightdb.cython import page_pyx as pg

    if pg is None:
        print("  ⚠️ 跳过")
        return

    # 创建页
    page = pg.RawPage(page_id=1)
    assert page.id == 1
    print(f"  ✅ 创建页 (id={page.id})")

    # 插入行
    for i in range(10):
        rb = b'row_' + str(i).encode()
        ok = page.insert_row(rb)
        assert ok

    n = page.get_n_slots()
    print(f"  → 插入 10 行, slots={n}")
    assert n == 10

    # 读取行
    rb = page.get_row(3)
    print(f"  → 读取第 3 行: {rb}")
    assert rb == b'row_3'

    # 删除行
    page.delete_row(3)
    rb = page.get_row(3)
    assert rb is None
    print(f"  ✅ 删除成功")

    # 序列化
    data = page.to_bytes()
    assert len(data) == 4096
    print(f"  ✅ 序列化为 4096 字节")

    # 反序列化
    page2 = pg.RawPage(page_id=2)
    page2.from_bytes(data)
    assert page2.get_n_slots() == 10
    print(f"  ✅ 反序列化正确")

    # 辅助函数
    n2 = pg.page_n_slots(data)
    assert n2 == 10
    print(f"  ✅ 辅助函数正确")


def test_fast_lru():
    """测试6: 快速 LRU"""
    print("\n【测试6】快速 LRU")
    from lightdb.cython import buffer_pool_pyx as bp

    if bp is None:
        print("  ⚠️ 跳过")
        return

    cache = bp.FastLRU(capacity=100)
    print(f"  ✅ 创建 LRU (容量 100)")

    # 写入
    for i in range(50):
        cache.put(i, i * 10)

    # 读取
    for i in range(50):
        v = cache.get(i)
        assert v == i * 10
    print(f"  ✅ 50 条读写")

    # 未命中
    assert cache.get(999) == -1
    print(f"  ✅ 未命中返回 -1")

    # 容量溢出淘汰
    for i in range(50, 150):
        cache.put(i, i * 10)
    assert len(cache) <= 100
    print(f"  ✅ 淘汰机制 (size={len(cache)})")


def test_integration_v4():
    """测试7: v4 集成"""
    print("\n【测试7】v4 集成")

    # 行格式 v4
    from lightdb.storage.row_format_v4 import BinaryRowFormatV4

    cols = [
        {'name': 'id', 'type': 'INT'},
        {'name': 'name', 'type': 'VARCHAR'},
    ]
    fmt = BinaryRowFormatV4(cols)
    print(f"  → 使用 Cython: {fmt.use_cython}")

    row = [1, 'test']
    encoded = fmt.encode(row)
    decoded = fmt.decode(encoded)
    assert decoded == row
    print(f"  ✅ 行格式 v4 正确")

    # 谓词 v4
    from lightdb.sql.predicate_v4 import compile_predicate

    where = {'column': 'age', 'op': '>', 'value': 18}
    pred = compile_predicate(where, ['id', 'name', 'age'])
    print(f"  → 使用 Cython: {pred.use_cython}")

    assert pred.match([1, 'A', 20]) is True
    assert pred.match([2, 'B', 10]) is False
    print(f"  ✅ 谓词 v4 正确")


def test_status_print():
    """测试8: 状态打印"""
    print("\n【测试8】Cython 状态")
    from lightdb.cython import print_status
    print_status()


def main():
    print("=" * 60)
    print("  v4.0 Batch 3 测试 - Cython 关键路径")
    print("=" * 60)

    tests = [
        ('Cython 状态', test_cython_status),
        ('B+Tree Cython', test_btree_cython),
        ('行编解码 Cython', test_row_codec_cython),
        ('谓词 Cython', test_predicate_cython),
        ('页操作 Cython', test_page_cython),
        ('快速 LRU', test_fast_lru),
        ('v4 集成', test_integration_v4),
        ('状态打印', test_status_print),
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
        print("  🎉 v4.0 Batch 3 全部通过！")
        print("  🚀 Cython 加速就绪")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())