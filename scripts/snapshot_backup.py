"""LightDB 二进制一致性快照备份 CLI（P2）。

用法::

    # 离线整库快照（默认检测 127.0.0.1:8888 是否有服务，检测到则拒绝）
    python scripts/snapshot_backup.py --data-dir data_demo --out backups/snap-20261027

    # 自定义服务端口 / 在线强制快照（有并发写入风险，警告）
    python scripts/snapshot_backup.py --data-dir data --out snap \
        --server-port 8899 --force

    # 单表快照 + 机器可读报告
    python scripts/snapshot_backup.py --data-dir data --out snap-users \
        --table users --json report.json

退出码：0 = 成功；1 = 失败；3 = 检测到服务在监听而拒绝（可 --force 覆盖）；
2 = 参数错误（argparse）。
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
    SnapshotError, SnapshotRefusedError, create_snapshot)


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
    ap.add_argument('--force', action='store_true',
                    help='服务在监听/输出目录非空时强制继续（记录 WARNING）')
    ap.add_argument('--server-port', action='append', type=int, default=None,
                    metavar='PORT', help='服务端口检测（可重复；默认 8888）')
    ap.add_argument('--no-server-check', action='store_true',
                    help='跳过服务端口检测（不建议；自定义端口场景）')
    ap.add_argument('--json', default='', help='把报告写到该 JSON 路径')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args(argv)
    _quiet_logging()
    try:
        report = create_snapshot(
            args.data_dir, args.out, table=args.table, force=args.force,
            server_ports=args.server_port,
            check_servers=not args.no_server_check, quiet=args.quiet)
    except SnapshotRefusedError as exc:
        print('[snapshot] 拒绝: %s' % exc, file=sys.stderr)
        return 3
    except SnapshotError as exc:
        print('[snapshot] 失败: %s' % exc, file=sys.stderr)
        return 1
    except Exception as exc:                       # noqa: BLE001
        print('[snapshot] 未预期错误: %s: %s' % (type(exc).__name__, exc),
              file=sys.stderr)
        return 1
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
