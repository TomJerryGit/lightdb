"""
lightdb - 交互式命令行客户端
对标 mysql 客户端
用法: python -m lightdb.tools.cli [-h host] [-P port] [-u user] [-p password]
"""
import argparse
import getpass
import os
import sys
import readline  # 命令历史
import cmd


class LightDBClient(cmd.Cmd):
    intro = "Welcome to LightDB CLI. Type 'help' for commands, '\\q' to quit.\n"
    prompt = "lightdb> "

    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.cur = conn.cursor()
        self._multiline = []
        self.prompt = "lightdb> "

    # ============ 命令 ============
    def default(self, line):
        line = line.strip()
        if not line:
            return

        # 特殊元命令
        if line in ('\\q', '\\quit', 'exit', 'quit'):
            return self.do_quit('')
        if line == '\\h' or line == 'help':
            return self.do_help('')
        if line.startswith('\\d '):
            table = line[3:].strip()
            return self.do_desc(table)
        if line == '\\l':
            return self._exec("SHOW DATABASES")
        if line.startswith('\\i '):
            return self.do_source(line[3:].strip())

        # SQL 多行支持
        if line.endswith(';') or line.upper().startswith(('SELECT', 'INSERT', 'UPDATE', 'DELETE',
                                'CREATE', 'DROP', 'SHOW', 'USE', 'GRANT',
                                'REVOKE', 'BEGIN', 'COMMIT', 'EXPLAIN', 'ANALYZE')):
            self._exec(line.rstrip(';'))
        else:
            # 继续输入
            self._multiline.append(line)
            while not self._multiline[-1].rstrip().endswith(';'):
                try:
                    more = input("    -> ")
                except EOFError:
                    break
                self._multiline.append(more)
            full = ' '.join(self._multiline).rstrip(';')
            self._multiline = []
            self._exec(full)

    def do_quit(self, arg):
        """退出客户端"""
        print("Bye")
        try:
            self.conn.close()
        except Exception:
            pass
        return True

    def do_help(self, arg):
        """显示帮助"""
        print("""
LightDB CLI 命令:
  \\q              退出
  \\l              列出数据库
  \\d <table>      查看表结构
  \\i <file>       执行 SQL 文件
  help            显示此帮助

SQL 语句直接输入，以分号结尾。
        """)

    def do_desc(self, table):
        self._exec(f"SHOW COLUMNS FROM {table}")

    def do_source(self, filename):
        if not os.path.exists(filename):
            print(f"Error: {filename} not found")
            return
        with open(filename, 'r', encoding='utf-8') as f:
            sql = f.read()
        # 按分号切
        stmts = [s.strip() for s in sql.split(';') if s.strip()]
        for s in stmts:
            if s.startswith('--'):
                continue
            self._exec(s)

    # ============ 执行 SQL ============
    def _exec(self, sql):
        sql = sql.strip().rstrip(';')
        if not sql:
            return
        try:
            self.cur.execute(sql)
            if self.cur.description:
                cols = [d[0] for d in self.cur.description]
                rows = self.cur.fetchall()
                self._print_table(cols, rows)
                print(f"{len(rows)} row(s) in set")
            else:
                print(f"Query OK, {self.cur.rowcount} row(s) affected")
        except Exception as e:
            print(f"ERROR: {e}")

    def _print_table(self, cols, rows):
        if not rows:
            print("Empty set")
            return

        # 计算宽度
        widths = [len(str(c)) for c in cols]
        for r in rows[:1000]:
            for i, v in enumerate(r):
                s = str(v) if v is not None else 'NULL'
                if len(s) > widths[i]:
                    widths[i] = len(s)

        # 表头
        sep = '+' + '+'.join('-' * (w + 2) for w in widths) + '+'
        print(sep)
        print('| ' + ' | '.join(str(c).ljust(w) for c, w in zip(cols, widths)) + ' |')
        print(sep)

        for r in rows[:1000]:
            cells = [str(v) if v is not None else 'NULL' for v in r]
            print('| ' + ' | '.join(c.ljust(w) for c, w in zip(cells, widths)) + ' |')
        print(sep)

        if len(rows) > 1000:
            print(f"... ({len(rows) - 1000} more rows)")


def main():
    ap = argparse.ArgumentParser(description='LightDB CLI')
    ap.add_argument('-h', '--host', default='127.0.0.1', dest='host')
    ap.add_argument('-P', '--port', type=int, default=8888)
    ap.add_argument('-u', '--user', default='root')
    ap.add_argument('-p', '--password', default=None)
    ap.add_argument('-D', '--database', default='default')
    ap.add_argument('-e', '--execute', default=None,
                    help='执行一条SQL后退出')
    args = ap.parse_args()

    if args.password is None:
        args.password = ''  # 允许空密码

    try:
        import pymysql
    except ImportError:
        print("请先安装: pip install pymysql")
        sys.exit(1)

    try:
        conn = pymysql.connect(
            host=args.host, port=args.port,
            user=args.user, password=args.password,
            database=args.database,
            autocommit=True,
            charset='utf8mb4',
        )
    except Exception as e:
        print(f"连接失败: {e}")
        sys.exit(1)

    if args.execute:
        cur = conn.cursor()
        cur.execute(args.execute)
        if cur.description:
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                print(r)
        else:
            print(f"OK, {cur.rowcount} rows affected")
        conn.close()
        return

    cli = LightDBClient(conn)
    try:
        cli.cmdloop()
    except KeyboardInterrupt:
        print("\nBye")
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()