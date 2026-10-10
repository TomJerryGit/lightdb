"""多进程服务端端到端回归

验证多进程接线后的真实行为：
1. Master 绑定端口，把 accept 到的连接交给 Worker
2. 连接分散到多个 Worker 进程
3. 写操作在 Master 侧统一执行 —— 跨进程立即可见
4. 优雅停机后端口释放、共享内存无残留

注：需要进程间队列（Windows 下是命名管道）。受限沙箱里会 skip。
"""
import os
import shutil
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(ROOT, 'data_mp_e2e_test')
PORT = 8893
WORKERS = 3


def _wait_port(port, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout=1)
            s.close()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _port_open(port):
    try:
        s = socket.create_connection(('127.0.0.1', port), timeout=1)
        s.close()
        return True
    except Exception:
        return False


def test_multiprocess_server_end_to_end():
    import pytest
    import pymysql

    # 环境自检：受限沙箱禁止进程间队列，此时跳过而不是误报失败
    try:
        import multiprocessing as mp
        _q = mp.Queue()
        _q.close()
    except Exception as e:
        pytest.skip(f'当前环境不支持进程间队列: {e}')

    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.master import MasterServer

    shutil.rmtree(DATA_DIR, ignore_errors=True)
    cfg = {
        'host': '127.0.0.1',
        'port': PORT,
        'workers': WORKERS,
        'data_dir': DATA_DIR,
        'log_level': 'INFO',
        'buffer_pool_size': 256,
    }

    db = Database(DATA_DIR)
    executor = Executor(db)
    master = MasterServer(cfg, db, executor)
    t = threading.Thread(target=master.start, daemon=True)
    t.start()

    try:
        # 1. 端口被监听
        assert _wait_port(PORT), 'Master 未监听端口'

        conn = pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                               password='', autocommit=True,
                               charset='utf8mb4', connect_timeout=15)
        try:
            cur = conn.cursor()
            cur.execute("CREATE TABLE t (id INT PRIMARY KEY, "
                        "name VARCHAR(20), v INT)")
        finally:
            conn.close()

        # 元数据必须能跨 Worker 刷新：快照每 5 秒同步一次，
        # 因此新连接上的 SHOW TABLES 最多等 12 秒应看到刚建的表
        deadline = time.time() + 12
        seen = False
        while time.time() < deadline:
            c = pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                                password='', autocommit=True,
                                charset='utf8mb4', connect_timeout=15)
            try:
                cu = c.cursor()
                cu.execute("SHOW TABLES")
                names = [row[0] for row in cu.fetchall()]
            finally:
                c.close()
            if 't' in names:
                seen = True
                break
            time.sleep(1)
        assert seen, 'SHOW TABLES 未看到刚创建的表（元数据快照未刷新）'

        # 2/3. 并发客户端：写完再查，三方必须互相可见
        results = {}
        errors = []
        barrier = threading.Barrier(WORKERS, timeout=90)
        after_insert = threading.Barrier(WORKERS, timeout=90)

        def client(i):
            try:
                c = pymysql.connect(host='127.0.0.1', port=PORT,
                                    user='root', password='',
                                    autocommit=True, charset='utf8mb4',
                                    connect_timeout=15)
                cu = c.cursor()
                barrier.wait()
                cu.execute("INSERT INTO t VALUES (%s, %s, %s)",
                           (i, 'name_%d' % i, i * 10))
                after_insert.wait()
                cu.execute("SELECT COUNT(*) FROM t")
                results[i] = cu.fetchone()[0]
                c.close()
            except Exception as e:
                errors.append(f'client{i}: {type(e).__name__}: {e}')

        threads = [threading.Thread(target=client, args=(i,))
                   for i in range(WORKERS)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=120)

        assert not errors, f'并发客户端出错: {errors}'
        assert results == {i: WORKERS for i in range(WORKERS)}, \
            f'跨进程可见性不一致: {results}'

        # 2. 连接确实分散到多个 Worker
        agg = master.stats_hub.aggregate() if master.stats_hub else {}
        per_worker = agg.get('per_worker', {})
        used = sum(1 for w in per_worker.values()
                   if w.get('connections', 0) > 0)
        assert used >= 2, f'连接未被分散到多个 Worker: {per_worker}'
    finally:
        master.shutdown()
        t.join(timeout=20)
        try:
            db.shutdown()
        except Exception:
            pass

    # 4. 停机后端口释放、共享内存无残留
    time.sleep(0.5)
    assert not _port_open(PORT), '停机后端口仍在监听'
    shm_dir = os.path.join(DATA_DIR, 'shm')
    left = os.listdir(shm_dir) if os.path.isdir(shm_dir) else []
    assert not left, f'共享内存未清理: {left}'
    shutil.rmtree(DATA_DIR, ignore_errors=True)


def main():
    test_multiprocess_server_end_to_end()
    print('多进程服务端端到端验证通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
