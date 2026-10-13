"""pytest 全局配置

中文 Windows 控制台默认 GBK(cp936)，而本仓库的测试里有大量 emoji 输出
（✅ / ❌ / → 等）。在 GBK 下这些 print 会直接抛 UnicodeEncodeError，
测试因此被判失败；更麻烦的是异常发生在 `db.shutdown()` 之前，插入的数据
还留在 BufferPool 内存里没有落盘，导致后面依赖同一数据目录的测试读到空表。

这里把标准流改成「无法编码的字符就替换」，与 lightdb/main.py 的处理一致：
中文照常显示，emoji 变问号，但不再中断测试。
"""
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except Exception:
        pass


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
