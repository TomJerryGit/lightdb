#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LightDB binlog 复制原型 CLI（task-71 生产化 B）

用法（仓库根目录）::

    # 一次性把主库 binlog 增量 apply 到目标数据目录（无事件则直接退出）
    python scripts/replica_apply.py --master-host 127.0.0.1 --master-port 8899 \
        --target-data-dir /tmp/replica-data

    # 持续跟随（Ctrl+C 停止；失败保留断点 + 指数退避重试，不跳过）
    python scripts/replica_apply.py --master-host 127.0.0.1 --master-port 8899 \
        --target-data-dir /tmp/replica-data --follow --interval 0.2 \
        [--retry-backoff 0.5] [--max-backoff 30] [--max-retries 0]

    # 排障/恢复：查看本地状态；位点缺口(1236)后人工确认跳过缺失区间
    python scripts/replica_apply.py ... --show-state --json
    python scripts/replica_apply.py ... --reset-position

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
    ap.add_argument('--target-data-dir', default=None,
                    help='目标库数据目录（不存在则创建 Database）')
    ap.add_argument('--state-file', default=None,
                    help='位点状态文件（默认 <target-data-dir>/replica_state[.<id>].json）')
    ap.add_argument('--replica-id', default='default',
                    help='多副本标识（默认 default；状态/心跳文件按副本区分）')
    ap.add_argument('--status-dir', default=None,
                    help='副本状态心跳目录（默认 <target-data-dir>）')
    ap.add_argument('--history', type=int, default=60,
                    help='状态采样历史条数上限（默认 60）')
    ap.add_argument('--list-replicas', action='store_true',
                    help='列出状态目录下的多副本（不启动复制），配合 --json 使用')
    ap.add_argument('--limit', type=int, default=500,
                    help='每批拉取事件上限（默认 500）')
    ap.add_argument('--max-batches', type=int, default=10000,
                    help='一次性模式最多批数（默认 10000）')
    ap.add_argument('--follow', action='store_true',
                    help='持续增量跟随主库（Ctrl+C 停止；失败保留断点并自动重连）')
    ap.add_argument('--protocol', action='store_true',
                    help='经 MySQL 协议 COM_BINLOG_DUMP 拉取事件（默认内部导出；'
                         '本引擎协议流为自定义 JSON，见 docs）')
    ap.add_argument('--interval', type=float, default=0.2,
                    help='follow 模式空闲轮询间隔秒（默认 0.2）')
    ap.add_argument('--retry-backoff', type=float, default=0.5,
                    help='follow 失败重试的初始退避秒（默认 0.5，指数增长）')
    ap.add_argument('--max-backoff', type=float, default=30.0,
                    help='follow 重试退避上限秒（默认 30）')
    ap.add_argument('--max-retries', type=int, default=0,
                    help='连续失败重试上限；0=无限重试（默认 0，不自动 failover）')
    ap.add_argument('--reconnect-backoff', type=float, default=None,
                    help='自动重连初始退避秒（覆盖 --retry-backoff）')
    ap.add_argument('--reconnect-max-backoff', type=float, default=None,
                    help='自动重连退避上限秒（覆盖 --max-backoff）')
    ap.add_argument('--reconnect-max-attempts', type=int, default=None,
                    help='自动重连最大连续尝试次数（覆盖 --max-retries；0=无限）')
    ap.add_argument('--force-after-promote', action='store_true',
                    help='目标目录已被 failover 演练提升为可写时仍继续复制'
                         '（默认显式拒绝，防止双写）')
    ap.add_argument('--reset-position', action='store_true',
                    help='把位点显式推进到主库当前可用末尾（会跳过缺失事件，'
                         '仅在 1236 位点缺口后人工确认时使用）')
    ap.add_argument('--show-state', action='store_true',
                    help='打印本地复制状态文件快照（不连主库、不 apply）')
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
    if args.json:
        # --json 的 stdout 必须是纯 JSON（便于脚本消费）：把引擎日志压到
        # ERROR（错误仍通过 stderr 的显式信息输出），避免 INFO/WARN 混入。
        try:
            from lightdb.logger import logger as _lg
            _lg.set_level('ERROR')
        except Exception:                         # pragma: no cover - 防御
            pass

    try:
        from lightdb.db.database import Database
    except Exception as e:                        # pragma: no cover - 环境问题
        print(f'[error] 无法导入 lightdb（请从仓库根目录运行）: {e}',
              file=sys.stderr)
        return 2

    from lightdb.net.replica_proto import (
        ReplicaBinlogGap, ReplicaError, ReplicaPrototype,
        read_replica_statuses)

    # ★ task-91：多副本列表（不启动复制；空结果显式 empty=true）
    if args.list_replicas:
        status_dir = os.path.abspath(
            args.status_dir or args.target_data_dir or '')
        if not status_dir:
            print('[error] --list-replicas 需要 --status-dir 或 '
                  '--target-data-dir', file=sys.stderr)
            return 2
        replicas = read_replica_statuses(status_dir,
                                         history_limit=args.history)
        payload = {'status_dir': status_dir,
                   'replicas': replicas, 'count': len(replicas),
                   'empty': not replicas}
        if args.json:
            _print_json(payload)
        elif not replicas:
            print(f'[replicas] 目录下没有副本状态（{status_dir}）')
        else:
            for r in replicas:
                print(f"[{r.get('replica_id')}] "
                      f"source={r.get('source')} "
                      f"caught_up={r.get('caught_up')} "
                      f"lag_lsn={r.get('lag_lsn')} "
                      f"lag_seconds={r.get('lag_seconds')} "
                      f"events={r.get('applied_events')} "
                      f"errors={r.get('apply_errors')} "
                      f"stale={r.get('stale')} "
                      f"updated={r.get('updated_age_seconds')}s")
        return 0

    if not args.target_data_dir:
        print('[error] 缺少 --target-data-dir', file=sys.stderr)
        return 2
    target_dir_abs = os.path.abspath(args.target_data_dir)
    promotion_marker = os.path.join(target_dir_abs, 'promotion.json')
    if os.path.exists(promotion_marker) and not args.force_after_promote:
        print('[error] 目标数据目录已被 failover 演练提升为可写主库'
              f' ({promotion_marker})：继续作为从库复制会造成双写，'
              '已显式拒绝；如确认请加 --force-after-promote',
              file=sys.stderr)
        return 2

    target_db = None
    proto = None
    try:
        target_db = Database(target_dir_abs)
        proto = ReplicaPrototype(
            args.master_host, args.master_port, target_db,
            user=args.master_user, password=password,
            state_path=(os.path.abspath(args.state_file)
                        if args.state_file else None),
            source=('protocol' if args.protocol else 'export'),
            replica_id=args.replica_id,
            status_dir=(os.path.abspath(args.status_dir)
                        if args.status_dir else None),
            history_size=args.history)
    except Exception as e:
        print(f'[error] 初始化复制原型失败: {e}', file=sys.stderr)
        if target_db is not None:
            try:
                target_db.shutdown()
            except Exception:
                pass
        return 2

    def _print_gap_error(e: 'ReplicaBinlogGap'):
        """MySQL 1236 风格显式错误 + 恢复策略（不自动跳过）"""
        print(f'ERROR 1236 (HY000): {e}', file=sys.stderr)
        print(f'[replica] 可用 binlog: {e.available_logs}', file=sys.stderr)
        print(f'[replica] 本地已应用 lsn={e.local_lsn}，'
              f'主库最早可读 lsn={e.earliest_lsn}', file=sys.stderr)
        print(f'[replica] 断点保留在 {proto.state_path}，'
              f'不会自动跳过缺失事件', file=sys.stderr)
        print(f'[replica] 恢复：重建目标库后从头复制，或人工确认后执行 '
              f'--reset-position 跳过缺失区间', file=sys.stderr)

    try:
        if args.show_state:
            state = proto.stats()
            _print_json(state) if args.json else print(
                json.dumps(state, ensure_ascii=False, indent=2, default=str))
            return 0

        if args.reset_position:
            reset = proto.reset_position()
            _print_json({'reset': reset, 'state': proto.stats()}) \
                if args.json else print(
                    f"位点已重置: lsn={reset['new_lsn']} "
                    f"seq={reset['new_seq']} file={reset['master_file']} "
                    f"(旧 lsn={reset['old_lsn']}；已跳过缺失区间，"
                    f"目标数据需自行校验)")
            return 0

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
            eff_backoff = (args.reconnect_backoff
                           if args.reconnect_backoff is not None
                           else args.retry_backoff)
            eff_max_backoff = (args.reconnect_max_backoff
                               if args.reconnect_max_backoff is not None
                               else args.max_backoff)
            eff_max_retries = (args.reconnect_max_attempts
                               if args.reconnect_max_attempts is not None
                               else args.max_retries)
            print(f'[replica] 副本 {proto.replica_id} 跟随主库 '
                  f'{args.master_host}:{args.master_port} '
                  f'-> {target_db.data_dir}（Ctrl+C 停止；'
                  f'失败保留断点并自动重连，退避 {eff_backoff}s→'
                  f'{eff_max_backoff}s，最大连续尝试 '
                  f'{eff_max_retries or "∞"}）', file=sys.stderr)
            stop = threading.Event()

            def _sigint(_s, _f):
                stop.set()
            try:
                import signal
                signal.signal(signal.SIGINT, _sigint)
            except Exception:
                pass
            try:
                stats = proto.follow(
                    interval=args.interval, batch=args.limit,
                    stop_event=stop,
                    retry_backoff=eff_backoff,
                    max_backoff=eff_max_backoff,
                    max_retries=eff_max_retries,
                    source=('protocol' if args.protocol else 'export'))
            except KeyboardInterrupt:
                stats = {'stopped': True, 'reason': 'KeyboardInterrupt'}
            except ReplicaBinlogGap as e:
                _print_gap_error(e)
                return 1
            except ReplicaError as e:
                print(f'[replica] 副本 {proto.replica_id} 跟随中止'
                      f'（断点保留，未跳过）: {e}', file=sys.stderr)
                return 1
            _print_json({'follow': stats, 'state': proto.stats()}) \
                if args.json else print(
                    f"follow stopped: replica={stats.get('replica_id')} "
                    f"applied={stats.get('applied')} "
                    f"events={stats.get('events')} "
                    f"reconnects={stats.get('reconnects')} "
                    f"disconnects={stats.get('disconnects')} "
                    f"recovery_ms_last={stats.get('recovery_ms_last')} "
                    f"lag_lsn={stats.get('lag_lsn')} "
                    f"caught_up={stats.get('caught_up')}")
            return 0

        stats = proto.run_until_caught_up(
            batch=args.limit, max_batches=args.max_batches,
            source=('protocol' if args.protocol else 'export'))
        out = {'summary': stats, 'state': proto.stats()}
        if args.json:
            _print_json(out)
        else:
            print(f"replica={proto.replica_id} source={proto.source} "
                  f"批数={stats['batches']} "
                  f"事件={stats['events']} "
                  f"applied={stats['applied']} "
                  f"idempotent={stats['idempotent']} "
                  f"耗时={stats['elapsed_ms']:.1f}ms "
                  f"速率={stats['events_per_sec']:.1f} events/s")
            print(f"目标位点: lsn={stats['state']['lsn']} "
                  f"seq={stats['state']['seq']} "
                  f"last_file={stats['state']['master_file']} "
                  f"protocol={stats['state'].get('protocol_file')}"
                  f"@{stats['state'].get('protocol_pos')} "
                  f"lag_lsn={stats.get('lag_lsn')} "
                  f"lag_seconds={stats.get('lag_seconds')} "
                  f"caught_up={stats.get('caught_up')} "
                  f"status={proto.status_path}")
        return 0
    except ReplicaBinlogGap as e:
        _print_gap_error(e)
        return 1
    except ReplicaError as e:
        print(f'[replica] 复制错误（断点保留，未跳过）: {e}', file=sys.stderr)
        return 1
    finally:
        if proto is not None:
            try:
                proto.close()
            except Exception:
                pass
        try:
            target_db.shutdown()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
