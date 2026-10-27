"""LightDB 二进制一致性快照备份 CLI（P2/P3）。

用法::

    # 离线整库快照（默认检测 127.0.0.1:8888 是否有服务，检测到则拒绝）
    python scripts/snapshot_backup.py --data-dir data_demo --out backups/snap-20261027

    # 在线热备（quiesce）：必须能定位服务进程（--server-pid，或 psutil 自动探测）
    python scripts/snapshot_backup.py --data-dir data --out snap-online \
        --online --server-port 8899 --server-pid 12345

    # 自定义端口 / 离线强制快照（--force 不冻结写入，仅覆盖端口检测，风险自负）
    python scripts/snapshot_backup.py --data-dir data --out snap --force

    # 单表快照 + 机器可读报告
    python scripts/snapshot_backup.py --data-dir data --out snap-users \
        --table users --json report.json

online quiesce 两种后端：
  * 跨进程（CLI 默认尝试）：暂停服务进程 → 拷贝 crash-consistent 镜像 →
    恢复进程 → 在快照副本上 WAL 恢复 + checkpoint（WAL=0）。
    需要 OS 进程暂停权限（受限沙箱会显式失败，不会假装成功）。
  * 进程内写锁：由 Python API `create_snapshot(online=True, live_db=...)`
    使用（按表写锁 + checkpoint + 拷贝 + 释放），CLI 无法拿到服务端
    Database 实例，故 CLI 只用跨进程后端。

退出码：0 = 成功；1 = 失败；2 = 参数错误；3 = 检测到服务在监听而拒绝
（离线模式下可 --force 覆盖）；5 = 在线 quiesce 无法建立/完成
（无权限、找不到服务进程、锁超时）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightdb.backup.snapshot import (          # noqa: E402
    OnlineQuiesceError, SnapshotError, SnapshotRefusedError, create_snapshot)


def _quiet_logging():
    os.environ.setdefault('LIGHTDB_AUDIT_LEVEL', 'NONE')
    os.environ.setdefault('LIGHTDB_LOG_LEVEL', 'ERROR')
    try:
        from lightdb.logger import logger
        logger.set_level('ERROR')
    except Exception:                              # pragma: no cover
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='LightDB 二进制一致性快照备份（checkpoint + 文件级拷贝 + manifest）')
    ap.add_argument('--data-dir', required=True, help='要备份的数据目录')
    ap.add_argument('--out', required=True, help='快照输出目录（非空需 --force）')
    ap.add_argument('--table', default=None,
                    help='只快照指定表（单表快照，可恢复到全新数据目录）')
    ap.add_argument('--online', action='store_true',
                    help='在线热备（quiesce）：阻断写入完成拷贝，避免 --force 的'
                         '不一致窗口；需要 --server-pid 或可自动探测服务进程')
    ap.add_argument('--server-pid', type=int, default=None,
                    help='服务进程 PID（--online 跨进程 quiesce 用；不传则用 '
                         'psutil 按监听端口自动探测）')
    ap.add_argument('--quiesce-wait', type=float, default=10.0,
                    help='等待全部表写锁的超时秒数（进程内 quiesce；默认 10）')
    ap.add_argument('--force', action='store_true',
                    help='离线模式下服务在监听/输出目录非空时强制继续（记录 WARNING）')
    ap.add_argument('--server-port', action='append', type=int, default=None,
                    metavar='PORT', help='服务端口检测（可重复；默认 8888）')
    ap.add_argument('--no-server-check', action='store_true',
                    help='跳过服务端口检测（不建议；自定义端口场景）')
    ap.add_argument('--json', default='', help='把报告写到该 JSON 路径')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args(argv)
    _quiet_logging()
    if args.online and args.no_server_check:
        print('--online 与 --no-server-check 互斥（在线热备必须能定位服务）',
              file=sys.stderr)
        return 2
    try:
        report = create_snapshot(
            args.data_dir, args.out, table=args.table, force=args.force,
            server_ports=args.server_port,
            check_servers=not args.no_server_check, quiet=args.quiet,
            online=args.online, server_pid=args.server_pid,
            quiesce_wait=args.quiesce_wait)
    except SnapshotRefusedError as exc:
        print('[snapshot] 拒绝: %s' % exc, file=sys.stderr)
        return 3
    except OnlineQuiesceError as exc:
        print('[snapshot] 在线 quiesce 失败: %s' % exc, file=sys.stderr)
        return 5
    except SnapshotError as exc:
        print('[snapshot] 失败: %s' % exc, file=sys.stderr)
        return 1
    except Exception as exc:                       # noqa: BLE001
        print('[snapshot] 未预期错误: %s: %s' % (type(exc).__name__, exc),
              file=sys.stderr)
        return 1
    online = report.get('online') or {}
    if online.get('enabled') and not args.quiet:
        print('[snapshot] quiesce: backend=%s 阻塞窗口=%.3fs 总耗时=%.2fs'
              % (online.get('backend'), online.get('blocked_window_s') or 0.0,
                 report.get('duration_s') or 0.0))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        if not args.quiet:
            print('[snapshot] JSON 报告: %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
