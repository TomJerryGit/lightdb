"""
lightdb-tune - 参数调优建议
用法: lightdb-tune -h 127.0.0.1 -P 8888
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


# 参数推荐规则
TUNING_RULES = [
    {
        'param': 'innodb_buffer_pool_size',
        'condition': 'always',
        'suggest': lambda ctx: int(
            ctx.get('total_memory_gb', 8) * 0.7 * 1024 * 1024 * 1024),
        'reason': '建议设为物理内存的 70%',
    },
    {
        'param': 'max_connections',
        'condition': 'usage>0.8',
        'suggest': lambda ctx: max(
            int(ctx.get('current_connections', 100) * 1.5), 200),
        'reason': '连接使用率 > 80%，建议提高',
    },
    {
        'param': 'innodb_flush_log_at_trx_commit',
        'condition': 'low_durability',
        'suggest': lambda ctx: 2,
        'reason': '允许降级可用性时提升性能（0/1/2）',
    },
    {
        'param': 'query_cache_size',
        'condition': 'always',
        'suggest': lambda ctx: 0,
        'reason': '查询缓存已废弃，建议关闭',
    },
    {
        'param': 'innodb_log_file_size',
        'condition': 'write_heavy',
        'suggest': lambda ctx: 256 * 1024 * 1024,
        'reason': '写密集场景建议 256MB 以上',
    },
    {
        'param': 'innodb_io_capacity',
        'condition': 'ssd',
        'suggest': lambda ctx: 2000,
        'reason': 'SSD 环境建议 2000（HDD: 200）',
    },
    {
        'param': 'innodb_thread_concurrency',
        'condition': 'high_cores',
        'suggest': lambda ctx: ctx.get('cpu_count', 8) * 2,
        'reason': '建议为 CPU 核数的 2 倍',
    },
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-h', '--host', default='127.0.0.1', dest='host')
    ap.add_argument('-P', '--port', type=int, default=8888)
    ap.add_argument('-u', '--user', default='root')
    ap.add_argument('-p', '--password', default='')
    ap.add_argument('--format', default='text',
                    choices=['text', 'sql', 'json'])
    args = ap.parse_args()

    import pymysql

    # 连接
    try:
        conn = pymysql.connect(
            host=args.host, port=args.port,
            user=args.user, password=args.password,
            autocommit=True)
    except Exception as e:
        print(f"❌ 无法连接: {e}")
        return 1

    # 采集环境信息
    try:
        import psutil
        total_memory_gb = psutil.virtual_memory().total / 1024**3
        cpu_count = psutil.cpu_count() or 8
    except ImportError:
        total_memory_gb = 8
        cpu_count = 4

    cur = conn.cursor()
    cur.execute("SHOW STATUS")
    status = dict(cur.fetchall())

    def _int(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    current_connections = _int(
        status.get('Threads_connected', 0))
    max_connections = 151
    usage = current_connections / max_connections

    # 参数建议
    context = {
        'total_memory_gb': total_memory_gb,
        'cpu_count': cpu_count,
        'current_connections': current_connections,
        'connection_usage': usage,
        'ssd': True,  # 假设
        'write_heavy': True,
    }

    suggestions = []
    for rule in TUNING_RULES:
        # 简单条件
        cond = rule['condition']
        apply = False
        if cond == 'always':
            apply = True
        elif cond == 'usage>0.8' and usage > 0.8:
            apply = True
        elif cond == 'low_durability':
            apply = True  # 可选
        elif cond == 'write_heavy':
            apply = True
        elif cond == 'ssd':
            apply = True
        elif cond == 'high_cores' and cpu_count >= 8:
            apply = True

        if not apply:
            continue

        suggested = rule['suggest'](context)
        suggestions.append({
            'parameter': rule['param'],
            'suggested': suggested,
            'reason': rule['reason'],
        })

    # 输出
    if args.format == 'sql':
        print("-- LightDB 参数调优建议")
        for s in suggestions:
            print(f"SET GLOBAL {s['parameter']} = {s['suggested']};")
    elif args.format == 'json':
        import json
        print(json.dumps({
            'environment': context,
            'suggestions': suggestions,
        }, indent=2, ensure_ascii=False))
    else:
        print("=" * 60)
        print("  LightDB 参数调优建议")
        print("=" * 60)
        print(f"  内存: {total_memory_gb:.1f} GB")
        print(f"  CPU: {cpu_count} 核")
        print(f"  连接使用率: {usage*100:.1f}%")
        print()
        for s in suggestions:
            print(f"  ⚙️  {s['parameter']}")
            print(f"      建议值: {s['suggested']}")
            print(f"      原因: {s['reason']}")
            print()

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())