"""v2.0 Batch 3 测试 - AI生成/数据科学/CDC/血缘可视化/协作

注意：原始文件在"SQL 提取"测试处被截断（末尾三引号未闭合、try 块无收尾）。
本次修复仅恢复该文件至语法正确、可运行状态：
- 闭合 SQL 提取测试的三引号字符串
- 依据 ai_sql_gen 模块 API（_extract_sql）补全该用例断言
- 为 try 块补充异常收尾
- 截断点之后的原始用例（数据科学/CDC/血缘可视化/协作）内容已丢失，不在本文件内
"""
import os
import sys
import time
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_ai_sql_gen():
    """测试1: AI SQL 生成器"""
    print("\n【测试1】AI SQL 生成器 2.0")
    from lightdb_studio.core.ai_sql_gen import (
        AISQLGenerator, Example, Generation)

    tmp = tempfile.mkdtemp()
    import lightdb_studio.core.ai_sql_gen as ag
    old = ag.LEARNING_FILE
    ag.LEARNING_FILE = Path(tmp) / 'learning.json'

    try:
        gen = AISQLGenerator()

        # 添加示例
        gen.add_example('查询用户', 'SELECT * FROM users')
        gen.add_example('统计订单', 'SELECT COUNT(*) FROM orders')
        gen.add_example('查询订单明细', 'SELECT * FROM orders WHERE id = 1')
        gen.add_example('坏例子', 'DROP TABLE users', feedback=-1)

        assert len(gen.examples) == 4
        print(f"  ✅ 添加了 {len(gen.examples)} 个示例")

        # 相似度匹配
        similar = gen.find_similar_examples('查询订单', 3)
        print(f"  → '查询订单' 相似示例:")
        for e in similar:
            print(f"    {e.question} → {e.sql[:40]}")
        assert len(similar) >= 1
        assert any('订单' in e.question for e in similar)

        # 坏例子应被排除
        assert all(e.feedback >= 0 for e in similar)
        print("  ✅ 相似度匹配正确")

        # 统计
        stats = gen.stats()
        print(f"  → 统计: {stats}")
        assert stats['total_examples'] == 4
        assert stats['good_examples'] == 3
        assert stats['bad_examples'] == 1

        # SQL 提取（原文件在此处截断，以下为修复补全）
        text = """好的，这是 SQL:
```sql
SELECT * FROM users WHERE age > 18
```
        """
        extracted = gen._extract_sql(text)
        print(f"  → 提取 SQL: {extracted}")
        assert 'SELECT * FROM users WHERE age > 18' in extracted
        print("  ✅ SQL 提取正确")

        # 恢复原始学习文件路径
        ag.LEARNING_FILE = old
        print("  ✅ AI SQL 生成器全部通过")
    except Exception as e:
        ag.LEARNING_FILE = old
        print(f"  ❌ 测试失败: {e}")
        raise


def main():
    print("=" * 60)
    print("  v2.0 Batch 3 测试 - AI生成（截断修复版，仅含测试1）")
    print("=" * 60)

    tests = [
        ('AI SQL 生成器', test_ai_sql_gen),
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
        print("  🎉 v2.0 Batch 3（修复版）通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())