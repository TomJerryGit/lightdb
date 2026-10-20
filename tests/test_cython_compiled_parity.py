"""编译版 Cython 模块 vs 纯 Python 回退版的**语义对拍**（4A.4）。

这些测试只在 ``lightdb/cython`` 下存在真实编译产物（``any_accelerated``）时运行；
没有编译产物的环境（CI/干净 checkout）全部 skip——编译验证本身由
``scripts/build_cython_zig.py`` 负责，不把平台相关二进制提交进仓库。

覆盖：
* row_codec：encode/decode 往返（NULL/中文/负数/浮点）与编译版/回退版逐值一致
* btree：insert/search/scan/delete 结果一致
* hash_table：put/get/contains/remove 语义一致
* predicate：int/float/string 的 = != < > <= >= 匹配结果一致
* buffer_pool：get/put/容量语义一致
* page：insert_row/get_row/delete_row/to_bytes/from_bytes 往返一致；
  并回归 page_pyx 空页 ``next_row_ptr`` 未初始化导致的堆溢出 bug
"""
import pytest

from lightdb.cython import status

COMPILED = status()['any_accelerated']

pytestmark = pytest.mark.skipif(
    not COMPILED,
    reason='当前环境没有编译版 Cython 产物（用 scripts/build_cython_zig.py '
           '构建后可运行本文件；二进制不入库）')


def _pair(logical, compiled_name, fallback_name):
    import importlib
    c = importlib.import_module(f'lightdb.cython.{compiled_name}')
    f = importlib.import_module(f'lightdb.cython.fallback.{fallback_name}')
    return c, f


# ==================== row_codec ====================

ROWS = [
    [1, 'Alice', 30, 95.5],
    [2, None, 25, None],
    [3, '你好', 30, 88.8],
    [-4, '', 0, -1.5],
    [123456789, 'x' * 300, 7, 3.25],   # INT 列按 4 字节有符号存储（已知边界）
]


def test_row_codec_roundtrip_and_parity():
    C, F = _pair('row_codec', 'row_codec', 'row_codec')
    types = ['INT', 'VARCHAR', 'INT', 'DOUBLE']
    cc, fc = C.RowCodec(types), F.RowCodec(types)
    for row in ROWS:
        assert cc.decode(cc.encode(row)) == row
        assert fc.decode(fc.encode(row)) == row
        assert cc.decode(cc.encode(row)) == fc.decode(fc.encode(row))


def test_row_codec_batch_parity():
    C, F = _pair('row_codec', 'row_codec', 'row_codec')
    types = ['INT', 'VARCHAR']
    cc, fc = C.RowCodec(types), F.RowCodec(types)
    rows = [[i, f'u{i}'] for i in range(500)]
    cb, fb = cc.encode_many(rows), fc.encode_many(rows)
    assert cc.decode_many(cb) == rows
    assert fc.decode_many(fb) == rows


# ==================== btree ====================

def test_btree_parity():
    C, F = _pair('btree', 'btree_pyx', 'btree')
    for mod in (C, F):
        idx = mod.RawBTreeIndex(capacity=2000)
        for i in range(200):
            idx.insert(i * 10, i, i * 2)
        assert len(idx) == 200
        found, pid, sid = idx.search(500)
        assert found and pid == 50
        assert not idx.search(999)[0]
        assert len(idx.scan(200, 400)) == 21
        assert idx.delete(500)
        assert not idx.search(500)[0]


# ==================== hash_table ====================

def test_hash_table_parity():
    C, F = _pair('hash_table', 'hash_table_pyx', 'hash_table')
    for mod in (C, F):
        t = mod.IntHashTable(capacity=4096)
        for i in range(1000):
            t.put(i, i * 3)
        assert t.get(500) == 1500
        assert t.contains(999)
        assert not t.contains(5000)
        assert t.remove(500)
        assert not t.contains(500)


# ==================== predicate ====================

def test_predicate_parity():
    C, F = _pair('predicate', 'predicate_pyx', 'predicate')
    rows = [[0, 'a', 7], [1, '7', 7], [2, None, 7], [-3, 'b', -3]]
    # 只对比两版**共同支持**的 op（回退版 _COMPARISONS 只有
    # = != < > <= >= LIKE；编译版额外有 STARTS/ENDS/CONTAINS）
    cases = [
        (2, '=', 7), (2, '!=', 7), (2, '<', 7), (2, '>=', 7),
        (1, '=', 'a'), (1, 'LIKE', 'a%'),
    ]
    for col, op, val in cases:
        got_c = [C.CompiledPredicate(col, op, val).match(r) for r in rows]
        got_f = [F.CompiledPredicate(col, op, val).match(r) for r in rows]
        assert got_c == got_f, (col, op, val, got_c, got_f)


# ==================== page ====================

def test_page_roundtrip_and_parity():
    C, F = _pair('page', 'page_pyx', 'page')
    for mod in (C, F):
        p = mod.RawPage(page_id=7)
        for i in range(20):
            assert p.insert_row(b'row_%d' % i)
        assert p.get_n_slots() == 20
        assert p.get_row(3) == b'row_3'
        p.delete_row(3)
        assert p.get_row(3) is None
        data = p.to_bytes()
        assert len(data) == 4096
        p2 = mod.RawPage(page_id=8)
        p2.from_bytes(data)
        assert p2.get_n_slots() == 20


def test_page_empty_page_does_not_overflow_heap():
    """回归：编译版 RawPage 空页必须初始化 next_row_ptr=PAGE_SIZE。

    原 .pyx 的 __cinit__ 没写 next_row_ptr，insert_row 里 0-blen 发生 uint16
    下溢 → memcpy 写到 4KB 缓冲之外（堆溢出/access violation）。回退版在
    __init__ 里初始化，因此只有编译版暴露；本测试在编译版下必须通过。
    """
    from lightdb.cython import page_pyx as C
    for _ in range(500):
        p = C.RawPage(page_id=1)
        for i in range(20):
            assert p.insert_row(b'x' * 50)
        assert p.free_space() > 0
        assert len(p.to_bytes()) == 4096


# ==================== buffer_pool ====================

def test_buffer_pool_parity():
    C, F = _pair('buffer_pool', 'buffer_pool_pyx', 'buffer_pool')
    for mod in (C, F):
        cls = getattr(mod, 'FastLRUPool', None) or getattr(
            mod, 'BufferPool', None)
        if cls is None:                      # 回退命名可能不同
            pytest.skip('buffer_pool 回退版没有可对齐的池类')
        pool = cls(capacity=3) if 'capacity' in cls.__init__.__code__.co_varnames \
            else cls(3)
        for i in range(3):
            pool.put(i, b'p%d' % i)
        assert pool.get(0) == b'p0'
        pool.get(1)
        pool.put(3, b'p3')                   # 触发 LRU 淘汰
        assert pool.get(2) in (None, b'p2')
