"""连接失败必须「可行动」：`core/db_errors.explain_connection_error` 的分支。

要求（任务 2）：
  * 2003 / WinError 10061 / ConnectionRefusedError → 明说「host:port 上没有
    服务端在监听」并给**可直接照抄**的命令；
  * 1045 → 用户名或密码不正确；
  * 1049 → 数据库不存在 + 「单库模型，库名可留空」；
  * 其它/超时 → 保留原始报文 + 一句下一步建议；
  * **原始报文完整保留**（`str(exc)` 与 errno 都在）。

本文件全部用**构造的同形异常**（真异常在 `test_studio_autoconnect.py` 里
对着真实服务端跑）——纯函数、无 GUI、无网络，零依赖所以不会 flaky。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest  # noqa: E402

from lightdb_studio.core.db_errors import (  # noqa: E402
    connection_error_summary, explain_connection_error, is_local_host,
    server_start_command)

REFUSED_MSG = ("Can't connect to MySQL server on '127.0.0.1' "
               "([WinError 10061] 由于目标计算机积极拒绝，无法连接。)")
LOCAL_CMD_8899 = ('python -m lightdb.main --mode single --host 127.0.0.1 '
                  '--port 8899 --data-dir data_demo')


def _make_exc(errno, message, exc_type=None):
    """造一个与 pymysql 同形的异常（args=(errno, message)，无 errno 属性）。"""
    from pymysql.err import OperationalError

    cls = exc_type or OperationalError
    return cls(errno, message)


# ==================== 2003 / WinError 10061 / ConnectionRefusedError ====================

def test_2003_winerror_10061_is_actionable_and_keeps_raw():
    """本机 8899 被拒：结论 + host:port + 可照抄命令 + 原始报文全在。"""
    exc = _make_exc(2003, REFUSED_MSG)
    text = explain_connection_error(exc, host='127.0.0.1', port=8899)
    print('\n【E-1】2003/WinError 10061 文案:')
    for line in text.splitlines():
        print('      ' + line)

    assert '127.0.0.1:8899' in text, '没写明是哪个 host:port'
    assert '没有服务端在监听' in text
    assert LOCAL_CMD_8899 in text, '没有给出可直接照抄的启动命令'
    # 原报文一字不丢
    assert REFUSED_MSG in text
    assert str(exc) in text
    assert '2003' in text and '错误码' in text and '报文' in text


def test_connection_refused_error_type_is_recognized():
    """原生 `ConnectionRefusedError`（没有 errno）也要走同一条分支。"""
    exc = ConnectionRefusedError(10061, '由于目标计算机积极拒绝，无法连接。')
    text = explain_connection_error(exc, host='127.0.0.1', port=8898)

    assert '127.0.0.1:8898' in text
    assert '没有服务端在监听' in text
    assert server_start_command('127.0.0.1', 8898) in text
    assert '积极拒绝' in text, '原始报文丢失'


def test_2003_on_remote_host_gives_firewall_advice_not_local_command():
    """非本机 host：不给「本机启动命令」，给「服务端在跑吗/防火墙」。"""
    exc = _make_exc(
        2003, "Can't connect to MySQL server on 'db.example.com:3306' "
              "([WinError 10061] 由于目标计算机积极拒绝，无法连接。)")
    text = explain_connection_error(exc, host='db.example.com', port=3306)

    assert 'db.example.com:3306' in text
    assert '防火墙' in text
    assert 'netstat' in text
    assert '--mode single' not in text, '远程 host 不该给本机启动命令'
    assert 'db.example.com' in text and '10061' in text


def test_2003_timeout_is_not_reported_as_refused():
    """2003 + timed out = 超时（多半防火墙丢包），不能说成「没有服务端」。"""
    exc = _make_exc(2003, "Can't connect to MySQL server on '192.0.2.1' "
                          "(timed out)")
    text = explain_connection_error(exc, host='192.0.2.1', port=3306)

    assert '超时' in text
    assert '没有服务端在监听' not in text
    assert 'timed out' in text, '原始报文丢失'


# ==================== 1045 ====================

def test_1045_says_user_or_password_wrong():
    exc = _make_exc(1045, "Access denied for user 'nobody'@'127.0.0.1'")
    text = explain_connection_error(exc, host='127.0.0.1', port=8899)
    print('\n【E-2】1045 文案:')
    for line in text.splitlines():
        print('      ' + line)

    assert '用户名或密码不正确' in text
    assert '1045' in text
    assert "Access denied for user 'nobody'@'127.0.0.1'" in text
    assert '28000' in text or 'SQLSTATE' in text


# ==================== 1049 ====================

def test_1049_says_database_missing_and_single_db_model():
    exc = _make_exc(1049, "Unknown database 'no_such_db'")
    text = explain_connection_error(exc, host='127.0.0.1', port=8899)
    print('\n【E-3】1049 文案:')
    for line in text.splitlines():
        print('      ' + line)

    assert '库名不存在' in text
    assert '1049' in text
    assert '单库模型' in text and '留空' in text
    assert "Unknown database 'no_such_db'" in text


# ==================== 其它 / 兜底 ====================

def test_other_error_keeps_raw_and_adds_next_step():
    """没有任何 errno 的异常：原始报文照旧，另附一句下一步。"""
    exc = RuntimeError('请先安装 pymysql')
    text = explain_connection_error(exc, host='127.0.0.1', port=8899)

    assert '请先安装 pymysql' in text, '原始报文丢失'
    assert '下一步' in text
    assert '127.0.0.1:8899' in text
    assert server_start_command('127.0.0.1', 8899) in text


def test_missing_host_port_falls_back_to_defaults():
    exc = _make_exc(2003, REFUSED_MSG)
    text = explain_connection_error(exc)

    assert '127.0.0.1:8888' in text, text[:200]
    assert server_start_command('127.0.0.1', 8888) in text


def test_dbquery_error_input_works_and_is_not_duplicated():
    """`DBQueryError`（GUI 里更常见的形态）也能用，且字段块不重复贴。"""
    from lightdb_studio.db.connection import DBQueryError

    exc = DBQueryError(errno=1049, sqlstate='42000',
                       message="Unknown database 'x'")
    text = explain_connection_error(exc, host='127.0.0.1', port=8899)

    assert text.count('报文    :') == 1, '原始报文块被贴了两遍'
    assert "Unknown database 'x'" in text
    assert '库名不存在' in text


def test_explainer_never_raises_on_broken_exception():
    """异常对象本身很怪（str() 抛错）也不能把报错路径弄崩。"""
    class Broken(Exception):
        def __str__(self):
            raise RuntimeError('boom')

    text = explain_connection_error(Broken(), host='127.0.0.1', port=1)
    print(f'\n【E-4】Broken 异常 → {text.splitlines()[0]!r}')
    assert text.strip()


# ==================== 状态栏一行版 / 辅助函数 ====================

def test_summary_is_one_line_and_keeps_command():
    exc = _make_exc(2003, REFUSED_MSG)
    line = connection_error_summary(exc, host='127.0.0.1', port=8899)

    assert '\n' not in line
    assert '127.0.0.1:8899' in line
    assert LOCAL_CMD_8899 in line


def test_server_start_command_is_copy_paste_ready():
    cmd = server_start_command('127.0.0.1', 8899)
    assert cmd == LOCAL_CMD_8899
    assert cmd.startswith('python -m lightdb.main --mode single')


@pytest.mark.parametrize('host,expected', [
    ('127.0.0.1', True), ('localhost', True), ('LOCALHOST', True),
    ('::1', True), ('db.example.com', False), ('10.0.0.5', False),
])
def test_is_local_host(host, expected):
    assert is_local_host(host) is expected
