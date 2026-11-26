"""检查点中断窗口的崩溃恢复：数据页已落盘、根指针未落盘时不得出现重复行。

场景（确定性、无随机）：
1) 建表并插入 N 行（WAL 已 flush 到 OS，页也已 flush 到磁盘）；
2) 只做 `BufferPool.flush()`（数据页落盘），**不**保存 BTree 根、**不**截断 WAL；
3) 直接 `os._exit(0)` 模拟硬杀；
4) 重新打开数据目录：recovery 应把每个主键恢复为恰好一行（不能重复）。
"""
import os
import subprocess
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_recovery_after_interrupted_checkpoint_no_duplicate_rows(tmp_path):
    data = str(tmp_path / 'data')
    script = textwrap.dedent('''
        import os, sys
        sys.path.insert(0, r"%s")
        from lightdb.db.database import Database
        from lightdb.sql.executor import Executor

        db = Database(r"%s")
        ex = Executor(db)
        ex.execute_sql("CREATE TABLE t (id INT PRIMARY KEY, v INT)", {})
        for i in range(1, 201):
            ex.execute_sql("INSERT INTO t VALUES (%%s, %%s)" %% (i, i), {})
        # 模拟 checkpoint 中断：数据页落盘，但根指针/ WAL 都停在旧状态
        db.tables["t"].bp.flush()
        os._exit(0)
    ''' % (ROOT.replace('\\', '\\\\'), data))
    env = {**os.environ, 'PYTHONPATH': ROOT,
           'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
    proc = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                          env=env, timeout=300)
    assert proc.returncode == 0

    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(data)
    ex = Executor(db)
    try:
        cnt = ex.execute_sql('SELECT COUNT(*) FROM t', {})['rows'][0][0]
        distinct = ex.execute_sql(
            'SELECT COUNT(DISTINCT id) FROM t', {})['rows'][0][0]
        assert cnt == 200, f'恢复后行数错误: {cnt}（应为 200）'
        assert distinct == 200, f'恢复后主键重复: distinct={distinct}'
        sm = ex.execute_sql('SELECT SUM(v) FROM t', {})['rows'][0][0]
        assert sm == 200 * 201 // 2
        ints = db.tables['t'].integrity_scan(deep=True)
        assert ints['live_rows'] == 200, ints
        assert ints['index_entries'] == 200, ints
    finally:
        db.shutdown()
