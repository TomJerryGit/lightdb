"""复制「摘牌 + 状态语义」诚实性（P1 修复 E）

背景（见 docs/analysis_2026-10-19/05_RISK_DEEP_DIVE.md §E）：

* 复制链路本身不可用于生产：位点来自"查询时重扫各表当前 WAL"的派生视图，
  ALTER / checkpoint 后会**回退或归零**（事件丢失）、DDL 不复制、没有
  `START SLAVE` 等控制语句。本修复**不做**真 binlog 重写，只做两件事：
  **E1** README 摘牌（实验性、单向、不可用于生产）+ **E2** 状态语义修正。
* E2：`_record_sql_error` 旧实现用 `_errno_of(exc)` 取错误号，内置异常
  （`KeyError` 缺表 / `SyntaxError` 语法 / `ValueError` 唯一冲突）没有
  errno → 写 0，于是 `Last_SQL_Errno=0` 与 `Last_SQL_Error` 非空并存。
  MySQL 里 0 = 没有错误，这是自相矛盾的假状态。现在按错误文本映射
  （缺表 1146 / 语法 1064 / 唯一冲突 1062 / 其它 1105），保证
  **有错误文本 ⇒ Errno 非 0**；`show_status()` 与 `SHOW SLAVE STATUS`
  是同一份取值。

本文件不启动复制线程（参考 tests/test_batch8.py 的构造方式，只验证状态
语义与文档口径）。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
except ImportError:                       # 直接 python tests/test_replication_honesty.py
    from conftest import track

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ==================== 公共工具 ====================

@pytest.fixture()
def env(tmp_path):
    """一个 Database/Executor + 未启动的 SlaveReplicator（不跑复制线程）"""
    from lightdb.db.database import Database
    from lightdb.replication.slave import SlaveReplicator
    from lightdb.sql.executor import Executor

    db = track(Database(str(tmp_path / 'data')))
    ex = track(Executor(db))
    rep = SlaveReplicator(db, '127.0.0.1', 3306, user='root')
    try:
        yield db, ex, rep
    finally:
        try:
            rep.stop()
        except Exception:
            pass


def show_status(ex):
    """执行器渲染的 `SHOW SLAVE STATUS`（dict：列名 -> 值）"""
    r = ex.execute_sql('SHOW SLAVE STATUS')
    assert r.get('type') == 'resultset', r
    assert len(r['rows']) == 1, r
    return dict(zip(r['columns'], r['rows'][0]))


def _readme():
    with open(os.path.join(ROOT, 'README.md'), encoding='utf-8') as f:
        return f.read()


# ==================== E2：错误号语义 ====================

def test_status_without_error_is_zero_and_empty(env):
    """没有错误时 `Last_SQL_Errno=0` / `Last_SQL_Error=''`（MySQL 语义）"""
    _db, ex, rep = env
    st = rep.show_status()
    assert st['Last_SQL_Errno'] == 0 and st['Last_SQL_Error'] == '', st
    assert st['Last_IO_Errno'] == 0 and st['Last_IO_Error'] == '', st
    # SHOW SLAVE STATUS 与 show_status() 一致
    shown = show_status(ex)
    assert shown['Last_SQL_Errno'] == 0
    assert shown['Last_SQL_Error'] == ''
    assert shown['Last_Errno'] == 0 and shown['Last_Error'] == ''
    print('  → 无错误：Errno 0 / Error 空，SHOW SLAVE STATUS 一致')


def test_missing_table_sql_error_maps_to_1146(env):
    """缺表错误（KeyError，没有 errno）→ Last_SQL_Errno 非 0（1146）"""
    _db, ex, rep = env
    rep._record_sql_error(KeyError('表 users 不存在'))
    st = rep.show_status()
    assert st['Last_SQL_Errno'] != 0, st
    assert st['Last_SQL_Errno'] == 1146, st
    assert st['Last_SQL_Error'], st
    assert 'users' in st['Last_SQL_Error']
    # 执行器 SHOW SLAVE STATUS 是同一份取值（含 Last_Errno 别名）
    shown = show_status(ex)
    for key in ('Last_SQL_Errno', 'Last_SQL_Error', 'Last_Errno',
                'Last_Error'):
        assert shown[key] == st[key], (key, shown[key], st[key])
    assert shown['Last_Errno'] != 0
    assert shown['Last_SQL_Error_Timestamp'], shown
    print(f"  → 缺表 KeyError -> Last_SQL_Errno={st['Last_SQL_Errno']}, "
          f"Error={st['Last_SQL_Error']!r}")


def test_other_error_classes_get_nonzero_errno(env):
    """语法 1064 / 唯一冲突 1062 / 其它 1105，共同保证"有文本 ⇒ 非 0\""""
    _db, ex, rep = env
    cases = [
        (SyntaxError('SQL 语法错误 near FROM'), 1064),
        (ValueError("Duplicate entry '1' for key 'PRIMARY'"), 1062),
        (RuntimeError('boom: 未知运行时错误'), 1105),
    ]
    for exc, want in cases:
        rep._record_sql_error(exc)
        st = rep.show_status()
        assert st['Last_SQL_Errno'] == want, (exc, st)
        assert st['Last_SQL_Errno'] != 0 and st['Last_SQL_Error'], st
        shown = show_status(ex)
        assert shown['Last_SQL_Errno'] == st['Last_SQL_Errno']
        print(f"  → {type(exc).__name__}: Last_SQL_Errno={st['Last_SQL_Errno']}"
              f", Error={st['Last_SQL_Error'][:40]!r}")


def test_errno_zero_only_when_error_text_empty(env):
    """`Errno=0` 只在确实没有错误文本时出现（不再 0 + 文本并存）"""
    _db, _ex, rep = env
    # 一个"没有错误文本"的异常：按 0 处理（确实没有错误信息）
    rep._record_sql_error(KeyError())
    st = rep.show_status()
    assert (st['Last_SQL_Errno'] == 0) == (st['Last_SQL_Error'] == ''), st
    if st['Last_SQL_Errno'] == 0:
        assert st['Last_SQL_Error'] == ''
    print('  → 0 与空文本严格同现（不再自相矛盾）')


# ==================== E1：README 复制段摘牌 ====================

def _replication_section(text):
    """截出 README 的「复制支持边界」一节（到下一个 `### ` 标题为止）"""
    m = re.search(r'^###\s*[^\n]*复制支持边界[^\n]*$', text, re.MULTILINE)
    assert m is not None, 'README 缺少「复制支持边界」一节'
    start = m.end()
    nxt = re.search(r'^#{2,3}\s', text[start:], re.MULTILINE)
    end = start + nxt.start() if nxt else len(text)
    return text[start:end]


def test_readme_replication_matches_beta_binlog_contract():
    """决策 D 后：README 复制段必须如实写明「beta + 真 append-only binlog + 剩余限制」

    契约随实现升级而升级（原 E1 摘牌契约已被真 binlog 取代）：
    * 定位必须是 beta（不是"生产可用"，也不是继续停留在"仅实验性摘牌"）；
    * 必须写明已落地的核心事实：append-only binlog / CRC / 真实位点 / DDL 复制；
    * 必须继续列明**尚未完成**的限制，防止重新过度宣称：无日志轮转、无 GTID、无半同步、
      binlog 写失败不拒绝提交。
    """
    text = _readme()
    section = _replication_section(text)
    for keyword in ('beta', '单向', 'append-only binlog', 'CRC'):
        assert keyword in section, (
            f'README 复制段缺少 beta 契约关键词 {keyword!r}:\n{section[:500]}')
    for defect in ('轮转', 'GTID', '半同步', '写失败'):
        assert defect in section, (
            f'README 复制段没有写明未完成项 {defect!r}:\n{section[:500]}')
    # 「当前实测状态」首段也要有明确结论（不埋在支持边界表格里）
    head = text[:text.index('### 🔐')]
    assert '复制' in head and 'beta' in head, (
        'README「当前实测状态」缺少复制 beta 定位的明确结论')
    print('  → README 复制段符合 beta 契约（append-only + CRC + 未完成项清单）')


def test_readme_has_no_replication_production_claim():
    """README 不得出现「复制生产可用」这类过度声明（beta 契约下仍禁止）"""
    text = _readme()
    for bad in ('复制可用于生产', '复制已经在生产', '生产级复制'):
        assert bad not in text, f'README 仍存在复制生产声明: {bad!r}'
    print('  → README 无复制"生产可用"类声明')
