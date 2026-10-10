"""
lightdb-index-advisor - 索引推荐
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-h', '--host', default='127.0.0.1', dest='host')
    ap.add_argument('-P', '--port', type=int, default=8888)
    ap.add_argument('-u', '--user', default='root')
    ap.add_argument('-p', '--password', default='')
    ap.add_argument('--format', default='text',
                    choices=['text', 'sql', 'json'])
    ap.add_argument('--from-history', action='store_true',
                    help='从查询历史分析')
    args = ap.parse_args()

    import pymysql
    try:
        conn = pymysql.connect(
            host=args.host, port=args.port,
            user=args.user, password=args.password,
            autocommit=True)
    except Exception as e:
        print(f"❌ 无法连接: {e}")
        return 1

    from lightdb.monitor.index_advisor import IndexAdvisor

    advisor = IndexAdvisor(conn=conn)

    # 从查询历史分析
    if args.from_history:
        from lightdb.utils.history import QueryHistory
        h = QueryHistory()
        queries = [e['sql'] for e in h.recent(500)]
    else:
        queries = []
        # 尝试从慢查询日志获取
        from lightdb.logger import logger
        for line in logger.slow_log:
            import re
            m = re.search(r's:\s*(.+)$', line)
            if m:
                queries.append(m.group(1))

    suggestions = advisor.analyze_queries(queries)

    # 输出
    if args.format == 'sql':
        print("-- 索引建议")
        for s in suggestions:
            print(f"{s.create_sql};")
    elif args.format == 'json':
        import json
        print(json.dumps([{
            'table': s.table,
            'columns': s.columns,
            'sql': s.create_sql,
            'priority': s.priority,
            'reason': s.reason,
        } for s in suggestions], indent=2, ensure_ascii=False))
    else:
        print(advisor.generate_report(suggestions))

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())