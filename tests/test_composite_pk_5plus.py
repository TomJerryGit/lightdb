"""复合主键 5–8 列：创建/写入/点查/前缀/重启。"""
from lightdb.db.database import Database
from lightdb.sql.executor import Executor


def _exec(ex, sql):
    return ex.execute_sql(sql, {})


def test_composite_pk_5_and_8_columns(tmp_path):
    db = Database(str(tmp_path / 'data'))
    ex = Executor(db)
    try:
        for n in (5, 8):
            cols = ', '.join(f'c{i} INT' for i in range(1, n + 1))
            pk = ', '.join(f'c{i}' for i in range(1, n + 1))
            t = f't{n}'
            _exec(ex, f'CREATE TABLE {t} ({cols}, PRIMARY KEY ({pk}))')
            v1 = ', '.join(str(i) for i in range(1, n + 1))
            v2 = ', '.join(str(i + 10) for i in range(1, n + 1))
            _exec(ex, f'INSERT INTO {t} VALUES ({v1})')
            _exec(ex, f'INSERT INTO {t} VALUES ({v2})')
            point = ' AND '.join(f'c{i}={i}' for i in range(1, n + 1))
            r = _exec(ex, f'SELECT COUNT(*) FROM {t} WHERE {point}')
            assert r['rows'][0][0] == 1
            r = _exec(ex, f'SELECT c1 FROM {t} WHERE c1=1')
            assert len(r['rows']) == 1
            r = _exec(ex, f'SELECT COUNT(*) FROM {t} WHERE c1=1 AND c2>0')
            assert r['rows'][0][0] == 1
            # 只有部分前缀（c1、c2）仍应可查
            r = _exec(ex, f'SELECT COUNT(*) FROM {t} WHERE c1=1 AND c2=2')
            assert r['rows'][0][0] == 1
    finally:
        db.shutdown()

    db2 = Database(str(tmp_path / 'data'))
    ex2 = Executor(db2)
    try:
        assert _exec(ex2, 'SELECT COUNT(*) FROM t8')['rows'][0][0] == 2
        assert _exec(ex2, 'SELECT COUNT(*) FROM t5')['rows'][0][0] == 2
    finally:
        db2.shutdown()
