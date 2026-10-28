"""
纯 Python 回退的可调用性 + 等价性测试
=====================================

背景（审计结论，直接采信）
--------------------------
旧版 ``lightdb/cython/__init__.py`` 在编译模块缺失时把模块名置为 ``None``，
而 ``tests/test_v4_batch3.py`` / ``tests/regression_v4/test_cython_compat.py``
用 ``if mod is None: return`` 提前返回 —— 它们的"通过"完全没有执行到实现。
本文件的作用就是**堵住这个假通过**：

1. ``test_status_*`` 断言 6 个模块**都不是 None**，且 ``status()`` 标出来源；
2. 每个模块至少一个**功能往返**测试（编解码、插入后能查到、LRU 命中、
   谓词求值为 bool ...）；
3. 每个模块都与**对应的既有纯 Python 实现**做结果一致性断言
   （``lightdb/storage/btree.py``、``row_format.py``、``page.py``、
   ``lightdb/sql/predicate_v4.py``），而不是自证自洽。

注意：这些测试**不证明** Cython 加速 —— 本机没有 Cython 也没有 C 编译器，
回退是纯 Python，没有任何加速比可测。
"""
import os
import random
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lightdb.cython import (          # noqa: E402
    AVAILABLE,
    MODULES,
    MODULE_SOURCES,
    btree_pyx,
    buffer_pool_pyx,
    hash_table_pyx,
    page_pyx,
    predicate_pyx,
    row_codec,
    status,
)

_MODULE_NAMES = ('btree', 'row_codec', 'page', 'predicate',
                 'buffer_pool', 'hash_table')


# =============================================================================
# 1. 状态：6 个模块必须都可调用，并标明来源
# =============================================================================

def test_status_all_six_modules_available():
    """6 个模块都可用，且不是 None（这是旧版本最大的问题）"""
    st = status()
    assert set(st['available']) == set(_MODULE_NAMES)
    assert st['all_available'] is True, f"不可用详情: {st['fallback_errors']}"
    assert st['fallback_errors'] == {}

    for name in _MODULE_NAMES:
        mod = MODULES[name]
        assert mod is not None, f"{name} 仍是 None —— 回退没生效"
        assert AVAILABLE[name] is True


def test_status_source_is_labelled():
    """status() 必须说清每个模块的来源（compiled / python-fallback）"""
    st = status()
    for name, src in st['available'].items():
        assert src in ('compiled', 'python-fallback'), (name, src)
        assert st['sources'][name]['source'] == src
        assert st['sources'][name]['module'], f"{name} 缺少来源模块路径"

    # 本机没有 Cython / 编译器 → 必须如实报告"没有加速""编译未验证"
    assert st['cython_installed'] is False
    assert st['compiled'] == []
    assert st['any_accelerated'] is False
    assert st['compiled_verified'] is False
    assert sorted(st['fallback']) == sorted(_MODULE_NAMES)
    assert all(v is False for v in st['accelerated'].values())


def test_module_attributes_are_not_none():
    """显式点名：这 6 个导入名都是对象，调用方 if mod is not None 会进入分支"""
    for mod in (btree_pyx, row_codec, page_pyx, predicate_pyx,
                buffer_pool_pyx, hash_table_pyx):
        assert mod is not None
        assert MODULE_SOURCES  # 非空即可


# =============================================================================
# 2. btree_pyx：插入后能查到 + 与 storage.btree 的页布局一致
# =============================================================================

def test_btree_insert_then_search_roundtrip():
    idx = btree_pyx.RawBTreeIndex(capacity=1000)
    for i in range(100):
        idx.insert(i * 10, i, i * 2)

    assert len(idx) == 100
    found, pid, sid = idx.search(500)
    assert found is True and pid == 50 and sid == 100
    assert idx.search(999) == (False, 0, 0)

    assert len(idx.scan(200, 400)) == 21          # 200,210,...,400
    assert idx.delete(500) is True
    assert idx.search(500) == (False, 0, 0)
    assert idx.delete(500) is False               # 重复删除

    # 乱序插入后仍是全局有序、范围扫描正确
    idx2 = btree_pyx.RawBTreeIndex(capacity=2000)
    random.seed(7)
    keys = list(range(1000))
    random.shuffle(keys)
    for k in keys:
        idx2.insert(k, k % 97, k % 1009)
    assert idx2.keys() == list(range(1000))
    found, pid, sid = idx2.search(321)
    assert (found, pid, sid) == (True, 321 % 97, 321 % 1009)
    assert [t[0] for t in idx2.scan(100, 105)] == [100, 101, 102, 103, 104, 105]

    # 容量语义与 .pyx 一致（定容，超了报错）
    small = btree_pyx.RawBTreeIndex(capacity=2)
    small.insert(1, 1, 1)
    small.insert(2, 2, 2)
    with pytest.raises(RuntimeError):
        small.insert(3, 3, 3)


def test_btree_leaf_functions_match_storage_btree_page_layout():
    """
    叶子页读函数必须与 ``lightdb/storage/btree.py`` 同一套字节布局：
    用 Page + BTree 真正落一行，再用回退的 leaf_search 读同一页。
    """
    import tempfile
    from lightdb.storage.btree import (
        BTree, LEAF_HEADER, LEAF_ENTRY_SIZE, INTERNAL_HEADER,
        INTERNAL_ENTRY_SIZE,
    )
    from lightdb.storage.buffer_pool import BufferPool
    from lightdb.storage.page import (
        PAGE_HEADER_SIZE, PAGE_TYPE_BTREE_LEAF, Page,
    )

    # 常量必须与 .pyx / 回退 / storage v2 完全一致，否则读到的页是错的
    assert btree_pyx.LEAF_HEADER == LEAF_HEADER == 6
    assert btree_pyx.INTERNAL_HEADER == INTERNAL_HEADER == 6
    assert btree_pyx.LEAF_ENTRY_SIZE == LEAF_ENTRY_SIZE == 16
    assert btree_pyx.INTERNAL_ENTRY_SIZE == INTERNAL_ENTRY_SIZE == 12
    assert btree_pyx.PAGE_HEADER_SIZE == PAGE_HEADER_SIZE == 24

    tmpdir = tempfile.mkdtemp(prefix='lightdb_cython_fb_')
    path = os.path.join(tmpdir, 'test.db')
    bp = BufferPool(path, capacity=64)
    try:
        tree = BTree(bp)
        for k in (10, 20, 30, 40):
            tree.insert(k, k * 2, k * 3)

        leaf = bp.get_page(tree.root_page_id)
        assert leaf.page_type == PAGE_TYPE_BTREE_LEAF
        data = bytes(leaf.data)

        # 回退的 leaf_search 读到的必须与真实插入一致
        assert btree_pyx.leaf_search(data, 30) == (True, 60, 90)
        assert btree_pyx.leaf_search(data, 35) == (False, 0, 0)
        assert btree_pyx.leaf_lower_bound(data, 25) == 2
        assert btree_pyx.leaf_lower_bound(data, 10) == 0
        assert btree_pyx.leaf_lower_bound(data, 99) == 4
        assert btree_pyx.leaf_range_scan(data, 20, 30) == [
            (20, 40, 60), (30, 60, 90)]
        view = btree_pyx.BTreeLeafView(data)
        assert view.n_keys == 4
        assert view.search(10) == (True, 20, 30)
        assert view.scan(30, 40) == [(30, 60, 90), (40, 80, 120)]

        # 手工构造一个内部节点页，核对 internal_find_child 的分支选择
        page = Page(0, page_type=3)
        struct.pack_into('<H', page.data, PAGE_HEADER_SIZE, 2)
        struct.pack_into('<I', page.data, PAGE_HEADER_SIZE + 2, 100)  # first child
        base = PAGE_HEADER_SIZE + 6
        struct.pack_into('<QI', page.data, base, 50, 200)     # key=50 → child 200
        struct.pack_into('<QI', page.data, base + 12, 80, 300)  # key=80 → child 300
        idata = bytes(page.data)
        assert btree_pyx.internal_find_child(idata, 10) == 100
        assert btree_pyx.internal_find_child(idata, 50) == 200
        assert btree_pyx.internal_find_child(idata, 79) == 200
        assert btree_pyx.internal_find_child(idata, 80) == 300
        assert btree_pyx.internal_find_child(idata, 10 ** 9) == 300
    finally:
        bp.close()
        for suffix in ('',):
            f = path + suffix
            if os.path.exists(f):
                os.remove(f)


def test_btree_fallback_matches_native_v2_after_splits():
    """v2 下原生 BTree 分裂后，回退读取每个叶子页与原生 scan/search 一致

    覆盖：构造（BTree 插入触发多级分裂）、查找（leaf_search/lower_bound）、
    遍历（leaf_range_scan/BTreeLeafView）、与 BTree.scan 结果逐项比对。
    """
    import tempfile
    from lightdb.storage.btree import BTree
    from lightdb.storage.buffer_pool import BufferPool
    from lightdb.storage.page import (
        PAGE_TYPE_BTREE_LEAF, PAGE_HEADER_SIZE,
    )

    tmpdir = tempfile.mkdtemp(prefix='lightdb_cython_fb_split_')
    path = os.path.join(tmpdir, 'split.db')
    bp = BufferPool(path, capacity=6000)
    try:
        tree = BTree(bp)
        # 乱序键触发叶子/内部节点分裂（800 行 > 单叶容量）
        keys = list(range(0, 8000, 10))
        random.seed(21)
        random.shuffle(keys)
        for k in keys:
            tree.insert(k, k + 1, k + 2)
        native = list(tree.scan())
        assert len(native) == len(keys)
        fallback_entries = []
        leaf_pages = []
        for pid in range(1, bp.total_pages()):
            page = bp.get_page(pid)
            if page is not None and page.page_type == PAGE_TYPE_BTREE_LEAF:
                leaf_pages.append(page)
        assert leaf_pages, 'BTree 应该至少有一个叶子页'
        leaf_data = []
        for page in leaf_pages:
            data = bytes(page.data)
            view = btree_pyx.BTreeLeafView(data)
            entries = btree_pyx.leaf_range_scan(data, 0, 2 ** 64 - 1)
            assert len(entries) == view.n_keys
            fallback_entries.extend(entries)
            leaf_data.append((data, entries))
        assert sorted(fallback_entries) == sorted(native)
        native_map = {k: (pid, sid) for k, pid, sid in native}
        # 每个键的查找结果与原生 BTree 完全一致
        for key in (0, 10, 390, 4000, 7990):
            expect = native_map[key]
            hit = None
            for data, entries in leaf_data:
                if any(e[0] == key for e in entries):
                    hit = btree_pyx.leaf_search(data, key)
                    break
            assert hit == (True, expect[0], expect[1]), (key, hit, expect)
        # lower_bound 与有序条目一致
        for data, entries in leaf_data:
            for k, _pid, _sid in entries[:5]:
                pos = btree_pyx.leaf_lower_bound(data, k)
                assert pos < len(entries)
                assert entries[pos][0] == k
        print(f'\n    v2 分裂后回退遍历 {len(leaf_pages)} 个叶子页、'
              f'{len(fallback_entries)} 个键，与原生 BTree.scan 一致')
    finally:
        bp.close()
        for f in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, f))
            except OSError:
                pass


def test_btree_fallback_reads_uint64_v2_keys():
    """v2 键是 uint64：回退必须能读 > 2**32（含 >2**63）的键，不得截断成 int32"""
    import tempfile
    from lightdb.storage.btree import BTree
    from lightdb.storage.buffer_pool import BufferPool
    from lightdb.storage.page import PAGE_TYPE_BTREE_LEAF

    tmpdir = tempfile.mkdtemp(prefix='lightdb_cython_fb_u64_')
    path = os.path.join(tmpdir, 'u64.db')
    bp = BufferPool(path, capacity=64)
    try:
        tree = BTree(bp)
        keys = [2 ** 32, 2 ** 40, 2 ** 63 + 123, 2 ** 64 - 1]
        for k in keys:
            tree.insert(k, k % 97, k % 1009)
        page = bp.get_page(tree.root_page_id)
        assert page.page_type == PAGE_TYPE_BTREE_LEAF
        data = bytes(page.data)
        for k in keys:
            found, pid, sid = btree_pyx.leaf_search(data, k)
            assert found and pid == k % 97 and sid == k % 1009, (k, pid, sid)
        assert btree_pyx.leaf_lower_bound(data, 2 ** 63) == 2
        assert [e[0] for e in btree_pyx.leaf_range_scan(
            data, 2 ** 40, 2 ** 63 + 123)] == [2 ** 40, 2 ** 63 + 123]
        print('\n    v2 uint64 键：回退读取 >2**32 / >2**63 键正确')
    finally:
        bp.close()
        for f in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, f))
            except OSError:
                pass


def test_btree_compare_keys_bulk_accepts_python_objects():
    """
    compare_keys_bulk 原先是 cpdef + const unsigned char*，Python 层无法调用
    （本身就是编译错误）。回退必须能直接吃 bytes。
    """
    a = struct.pack('<3Q', 1, 2, 3)
    b = struct.pack('<3Q', 1, 2, 3)
    c = struct.pack('<3Q', 1, 2, 4)
    d = struct.pack('<3Q', 1, 1, 9)
    assert btree_pyx.compare_keys_bulk(a, b, 3) == 0
    assert btree_pyx.compare_keys_bulk(a, c, 3) == -1
    assert btree_pyx.compare_keys_bulk(c, a, 3) == 1
    assert btree_pyx.compare_keys_bulk(a, d, 2) == 1
    assert btree_pyx.compare_keys_bulk(bytearray(a), memoryview(b), 3) == 0


# =============================================================================
# 3. row_codec：encode→decode 一致 + 与 BinaryRowFormat 一致
# =============================================================================

def test_row_codec_encode_decode_roundtrip():
    codec = row_codec.RowCodec(['INT', 'VARCHAR', 'INT', 'DOUBLE'])

    row = [1, 'Alice', 30, 95.5]
    assert codec.decode(codec.encode(row)) == row

    row_null = [2, None, 25, None]
    assert codec.decode(codec.encode(row_null)) == row_null

    row_cn = [3, '你好世界 😀', 30, 88.8]
    assert codec.decode(codec.encode(row_cn)) == row_cn

    row_empty = [4, '', 0, 0.0]
    assert codec.decode(codec.encode(row_empty)) == row_empty

    assert codec.n_cols == 4
    assert codec.null_bitmap_len == 1


def test_row_codec_matches_pure_python_row_format():
    """与 ``lightdb/storage/row_format.py`` 的 BinaryRowFormat 逐行比对"""
    from lightdb.storage.row_format import BinaryRowFormat

    types = ['INT', 'VARCHAR', 'BIGINT', 'DOUBLE', 'BOOLEAN']
    codec = row_codec.RowCodec(types)
    py_fmt = BinaryRowFormat(
        [{'name': f'c{i}', 'type': t} for i, t in enumerate(types)])
    ref = codec.to_python_format()

    random.seed(11)
    rows = []
    for i in range(300):
        rows.append([
            i,
            f'user_{random.randint(0, 999)}' if i % 5 else None,
            random.randint(-2 ** 40, 2 ** 40),
            round(random.uniform(-50, 50), 6),
            random.choice([True, False]),
        ])

    for row in rows:
        encoded = codec.encode(row)
        # ① 回退与既有纯 Python 实现字节完全一致
        assert encoded == py_fmt.encode(row)
        assert encoded == ref.encode(row)
        # ② 往返一致
        assert codec.decode(encoded) == row
        assert codec.decode(encoded) == py_fmt.decode(encoded)


def test_row_codec_batch_and_int64_list():
    codec = row_codec.RowCodec(['INT', 'VARCHAR', 'DOUBLE'])
    rows = [[i, f'user{i}', i * 1.5] for i in range(500)]

    blob = codec.encode_many(rows)
    assert isinstance(blob, bytes)
    assert struct.unpack_from('<I', blob, 0)[0] == 500

    decoded = codec.decode_many(blob)
    assert len(decoded) == 500
    assert decoded[0] == rows[0]
    assert decoded[42] == rows[42]
    assert decoded == rows

    vals = [0, 1, -1, 2 ** 40, -(2 ** 40)]
    assert row_codec.decode_int64_list(row_codec.encode_int64_list(vals)) == vals
    assert row_codec.encode_int64_list(vals) == struct.pack('<5q', *vals)
    assert row_codec.decode_int64_list(b'') == []


def test_row_codec_rejects_bad_types_arg():
    with pytest.raises(TypeError):
        row_codec.RowCodec('INT')      # 传了字符串而不是列表


# =============================================================================
# 4. page_pyx：页内存操作 + 与 storage.page.Page 一致
# =============================================================================

def test_page_insert_get_delete_roundtrip():
    page = page_pyx.RawPage(page_id=1)
    assert page.id == 1
    assert page.is_dirty is False

    for i in range(10):
        assert page.insert_row(b'row_' + str(i).encode()) is True
    assert page.get_n_slots() == 10
    assert page.get_row(3) == b'row_3'
    assert page.is_dirty is True

    page.delete_row(3)
    assert page.get_row(3) is None
    assert page.get_row(99) is None

    data = page.to_bytes()
    assert isinstance(data, bytes) and len(data) == 4096

    page2 = page_pyx.RawPage(page_id=2)
    page2.from_bytes(data)
    assert page2.get_n_slots() == 10
    assert page2.get_row(5) == b'row_5'
    assert page2.get_row(3) is None

    # 模块级快速函数
    assert page_pyx.page_n_slots(data) == 10
    assert page_pyx.page_get_row(data, 5) == b'row_5'
    assert page_pyx.page_get_row(data, 3) is None
    assert page_pyx.page_get_row(data, 999) is None

    with pytest.raises(ValueError):
        page2.from_bytes(b'short')

    # 空间不足时必须返回 False（而不是抛异常）
    tiny = page_pyx.RawPage(3)
    assert tiny.insert_row(b'x' * 5000) is False


def test_page_bytes_match_storage_page():
    """与 ``lightdb/storage/page.py`` 的 Page 落在同一份字节布局上"""
    from lightdb.storage.page import Page, PAGE_SIZE, PAGE_MAGIC

    raw = page_pyx.RawPage(7)
    ref = Page(7)
    assert raw.to_bytes()[:24] == bytes(ref.data)[:24]
    assert PAGE_SIZE == 4096
    assert raw.get_next_row_ptr() == PAGE_SIZE

    payloads = [b'a', b'hello world', b'x' * 100]
    for p in payloads:
        assert raw.insert_row(p) is True
        assert ref.insert_row(p) is not None

    assert raw.get_n_slots() == ref.n_slots
    assert raw.get_next_row_ptr() == ref.next_row_ptr
    for slot in range(len(payloads)):
        assert raw.get_row(slot) == ref.get_row(slot)

    assert raw.to_bytes() == bytes(ref.data)     # 整页字节逐字节相同

    raw.delete_row(1)
    ref.delete_row(1)
    assert raw.get_row(1) is None and ref.get_row(1) is None
    assert raw.to_bytes() == bytes(ref.data)
    assert raw.free_space() == ref.free_space()

    # 魔数落盘字节是 b'LDBP'（BufferPool 的校验依据）
    assert raw.to_bytes()[:4] == b'LDBP'
    assert struct.unpack('<I', struct.pack('>I', PAGE_MAGIC))[0] == 0x5042444C


def test_page_checksum_matches_reference_algorithm():
    """page_batch_checksum 必须逐字节复刻 .pyx 的算法（非通用 CRC）"""
    data = bytes(range(256)) * 16

    def reference(buf):
        crc = 0xFFFFFFFF
        for i in range(0, len(buf), 4):
            crc = (crc >> 8) ^ ((crc ^ buf[i]) & 0xFF)
        return crc & 0x7FFFFFFF

    got = page_pyx.page_batch_checksum(data)
    assert got == reference(data)
    assert isinstance(got, int) and 0 <= got <= 0x7FFFFFFF
    assert page_pyx.page_batch_checksum(data) == got          # 确定性
    assert page_pyx.page_batch_checksum(b'') == 0x7FFFFFFF
    assert page_pyx.page_batch_checksum(b'LDBP') == reference(b'LDBP')


# =============================================================================
# 5. predicate_pyx：谓词求值为布尔 + 与 predicate_v4 一致
# =============================================================================

def test_predicate_returns_bool_roundtrip():
    p = predicate_pyx.CompiledPredicate(0, '>', 10)
    assert p.match([15]) is True
    assert p.match([5]) is False
    assert p.match([10]) is False
    assert p.match([None]) is False
    assert p.match([]) is False                    # 列越界

    ps = predicate_pyx.PredicateSet([
        predicate_pyx.CompiledPredicate(0, '>', 10),
        predicate_pyx.CompiledPredicate(1, '=', 'Alice'),
    ])
    assert ps.match([15, 'Alice']) is True
    assert ps.match([15, 'Bob']) is False
    assert ps.match([5, 'Alice']) is False
    assert ps.match([None, 'Alice']) is False
    assert len(ps) == 2

    rows = [[i, 'Alice' if i % 2 == 0 else 'Bob'] for i in range(100)]
    assert len(ps.filter(rows)) == 44              # i>10 且 i 为偶数 → 44
    assert predicate_pyx.PredicateSet(
        [predicate_pyx.CompiledPredicate(0, '>', 50)]).count_matches(rows) == 49

    # 各类运算符都要给布尔
    for op, val, hit in (('=', 5, True), ('!=', 5, False), ('<', 9, True),
                         ('<=', 5, True), (">=", 5, True), ('>', 4, True)):
        q = predicate_pyx.CompiledPredicate(0, op, val)
        assert q.match([5]) is hit, (op, val)

    # 字符串 / LIKE
    for op, val, s, hit in (('=', 'abc', 'abc', True),
                            ('!=', 'abc', 'abd', True),
                            ('<', 'b', 'a', True),
                            ('LIKE', 'a%c', 'abbbc', True),
                            ('LIKE', 'a_c', 'abc', True),
                            ('LIKE', 'a_c', 'abbc', False)):
        q = predicate_pyx.CompiledPredicate(0, op, val)
        assert q.match([s]) is hit, (op, val, s)


def test_predicate_matches_pure_python_predicate_v4():
    """与 ``lightdb/sql/predicate_v4.py`` 的求值结果逐行比对"""
    from lightdb.sql.predicate_v4 import compile_predicate

    col_names = ['id', 'name', 'score']
    conditions = [
        {'column': 'id', 'op': '>', 'value': 10},
        {'column': 'name', 'op': '=', 'value': 'Alice'},
        {'column': 'score', 'op': '>=', 'value': 60.0},
    ]
    py_pred = compile_predicate({'and': conditions}, col_names)
    compiled = [predicate_pyx.CompiledPredicate(i, c['op'], c['value'])
                for i, c in enumerate(conditions)]
    cy_set = predicate_pyx.PredicateSet(compiled)

    random.seed(3)
    rows = []
    for _ in range(200):
        rows.append([
            random.randint(0, 30),
            random.choice(['Alice', 'Bob', 'alice', None]),
            round(random.uniform(0, 100), 3),
        ])

    for row in rows:
        expected = py_pred.match(row)
        assert cy_set.match(row) is bool(expected), row

    # 单谓词层面：纯 Python 的 _eval 作为参照
    from lightdb.sql.predicate_v4 import CompiledPredicateSetV4
    for op, val in (('>', 10), ('=', 10), ('<=', 10), ('!=', 10), ('LIKE', '1%')):
        p = predicate_pyx.CompiledPredicate(0, op, val)
        for v in (0, 10, 11, 21, 100):
            assert p.match([v]) == bool(
                CompiledPredicateSetV4._eval(None, v, op, val)), (op, val, v)


def test_predicate_bulk_filters():
    rows = [[i, None if i % 7 == 0 else i * 2] for i in range(50)]
    r = predicate_pyx.int_range_filter(rows, 0, 10, 20)
    assert [x[0] for x in r] == list(range(10, 21))

    e = predicate_pyx.int_eq_filter(rows, 0, 7)
    assert [x[0] for x in e] == [7]

    # 含 None 的列必须被丢弃，而不是抛 TypeError
    r2 = predicate_pyx.int_range_filter(rows, 1, 0, 10 ** 9)
    assert all(x[1] is not None for x in r2)
    assert len(r2) == 50 - len([i for i in range(50) if i % 7 == 0])


# =============================================================================
# 6. buffer_pool_pyx：LRU 命中 / 淘汰
# =============================================================================

def test_fast_lru_hit_miss_eviction():
    cache = buffer_pool_pyx.FastLRU(capacity=100)
    for i in range(50):
        cache.put(i, i * 10)
    assert len(cache) == 50

    for i in range(50):
        assert cache.get(i) == i * 10            # 命中
    assert cache.get(999) == -1                  # 未命中
    assert 999 not in cache and 7 in cache

    for i in range(50, 150):
        cache.put(i, i * 10)
    assert len(cache) == 100                     # 不超容量
    assert cache.get(149) == 1490
    assert cache.get(0) == -1                    # 最久未使用者已被淘汰

    with pytest.raises(ValueError):
        buffer_pool_pyx.FastLRU(capacity=0)


def test_fast_lru_is_lru_not_fifo():
    """被访问过的键必须续命 —— 检验真的是 LRU 而不是 FIFO"""
    cache = buffer_pool_pyx.FastLRU(capacity=3)
    cache.put(1, 10)
    cache.put(2, 20)
    cache.put(3, 30)

    assert cache.get(1) == 10          # 1 变为最近使用 → 2 最久未使用
    cache.put(4, 40)                   # 应淘汰 2

    assert cache.get(2) == -1
    assert cache.get(1) == 10
    assert cache.get(3) == 30
    assert cache.get(4) == 40
    assert sorted(cache.keys()) == [1, 3, 4]


def test_fast_lru_matches_reference_dict_and_stats():
    """回退的读写结果必须与参照 LRU（OrderedDict）一致（覆盖随机访问序列）"""
    from collections import OrderedDict

    random.seed(5)
    cache = buffer_pool_pyx.FastLRU(capacity=64)
    ref = OrderedDict()

    def ref_put(k, v):
        if k in ref:
            ref[k] = v
            ref.move_to_end(k)
            return
        if len(ref) >= 64:
            ref.popitem(last=False)
        ref[k] = v

    def ref_get(k):
        if k in ref:
            ref.move_to_end(k)
            return ref[k]
        return -1

    for _ in range(2000):
        k = random.randint(0, 200)
        if random.random() < 0.6:
            v = random.randint(-1000, 1000)
            cache.put(k, v)
            ref_put(k, v)
        else:
            assert cache.get(k) == ref_get(k), k

    assert len(cache) == len(ref)
    assert set(cache.keys()) == set(ref)
    assert [cache.get(k) for k in cache.keys()] == [ref[k] for k in ref]
    stats = cache.stats()
    assert stats['hits'] + stats['misses'] > 0
    assert stats['size'] == len(cache)
    assert stats['capacity'] == 64
    assert 0.0 <= stats['hit_rate'] <= 1.0


def test_int_hash_table_raises_when_truly_full():
    """与 .pyx 一致：定容数组，容量用尽后不再扩容而是报错"""
    t = hash_table_pyx.IntHashTable(capacity=16)
    assert t.capacity == 16
    for i in range(16):
        t.put(i, i)
    assert len(t) == 16
    with pytest.raises(RuntimeError):
        t.put(999, 1)


# =============================================================================
# 7. hash_table_pyx：读写删往返 + 与 dict 一致
# =============================================================================

def test_int_hash_table_roundtrip():
    t = hash_table_pyx.IntHashTable(capacity=1024)
    assert t.capacity == 1024            # 已经是 2 的幂

    for i in range(200):
        t.put(i, i * 3)
    assert len(t) == 200
    for i in range(200):
        assert t.get(i) == i * 3
        assert t.contains(i) is True
    assert t.get(9999) == -1
    assert t.contains(9999) is False

    assert t.remove(50) is True
    assert t.get(50) == -1
    assert t.contains(50) is False
    assert len(t) == 199
    assert t.remove(50) is False         # 重复删除

    t.put(50, 777)                       # 删除后重新插入（墓碑槽位复用）
    assert t.get(50) == 777
    assert len(t) == 200


def test_int_hash_table_capacity_rounds_to_power_of_two():
    assert hash_table_pyx.IntHashTable(1).capacity == 16
    assert hash_table_pyx.IntHashTable(17).capacity == 32
    assert hash_table_pyx.IntHashTable(1025).capacity == 2048


def test_int_hash_table_matches_dict():
    """与普通 dict 参照逐键比对，并覆盖碰撞 / 负数 / 删除后重插"""
    random.seed(13)
    # 注意：回退与 .pyx 一样是**定容**开放寻址表（不扩容），
    # 容量必须大于插入的键数，否则会如实抛 RuntimeError。
    t = hash_table_pyx.IntHashTable(capacity=1024)
    ref = {}

    keys = [0, 1, -1, 2 ** 40, -(2 ** 40)]
    keys += [random.randint(-10 ** 6, 10 ** 6) for _ in range(600)]

    for k in keys:
        v = (k * 7) % 100000
        t.put(k, v)
        ref[k] = v

    assert len(t) == len(ref)
    assert len(t) <= t.capacity
    for k in ref:
        assert t.get(k) == ref[k]
        assert t.contains(k) is True
    assert len(t) == len(set(keys))

    # 删除一半，再校验另一半仍然可读（回退修正了 .pyx 墓碑截断探测链的问题）
    victims = list(ref)[::2]
    for k in victims:
        assert t.remove(k) is True
        del ref[k]
    assert len(t) == len(ref)
    for k in ref:
        assert t.get(k) == ref[k], k
    for k in victims:
        assert t.get(k) == -1, k
    # 探测链上的键在邻居被删后依然可达
    assert t.get(list(ref)[-1]) == ref[list(ref)[-1]]


# =============================================================================
# 8. 反例守卫：确认这些测试**不会**再"因 None 而假通过"
# =============================================================================

def test_tests_would_fail_if_module_were_none():
    """
    自检：本文件依赖的"非 None"前提是真实的 —— 若哪天回退被删掉，
    上面的 test_status_* 会失败，而不是静默跳过。
    """
    st = status()
    assert st['all_available'] is True
    assert None not in MODULES.values()
    assert None not in (btree_pyx, row_codec, page_pyx,
                        predicate_pyx, buffer_pool_pyx, hash_table_pyx)
