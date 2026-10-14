"""
lightdbdump - 逻辑备份工具（对标 mysqldump）

用法:
    python -m lightdb.tools.dump -h 127.0.0.1 -P 8888 -u root -o backup.sql
    python -m lightdb.tools.dump --host 127.0.0.1 -D default -t users -o users.sql
    python -m lightdb.tools.dump --no-data -o schema.sql

输出格式 = lightdb-logical-dump v1（规范与已知限制见
`lightdb/sql/logical_dump.py` 顶部注释）。渲染逻辑与 `BackupManager`
共用 `render_dump`，因此"命令行导出的文件"和"内部 API 导出的文件"
逐字节同构，可以互相恢复。

历史缺陷：`-h/--host` 与 argparse 内置的 `-h/--help` 冲突，工具一启动
就抛 `ArgumentError` 退出（`tests/test_batch9.py::test_dump_tool` 的
`assert result.returncode == 0` 就是踩这个）。现在 `add_help=False`，
`-h` 归 host，帮助改用 `--help`。
"""
import argparse
import sys
import time

from ..sql.logical_dump import (BackupError, TableSchema, normalize_type,
                                render_dump)


def build_parser() -> argparse.ArgumentParser:
    # add_help=False：把 -h 让给 --host（mysql 客户端习惯），--help 另加
    ap = argparse.ArgumentParser(description='LightDB Dump', add_help=False)
    ap.add_argument('--help', action='help', help='显示帮助并退出')
    ap.add_argument('-h', '--host', default='127.0.0.1', dest='host')
    ap.add_argument('-P', '--port', type=int, default=8888)
    ap.add_argument('-u', '--user', default='root')
    ap.add_argument('-p', '--password', default='')
    ap.add_argument('-D', '--database', default='default')
    ap.add_argument('-o', '--output', default=None, help='输出文件')
    ap.add_argument('-t', '--tables', nargs='*', help='仅导出指定表')
    ap.add_argument('--no-data', action='store_true', help='仅结构')
    ap.add_argument('--no-create', action='store_true', help='仅数据')
    ap.add_argument('--no-header', action='store_true',
                    help='不写 -- lightdb-logical-dump 头部注释')
    return ap


def read_schema(cur, tname: str) -> TableSchema:
    """用 `SHOW COLUMNS` 读表结构 → TableSchema

    `SHOW COLUMNS` 的 `Key` 列取值：`PRI` / `UNI` / `MUL` / ''。
    只把 PRI/UNI 表达成列属性（与 `BackupManager` 的导出形态一致）。
    """
    cur.execute(f"SHOW COLUMNS FROM {tname}")
    cols = []
    for c in cur.fetchall():
        cols.append({
            'name': c[0],
            'type': normalize_type(c[1]),
            'primary': c[3] == 'PRI',
            'unique': c[3] == 'UNI',
            'notnull': c[2] == 'NO',
        })
    schema = TableSchema(name=tname, columns=cols)
    schema.assert_supported()
    return schema


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        import pymysql
    except ImportError:
        print("请先安装: pip install pymysql", file=sys.stderr)
        return 1

    try:
        conn = pymysql.connect(
            host=args.host, port=args.port,
            user=args.user, password=args.password,
            database=args.database, autocommit=True,
            charset='utf8mb4',
        )
    except Exception as e:
        print(f"连接失败: {e}", file=sys.stderr)
        return 1

    cur = conn.cursor()
    try:
        cur.execute("SHOW TABLES")
        tables = sorted(r[0] for r in cur.fetchall())
        if args.tables:
            want = set(args.tables)
            missing = want - set(tables)
            if missing:
                print(f"表不存在: {sorted(missing)}", file=sys.stderr)
                return 1
            tables = [t for t in tables if t in want]

        schemas = [read_schema(cur, t) for t in tables]

        def rows_for(schema):
            cur.execute(f"SELECT * FROM {schema.name}")
            return [list(r) for r in cur.fetchall()]

        text = render_dump(
            schemas, rows_for, no_data=args.no_data,
            no_create=args.no_create, source='LightDB',
            include_header=not args.no_header)
    except BackupError as e:
        print(f"导出失败: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if args.output:
        # 先写临时文件再原子替换：半截的 dump 比没有 dump 更危险
        tmp = args.output + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        import os
        os.replace(tmp, args.output)
        print(f"备份完成: {args.output} "
              f"({os.path.getsize(args.output)} 字节, "
              f"{len(schemas)} 表)", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
