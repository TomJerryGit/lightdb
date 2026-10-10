# -*- coding: utf-8 -*-
"""诊断：pymysql 空密码握手时实际发送的 auth_response 长度/内容"""
import os
import shutil
import sys
import time
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

DATA_DIR = os.path.join(ROOT, 'data_diag')
PORT = 8898


def main():
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)

    import lightdb.net.mysql_protocol as mp

    orig = mp.mysql_native_password

    def spy(auth_response, auth_seed, stored_hash):
        print(f"[SPY] auth_response len={len(auth_response)} "
              f"first8={auth_response[:8].hex()} seed={auth_seed[:4].hex()} "
              f"stored_hash={stored_hash.hex() or '(empty)'}")
        # 检查空密码场景的正确算法
        import hashlib
        if stored_hash == b'':
            # 尝试按正确算法：candidate = resp XOR sha1(seed+stored_hash)
            # 空密码下 pymysql 若发送 scramble，则 resp = sha1(b'') XOR sha1(seed+stored_hash)
            print("[SPY] stored_hash empty -> 空密码分支判定:",
                  len(auth_response) == 0)
        return orig(auth_response, auth_seed, stored_hash)

    mp.mysql_native_password = spy

    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = PORT
    db = Database(DATA_DIR)
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(1.0)

    import pymysql
    try:
        conn = pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                               autocommit=True)
        print("CONNECT OK")
        conn.close()
    except Exception as e:
        print(f"CONNECT FAIL: {e}")

    import pymysql.version
    print("pymysql version:", pymysql.version.VERSION)
    srv.stop()
    shutil.rmtree(DATA_DIR, ignore_errors=True)


if __name__ == '__main__':
    main()
