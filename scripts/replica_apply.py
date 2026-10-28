#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LightDB binlog 复制原型 CLI（task-71 生产化 B）

用法（仓库根目录）::

    # 一次性把主库 binlog 增量 apply 到目标数据目录（无事件则直接退出）
    python scripts/replica_apply.py --master-host 127.0.0.1 --master-port 8899 \
        --target-data-dir /tmp/replica-data

    # 持续跟随（Ctrl+C 停止）
    python scripts/replica_apply.py --master-host 127.0.0.1 --master-port 8899 \
        --target-data-dir /tmp/replica-data --follow --interval 0.2

    # 只查看主库 binlog 文件与位点（不 apply）
    python scripts/replica_apply.py --master-host 127.0.0.1 --master-port 8899 \
        --target-data-dir /tmp/replica-data --show-binary-logs

退出码：0 = 成功/追平；1 = 复制错误（显式）；2 = 参数/配置错误。

原型限制（详见 docs/PRODUCTION_READINESS.md）：
  * 单主，无自动 failover；主库不可达直接报错退出；
  * 单线程按顺序 apply，无并行/无冲突解决；
  * 目标库 schema 必须与主库一致（DDL 按原始 SQL 重放，DML 按行镜像）；
  * 位点持久化在目标库数据目录的 `replica_state.json`；崩溃后自动续传
    （已应用事件重放会按幂等处理）。
"""
import argparse
import json
import os
import sys
import threading

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='replica_apply',
        description='LightDB binlog 复制原型：从主库拉取增量并在目标库 apply',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument('--master-host', default='127.0.0.1',
                    help='主库地址（默认 127.0.0.1）')
    ap.add_argument('--master-port', type=int, default=8888,
                    help='主库端口（默认 8888）')
    ap.add_argument('--master-user', default='root', help='主库用户（默认 root）')
    ap.add_argument('--master-password', default=None,
                    help='主库口令（默认读 LIGHTDB_MASTER_PASSWORD，可为空）')
    ap.add_argument('--target-data-dir', required=True,
                    help='目标库数据目录（不存在则创建 Database）')
    ap.add_argument('--state-file', default=None,
                    help='位点状态文件（默认 <target-data-dir>/replica_state.json）')
    ap.add_argument('--limit', type=int, default=500,
                    help='每批拉取事件上限（默认 500）')
    ap.add_argument('--max-batches', type=int, default=10000,
                    help='一次性模式最多批数（默认 10000）')
    ap.add_argument('--follow', action='store_true',
                    help='持续跟随主库增量（Ctrl+C 停止）')
    ap.add_argument('--interval', type=float, default=0.2,
                    help='follow 模式空闲轮询间隔秒（默认 0.2）')
    ap.add_argument('--show-binary-logs', action='store_true',
                    help='只打印主库 SHOW BINARY LOGS / SHOW MASTER STATUS')
    ap.add_argument('--json', action='store_true',
                    help='输出 JSON（便于脚本消费）')
    return ap


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    password = args.master_password
    if password is None:
        password = os.environ.get('LIGHTDB_MASTER_PASSWORD', '')

    try:
        from lightdb.db.database import Database
    except Exception as e:                        # pragma: no cover - 环境问题
        print(f'[error] 无法导入 lightdb（请从仓库根目录运行）: {e}',
              file=sys.stderr)
        return 2

    from lightdb.net.replica_proto import ReplicaError, ReplicaPrototype

    target_db = None
    try:
        target_db = Database(os.path.abspath(args.target_data_dir))
        proto = ReplicaPrototype(
            args.master_host, args.master_port, target_db,
            user=args.master_user, password=password,
            state_path=(os.path.abspath(args.state_file)
                        if args.state_file else None))
    except Exception as e:
        print(f'[error] 初始化复制原型失败: {e}', file=sys.stderr)
        if target_db is not None:
            try:
                target_db.shutdown()
            except Exception:
                pass
        return 2

    try:
        if args.show_binary_logs:
            info = {
                'master_status': proto.master_status(),
                'binary_logs': proto.binary_logs(),
            }
            _print_json(info) if args.json else print(
                f"master status: {info['master_status']}\n"
                f"binary logs: {info['binary_logs']}")
            return 0

        if args.follow:
            print(f'[replica] 跟随主库 {args.master_host}:{args.master_port} '
                  f'-> {target_db.data_dir}（Ctrl+C 停止）', file=sys.stderr)
            stop = threading.Event()

            def _sigint(_s, _f):
                stop.set()
            try:
                import signal
                signal.signal(signal.SIGINT, _sigint)
            except Exception:
                pass
            try:
                stats = proto.follow(interval=args.interval,
                                     batch=args.limit, stop_event=stop)
            except KeyboardInterrupt:
                stats = {'stopped': True, 'reason': 'KeyboardInterrupt'}
            except ReplicaError as e:
                print(f'[replica] 跟随中止: {e}', file=sys.stderr)
                return 1
            _print_json({'follow': stats, 'state': proto.stats()}) \
                if args.json else print(f'follow stopped: {stats}')
            return 0

        stats = proto.run_until_caught_up(
            batch=args.limit, max_batches=args.max_batches)
        out = {'summary': stats, 'state': proto.stats()}
        if args.json:
            _print_json(out)
        else:
            print(f"批数={stats['batches']} 事件={stats['events']} "
                  f"applied={stats['applied']} "
                  f"idempotent={stats['idempotent']} "
                  f"耗时={stats['elapsed_ms']:.1f}ms "
                  f"速率={stats['events_per_sec']:.1f} events/s")
            print(f"目标位点: lsn={stats['state']['lsn']} "
                  f"seq={stats['state']['seq']} "
                  f"last_file={stats['state']['master_file']}")
        return 0
    except ReplicaError as e:
        print(f'[replica] 复制错误: {e}', file=sys.stderr)
        return 1
    finally:
        try:
            target_db.shutdown()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
