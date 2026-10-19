"""pytest 全局配置

中文 Windows 控制台默认 GBK(cp936)，而本仓库的测试里有大量 emoji 输出
（✅ / ❌ / → 等）。在 GBK 下这些 print 会直接抛 UnicodeEncodeError，
测试因此被判失败；更麻烦的是异常发生在 `db.shutdown()` 之前，插入的数据
还留在 BufferPool 内存里没有落盘，导致后面依赖同一数据目录的测试读到空表。

这里把标准流改成「无法编码的字符就替换」，与 lightdb/main.py 的处理一致：
中文照常显示，emoji 变问号，但不再中断测试。
"""
import gc
import os
import shutil
import sys
import time

import pytest

# ==================== P0-A 安全修复的测试降级开关 ====================
# 引擎默认部署不再有「root 空口令」：新库会生成随机口令、旧空口令库会在
# 启动时迁移。而本仓库 700+ 既有用例（含大量经真实 MySQLServer 的
# pymysql 连接，例如 tests/test_batch*.py、test_ddl_p1、test_index_declarations、
# test_metadata_honesty、test_mysql_connect、test_snapshot_integrity）都按
# `root/''` 连接。这里在**收集阶段、任何 Database 构造之前**显式降级，等价于
# CLI `--allow-empty-password`（显式、可审计，不是把安全默认改回不安全）。
# 新默认行为本身由 tests/test_auth_defaults.py 单独验证（那里会 delenv）。
os.environ.setdefault('LIGHTDB_ALLOW_EMPTY_PASSWORD', '1')


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except Exception:
        pass


# ==================== 资源生命周期：用例结束时一定关闭 ====================
# 背景（见 docs/FAILURE_TRIAGE.md 第 4 节与「teardown 修复后复核」）：
# 用例通常写成 `db = Database(DATA_DIR)` … 末尾 `db.shutdown()`。一旦断言
# 失败或中途抛异常，末尾的 shutdown 就执行不到。CPython 本可在帧销毁时
# 回收句柄，但 pytest 会一直持有失败用例的异常回溯（traceback → 帧 →
# 局部变量 db），句柄于是活到整个会话结束；下一个用例 reset() 里的
# shutil.rmtree 就撞上「文件被其它进程占用」（winerror=32），把真正的
# 实现缺陷掩盖成清理失败。
#
# 修法：每处 Database / MySQLServer / 客户端连接等资源创建都用 track()
# 登记，下面的 autouse fixture 在用例结束时（正常结束、断言失败、异常、
# fixture/setup 报错）统一关闭，与用例里显式的 shutdown() 互补。

_TRACKED = []


def track(resource):
    """登记一个需要在本用例结束时关闭的资源，并原样返回它。

    用法：``db = track(Database(DATA_DIR))`` / ``srv = track(MySQLServer(...))``
    """
    _TRACKED.append(resource)
    return resource


def shutdown_quietly(*resources):
    """尽最大努力关闭资源；单个资源关闭失败不影响其它资源。

    依次尝试 shutdown() / server_close() / stop() / stop_event_scheduler()
    / close()，只调用对象真实拥有的方法。
    """
    for res in resources:
        if res is None:
            continue
        for name in ('shutdown', 'server_close', 'stop',
                     'stop_event_scheduler', 'close'):
            fn = getattr(res, name, None)
            if not callable(fn):
                continue
            try:
                fn()
            except Exception:
                pass


@pytest.fixture(autouse=True)
def _guarantee_resource_cleanup():
    """用例结束后强制关闭本用例登记过的资源（含失败路径）。"""
    yield
    pending = list(_TRACKED)
    del _TRACKED[:]
    shutdown_quietly(*pending)


# ==================== 目录清理兜底：safe_rmtree ====================
# 正常情况下，上面的 track()+fixture 已经保证句柄全部释放，rmtree 一次
# 成功。这里再做一层兜底：万一还有别的持有者（外部进程、杀毒软件、
# Windows 索引器）短暂占用，就有限次重试 + 短退避；仍然失败则**抛出**
# 带路径与占用文件的异常。绝不 pytest.skip、绝不 ignore_errors——清理
# 失败必须让用例失败，否则脏目录会让后续用例读到旧数据，把实现缺陷
# 掩盖成"通过"。

_WIN_BUSY_WINERRORS = (32, 33)   # 文件被占用 / 文件被锁定
_RMTREE_ATTEMPTS = 5
_RMTREE_DELAY = 0.2


def _error_fingerprint(exc):
    """把底层错误压成不依赖本地化文本的短描述。"""
    return (f'{type(exc).__name__}(errno={getattr(exc, "errno", None)}, '
            f'winerror={getattr(exc, "winerror", None)}, '
            f'file={getattr(exc, "filename", None)!r})')


def _occupied_files(path):
    """列出目录下的残留文件，并指出其中仍打不开（=还被占用）的那些。"""
    remaining, occupied = [], []
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            remaining.append(fp)
            try:
                with open(fp, 'r+b'):
                    pass
            except OSError as e:
                occupied.append(f'{fp} [{_error_fingerprint(e)}]')
    return remaining, occupied


def safe_rmtree(path, attempts=_RMTREE_ATTEMPTS, delay=_RMTREE_DELAY):
    """删除目录树；对「文件被占用」做有限次重试，仍失败则抛 RuntimeError。

    - 目录不存在视为成功（幂等）。
    - 每次失败先 gc.collect() 再退避，给即将退出的持有者一点时间。
    - 最终失败时异常信息包含目录路径、最后一次底层错误（errno/winerror）、
      以及仍在目录里且打不开的具体文件，便于直接定位是谁没关。
    """
    for i in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as e:
            last = e
            gc.collect()
            if i < attempts:
                time.sleep(delay)
    remaining, occupied = _occupied_files(path)
    detail = '；'.join(occupied) or '；'.join(remaining) or '（已无残留文件）'
    raise RuntimeError(
        f'safe_rmtree 清理失败：重试 {attempts} 次 × {delay}s 仍未删除 {path}。'
        f'最后一次错误 {_error_fingerprint(last)}；'
        f'仍被占用或残留：{detail}。'
        '请检查该用例是否漏关 Database / MySQLServer / pymysql 连接'
        '（用 tests/conftest.py 的 track() 登记即可自动关闭）')


# ==================== 多进程原语能力探测 ====================
# 受限沙箱（DSH workspace-write 等）禁止命名管道/进程间原语：
#   multiprocessing.Queue()  -> PermissionError: [WinError 5] 拒绝访问
#   multiprocessing.Pipe()   -> PermissionError: [WinError 5] 拒绝访问
#   multiprocessing.Pool(2)  -> PermissionError: [WinError 5] 拒绝访问
# 这类失败是「环境不支持」，不是「实现不达标」，必须在做危险动作之前
# 探测出来并 pytest.skip，否则测试会把环境限制误报成实现缺陷。
#
# 探测是真实的能力探测（真的创建队列/管道、真的起一个最简子进程并等它
# 退出）：在任何多进程可用的环境（Linux、macOS、放宽权限后的 Windows）
# 上探测会成功，测试照原样跑完全部断言，不会误 skip。
MP_PROBE_HINT = (
    '本环境禁用多进程原语（沙箱），该用例无法运行')


def mp_primitives_probe():
    """真实探测多进程原语是否可用。

    返回 None 表示可用；返回错误字符串表示不可用（含原始错误文本）。
    只读探测，不改变任何全局状态。
    """
    import multiprocessing as mp

    # 1) 队列（命名管道）
    try:
        q = mp.Queue()
        try:
            q.put(1)
            q.get_nowait()
        finally:
            q.close()
            try:
                q.join_thread()
            except Exception:
                pass
    except BaseException as e:           # noqa: BLE001 - 探测要吞掉一切
        return f'{type(e).__name__}: {e}'

    # 2) 最简子进程 + 双向管道，并带超时 join
    proc = None
    try:
        parent_conn, child_conn = mp.Pipe()

        def _probe_child(conn):
            try:
                conn.send('ok')
            except Exception:
                pass

        proc = mp.Process(target=_probe_child, args=(child_conn,))
        proc.start()
        if not parent_conn.poll(30):
            return '子进程 30s 内没有回话（进程启动被阻断）'
        if parent_conn.recv() != 'ok':
            return '子进程回话内容异常'
        proc.join(30)
        if proc.is_alive():
            proc.terminate()
            proc.join(10)
            return '子进程 30s 内没有退出（进程 join 被阻断）'
        if proc.exitcode != 0:
            return f'最简子进程退出码异常: {proc.exitcode}'
    except BaseException as e:           # noqa: BLE001
        return f'{type(e).__name__}: {e}'
    finally:
        if proc is not None and proc.is_alive():
            try:
                proc.kill()
                proc.join(5)
            except Exception:
                pass
    return None


def require_mp_primitives():
    """环境不支持多进程原语时 skip，支持时静默返回（不削弱任何断言）。"""
    import pytest

    err = mp_primitives_probe()
    if err is not None:
        pytest.skip(f'{MP_PROBE_HINT}：{err}')


# 兼容两种叫法（pytest 可能以 conftest 之外的模块名注册本文件）
require_multiprocess = require_mp_primitives


def thread_stack_probe():
    """真实探测「能否新建线程 + 线程栈是否可写」。

    返回 None 表示可用；返回错误字符串表示不可用（含原始错误文本）。
    线程池/工作窃取类并行用例依赖它；它探测的是环境能力，不涉及任何
    性能判据。
    """
    import threading

    result = {}

    def _tiny():
        result['ok'] = True

    try:
        t = threading.Thread(target=_tiny, name='dsh-probe')
        t.start()
        t.join(30)
        if t.is_alive():
            return '探测线程 30s 内没有结束（线程创建/调度被阻断）'
    except BaseException as e:           # noqa: BLE001
        return f'{type(e).__name__}: {e}'
    if not result.get('ok'):
        return '探测线程没有正常执行'
    return None


def require_thread_stacks():
    """环境不支持新建线程时 skip，支持时静默返回（不削弱任何断言）。"""
    import pytest

    err = thread_stack_probe()
    if err is not None:
        pytest.skip(
            f'本环境禁用线程/线程池原语（沙箱），该并行用例无法运行：{err}')
