"""
lightdbadmin - 管理工具
对标 mysqladmin
用法: python -m lightdb.tools.admin -h 127.0.0.1 -P 8888 -u root status
"""
import argparse
import sys
import time


def main():
    # add_help=False：`-h` 让给 `--host`（mysql 客户端习惯），否则 argparse
    # 直接抛 `ArgumentError: -h/--host conflicting option string: -h`，
    # 工具一启动就退出（tests/test_batch9.py::test_admin_tool 踩的就是这个）
    ap = argparse.ArgumentParser(description='LightDB Admin', add_help=False)
    ap.add_argument('--help', action='help', help='显示帮助并退出')
    ap.add_argument('-h', '--host', default='127.0.0.1', dest='host')
    ap.add_argument('-P', '--port', type=int, default=8888)
    ap.add_argument('-u', '--user', default='root')
    ap.add_argument('-p', '--password', default='')
    ap.add_argument('cmd', choices=[
        'status', 'ping', 'processlist', 'version',
        'master', 'slave', 'variables', 'stats',
        'kill',
    ])
    ap.add_argument('args', nargs='*')
    args = ap.parse_args()

    try:
        import pymysql
    except ImportError:
        print("请先安装: pip install pymysql", file=sys.stderr)
        sys.exit(1)

    try:
        conn = pymysql.connect(
            host=args.host, port=args.port,
            user=args.user, password=args.password,
            autocommit=True,
        )
    except Exception as e:
        print(f"连接失败: {e}", file=sys.stderr)
        sys.exit(1)

    cur = conn.cursor()

    if args.cmd == 'ping':
        print("mysqld is alive")
    elif args.cmd == 'version':
        cur.execute("SELECT VERSION()")
        print("Server version:", cur.fetchone()[0])
    elif args.cmd == 'status':
        cur.execute("SHOW STATUS")
        data = dict(cur.fetchall())
        cur.execute("SHOW VARIABLES LIKE 'version%'")
        info = dict(cur.fetchall())
        print(f"Uptime: {data.get('Uptime', 0)}  "
              f"Threads: {data.get('Threads_connected', 0)}  "
              f"Questions: {data.get('Questions', 0)}  "
              f"Slow queries: 0  "
              f"Version: {info.get('version', '?')}")
    elif args.cmd == 'processlist':
        cur.execute("SHOW PROCESSLIST")
        cols = [d[0] for d in cur.description]
        print(' | '.join(cols))
        print('-' * 60)
        for r in cur.fetchall():
            print(' | '.join(str(v) for v in r))
    elif args.cmd == 'master':
        cur.execute("SHOW MASTER STATUS")
        cols = [d[0] for d in cur.description]
        print(' | '.join(cols))
        for r in cur.fetchall():
            print(' | '.join(str(v) for v in r))
    elif args.cmd == 'slave':
        cur.execute("SHOW SLAVE STATUS")
        cols = [d[0] for d in cur.description]
        print(' | '.join(cols))
        for r in cur.fetchall():
            print(' | '.join(str(v) for v in r))
    elif args.cmd == 'variables':
        cur.execute("SHOW VARIABLES")
        for k, v in cur.fetchall():
            print(f"{k:40s} = {v}")
    elif args.cmd == 'stats':
        cur.execute("SHOW STATS")
        cols = [d[0] for d in cur.description]
        print(' | '.join(cols))
        for r in cur.fetchall():
            print(' | '.join(str(v) for v in r))
    elif args.cmd == 'kill':
        if not args.args:
            print("用法: kill <connection_id>")
            sys.exit(1)
        cid = args.args[0]
        cur.execute(f"KILL {cid}")
        print(f"Connection {cid} killed")

    conn.close()


if __name__ == '__main__':
    main()