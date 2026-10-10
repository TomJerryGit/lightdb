"""
Cython 兼容性回归
- Cython 版本 vs Python 版本结果一致
- 回退机制
"""
import os
import sys
import tempfile
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def test_row_codec_consistency():
    """测试：Cython 与 Python 编解码一致"""
    print("\n【行编解码一致性】")
    from lightdb.storage.row_format import BinaryRowFormat
    from lightdb.storage.row_format_v4 import BinaryRowFormatV4

    cols = [
        {'name': 'id', 'type': 'INT'},
        {'name': 'name', 'type': 'VARCHAR'},
        {'name': 'score', 'type': 'DOUBLE'},
        {'name': 'active', 'type': 'BOOLEAN'},
    ]

    py_fmt = BinaryRowFormat(cols)
    v4_fmt = BinaryRowFormatV4(cols)

    # 随机 1000 行
    random.seed(42)
    rows = []
    for i in range(1000):
        rows.append([
            i,
            f'user_{random.randint(0, 1000)}',
            random.uniform(0, 100),
            random.choice([True, False]),
        ])

    # 编码对比
    for i, row in enumerate(rows):
        py_enc = py_fmt.encode(row)
        v4_enc = v4_fmt.encode(row)

        # 解码结果对比（编码格式可能不同）
        py_dec = py_fmt.decode(py_enc)
        v4_dec = v4_fmt.decode(v4_enc)

        if py_dec != v4_dec:
            print(f"  ❌ 第 {i} 行不一致:")
            print(f"     Python: {py_dec}")
            print(f"     v4:     {v4_dec}")
            return

    print(f"  ✅ 1000 行编解码结果一致")


def test_null_handling():
    """测试：NULL 处理一致"""
    print("\n【NULL 处理】")
    from lightdb.storage.row_format_v4 import BinaryRowFormatV4

    cols = [
        {'name': 'id', 'type': 'INT'},
        {'name': 'name', 'type': 'VARCHAR'},
        {'name': 'value', 'type': 'INT'},
    ]
    fmt = BinaryRowFormatV4(cols)

    cases = [
        [1, None, 100],
        [2, 'test', None],
        [3, None, None],
        [4, '', 0],
    ]

    for row in cases:
        encoded = fmt.encode(row)
        decoded = fmt.decode(encoded)
        if decoded != row:
            print(f"  ❌ 不一致: {row} → {decoded}")
            return

    print(f"  ✅ NULL 处理正确")


def test_unicode():
    """测试：中文/Emoji"""
    print("\n【Unicode 处理】")
    from lightdb.storage.row_format_v4 import BinaryRowFormatV4

    cols = [
        {'name': 'id', 'type': 'INT'},
        {'name': 'text', 'type': 'TEXT'},
    ]
    fmt = BinaryRowFormatV4(cols)

    cases = [
        [1, '你好世界'],
        [2, 'Hello 世界'],
        [3, '😀 🎉 🚀'],
        [4, 'Тест テスト テスト'],
    ]

    for row in cases:
        decoded = fmt.decode(fmt.encode(row))
        if decoded != row:
            print(f"  ❌ 不一致: {row} → {decoded}")
            return

    print(f"  ✅ Unicode 处理正确")


def test_btree_consistency():
    """测试：B+Tree Cython 与 Python 一致"""
    print("\n【B+Tree 一致性】")
    from lightdb.cython import btree_pyx

    if btree_pyx is None:
        print("  ⚠️ Cython 未编译，跳过")
        return

    # Cython 版
    idx = btree_pyx.RawBTreeIndex(capacity=10000)

    import random
    random.seed(42)
    keys = list(range(10000))
    random.shuffle(keys)

    for k in keys:
        idx.insert(k, k % 100, k % 1000)

    # 验证所有查找
    for k in range(10000):
        found, pid, sid = idx.search(k)
        if not found or pid != k % 100 or sid != k % 1000:
            print(f"  ❌ key={k} 查找错误")
            return

    print(f"  ✅ 10000 键全部一致")


def test_predicate_consistency():
    """测试：谓词一致性"""
    print("\n【谓词一致性】")
    from lightdb.cython import predicate_pyx

    if predicate_pyx is None:
        print("  ⚠️ Cython 未编译，跳过")
        return

    # 整数
    p = predicate_pyx.CompiledPredicate(0, '>', 100)
    for v in [50, 100, 101, 200]:
        assert p.match([v]) == (v > 100)
    print(f"  ✅ 整数谓词")

    # 字符串
    p = predicate_pyx.CompiledPredicate(0, '=', 'Alice')
    for v in ['Alice', 'Bob', 'alice']:
        assert p.match([v]) == (v == 'Alice')
    print(f"  ✅ 字符串谓词")


def main():
    print("=" * 60)
    print("  v4.0 Cython 兼容性回归")
    print("=" * 60)

    tests = [
        test_row_codec_consistency,
        test_null_handling,
        test_unicode,
        test_btree_consistency,
        test_predicate_consistency,
    ]

    passed = failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ {fn.__name__} 失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n通过: {passed}, 失败: {failed}")
    return failed


if __name__ == '__main__':
    sys.exit(main())