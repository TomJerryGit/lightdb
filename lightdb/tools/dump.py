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
                                parse_create_table, render_dump)


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
    """读表结构 → TableSchema（含 CHECK 约束）

    结构主体来自 `SHOW COLUMNS`（`Key` 列取值 `PRI` / `UNI` / `MUL` / ''；
    只把 PRI/UNI 表达成列属性，与 `BackupManager` 的导出形态一致）。

    CHECK 约束**必须**另取：`SHOW COLUMNS` 的列集是
    Field/Type/Null/Key/Default/Extra，**没有**约束那一列，只靠它导出的
    dump 会静默丢掉全部 CHECK（"备份→恢复到新库，约束没了"）。这里再发
    一条 `SHOW CREATE TABLE`（服务端由
    `sql/logical_dump.py::check_ddl` 生成，与逻辑 dump 共用同一份 DDL
    实现），把 CHECK **合并**进来：

    * 服务端还没有 `SHOW CREATE TABLE`（老版本）→ 1064/1105：此时它不会
      返回任何"有 CHECK"的信息，无法判断是否漏了约束，因此**明确打一行
      警告**（stderr），不假装导出了约束；
    * `SHOW CREATE TABLE` 成功但 CHECK 条数与 `SHOW COLUMNS` 推不出约束
      信息之间不可能做交叉核对（列集里没有约束），所以这里只做"能读到
      就合并、读不到就告警"，并且合并后由 `render_dump` → 
      `TableSchema.canonical_ddl` 统一渲染 —— 不另写一份 DDL 生成逻辑。
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

    ddl = _fetch_create_table(cur, tname)
    if ddl:
        try:
            parsed = parse_create_table(ddl)
        except BackupError as e:
            # 服务端给的 DDL 解析不了 → 宁可失败也不导出"缺约束"的备份
            raise BackupError(
                f"表 {tname}: SHOW CREATE TABLE 的输出无法解析，拒绝导出"
                f"（继续导出会丢掉 CHECK 约束等结构）：{e}") from e
        if parsed.name != tname or parsed.col_names != schema.col_names:
            raise BackupError(
                f"表 {tname}: SHOW CREATE TABLE 与 SHOW COLUMNS 的列结构"
                f"不一致（{parsed.col_names} != {schema.col_names}），"
                f"拒绝导出（两份结构来源矛盾时无法保证备份完整）")
        schema.table_checks = [dict(ck) for ck in parsed.table_checks]
        by_name = {c['name']: c for c in parsed.columns}
        for c in schema.columns:
            src = by_name.get(c['name']) or {}
            if (src.get('check') or {}).get('expr'):
                c['check'] = dict(src['check'])

    schema.assert_supported()
    return schema


def _fetch_create_table(cur, tname: str):
    """尽力取一版真实 DDL；服务端不支持时返回 None（并告警，不静默）"""
    try:
        cur.execute(f"SHOW CREATE TABLE {tname}")
    except Exception as e:                          # noqa: BLE001
        if _looks_like_unsupported(e):
            print(f"警告: 服务端不支持 SHOW CREATE TABLE（{e}）—— "
                  f"本次导出的表 {tname} 不含 CHECK 约束，"
                  f"恢复后约束会缺失（需升级服务端或手动补 DDL）",
                  file=sys.stderr)
            return None
        raise
    row = cur.fetchone()
    if not row:
        return None
    # (Table, Create Table, ...) 或 (Create Table, ...)：取含 CREATE 的那一列
    for v in row:
        if isinstance(v, str) and v.lstrip().upper().startswith('CREATE'):
            return v
    return row[-1] if row else None


def _looks_like_unsupported(exc) -> bool:
    """1064 语法错误 / 1105 未知语句 —— "服务端不认识这条命令"的形态"""
    code = None
    args = getattr(exc, 'args', None)
    if args:
        code = args[0]
    if isinstance(code, int) and code in (1064, 1105):
        return True
    text = str(exc).lower()
    return 'syntax' in text or 'unsupported' in text or '不支持' in text


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
