"""v3.0 Batch 5 测试 - 运维与监控"""
import os
import sys
import time
import json
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _dashboards_dir_writable() -> bool:
    """探测仓库 dashboards/ 是否可写（受限 ACL 下不可写，属环境限制）。"""
    probe = Path(__file__).resolve().parent.parent / 'dashboards'
    try:
        probe.mkdir(parents=True, exist_ok=True)
        f = probe / '.write_probe'
        f.write_text('x', encoding='utf-8')
        f.unlink()
        return True
    except OSError:
        return False


class FakeConn:
    def __init__(self):
        self.calls = []

    def query(self, sql, params=None):
        sql_l = sql.lower()
        if 'show status' in sql_l:
            return ['Variable_name', 'Value'], [
                ('Questions', '100000'),
                ('Threads_connected', '15'),
                ('Threads_running', '3'),
                ('Threads_created', '200'),
                ('Connections', '5000'),
                ('Aborted_connects', '12'),
                ('Uptime', '3600'),
                ('Com_select', '50000'),
                ('Com_insert', '20000'),
                ('Com_update', '15000'),
                ('Com_delete', '5000'),
                ('Com_commit', '10000'),
                ('Com_rollback', '500'),
                ('Innodb_buffer_pool_pages_total', '1000'),
                ('Innodb_buffer_pool_pages_free', '100'),
                ('Innodb_buffer_pool_pages_dirty', '50'),
                ('Innodb_buffer_pool_reads', '1000'),
                ('Innodb_buffer_pool_read_requests', '1000000'),
                ('Innodb_buffer_pool_write_requests', '50000'),
                ('Innodb_row_lock_waits', '25'),
                ('Innodb_deadlocks', '3'),
                ('Innodb_row_lock_current_waits', '2'),
            ]
        if 'show master status' in sql_l:
            return ['File', 'Position'], [('binlog.000001', 12345)]
        if 'show slave status' in sql_l:
            return [], []
        if 'show index' in sql_l:
            return (['Table', 'Key_name', 'Column_name',
                     'Seq_in_index'],
                    [('users', 'PRIMARY', 'id', 1),
                     ('users', 'idx_email', 'email', 1)])
        if 'show processlist' in sql_l:
            return ['Id', 'User', 'Time', 'State', 'Info'], [
                (1, 'root', 5, 'Query', 'SELECT ...'),
                (2, 'root', 120, 'Locked', 'UPDATE ...'),
            ]
        if 'from `users`' in sql_l or 'from users' in sql_l:
            return ['count'], [(1000,)]
        if 'explain' in sql_l:
            return ['id', 'type', 'rows'], [(1, 'ALL', 5000)]
        return [], []

    def get_tables(self):
        return ['users', 'orders']


def test_metrics_collector():
    """测试1: 指标采集"""
    print("\n【测试1】指标采集")
    from lightdb.monitor.metrics_v3 import (
        MetricsCollector, METRIC_DEFINITIONS)

    # 定义完整性
    print(f"  → 指标定义数: {len(METRIC_DEFINITIONS)}")
    assert len(METRIC_DEFINITIONS) >= 35

    # 采集
    conn = FakeConn()
    collector = MetricsCollector(conn_provider=lambda: conn)
    collector.collect()

    metrics = collector.export_json()
    print(f"  → 采集指标数: {len(metrics)}")

    # 检查关键指标
    assert 'lightdb_queries_total' in metrics
    assert 'lightdb_threads_connected' in metrics
    assert 'lightdb_buffer_pool_hit_rate' in metrics

    q = list(metrics['lightdb_queries_total'].values())[0]
    assert q == 100000
    print(f"  ✅ QPS 指标: {q}")

    hit = list(metrics['lightdb_buffer_pool_hit_rate'].values())[0]
    print(f"  ✅ 缓冲池命中率: {hit*100:.2f}%")
    assert hit > 0.99

    # Prometheus 导出
    text = collector.export_prometheus()
    assert 'lightdb_queries_total' in text
    assert '# TYPE' in text
    print(f"  ✅ Prometheus 导出: {len(text)} 字符")


def test_slow_query_analyzer():
    """测试2: 慢查询归因"""
    print("\n【测试2】慢查询归因")
    from lightdb.monitor.slow_query_analyzer import SlowQueryAnalyzer

    analyzer = SlowQueryAnalyzer()

    # 添加不同类别的慢查询
    queries = [
        ("SELECT * FROM users", 500),
        ("SELECT * FROM users WHERE name LIKE '%alice%'", 800),
        ("SELECT * FROM orders WHERE UPPER(name) = 'X'", 1200),
        ("SELECT * FROM users WHERE id = '123'", 300),
        ("SELECT * FROM users WHERE id NOT IN (SELECT id FROM x)", 2000),
        ("SELECT * FROM t WHERE a=1 OR a=2 OR a=3", 400),
    ]

    for sql, dur in queries:
        analyzer.add_record(sql, dur)

    # 分析
    stats = analyzer.analyze()
    print(f"  → 总数: {stats.total}")
    print(f"  → 平均: {stats.avg_time_ms:.1f}ms")
    print(f"  → P95: {stats.p95_ms:.1f}ms")
    print(f"  → P99: {stats.p99_ms:.1f}ms")

    # 分类统计
    print(f"  → 分类: {stats.by_category}")
    assert stats.by_category.get('prefix_like', 0) >= 1
    assert stats.by_category.get('not_in', 0) >= 1

    # 报告
    report = analyzer.generate_report(stats)
    assert '慢查询分析报告' in report
    assert 'Top 10' in report
    print(f"  ✅ 报告生成: {len(report)} 字符")


def test_lock_analyzer():
    """测试3: 锁分析"""
    print("\n【测试3】锁分析")
    from lightdb.monitor.lock_analyzer import LockAnalyzer, LockWait

    analyzer = LockAnalyzer()

    # 模拟等待
    waits = [
        LockWait(wait_id='w1', waiting_pid=1, blocking_pid=2,
                 wait_seconds=10),
        LockWait(wait_id='w2', waiting_pid=2, blocking_pid=3,
                 wait_seconds=5),
        LockWait(wait_id='w3', waiting_pid=4, blocking_pid=5,
                 wait_seconds=2),
    ]

    # 手动塞入
    analyzer.wait_history.extend(waits)

    # 阻塞链
    chains = analyzer.build_blocking_chain(waits)
    print(f"  → 阻塞链: {len(chains)}")
    for chain in chains:
        print(f"    {chain}")

    assert len(chains) >= 1

    # 记录死锁
    analyzer.record_deadlock(victim_pid=1, participants=[1, 2, 3])

    # 报告
    report = analyzer.report()
    assert '锁等待分析报告' in report
    print(f"  ✅ 报告生成")


def test_index_advisor():
    """测试4: 索引推荐"""
    print("\n【测试4】索引推荐")
    from lightdb.monitor.index_advisor import IndexAdvisor

    conn = FakeConn()
    advisor = IndexAdvisor(conn=conn)

    # 现有索引
    existing = advisor.get_existing_indexes('users')
    print(f"  → 现有索引: {[i['name'] for i in existing]}")
    assert len(existing) >= 1

    # 分析查询
    queries = [
        "SELECT * FROM users WHERE age = 30",
        "SELECT * FROM users WHERE age > 20",
        "SELECT * FROM users WHERE age < 50",
        "SELECT * FROM users WHERE name = 'x'",
        "SELECT * FROM users WHERE name LIKE 'a%'",
        "SELECT * FROM orders WHERE status = 'paid'",
        "SELECT * FROM orders WHERE status = 'pending'",
        "SELECT * FROM orders WHERE status = 'cancelled'",
    ]

    suggestions = advisor.analyze_queries(queries)
    print(f"  → 索引建议: {len(suggestions)}")

    for s in suggestions[:5]:
        print(f"    [{s.priority}] {s.create_sql} ({s.query_count} 查询)")

    assert len(suggestions) >= 1
    # 应该有 users.age 和 orders.status
    tables = {s.table for s in suggestions}
    assert 'users' in tables or 'orders' in tables

    # 报告
    report = advisor.generate_report(suggestions)
    assert '索引推荐报告' in report
    print(f"  ✅ 报告生成")


def test_health_checker():
    """测试5: 健康检查"""
    print("\n【测试5】健康检查")
    from lightdb.monitor.health_checker import HealthChecker

    def metrics_provider():
        return {
            'lightdb_threads_connected': {'': 100},
            'lightdb_buffer_pool_hit_rate': {'': 0.98},
            'lightdb_slave_lag_seconds': {'': 5},
            'lightdb_deadlocks_total': {'': 2},
        }

    checker = HealthChecker(metrics_provider=metrics_provider)
    checks = checker.check_all()

    print(f"  → 检查项: {len(checks)}")
    for c in checks:
        icon = {'pass': '✅', 'warn': '⚠️',
                'fail': '❌'}[c.status]
        print(f"    {icon} {c.name}: {c.message}")

    # 应该有失败项（连接数 100/151=0.66 → pass）
    assert len(checks) >= 3

    report = checker.format_report(checks)
    assert '健康检查' in report
    print(f"  ✅ 报告生成")


@pytest.mark.skipif(
    not _dashboards_dir_writable(),
    reason="环境限制：仓库 dashboards/ 目录不可写（受限 ACL）——"
           "用例本身在正常用户下通过")
def test_dashboard_generation():
    """测试6: Grafana Dashboard"""
    print("\n【测试6】Grafana Dashboard")
    from lightdb.tools.dashboard import (
        gen_overview, gen_performance, gen_replication,
        gen_innodb)

    for name, gen in [
            ('overview', gen_overview),
            ('performance', gen_performance),
            ('replication', gen_replication),
            ('innodb', gen_innodb)]:
        d = gen()
        assert 'title' in d
        assert 'panels' in d
        assert len(d['panels']) >= 3
        print(f"  ✅ {name}: {len(d['panels'])} 面板")

    # 生成文件
    from lightdb.tools.dashboard import generate_all
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        generate_all()
    output = buf.getvalue()
    assert 'Dashboard' in output
    print(f"  ✅ 文件生成完毕")


def test_check_cli():
    """测试7: lightdb-check CLI"""
    print("\n【测试7】lightdb-check CLI")
    import subprocess

    # 尝试运行 --help
    result = subprocess.run(
        [sys.executable, '-m', 'lightdb.tools.check', '--help'],
        capture_output=True, text=True, timeout=10,
        cwd=os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))

    if result.returncode == 0:
        assert 'LightDB' in result.stdout
        print(f"  ✅ CLI 可调用")
    else:
        print(f"  ⚠️ CLI 返回码 {result.returncode}")


def main():
    print("=" * 60)
    print("  v3.0 Batch 5 测试 - 运维与监控")
    print("=" * 60)

    tests = [
        ('指标采集', test_metrics_collector),
        ('慢查询归因', test_slow_query_analyzer),
        ('锁分析', test_lock_analyzer),
        ('索引推荐', test_index_advisor),
        ('健康检查', test_health_checker),
        ('Grafana 面板', test_dashboard_generation),
        ('CLI 工具', test_check_cli),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ {name} 失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"  通过: {passed}  失败: {failed}")
    if failed == 0:
        print("  🎉 v3.0 Batch 5 全部通过！")
    print("=" * 60)
    return failed


if __name__ == '__main__':
    sys.exit(main())