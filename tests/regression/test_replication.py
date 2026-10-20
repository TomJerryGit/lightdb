"""
主从复制回归测试
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def test_binlog_write():
    """Binlog 写入"""
    print("\n【Binlog 写入】")
    from lightdb.replication.binlog import Binlog
    from pathlib import Path
    import tempfile

    tmp = tempfile.mkdtemp()
    bl = Binlog(tmp)

    pos1 = bl.append(1, 'INSERT', 'users', {'row': [1, 'a']})
    pos2 = bl.append(1, 'INSERT', 'users', {'row': [2, 'b']})
    pos3 = bl.append(1, 'COMMIT', None, {})
    bl.flush()

    assert pos1 > 0
    assert pos2 > pos1
    assert pos3 > pos2
    print(f"  ✅ 写入 3 事件，末位点={pos3}")

    # 读取
    events = bl.read_from(1, limit=10)
    assert len(events) >= 3
    print(f"  ✅ 读取 {len(events)} 事件")

    bl.close()


def test_binlog_rotation():
    """Binlog 轮转"""
    print("\n【Binlog 轮转】")
    from lightdb.replication.binlog import Binlog
    from pathlib import Path
    import tempfile

    tmp = tempfile.mkdtemp()
    bl = Binlog(tmp)
    bl.MAX_FILE_SIZE = 1024    # 小一点触发轮转

    # 写很多事件
    for i in range(100):
        bl.append(1, 'INSERT', 't1', {'row': [i] * 20})

    bl.flush()
    print(f"  ✅ 写入 100 事件后当前文件: {bl.current_file()}")

    bl.close()


def test_relay_log():
    """Relay Log"""
    print("\n【Relay Log】")
    from lightdb.replication.slave import RelayLog
    from pathlib import Path
    import tempfile

    tmp = tempfile.mkdtemp()
    relay = RelayLog(tmp)

    relay.append((1, 1, 'INSERT', 't1', '{}'))
    relay.append((2, 1, 'INSERT', 't1', '{}'))
    relay.append((3, 1, 'COMMIT', None, '{}'))

    events = relay.pop_all()
    assert len(events) == 3
    print(f"  ✅ Relay 取出 {len(events)} 事件")

    # 再次取应为空
    assert len(relay.pop_all()) == 0
    relay.close()


def test_semi_sync_removed():
    """半同步已移除（决策 D 阶段 2 —— 不实现）

    原 `test_semi_sync` 实例化的是死代码（无任何生产调用者）；阶段 2 按
    决策删除该类与导出。这里锁住"不再暴露"：包上无 SemiSync 类、
    历史模块路径导入显式报 ImportError。
    """
    print("\n【半同步已移除】")
    import pytest
    import lightdb.replication as rep_pkg

    assert not hasattr(rep_pkg, 'SemiSyncMaster')
    assert not hasattr(rep_pkg, 'SemiSyncSlave')
    with pytest.raises(ImportError):
        from lightdb.replication import semi_sync  # noqa: F401
    print("  ✅ 半同步死代码已移除（决策：不实现）")


def main():
    print("=" * 60)
    print("  复制回归测试")
    print("=" * 60)

    tests = [
        test_binlog_write,
        test_binlog_rotation,
        test_relay_log,
        test_semi_sync_removed,
    ]

    passed = failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ {fn.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n通过: {passed}, 失败: {failed}")
    return failed


if __name__ == '__main__':
    sys.exit(main())