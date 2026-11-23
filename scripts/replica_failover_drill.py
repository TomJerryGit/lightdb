#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LightDB failover 演练（**人工触发，不自动选主/切换**，task-91）

流程：确认旧主库不可达 → 校验目标副本已追平（lag/events/心跳新鲜度）→
（可选 ``--promote``）把副本数据目录标记提升为可写主库，打开可写 Database，
校验 `SHOW MASTER STATUS` 与新的 binlog 文件 → 输出演练报告。

用法::

    # 只演练报告（默认；不修改任何数据目录）
    python scripts/replica_failover_drill.py \
        --target-data-dir /tmp/replica --master-host 127.0.0.1 \
        --master-port 8899 --replica-id r1 --json

    # 演练 + 人工确认提升（生成 promotion.json，校验可写/new binlog）
    python scripts/replica_failover_drill.py \
        --target-data-dir /tmp/replica --master-host 127.0.0.1 \
        --master-port 8899 --promote --json

退出码：0=演练成功（含 promote）；2=前置条件不满足（主库仍可达/未追平/
无副本状态），显式拒绝；1=运行时错误。

**不做什么**：不自动发现/选举新主、不切换其他副本、不修改 DNS/配置；
提升后的目标目录写 `promotion.json`，`replica_apply.py` 会拒绝继续作为从库
复制（防双写，需显式 `--force-after-promote`）。
"""
import argparse
import json
import os
import socket
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='replica_failover_drill',
        description='LightDB failover 演练（人工触发；不自动选主/切换）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument('--target-data-dir', required=True,
                    help='副本数据目录（待校验/提升的目标）')
    ap.add_argument('--master-host', default='127.0.0.1',
                    help='旧主库地址（用于确认"不可达"，默认 127.0.0.1）')
    ap.add_argument('--master-port', type=int, default=8888,
                    help='旧主库端口（默认 8888）')
    ap.add_argument('--replica-id', default=None,
                    help='副本标识（默认：目录下唯一副本；多个副本时必须指定）')
    ap.add_argument('--status-dir', default=None,
                    help='副本状态心跳目录（默认 --target-data-dir）')
    ap.add_argument('--state-file', default=None,
                    help='权威状态文件（默认按 replica-id 约定；仅报告用）')
    ap.add_argument('--max-lag-seconds', type=float, default=0.0,
                    help='允许的最大延迟秒（默认 0=必须完全追平）')
    ap.add_argument('--max-events-behind', type=int, default=0,
                    help='允许的最大未应用事件数（默认 0）')
    ap.add_argument('--max-lag-lsn', type=int, default=0,
                    help='允许的最大落后 lsn 字节数（默认 0）')
    ap.add_argument('--freshness', type=float, default=60.0,
                    help='状态心跳新鲜度上限秒（默认 60；超过视为不追平）')
    ap.add_argument('--connect-timeout', type=float, default=2.0,
                    help='主库不可达探测超时秒（默认 2）')
    ap.add_argument('--promote', action='store_true',
                    help='前置检查通过后执行提升（写 promotion.json 并校验可写）')
    ap.add_argument('--json', action='store_true',
                    help='输出 JSON 报告（脚本消费）')
    return ap


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _tcp_reachable(host: str, port: int, timeout: float) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(max(0.1, float(timeout)))
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        try:
            s.close()
        except OSError:                          # pragma: no cover
            pass


def _select_replica(statuses: list, replica_id: str):
    """按 --replica-id 选择副本状态；未指定时要求目录里只有一个副本"""
    if replica_id:
        for item in statuses:
            if str(item.get('replica_id')) == str(replica_id):
                return item, None
        return None, (f'状态目录里没有副本 {replica_id!r}'
                      f'（现有: '
                      f'{[s.get("replica_id") for s in statuses]}）')
    if len(statuses) == 1:
        return statuses[0], None
    if not statuses:
        return None, '状态目录里没有任何副本（无 replica_status.*.json）'
    return None, (f'目录里有多个副本 '
                  f'{[s.get("replica_id") for s in statuses]}，'
                  f'请用 --replica-id 指定')


def _check_caught_up(status: dict, args) -> list:
    """追平校验（每项 {name, ok, detail}）；任何一项失败即拒绝提升"""
    checks = []
    has_status = bool(status.get('has_status'))
    checks.append({'name': 'has_heartbeat', 'ok': has_status,
                   'detail': ('有状态心跳' if has_status else
                              '只有权威状态文件、无心跳（无法确认延迟/错误）')})
    if not has_status:
        return checks
    stale = bool(status.get('stale'))
    age = status.get('updated_age_seconds')
    checks.append({'name': 'heartbeat_fresh', 'ok': not stale,
                   'detail': f'心跳 age={age}s（上限 {args.freshness}s）'})
    caught_up = status.get('caught_up')
    checks.append({'name': 'caught_up', 'ok': caught_up is True,
                   'detail': f'caught_up={caught_up}'})
    lag_seconds = status.get('lag_seconds')
    ok_lag = (lag_seconds is not None
              and float(lag_seconds) <= float(args.max_lag_seconds))
    checks.append({'name': 'lag_seconds_threshold', 'ok': bool(ok_lag),
                   'detail': f'lag_seconds={lag_seconds}'
                             f' <= {args.max_lag_seconds}?'})
    events_behind = status.get('events_behind')
    ok_events = (events_behind is not None
                 and int(events_behind) <= int(args.max_events_behind))
    checks.append({'name': 'events_behind_threshold', 'ok': bool(ok_events),
                   'detail': f'events_behind={events_behind}'
                             f' <= {args.max_events_behind}?'})
    lag_lsn = status.get('lag_lsn')
    ok_lsn = (lag_lsn is not None
              and int(lag_lsn) <= int(args.max_lag_lsn))
    checks.append({'name': 'lag_lsn_threshold', 'ok': bool(ok_lsn),
                   'detail': f'lag_lsn={lag_lsn}'
                             f' <= {args.max_lag_lsn}?'})
    last_error = str(status.get('last_error') or '')
    # 有历史错误不是阻塞条件（重连后可能仍有旧错误文本），但报告里显式列出
    checks.append({'name': 'last_error_reported', 'ok': True,
                   'detail': last_error[:200] if last_error else '(无)'})
    return checks


def _verify_target_writable(target_dir: str) -> dict:
    """打开副本目录为可写 Database，checkpoint 并读取表数（不写用户数据）"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    t0 = time.perf_counter()
    db = Database(target_dir)
    try:
        tables = sorted(db.tables.keys())
        checked = int(db.checkpoint(force=True))
        ex = Executor(db)
        rows = ex.execute_sql('SHOW MASTER STATUS', {})
        status_row = None
        if isinstance(rows, dict):
            cols = rows.get('columns') or []
            data = rows.get('rows') or []
            if data:
                status_row = dict(zip(cols, data[0]))
        return {'ok': True,
                'tables': tables,
                'checkpoint_tables': checked,
                'show_master_status': status_row,
                'elapsed_s': round(time.perf_counter() - t0, 3)}
    finally:
        try:
            db.shutdown()
        except Exception:                        # pragma: no cover
            pass


def _promote(target_dir: str, status: dict, args) -> dict:
    """人工提升：写 promotion.json（原子）+ 打开可写 DB/开新 binlog 并校验"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    t0 = time.perf_counter()
    # 1) 先确保副本目录可写且一致（checkpoint 会把 WAL 落盘）
    db = Database(target_dir)
    try:
        tables = sorted(db.tables.keys())
        checked = int(db.checkpoint(force=True))
        # 2) 开启/确认 binlog（新主库的 binlog 从这里开始）
        writer = db.binlog_log
        new_binlog = bool(writer is not None)
        binlog_info = None
        if new_binlog:
            binlog_info = {'file': writer.current_file,
                           'position': int(writer.current_pos),
                           'files': list(writer.file_names())}
        ex = Executor(db)
        sms = ex.execute_sql('SHOW MASTER STATUS', {})
        new_status = None
        if isinstance(sms, dict) and sms.get('type') == 'resultset':
            cols = sms.get('columns') or []
            rows = sms.get('rows') or []
            if rows:
                new_status = dict(zip(cols, rows[0]))
        if new_binlog and binlog_info:
            new_binlog = bool(binlog_info.get('files'))
        promotion = {
            'schema': 'lightdb-promotion/1',
            'mode': 'manual-drill',
            'automatic_failover': False,
            'promoted_at': round(time.time(), 3),
            'target_data_dir': os.path.abspath(target_dir),
            'replica_id': status.get('replica_id'),
            'previous_master': {
                'host': args.master_host, 'port': int(args.master_port)},
            'previous_master_reachable': False,
            'old_applied_lsn': status.get('applied_lsn'),
            'old_applied_events': status.get('applied_events'),
            'checkpoint_tables': checked,
            'tables': tables,
            'new_binlog': new_binlog,
            'binlog': binlog_info,
            'show_master_status': new_status,
            'note': ('人工触发 failover 演练提升；不自动选举/切换其他副本。'
                     'promotion.json 存在时 replica_apply 默认拒绝继续从库复制'
                     '（防止双写），需显式 --force-after-promote。'),
        }
        marker = os.path.join(os.path.abspath(target_dir), 'promotion.json')
        tmp = marker + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(promotion, f, ensure_ascii=False, indent=2,
                      default=str)
        os.replace(tmp, marker)
        promotion['marker_path'] = marker
        promotion['verify_elapsed_s'] = round(
            time.perf_counter() - t0, 3)
        if not new_binlog:
            raise RuntimeError(
                '提升后未检测到可用的新 binlog（LIGHTDB_BINLOG_ENABLED=0?）')
        if new_status is None:
            raise RuntimeError('提升后 SHOW MASTER STATUS 无结果（binlog 不可用）')
        return promotion
    finally:
        try:
            db.shutdown()
        except Exception:                        # pragma: no cover
            pass


def run_drill(args) -> dict:
    from lightdb.net.replica_proto import (
        default_replica_state_path, read_replica_statuses)

    started = time.perf_counter()
    target_dir = os.path.abspath(args.target_data_dir)
    status_dir = os.path.abspath(args.status_dir or target_dir)
    state_file = (os.path.abspath(args.state_file)
                  if args.state_file else None)
    report = {
        'schema': 'lightdb-failover-drill/1',
        'mode': 'manual-drill',
        'automatic_failover': False,
        'generated_at': round(time.time(), 3),
        'target_data_dir': target_dir,
        'status_dir': status_dir,
        'master': {'host': args.master_host,
                   'port': int(args.master_port)},
        'requested_replica_id': args.replica_id,
        'promote_requested': bool(args.promote),
        'result': 'error',
        'checks': [],
        'timings': {},
    }
    t0 = time.perf_counter()
    if not os.path.isdir(target_dir):
        report['reason'] = f'目标数据目录不存在: {target_dir}'
        return report
    # 1) 选择副本状态
    statuses = read_replica_statuses(status_dir)
    status, err = _select_replica(statuses, args.replica_id)
    report['timings']['read_status_s'] = round(
        time.perf_counter() - t0, 3)
    if err:
        report['result'] = 'refused'
        report['reason'] = err
        return report
    report['replica'] = {
        'replica_id': status.get('replica_id'),
        'has_status': status.get('has_status'),
        'stale': status.get('stale'),
        'updated_age_seconds': status.get('updated_age_seconds'),
        'applied_lsn': status.get('applied_lsn'),
        'applied_events': status.get('applied_events'),
        'lag_seconds': status.get('lag_seconds'),
        'events_behind': status.get('events_behind'),
        'caught_up': status.get('caught_up'),
        'last_error': status.get('last_error'),
        'state_path': state_file or status.get('state_path')
        or default_replica_state_path(target_dir,
                                      status.get('replica_id') or 'default'),
    }
    # 2) 旧主库必须不可达（演练前提；可达则显式拒绝）
    reachable = _tcp_reachable(args.master_host, args.master_port,
                               args.connect_timeout)
    report['master']['reachable'] = reachable
    report['checks'].append({
        'name': 'master_unreachable', 'ok': not reachable,
        'detail': (f'{args.master_host}:{args.master_port} '
                   f'{"仍可达" if reachable else "不可达"}')})
    if reachable:
        report['result'] = 'refused'
        report['reason'] = ('旧主库仍可达：failover 演练要求先确认真实故障/'
                            '主库不可达，拒绝在可达主库上提升副本（防双写）')
        return report
    # 3) 追平校验
    checks = _check_caught_up(status, args)
    report['checks'].extend(checks)
    failed = [c for c in checks if not c['ok']]
    if failed:
        report['result'] = 'refused'
        report['reason'] = ('副本未满足追平条件: '
                            + '; '.join(c['name'] for c in failed))
        return report
    report['caught_up'] = True
    # 4) 可写性/checkpoint 校验（只读检查，不改用户数据）
    t0 = time.perf_counter()
    try:
        verify = _verify_target_writable(target_dir)
        report['verify'] = verify
        report['checks'].append({'name': 'target_writable', 'ok': True,
                                 'detail': f'checkpoint {verify["checkpoint_tables"]} 表，'
                                           f'{len(verify["tables"])} 张表可读'})
    except Exception as exc:                     # noqa: BLE001
        report['result'] = 'error'
        report['reason'] = f'目标副本可写性校验失败: {exc}'
        report['checks'].append({'name': 'target_writable', 'ok': False,
                                 'detail': str(exc)[:300]})
        return report
    report['timings']['verify_s'] = round(time.perf_counter() - t0, 3)
    if not args.promote:
        report['result'] = 'success'
        report['promoted'] = False
        report['reason'] = ('演练通过（未提升）：主库不可达、副本已追平、'
                            '目标目录可写；如需人工提升请加 --promote')
        report['timings']['total_s'] = round(
            time.perf_counter() - started, 3)
        return report
    # 5) 人工提升
    try:
        promotion = _promote(target_dir, status, args)
        report['promoted'] = True
        report['promotion'] = promotion
        report['checks'].append({'name': 'promoted_new_binlog', 'ok': True,
                                 'detail': f"new binlog={promotion.get('binlog')}"})
        report['result'] = 'success'
        report['reason'] = ('人工提升完成：目标副本已标记为可写主库；'
                            '不自动切换其他副本/选主')
    except Exception as exc:                     # noqa: BLE001
        report['result'] = 'error'
        report['reason'] = f'提升失败: {exc}'
        report['checks'].append({'name': 'promote', 'ok': False,
                                 'detail': str(exc)[:300]})
    report['timings']['total_s'] = round(
        time.perf_counter() - started, 3)
    return report


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.json:
        try:
            from lightdb.logger import logger as _lg
            _lg.set_level('ERROR')
        except Exception:                        # pragma: no cover - 防御
            pass
    try:
        report = run_drill(args)
    except Exception as exc:                     # noqa: BLE001
        print(f'[drill] 运行时错误: {exc}', file=sys.stderr)
        return 1
    if args.json:
        _print_json(report)
    else:
        print(f"failover 演练: {report.get('result')} "
              f"(mode={report.get('mode')}, "
              f"automatic_failover={report.get('automatic_failover')})")
        print(f"  目标: {report.get('target_data_dir')}")
        print(f"  旧主库: {report.get('master')}")
        print(f"  副本: {report.get('replica')}")
        for c in report.get('checks') or []:
            print(f"  [{'OK' if c.get('ok') else 'FAIL'}] "
                  f"{c.get('name')}: {c.get('detail')}")
        if report.get('promotion'):
            print(f"  提升: {report['promotion'].get('marker_path')} "
                  f"binlog={report['promotion'].get('binlog')}")
        print(f"  结论: {report.get('reason')}")
    if report.get('result') == 'success':
        return 0
    if report.get('result') == 'refused':
        return 2
    return 1


if __name__ == '__main__':
    sys.exit(main())
