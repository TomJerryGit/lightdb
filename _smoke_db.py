# -*- coding: utf-8 -*-
"""LightDB 数据库冒烟测试：启动服务器 -> 客户端连接 -> 建表/插入/查询"""
import os
import shutil
import sys
import time
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

DATA_DIR = os.path.join(ROOT, 'data_smoke')
PORT = 8999


def main():
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)

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
    conn = pymysql.connect(host='127.0.0.1', port=PORT, user='root',
                           autocommit=True)
    cur = conn.cursor()

    # 建表
    cur.execute("CREATE TABLE IF NOT EXISTS smoke_users (id INT PRIMARY KEY, name VARCHAR(50), age INT)")
    # 插入
    for i in range(5):
        cur.execute("INSERT INTO smoke_users (id, name, age) VALUES (%s, %s, %s)",
                    (i, f'user{i}', 20 + i))
    conn.commit()
    # 查询
    cur.execute("SELECT COUNT(*) FROM smoke_users")
    cnt = cur.fetchone()[0]
    assert cnt == 5, f"COUNT = {cnt}"
    cur.execute("SELECT id, name, age FROM smoke_users WHERE age >= 22 ORDER BY id")
    rows = cur.fetchall()
    print(f"查询结果 (age>=22): {rows}")
    assert len(rows) == 3
    # 聚合
    cur.execute("SELECT AVG(age), MAX(age) FROM smoke_users")
    avg, mx = cur.fetchone()
    print(f"AVG(age)={avg}, MAX(age)={mx}")
    # 更新
    cur.execute("UPDATE smoke_users SET age = age + 1 WHERE id = 0")
    conn.commit()
    cur.execute("SELECT age FROM smoke_users WHERE id = 0")
    assert cur.fetchone()[0] == 21
    # 删除
    cur.execute("DELETE FROM smoke_users WHERE id = 4")
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM smoke_users")
    assert cur.fetchone()[0] == 4
    cur.close()
    conn.close()
    srv.stop()
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    print("SMOKE TEST PASSED ✅  (建表/插入/查询/聚合/更新/删除 全部正常)")


if __name__ == '__main__':
    main()
