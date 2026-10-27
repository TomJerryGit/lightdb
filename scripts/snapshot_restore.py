"""LightDB 二进制一致性快照恢复 CLI（P2）。

用法::

    # 恢复到全新数据目录（目标非空默认拒绝；--force 会把旧目录改名保留）
    python scripts/snapshot_restore.py --snapshot backups/snap-20261027 \
        --data-dir data_restored

    # 只看快照内容，不恢复
    python scripts/snapshot_restore.py --snapshot snap --list

    # 校验失败/版本不兼容会显式失败（不静默返回成功）
    python scripts/snapshot_restore.py --snapshot snap --data-dir target \
        --allow-version-mismatch

退出码：0 = 成功；1 = 失败（manifest/哈希/版本/自检）；3 = 检测到服务监听而
拒绝；2 = 参数错误。
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
    SnapshotError, SnapshotRefusedError, describe_snapshot, restore_snapshot)


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
        description='LightDB 二进制一致性快照恢复（校验 manifest/hash/版本 + 恢复后自检）')
    ap.add_argument('--snapshot', required=True, help='快照目录（含 manifest.json）')
    ap.add_argument('--data-dir', default='', help='恢复目标数据目录')
    ap.add_argument('--list', action='store_true',
                    help='只打印快照信息，不恢复')
    ap.add_argument('--force', action='store_true',
                    help='目标非空/服务监听时强制继续（旧目录会改名保留）')
    ap.add_argument('--server-port', action='append', type=int, default=None,
                    metavar='PORT', help='服务端口检测（可重复；默认 8888）')
    ap.add_argument('--no-server-check', action='store_true',
                    help='跳过服务端口检测（不建议）')
    ap.add_argument('--no-verify', action='store_true',
                    help='拷贝后不做逐表自检（默认必做；不建议）')
    ap.add_argument('--allow-version-mismatch', action='store_true',
                    help='允许跨 lightdb 版本恢复（物理格式可能不兼容，风险自负）')
    ap.add_argument('--json', default='', help='把报告写到该 JSON 路径')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args(argv)
    _quiet_logging()

    try:
        if args.list:
            info = describe_snapshot(args.snapshot)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        if not args.data_dir:
            print('缺少 --data-dir（或用 --list 只看快照信息）', file=sys.stderr)
            return 2
        report = restore_snapshot(
            args.snapshot, args.data_dir, force=args.force,
            server_ports=args.server_port,
            check_servers=not args.no_server_check,
            verify=not args.no_verify,
            allow_version_mismatch=args.allow_version_mismatch,
            quiet=args.quiet)
    except SnapshotRefusedError as exc:
        print('[restore] 拒绝: %s' % exc, file=sys.stderr)
        return 3
    except SnapshotError as exc:
        print('[restore] 失败: %s' % exc, file=sys.stderr)
        return 1
    except Exception as exc:                       # noqa: BLE001
        print('[restore] 未预期错误: %s: %s' % (type(exc).__name__, exc),
              file=sys.stderr)
        return 1
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        if not args.quiet:
            print('[restore] JSON 报告: %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
