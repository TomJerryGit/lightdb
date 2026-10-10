"""
lightdb-check - 数据库健康检查
用法: lightdb-check -h 127.0.0.1 -P 8888 -u root
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def main():
    ap = argparse.ArgumentParser(
        prog='lightdb-check',
        description='LightDB 健康检查工具')
    ap.add_argument('-h', '--host', default='127.0.0.1', dest='host')
    ap.add_argument('-P', '--port', type=int, default=8888)
    ap.add_argument('-u', '--user', default='root')
    ap.add_argument('-p', '--password', default='')
    ap.add_argument('--format', default='text',
                    choices=['text', 'json'])
    args = ap.parse_args()

    from lightdb.monitor.health_checker import HealthChecker
    from lightdb.monitor.metrics_v3 import MetricsCollector

    def conn_provider():
        import pymysql
        return pymysql.connect(
            host=args.host, port=args.port,
            user=args.user, password=args.password,
            autocommit=True,
            connect_timeout=5)

    # 采集指标
    collector = MetricsCollector(conn_provider=conn_provider)
    try:
        collector.collect()
    except Exception as e:
        print(f"❌ 无法连接: {e}")
        return 1

    # 健康检查
    checker = HealthChecker(
        metrics_provider=collector.export_json,
        conn_provider=conn_provider)
    checks = checker.check_all()

    # 输出
    if args.format == 'json':
        import json
        print(json.dumps([{
            'name': c.name,
            'status': c.status,
            'value': c.value,
            'message': c.message,
        } for c in checks], indent=2, ensure_ascii=False))
    else:
        print(checker.format_report(checks))

    # 退出码
    fail_count = sum(1 for c in checks if c.status == 'fail')
    return 1 if fail_count > 0 else 0


if __name__ == '__main__':
    sys.exit(main())