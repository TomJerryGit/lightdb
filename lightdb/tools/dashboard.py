"""
Grafana Dashboard 生成器
"""
import json
import os
import sys
from pathlib import Path


DASHBOARDS_DIR = Path(__file__).parent.parent.parent / 'dashboards'
DASHBOARDS_DIR.mkdir(exist_ok=True)


def gen_overview() -> dict:
    """总览面板"""
    return {
        "title": "LightDB - Overview",
        "uid": "lightdb-overview",
        "tags": ["lightdb"],
        "timezone": "browser",
        "schemaVersion": 38,
        "refresh": "10s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": [
            {
                "id": 1,
                "title": "QPS",
                "type": "graph",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 6},
                "targets": [{
                    "expr": "lightdb_queries_per_second",
                    "legendFormat": "QPS",
                }],
            },
            {
                "id": 2,
                "title": "当前连接数",
                "type": "graph",
                "gridPos": {"x": 12, "y": 0, "w": 12, "h": 6},
                "targets": [{
                    "expr": "lightdb_threads_connected",
                    "legendFormat": "connections",
                }],
            },
            {
                "id": 3,
                "title": "慢查询 / 秒",
                "type": "graph",
                "gridPos": {"x": 0, "y": 6, "w": 8, "h": 6},
                "targets": [{
                    "expr": "rate(lightdb_slow_queries_total[5m])",
                    "legendFormat": "slow/s",
                }],
            },
            {
                "id": 4,
                "title": "缓冲池命中率",
                "type": "gauge",
                "gridPos": {"x": 8, "y": 6, "w": 8, "h": 6},
                "targets": [{
                    "expr": "lightdb_buffer_pool_hit_rate",
                }],
            },
            {
                "id": 5,
                "title": "活跃事务",
                "type": "graph",
                "gridPos": {"x": 16, "y": 6, "w": 8, "h": 6},
                "targets": [{
                    "expr": "lightdb_transactions_active",
                }],
            },
            {
                "id": 6,
                "title": "锁等待",
                "type": "graph",
                "gridPos": {"x": 0, "y": 12, "w": 12, "h": 6},
                "targets": [{
                    "expr": "lightdb_lock_waits_total",
                }],
            },
            {
                "id": 7,
                "title": "死锁",
                "type": "stat",
                "gridPos": {"x": 12, "y": 12, "w": 6, "h": 6},
                "targets": [{
                    "expr": "increase(lightdb_deadlocks_total[1h])",
                }],
            },
            {
                "id": 8,
                "title": "运行时长",
                "type": "stat",
                "gridPos": {"x": 18, "y": 12, "w": 6, "h": 6},
                "targets": [{
                    "expr": "lightdb_uptime_seconds",
                }],
            },
        ],
    }


def gen_performance() -> dict:
    """性能面板"""
    return {
        "title": "LightDB - Performance",
        "uid": "lightdb-performance",
        "tags": ["lightdb", "performance"],
        "schemaVersion": 38,
        "refresh": "5s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": [
            {
                "id": 1,
                "title": "QPS by Command",
                "type": "graph",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                "targets": [
                    {"expr": "rate(lightdb_com_select[1m])",
                     "legendFormat": "SELECT"},
                    {"expr": "rate(lightdb_com_insert[1m])",
                     "legendFormat": "INSERT"},
                    {"expr": "rate(lightdb_com_update[1m])",
                     "legendFormat": "UPDATE"},
                    {"expr": "rate(lightdb_com_delete[1m])",
                     "legendFormat": "DELETE"},
                ],
            },
            {
                "id": 2,
                "title": "查询延迟 P95/P99",
                "type": "graph",
                "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8},
                "targets": [
                    {"expr": "histogram_quantile(0.95, "
                             "rate(lightdb_query_duration_seconds_bucket[5m]))",
                     "legendFormat": "P95"},
                    {"expr": "histogram_quantile(0.99, "
                             "rate(lightdb_query_duration_seconds_bucket[5m]))",
                     "legendFormat": "P99"},
                ],
            },
            {
                "id": 3,
                "title": "物理读 IOPS",
                "type": "graph",
                "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8},
                "targets": [
                    {"expr": "rate(lightdb_buffer_pool_reads_total[1m])",
                     "legendFormat": "reads/s"},
                ],
            },
            {
                "id": 4,
                "title": "磁盘写 IOPS",
                "type": "graph",
                "gridPos": {"x": 12, "y": 8, "w": 12, "h": 8},
                "targets": [
                    {"expr": "rate(lightdb_disk_write_iops[1m])",
                     "legendFormat": "write/s"},
                ],
            },
        ],
    }


def gen_replication() -> dict:
    """复制面板"""
    return {
        "title": "LightDB - Replication",
        "uid": "lightdb-replication",
        "tags": ["lightdb", "replication"],
        "schemaVersion": 38,
        "refresh": "5s",
        "panels": [
            {
                "id": 1,
                "title": "主从延迟",
                "type": "graph",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                "targets": [
                    {"expr": "lightdb_slave_lag_seconds",
                     "legendFormat": "lag"},
                ],
            },
            {
                "id": 2,
                "title": "从库状态",
                "type": "stat",
                "gridPos": {"x": 12, "y": 0, "w": 6, "h": 8},
                "targets": [
                    {"expr": "lightdb_slave_io_running",
                     "legendFormat": "IO"},
                    {"expr": "lightdb_slave_sql_running",
                     "legendFormat": "SQL"},
                ],
            },
            {
                "id": 3,
                "title": "Binlog 位置",
                "type": "graph",
                "gridPos": {"x": 18, "y": 0, "w": 6, "h": 8},
                "targets": [
                    {"expr": "lightdb_binlog_position",
                     "legendFormat": "position"},
                ],
            },
        ],
    }


def gen_innodb() -> dict:
    """InnoDB 面板"""
    return {
        "title": "LightDB - InnoDB",
        "uid": "lightdb-innodb",
        "tags": ["lightdb", "innodb"],
        "schemaVersion": 38,
        "refresh": "10s",
        "panels": [
            {
                "id": 1,
                "title": "Buffer Pool 使用",
                "type": "graph",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                "targets": [
                    {"expr": "lightdb_buffer_pool_pages_total",
                     "legendFormat": "total"},
                    {"expr": "lightdb_buffer_pool_pages_free",
                     "legendFormat": "free"},
                    {"expr": "lightdb_buffer_pool_pages_dirty",
                     "legendFormat": "dirty"},
                ],
            },
            {
                "id": 2,
                "title": "命中率",
                "type": "gauge",
                "gridPos": {"x": 12, "y": 0, "w": 6, "h": 8},
                "targets": [
                    {"expr": "lightdb_buffer_pool_hit_rate",
                     "legendFormat": "hit rate"},
                ],
            },
            {
                "id": 3,
                "title": "行锁等待",
                "type": "graph",
                "gridPos": {"x": 18, "y": 0, "w": 6, "h": 8},
                "targets": [
                    {"expr": "lightdb_row_locks_current",
                     "legendFormat": "locks"},
                ],
            },
        ],
    }


def generate_all():
    """生成所有 Dashboard"""
    dashboards = {
        'lightdb_overview.json': gen_overview(),
        'lightdb_performance.json': gen_performance(),
        'lightdb_replication.json': gen_replication(),
        'lightdb_innodb.json': gen_innodb(),
    }

    for name, data in dashboards.items():
        path = DASHBOARDS_DIR / name
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  ✓ {name}")

    print(f"\n✅ 已生成 {len(dashboards)} 个 Dashboard")
    print(f"📁 目录: {DASHBOARDS_DIR}")
    print()
    print("导入 Grafana:")
    print("  1. Grafana → Dashboards → Import")
    print("  2. 上传 JSON 文件")
    print("  3. 选择数据源（Prometheus）")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--action', default='generate',
                    choices=['generate', 'list'])
    args = ap.parse_args()

    if args.action == 'generate':
        generate_all()
    else:
        for f in DASHBOARDS_DIR.glob('*.json'):
            print(f"  {f.name}")


if __name__ == '__main__':
    main()