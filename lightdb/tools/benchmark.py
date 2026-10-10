"""
lightdb-bench - 性能基准工具
用法: python -m lightdb.tools.benchmark -h 127.0.0.1 -P 8888 -u root
"""
import argparse
import sys
import time


def main():
    ap = argparse.ArgumentParser(description='LightDB Benchmark')
    ap.add_argument('-h', '--host', default='127.0.0.1', dest='host')
    ap.add_argument('-P', '--port', type=int, default=8888)
    ap.add_argument('-u', '--user', default='root')
    ap.add_argument('-p', '--password', default='')
    ap.add_argument('-n', '--rows', type=int, default=5000)
    ap.add_argument('-c', '--concurrency', type=int, default=10)
    args = ap.parse_args()

    try:
        import pymysql
    except ImportError:
        print("pip install pymysql", file=sys.stderr)
        sys.exit(1)

    try:
        conn = pymysql.connect(
            host=args.host, port=args.port,
            user=args.user, password=args.password,
            autocommit=True)
    except Exception as e:
        print(f"连接失败: {e}")
        sys.exit(1)

    cur = conn.cursor()
    table = '__bench__'

    print("=" * 60)
    print(f"  LightDB Benchmark ({args.rows} rows, {args.concurrency} threads)")
    print("=" * 60)

    # 准备
    cur.execute(f"DROP TABLE {table}")
    cur.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, name VARCHAR, score INT)")

    # 1. 批量插入
    print("\n[1] 批量插入")
    t0 = time.time()
    for i in range(args.rows):
        cur.execute(f"INSERT INTO {table} VALUES ({i+1}, 'u{i}', {i%100})")
    t1 = time.time()
    el = t1 - t0
    print(f"    {args.rows} 行, {el:.2f}s, {args.rows/el:.0f} rows/s")

    # 2. 点查
    print("\n[2] 主键点查")
    N = 1000
    import random
    t0 = time.time()
    for _ in range(N):
        cur.execute(f"SELECT * FROM {table} WHERE id = {random.randint(1, args.rows)}")
        cur.fetchone()
    t1 = time.time()
    el = t1 - t0
    print(f"    {N} 次, {el:.2f}s, {N/el:.0f} QPS, {el/N*1000:.2f} ms/op")

    # 3. 范围扫描
    print("\n[3] 范围扫描")
    N = 100
    t0 = time.time()
    for _ in range(N):
        lo = random.randint(1, args.rows - 100)
        cur.execute(f"SELECT * FROM {table} WHERE id >= {lo} AND id <= {lo+100}")
        cur.fetchall()
    t1 = time.time()
    el = t1 - t0
    print(f"    {N} 次, {el:.2f}s, {N/el:.0f} QPS")

    # 4. COUNT
    print("\n[4] 全表 COUNT")
    t0 = time.time()
    for _ in range(10):
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        cur.fetchone()
    t1 = time.time()
    el = t1 - t0
    print(f"    10 次, {el:.2f}s, {el/10*1000:.2f} ms/op")

    # 5. 清理
    cur.execute(f"DROP TABLE {table}")

    print("\n" + "=" * 60)
    conn.close()


if __name__ == '__main__':
    main()