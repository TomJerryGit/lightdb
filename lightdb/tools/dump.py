"""
lightdbdump - 逻辑备份工具
对标 mysqldump
用法: python -m lightdb.tools.dump -h 127.0.0.1 -P 8888 -u root > backup.sql
"""
import argparse
import sys
import time


def main():
    ap = argparse.ArgumentParser(description='LightDB Dump')
    ap.add_argument('-h', '--host', default='127.0.0.1', dest='host')
    ap.add_argument('-P', '--port', type=int, default=8888)
    ap.add_argument('-u', '--user', default='root')
    ap.add_argument('-p', '--password', default='')
    ap.add_argument('-D', '--database', default='default')
    ap.add_argument('-o', '--output', default=None, help='输出文件')
    ap.add_argument('-t', '--tables', nargs='*', help='仅导出指定表')
    ap.add_argument('--no-data', action='store_true', help='仅结构')
    ap.add_argument('--no-create', action='store_true', help='仅数据')
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
            database=args.database, autocommit=True,
            charset='utf8mb4',
        )
    except Exception as e:
        print(f"连接失败: {e}", file=sys.stderr)
        sys.exit(1)

    out = open(args.output, 'w', encoding='utf-8') if args.output else sys.stdout

    def w(s=''):
        out.write(s + '\n')

    w("-- LightDB Dump")
    w(f"-- Host: {args.host}:{args.port}  Database: {args.database}")
    w(f"-- Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    w()
    w("SET NAMES utf8mb4;")
    w("SET FOREIGN_KEY_CHECKS=0;")
    w()

    cur = conn.cursor()
    cur.execute("SHOW TABLES")
    tables = [r[0] for r in cur.fetchall()]

    if args.tables:
        tables = [t for t in tables if t in args.tables]

    for tname in tables:
        w(f"-- ========== Table: {tname} ==========")
        w(f"DROP TABLE IF EXISTS {tname};")

        # 表结构
        if not args.no_create:
            cur.execute(f"SHOW COLUMNS FROM {tname}")
            cols = cur.fetchall()
            col_defs = []
            for c in cols:
                name = c[0]
                ctype = c[1]
                null = 'NOT NULL' if c[2] == 'NO' else 'NULL'
                key = c[3]
                extra = ''
                if key == 'PRI':
                    extra = ' PRIMARY KEY'
                elif key == 'UNI':
                    extra = ' UNIQUE'
                col_defs.append(f"  `{name}` {ctype} {null}{extra}")

            w(f"CREATE TABLE {tname} (")
            w(',\n'.join(col_defs))
            w(");")
            w()

        # 表数据
        if not args.no_data:
            cur.execute(f"SELECT * FROM {tname}")
            rows = cur.fetchall()
            for r in rows:
                vals = ', '.join(_sql_literal(v) for v in r)
                w(f"INSERT INTO {tname} VALUES ({vals});")
            w(f"-- {len(rows)} rows")
            w()

    w("SET FOREIGN_KEY_CHECKS=1;")
    w("-- Dump completed")

    if args.output:
        out.close()

    conn.close()


def _sql_literal(v):
    if v is None:
        return 'NULL'
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("'", "''").replace('\\', '\\\\')
    return f"'{s}'"


if __name__ == '__main__':
    main()