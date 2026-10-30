"""增量备份链（init/incr/restore/prune/bench）。

方案（**行级差分 + binlog DDL 捕获 + 一致性快照基线**）
====================================================

* `init`：对数据目录做一致性全量基线：
  - 拒绝服务运行中（TCP 探测默认端口）；
  - 打开 Database 并 `checkpoint(force=True)`，记录**基线状态指纹**
    （逐表行数/主键首末值/全表行摘要 SHA256/SHOW CREATE/自增值/WAL=0）；
  - 复用 `lightdb.backup.snapshot.create_snapshot()` 生成全量快照（基线）；
  - 记录 binlog 当前全局位置作为链起点。
* `incr`（行级差分，不依赖 binlog 回放 DML）：
  - 再次要求数据目录静默（无服务/无写入），checkpoint 后扫描当前全表行，
    与链内缓存的上次状态**逐行比较**：
      + 主键新增 → `inserts`（含完整行值，含 AUTO_INCREMENT 实际分配值）；
      + 主键消失 → `deletes`；
      + 主键相同但行摘要变化 → `updates`（完整新行值，恢复时 delete+insert）；
  - 同时从 binlog 捕获**区间内的 DDL SQL**（CREATE/ALTER/DROP…）用于恢复
    schema 变更；DDL 缺失/被截断 → 显式失败（不猜测）。
  - 每次增量记录：`binlog_start/end`、DDL、行差分、本段结束状态指纹、
    sha256/大小；更新 manifest 与行级状态缓存。
  - TRUNCATE/RENAME 等无法安全差分的 DDL **显式拒绝**（1105 语义的错误）。
* `restore`：先完整校验 manifest/链结构/每个段的 sha256/大小/版本（任何
  断链/篡改/缺段 → 失败且**不触碰目标**）→ 把基线快照恢复到临时 staging →
  按顺序应用增量（DDL→行差分）→ 设置 AUTO_INCREMENT → checkpoint（WAL=0）→
  与目标段状态指纹逐项比较（行数/主键首末值/全表行摘要/SHOW CREATE/自增）→
  校验 WAL=0 → 全部通过后才把 staging 安装到目标目录。
  `--to N` 恢复到第 N 个增量（0=仅基线），`--latest` 恢复全部。
* `prune --keep N`（或 `incr --keep N`）：保留基线 + 最新 N 个增量；当需丢弃
  旧增量时，先恢复前缀到 staging、把该前缀压成**新的全量基线**（复用
  snapshot），再保留最新 N 个增量，最后删除被淘汰文件。manifest 链结构始终
  连续。
* `bench`：10k 行基线/增量大小与耗时、恢复链耗时量化。

一致性边界
----------
快照/增量捕获时要求 **数据目录静默**（无服务监听、无并发写），先在
`checkpoint(force=True)`（WAL=0）后读取一致状态；增量是"两个静默时刻之间的
行级差分 + 区间内 DDL"，不包含未提交事务/未落盘写入。恢复只在成功后才替换
目标目录；失败时 staging 被清理、目标保持原样。

用法::

    python scripts/backup_chain.py init --data-dir D:/db --chain-dir D:/chain
    python scripts/backup_chain.py incr --data-dir D:/db --chain-dir D:/chain [--keep 3]
    python scripts/backup_chain.py restore --chain-dir D:/chain --target D:/restore \
        [--to 2 | --latest] [--force]
    python scripts/backup_chain.py prune --chain-dir D:/chain --keep 2
    python scripts/backup_chain.py bench --rows 10000 --changes 1000 --json out.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightdb.db.database import Database                    # noqa: E402
from lightdb.sql.executor import Executor                   # noqa: E402
from lightdb.sql.parser import parser                       # noqa: E402
from lightdb.backup import snapshot as snap                 # noqa: E402
from lightdb.replication.append_only_binlog import (        # noqa: E402
    BinlogReader, binlog_enabled)
from lightdb import __version__ as LIGHTDB_VERSION          # noqa: E402

CHAIN_SCHEMA = 'lightdb-backup-chain/1'
CHAIN_FORMAT = 1
MANIFEST_NAME = 'chain_manifest.json'
STATE_NAME = 'chain_state.json.gz'
FULL_PREFIX = 'full_'
INCR_PREFIX = 'incr_'
_ROW_HASH_LEN = 16


class ChainError(Exception):
    """备份链错误基类（显式失败；调用方以非 0 退出码报告）。"""


class ChainVersionError(ChainError):
    """链 manifest/数据格式/引擎版本不兼容。"""


class ChainIntegrityError(ChainError):
    """断链/缺段/sha256/大小/状态指纹不符。"""


class ChainUnsupported(ChainError):
    """无法安全增量/恢复的变更（如 TRUNCATE/RENAME、无 binlog DDL）。"""


# ==================== 基础工具 ====================

def _quiet() -> None:
    os.environ.setdefault('LIGHTDB_AUDIT_LEVEL', 'NONE')
    os.environ.setdefault('LIGHTDB_LOG_LEVEL', 'ERROR')
    try:
        from lightdb.logger import logger
        logger.set_level('ERROR')
    except Exception:                                # pragma: no cover
        pass


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _jsonable(value):
    import datetime
    import decimal
    if isinstance(value, bytes):
        return {'__bytes_hex__': value.hex()}
    if isinstance(value, bytearray):
        return {'__bytes_hex__': bytes(value).hex()}
    if isinstance(value, memoryview):
        return {'__bytes_hex__': value.tobytes().hex()}
    if isinstance(value, decimal.Decimal):
        return {'__decimal__': str(value)}
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return {'__iso__': value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _unjsonable(value):
    if isinstance(value, list):
        return [_unjsonable(v) for v in value]
    if isinstance(value, dict):
        if set(value) == {'__bytes_hex__'}:
            return bytes.fromhex(value['__bytes_hex__'])
        if set(value) == {'__decimal__'}:
            import decimal
            return decimal.Decimal(str(value['__decimal__']))
        if set(value) == {'__iso__'}:
            import datetime
            text = str(value['__iso__'])
            try:
                return datetime.datetime.fromisoformat(text)
            except ValueError:
                return text
        return {k: _unjsonable(v) for k, v in value.items()}
    return value


def _norm_schema(text: str) -> str:
    """归一化 SHOW CREATE：去尾空白，并把 AUTO_INCREMENT 的具体值归一化。

    AUTO_INCREMENT 的当前值随插入变化但不属于 schema；它由独立字段
    `auto_increment_next` 校验（恢复后比较精确值），因此这里把
    `AUTO_INCREMENT=123` 归一成 `AUTO_INCREMENT=<n>`，避免把正常自增推进
    误判成 DDL/schema 变化。
    """
    out = []
    for line in str(text or '').splitlines():
        line = re.sub(r'AUTO_INCREMENT\s*=\s*\d+', 'AUTO_INCREMENT=<n>',
                      line.rstrip())
        out.append(line)
    return '\n'.join(out).strip()


def _pk_key(value) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    os.replace(tmp, path)


def _read_json(path: Path):
    if not path.is_file():
        raise ChainIntegrityError(f'缺少文件: {path}')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:                         # noqa: BLE001
        raise ChainIntegrityError(f'文件不是合法 JSON: {path}: {exc}') from exc


def _write_gzip_json(path: Path, payload) -> None:
    import gzip
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with gzip.open(tmp, 'wt', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read_gzip_json(path: Path):
    import gzip
    if not path.is_file():
        raise ChainIntegrityError(f'缺少状态缓存: {path}')
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            return json.load(f)
    except Exception as exc:                         # noqa: BLE001
        raise ChainIntegrityError(f'状态缓存损坏: {path}: {exc}') from exc


def _ensure_offline(data_dir, action: str) -> None:
    running = snap.find_running_servers()
    if running:
        detail = ', '.join('%s:%s' % (r['host'], r['port']) for r in running)
        raise ChainError(f'检测到 LightDB 服务运行中（{detail}），拒绝{action}；'
                         '增量/基线必须基于静默数据目录（先停服）')


def _binlog_pos(data_dir) -> int:
    reader = BinlogReader(str(data_dir))
    try:
        impl = reader._open()                        # BinlogReader 只读封装
        return int(impl.current_global_pos)
    finally:
        try:
            reader.close()
        except Exception:                            # pragma: no cover
            pass


def _earliest_binlog_pos(data_dir) -> int:
    reader = BinlogReader(str(data_dir))
    try:
        impl = reader._open()
        files = list(impl._files or [])
        return int(files[0]['base']) if files else 0
    finally:
        try:
            reader.close()
        except Exception:                            # pragma: no cover
            pass


def _collect_ddl(data_dir, start_pos: int):
    """读取 binlog [start_pos, now) 中**已提交**的 DDL SQL；返回 (ddl, end, gap)。"""
    reader = BinlogReader(str(data_dir))
    try:
        impl = reader._open()
        earliest = int((impl._files or [{}])[0].get('base', 0))
        gap = start_pos < earliest
        current = int(impl.current_global_pos)
        records = impl.read_global_from(max(0, start_pos), include_control=True)
    finally:
        try:
            reader.close()
        except Exception:                            # pragma: no cover
            pass
    ddl, group = [], []
    for rec in records:
        t = rec.type_name
        if t == 'BEGIN':
            group = []
        elif t in ('INSERT', 'UPDATE', 'DELETE'):
            group.append(rec)
        elif t == 'DDL':
            group.append(rec)
        elif t == 'COMMIT':
            for r in group:
                if r.type_name == 'DDL':
                    data = r.data or {}
                    sql = str(data.get('sql') or '').strip()
                    if sql:
                        ddl.append(sql)
            group = []
        elif t == 'ROLLBACK':
            group = []
    return ddl, current, gap


def _show_create(ex, table: str) -> str:
    res = ex.execute_sql('SHOW CREATE TABLE %s' % table, {})
    if not isinstance(res, dict) or res.get('type') == 'error':
        raise ChainError(f'SHOW CREATE TABLE {table} 失败: {res}')
    rows = res.get('rows') or []
    if not rows or len(rows[0]) < 2:
        raise ChainError(f'SHOW CREATE TABLE {table} 返回异常: {res}')
    return str(rows[0][1])


def _capture_state(db, include_rows: bool = True):
    """读取数据库完整状态指纹；include_rows 时同时返回逐行缓存（用于差分层）。"""
    ex = Executor(db)
    state, cache_tables = {}, {}
    for table in sorted(db.tables):
        tbl = db.tables[table]
        cols = [c['name'] for c in getattr(tbl, 'cols', []) or []]
        pk = str(getattr(tbl, 'pk_col', ''))
        if not pk:
            raise ChainUnsupported(f'表 {table!r} 无主键，增量链不支持')
        pk_idx = cols.index(pk)
        res = ex.execute_sql('SELECT * FROM %s' % table, {})
        if not isinstance(res, dict) or res.get('type') == 'error':
            raise ChainError(f'读取表 {table} 行失败: {res}')
        rows = [list(r) for r in (res.get('rows') or [])]
        rows.sort(key=lambda r: _pk_key(r[pk_idx]))
        row_map, digest_pairs = {}, []
        for row in rows:
            rowj = _jsonable(row)
            h = _sha256_bytes(json.dumps(rowj, ensure_ascii=False,
                                         sort_keys=True).encode('utf-8'))[
                :_ROW_HASH_LEN]
            key = _pk_key(row[pk_idx])
            row_map[key] = {'hash': h, 'row': rowj}
            digest_pairs.append([key, h])
        digest = _sha256_bytes(json.dumps(digest_pairs, ensure_ascii=False,
                                          sort_keys=False).encode('utf-8'))
        auto_inc = {}
        try:
            auto_inc = {str(k): int(v) for k, v in
                        (tbl.auto_increment_next_values() or {}).items()}
        except Exception:                            # noqa: BLE001
            pass
        first = row_map[_pk_key(rows[0][pk_idx])]['row'] if rows else None
        last = row_map[_pk_key(rows[-1][pk_idx])]['row'] if rows else None
        state[table] = {
            'columns': cols, 'primary_key': pk,
            'row_count': len(rows), 'row_digest': digest,
            'first_row': first, 'last_row': last,
            'show_create': _norm_schema(_show_create(ex, table)),
            'auto_increment_next': auto_inc,
        }
        cache_tables[table] = {
            'columns': cols, 'primary_key': pk, 'pk_index': pk_idx,
            'rows': row_map,
        }
    cache = {'tables': cache_tables, 'schemas': {t: state[t]['show_create']
                                                 for t in state}}
    return state, cache


def _state_compare(expected: dict, actual: dict) -> list:
    problems = []
    tables = sorted(set(expected) | set(actual))
    for t in tables:
        e = expected.get(t)
        a = actual.get(t)
        if e is None or a is None:
            problems.append(f'表集合不一致: {t}（期望存在={e is not None}, '
                            f'实际存在={a is not None}）')
            continue
        for field in ('row_count', 'row_digest', 'first_row', 'last_row',
                      'show_create', 'auto_increment_next', 'columns',
                      'primary_key'):
            if e.get(field) != a.get(field):
                problems.append(f'表 {t} 字段 {field} 不一致')
    return problems


# ==================== 链 manifest ====================

def _manifest_path(chain_dir) -> Path:
    return Path(chain_dir) / MANIFEST_NAME


def _load_manifest(chain_dir, require_version: bool = True) -> dict:
    chain_dir = Path(chain_dir)
    manifest = _read_json(_manifest_path(chain_dir))
    if not isinstance(manifest, dict):
        raise ChainIntegrityError('manifest 根节点不是对象')
    if manifest.get('schema') != CHAIN_SCHEMA:
        raise ChainVersionError(f"不支持的链 schema: {manifest.get('schema')!r}")
    if int(manifest.get('format_version') or 0) != CHAIN_FORMAT:
        raise ChainVersionError(
            f"不支持的链 format_version: {manifest.get('format_version')!r}")
    if require_version:
        lv = str(manifest.get('lightdb_version') or '')
        if lv != LIGHTDB_VERSION:
            raise ChainVersionError(
                f'链版本不兼容: 链 lightdb_version={lv!r}，'
                f'当前引擎={LIGHTDB_VERSION!r}；拒绝恢复（不做猜测迁移）')
    entries = manifest.get('entries') or []
    if not entries or entries[0].get('kind') != 'full':
        raise ChainIntegrityError('链 manifest 必须以 full 基线开头')
    return manifest


def _save_manifest(chain_dir, manifest: dict) -> None:
    manifest['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    _write_json_atomic(_manifest_path(chain_dir), manifest)


def _full_dir(chain_dir, entry) -> Path:
    return Path(chain_dir) / str(entry.get('dir'))


def _incr_path(chain_dir, entry) -> Path:
    return Path(chain_dir) / str(entry.get('file'))


def _validate_chain_files(chain_dir, manifest: dict) -> None:
    """恢复前完整校验链结构/文件哈希/连续性/版本（不触碰目标）。"""
    entries = manifest['entries']
    prev_end = None
    for i, entry in enumerate(entries):
        if int(entry.get('id') if entry.get('id') is not None else -1) != i:
            raise ChainIntegrityError(f'链段 id 不连续: 期望 {i}, 实际 '
                                      f'{entry.get("id")!r}')
        if i == 0:
            if entry.get('kind') != 'full':
                raise ChainIntegrityError('首段必须是 full')
            full = _full_dir(chain_dir, entry)
            man_path = full / snap.MANIFEST_NAME
            if not man_path.is_file():
                raise ChainIntegrityError(f'基线快照缺少 manifest: {man_path}')
            want = str(entry.get('manifest_sha256') or '')
            if want and _sha256_file(man_path) != want:
                raise ChainIntegrityError(f'基线 manifest sha256 不符: {man_path}')
            prev_end = int(entry.get('binlog_end') or 0)
            continue
        path = _incr_path(chain_dir, entry)
        if not path.is_file():
            raise ChainIntegrityError(f'缺段（增量文件不存在）: {path}')
        if int(path.stat().st_size) != int(entry.get('size') or -1):
            raise ChainIntegrityError(f'增量大小不符: {path}')
        if _sha256_file(path) != str(entry.get('sha256') or ''):
            raise ChainIntegrityError(f'增量 sha256 不符（疑似篡改）: {path}')
        start = int(entry.get('binlog_start') or 0)
        end = int(entry.get('binlog_end') or 0)
        if prev_end is not None and start != prev_end:
            raise ChainIntegrityError(
                f'断链: 段 {i} binlog_start={start} 与上一段 end={prev_end} 不连续')
        if end < start:
            raise ChainIntegrityError(f'段 {i} binlog 范围非法: {start}..{end}')
        prev_end = end
    # 基线快照文件本身也做一次轻量存在性检查（SHA 由 restore_snapshot 校验）
    full = _full_dir(chain_dir, entries[0])
    try:
        snap.read_manifest(full)
    except Exception as exc:                         # noqa: BLE001
        raise ChainIntegrityError(f'基线快照 manifest 无法读取: {exc}') from exc


def _load_incr(chain_dir, entry) -> dict:
    data = _read_json(_incr_path(chain_dir, entry))
    if data.get('schema') != 'lightdb-backup-increment/1':
        raise ChainVersionError(f"增量 schema 不支持: {data.get('schema')!r}")
    if int(data.get('id', -1)) != int(entry.get('id', -2)):
        raise ChainIntegrityError('增量文件 id 与 manifest 不一致')
    return data


def _rebuild_state_cache(chain_dir, head_entry) -> dict:
    """从链本身重建行级状态缓存（prune/rebase 后用）。

    不依赖缓存文件，而是把"基线 + head 之前的全部增量"恢复到临时目录，
    再从该目录读出逐行缓存；同时校验恢复状态与 head_entry['state'] 一致。
    """
    manifest = _load_manifest(chain_dir)
    head_id = int(head_entry.get('id') or 0)
    selected = [e for e in manifest['entries'] if int(e.get('id') or 0) <= head_id]
    staging = Path(tempfile.mkdtemp(prefix='lightdb_chain_state_'))
    try:
        _restore_entries_to_dir(chain_dir, selected, staging)
        db = Database(str(staging))
        try:
            state, cache = _capture_state(db, include_rows=True)
        finally:
            db.shutdown()
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    expected = head_entry.get('state') or {}
    problems = _state_compare(expected, state)
    if problems:
        raise ChainIntegrityError(
            '重建状态缓存时恢复状态与链记录不一致: ' + '; '.join(problems[:10]))
    _write_gzip_json(chain_dir / STATE_NAME, cache)
    return cache


def _load_prev_cache(chain_dir, head_entry) -> dict:
    try:
        return _read_gzip_json(chain_dir / STATE_NAME)
    except ChainIntegrityError:
        return _rebuild_state_cache(chain_dir, head_entry)


# ==================== init ====================

def chain_init(data_dir, chain_dir, keep: int = None) -> dict:
    _quiet()
    data_dir, chain_dir = Path(data_dir), Path(chain_dir)
    if not (data_dir / '_meta.json').is_file():
        raise ChainError(f'数据目录无效（缺少 _meta.json）: {data_dir}')
    _ensure_offline(data_dir, '创建全量基线')
    if not binlog_enabled():
        raise ChainUnsupported('binlog 未启用（LIGHTDB_BINLOG_ENABLED=0），'
                               '无法建立增量链所需的 DDL 捕获')
    if _manifest_path(chain_dir).exists():
        raise ChainError(f'链目录已存在 manifest，拒绝覆盖: {chain_dir}；'
                         '请换新目录或先归档')
    chain_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    db = Database(str(data_dir))
    try:
        db.checkpoint(force=True)
        state, cache = _capture_state(db, include_rows=True)
    finally:
        db.shutdown()
    pos = _binlog_pos(data_dir)
    if not binlog_enabled():
        raise ChainUnsupported('binlog 未启用，无法记录链起点')
    full_name = FULL_PREFIX + '0000'
    full_dir = chain_dir / full_name
    if full_dir.exists():
        raise ChainError(f'基线目录已存在: {full_dir}')
    snap_info = snap.create_snapshot(data_dir, full_dir, force=True)
    if not snap_info.get('wal_zero'):
        raise ChainError('基线快照 WAL 非零，拒绝作为链基线')
    if _binlog_pos(data_dir) != pos:
        raise ChainError('快照期间 binlog 位置发生变化（有写入？），拒绝基线')
    manifest = {
        'schema': CHAIN_SCHEMA, 'format_version': CHAIN_FORMAT,
        'lightdb_version': LIGHTDB_VERSION,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source_data_dir': str(data_dir.resolve()),
        'entries': [{
            'id': 0, 'kind': 'full', 'dir': full_name,
            'binlog_start': pos, 'binlog_end': pos,
            'manifest_sha256': _sha256_file(full_dir / snap.MANIFEST_NAME),
            'snapshot_files': snap_info.get('total_files'),
            'snapshot_bytes': snap_info.get('total_bytes'),
            'snapshot_row_total': snap_info.get('row_total'),
            'state': state, 'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }],
        'keep': keep,
    }
    _save_manifest(chain_dir, manifest)
    _write_gzip_json(chain_dir / STATE_NAME, cache)
    report = {
        'status': 'ok', 'action': 'init', 'chain_dir': str(chain_dir),
        'source_data_dir': str(data_dir), 'binlog_pos': pos,
        'baseline_dir': str(full_dir), 'tables': len(state),
        'row_total': sum(s['row_count'] for s in state.values()),
        'baseline_bytes': snap_info.get('total_bytes'),
        'duration_s': round(time.perf_counter() - t0, 3),
        'manifest': str(_manifest_path(chain_dir)),
    }
    print('[chain] init 完成: %s（%d 表 / %d 行 / %.1f KB / %.2fs，binlog=%d）'
          % (chain_dir, report['tables'], report['row_total'],
             (snap_info.get('total_bytes') or 0) / 1024.0,
             report['duration_s'], pos))
    return report


# ==================== incr ====================

_ALLOWED_DDL = re.compile(
    r'^\s*(CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE|CREATE\s+(UNIQUE\s+)?'
    r'INDEX|DROP\s+INDEX|CREATE\s+VIEW|DROP\s+VIEW)\b', re.IGNORECASE)
_FORBIDDEN_DDL = re.compile(
    r'^\s*(TRUNCATE\b|RENAME\b|DROP\s+DATABASE\b|CREATE\s+DATABASE\b)',
    re.IGNORECASE)


def _check_ddl(ddl) -> None:
    for sql in ddl:
        if _FORBIDDEN_DDL.match(sql):
            raise ChainUnsupported(
                f'不支持的 DDL（增量链无法安全回放）: {sql[:120]}')
        if not _ALLOWED_DDL.match(sql):
            raise ChainUnsupported(
                f'不支持的 DDL（不在允许清单内）: {sql[:120]}')


def _schema_changed(prev_state: dict, cur_state: dict) -> bool:
    if set(prev_state) != set(cur_state):
        return True
    for t in prev_state:
        if (_norm_schema(prev_state[t].get('show_create', '')) !=
                _norm_schema(cur_state[t].get('show_create', '')) or
                prev_state[t].get('columns') != cur_state[t].get('columns')):
            return True
    return False


def _diff_rows(prev_cache: dict, cur_cache: dict) -> dict:
    """逐表 diff：inserts（完整行）/ updates（完整新行）/ deletes（主键值）。"""
    out = {}
    prev_tables = prev_cache.get('tables') or {}
    cur_tables = cur_cache.get('tables') or {}
    for t in sorted(set(prev_tables) | set(cur_tables)):
        p = prev_tables.get(t)
        c = cur_tables.get(t)
        ops = {'inserts': [], 'updates': [], 'deletes': []}
        if p is None:
            ops['inserts'] = [v['row'] for v in c['rows'].values()] \
                if c else []
        elif c is None:
            continue                                  # 整表删除由 DDL 处理
        else:
            pk_index = c['pk_index']
            p_rows, c_rows = p['rows'], c['rows']
            for key in p_rows.keys() - c_rows.keys():
                row = p_rows[key]['row']
                ops['deletes'].append(row[pk_index])
            for key in c_rows.keys() - p_rows.keys():
                ops['inserts'].append(c_rows[key]['row'])
            for key in c_rows.keys() & p_rows.keys():
                if c_rows[key]['hash'] != p_rows[key]['hash']:
                    ops['updates'].append(c_rows[key]['row'])
        if ops['inserts'] or ops['updates'] or ops['deletes']:
            out[t] = ops
    return out


def chain_incr(data_dir, chain_dir, keep: int = None) -> dict:
    _quiet()
    data_dir, chain_dir = Path(data_dir), Path(chain_dir)
    manifest = _load_manifest(chain_dir)
    _ensure_offline(data_dir, '生成增量')
    if not binlog_enabled():
        raise ChainUnsupported('binlog 未启用，无法捕获 DDL（拒绝静默生成'
                               '可能缺 schema 的增量）')
    prev_entry = manifest['entries'][-1]
    prev_end = int(prev_entry.get('binlog_end') or 0)
    prev_state = prev_entry.get('state') or {}
    prev_cache = _load_prev_cache(chain_dir, prev_entry)
    t0 = time.perf_counter()
    db = Database(str(data_dir))
    try:
        db.checkpoint(force=True)
        cur_state, cur_cache = _capture_state(db, include_rows=True)
    finally:
        db.shutdown()
    pos = _binlog_pos(data_dir)
    ddl, ddl_end, gap = _collect_ddl(data_dir, prev_end)
    if ddl_end < prev_end:
        raise ChainIntegrityError(f'binlog 回溯：current={ddl_end} < 上次 end={prev_end}')
    _check_ddl(ddl)
    schema_changed = _schema_changed(prev_state, cur_state)
    if schema_changed and not ddl:
        raise ChainUnsupported(
            '检测到 schema 变化，但 binlog 区间内没有 DDL 事件（binlog 被'
            '轮转/清理或修改绕过执行器）；拒绝生成无法安全恢复的增量')
    changes = _diff_rows(prev_cache, cur_cache)
    if not changes and not ddl and not schema_changed:
        return {'status': 'noop', 'action': 'incr',
                'message': '自上次增量后无数据/DDL 变化，未生成新段',
                'duration_s': round(time.perf_counter() - t0, 3)}
    new_id = int(prev_entry['id']) + 1
    payload = {
        'schema': 'lightdb-backup-increment/1', 'format_version': CHAIN_FORMAT,
        'id': new_id, 'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'lightdb_version': LIGHTDB_VERSION,
        'binlog_start': prev_end, 'binlog_end': ddl_end,
        'binlog_gap': bool(gap),
        'ddl': ddl, 'tables': changes,
        'state': cur_state,
        'stats': {
            'tables_changed': len(changes),
            'inserts': sum(len(v['inserts']) for v in changes.values()),
            'updates': sum(len(v['updates']) for v in changes.values()),
            'deletes': sum(len(v['deletes']) for v in changes.values()),
            'ddl_count': len(ddl),
        },
    }
    file_name = (INCR_PREFIX + '%04d_%d_%d.json'
                 % (new_id, int(time.time() * 1000) % 10 ** 8,
                    os.getpid() % 10000))
    body = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    raw = body.encode('utf-8')
    path = chain_dir / file_name
    tmp = path.with_suffix('.json.tmp')
    tmp.write_bytes(raw)
    os.replace(tmp, path)
    entry = {
        'id': new_id, 'kind': 'incr', 'file': file_name,
        'binlog_start': prev_end, 'binlog_end': ddl_end,
        'size': len(raw), 'sha256': _sha256_bytes(raw),
        'created_at': payload['created_at'], 'state': cur_state,
        'stats': payload['stats'], 'binlog_gap': bool(gap),
        'ddl': len(ddl),
    }
    manifest['entries'].append(entry)
    if keep is not None:
        manifest['keep'] = int(keep)
    _save_manifest(chain_dir, manifest)
    _write_gzip_json(chain_dir / STATE_NAME, cur_cache)
    report = {
        'status': 'ok', 'action': 'incr', 'chain_dir': str(chain_dir),
        'entry_id': new_id, 'file': str(path), 'size': len(raw),
        'sha256': entry['sha256'], 'stats': payload['stats'],
        'binlog': [prev_end, ddl_end], 'binlog_gap': bool(gap),
        'increment_bytes': len(raw),
        'duration_s': round(time.perf_counter() - t0, 3),
    }
    print('[chain] incr 完成: #%d %s（%d 表变化 / +%d / ~%d / -%d / DDL %d '
          '/ %.1f KB / %.2fs）'
          % (new_id, file_name, report['stats']['tables_changed'],
             report['stats']['inserts'], report['stats']['updates'],
             report['stats']['deletes'], report['stats']['ddl_count'],
             len(raw) / 1024.0, report['duration_s']))
    if keep is not None:
        report['prune'] = chain_prune(chain_dir, int(keep), quiet=True)
    return report


# ==================== 恢复：应用 + 校验 ====================

def _decode_row(row):
    return [_unjsonable(v) for v in (row or [])]


def _apply_increment(db, ex, incr: dict) -> None:
    with db.suspend_binlog():
        for sql in incr.get('ddl') or []:
            res = ex.execute_sql(sql, {})
            if isinstance(res, dict) and res.get('type') == 'error':
                raise ChainIntegrityError(
                    f'DDL 回放失败（段 {incr.get("id")}）: {sql[:160]} -> {res}')
        for table, ops in (incr.get('tables') or {}).items():
            try:
                tbl = db.get_table(table)
            except Exception:                        # noqa: BLE001
                # 表已被同段 DDL 删除/重建；行操作应不存在于最终状态
                raise ChainIntegrityError(
                    f'回放行差分时表不存在: {table!r}（段 {incr.get("id")}）')
            pk = str(tbl.pk_col)
            for pk_value in ops.get('deletes') or []:
                tbl.delete({'column': pk, 'op': '=', 'value': _unjsonable(pk_value)})
            for row in ops.get('inserts') or []:
                tbl.insert(_decode_row(row))
            for row in ops.get('updates') or []:
                decoded = _decode_row(row)
                pk_value = decoded[tbl.pk_idx]
                tbl.delete({'column': pk, 'op': '=', 'value': pk_value})
                tbl.insert(decoded)
        db.checkpoint(force=False)


def _set_auto_increment(db, state: dict) -> None:
    for table, st in (state or {}).items():
        expected = st.get('auto_increment_next') or {}
        if not expected:
            continue
        tbl = db.tables.get(table)
        if tbl is None:
            continue
        try:
            current = {str(k): int(v) for k, v in
                       (tbl.auto_increment_next_values() or {}).items()}
        except Exception:                            # noqa: BLE001
            current = {}
        for col, want in expected.items():
            if current.get(col) == int(want):
                continue
            try:
                actual = int(tbl.set_auto_increment(int(want)))
            except Exception as exc:                 # noqa: BLE001
                raise ChainIntegrityError(
                    f'恢复自增值失败（表 {table}.{col} 期望 {want}）: {exc}') from exc
            if actual != int(want):
                raise ChainIntegrityError(
                    f'自增值无法恢复（表 {table}.{col} 期望 {want}，实际 {actual}）')


def _restore_entries_to_dir(chain_dir, entries, staging: Path) -> dict:
    """把基线 + 指定增量恢复到 staging，并校验最终状态指纹；返回 report。"""
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    snap_report = snap.restore_snapshot(
        _full_dir(chain_dir, entries[0]), staging, force=True,
        check_servers=False, verify=True, quiet=True)
    db = Database(str(staging))
    try:
        ex = Executor(db)
        for entry in entries[1:]:
            incr = _load_incr(chain_dir, entry)
            _apply_increment(db, ex, incr)
        final_state = entries[-1].get('state') or {}
        _set_auto_increment(db, final_state)
        db.checkpoint(force=True)
        actual_state, _ = _capture_state(db, include_rows=False)
    finally:
        db.shutdown()
    # WAL=0 校验（恢复后不应有未落盘 WAL）
    wal_problems = []
    for table in actual_state:
        wal = staging / (table + '.wal')
        if wal.exists() and wal.stat().st_size != 0:
            wal_problems.append(f'{table}.wal={wal.stat().st_size}B')
    if wal_problems:
        raise ChainIntegrityError('恢复后 WAL 非零: ' + ', '.join(wal_problems))
    problems = _state_compare(final_state, actual_state)
    if problems:
        raise ChainIntegrityError(
            '恢复后状态指纹不一致（链损坏/版本不兼容）: ' + '; '.join(problems[:10]))
    return {
        'staging': str(staging),
        'snapshot_restore': snap_report,
        'tables': len(actual_state),
        'row_total': sum(s['row_count'] for s in actual_state.values()),
        'duration_s': round(time.perf_counter() - t0, 3),
        'state_verified': True,
        'wal_zero': True,
    }


def chain_restore(chain_dir, target, to: int = None, latest: bool = True,
                  force: bool = False, keep_staging: bool = False) -> dict:
    _quiet()
    chain_dir, target = Path(chain_dir), Path(target)
    manifest = _load_manifest(chain_dir)
    _validate_chain_files(chain_dir, manifest)
    entries = manifest['entries']
    max_incr = len(entries) - 1
    if latest or to is None:
        want = max_incr
    else:
        want = int(to)
        if want < 0 or want > max_incr:
            raise ChainError(f'--to 越界: {want}（有效范围 0..{max_incr}）')
    selected = entries[:want + 1]
    # 目标前置检查：失败时绝不触碰目标
    if target.exists() and any(target.iterdir()):
        if not force:
            raise ChainError(f'目标目录非空: {target}（恢复会覆盖；确认请加 --force）')
    staging = Path(tempfile.mkdtemp(prefix='lightdb_chain_restore_'))
    try:
        report = _restore_entries_to_dir(chain_dir, selected, staging)
        install_t0 = time.perf_counter()
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(staging, target)
        install_s = time.perf_counter() - install_t0
    finally:
        if not keep_staging:
            shutil.rmtree(staging, ignore_errors=True)
    report.update({
        'status': 'ok', 'action': 'restore', 'chain_dir': str(chain_dir),
        'target': str(target), 'applied_increments': want,
        'latest': bool(latest), 'install_s': round(install_s, 3),
        'total_s': round(report['duration_s'] + install_s, 3),
        'entry_ids': [e['id'] for e in selected],
    })
    print('[chain] restore 完成: %s -> %s（基线 + %d 增量 / %d 表 / %d 行 / '
          'WAL=0 / 状态校验通过 / %.2fs）'
          % (chain_dir, target, want, report['tables'], report['row_total'],
             report['total_s']))
    return report


# ==================== 保留策略 / rebase ====================

def chain_prune(chain_dir, keep: int, quiet: bool = False) -> dict:
    _quiet()
    chain_dir = Path(chain_dir)
    manifest = _load_manifest(chain_dir)
    entries = manifest['entries']
    incs = entries[1:]
    if len(incs) <= keep:
        return {'status': 'noop', 'action': 'prune', 'kept_increments': len(incs),
                'keep': keep}
    prefix_count = len(entries) - keep
    prefix = entries[:prefix_count]
    staging = Path(tempfile.mkdtemp(prefix='lightdb_chain_prune_'))
    try:
        restore_report = _restore_entries_to_dir(chain_dir, prefix, staging)
        new_full_name = (FULL_PREFIX + '%d_%d_prune'
                         % (int(time.time() * 1000) % 10 ** 8,
                            os.getpid() % 1000))
        new_full = chain_dir / new_full_name
        if new_full.exists():
            shutil.rmtree(new_full)
        snap_info = snap.create_snapshot(staging, new_full, force=True)
        if not snap_info.get('wal_zero'):
            raise ChainIntegrityError('rebase 新基线 WAL 非零')
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    retained = incs[-keep:] if keep > 0 else []
    # rebase 后链头 = 新基线 + 保留增量；行级缓存交给下一次 incr 从链重建
    # （前缀 staging 的快照不等于链头状态，绝不能把它当上一状态缓存）。
    try:
        (chain_dir / STATE_NAME).unlink()
    except OSError:
        pass
    new_full_id = 0
    full_entry = {
        'id': new_full_id, 'kind': 'full', 'dir': new_full_name,
        'binlog_start': (int(retained[0]['binlog_start']) if retained
                         else int(entries[-1]['binlog_end'])),
        'binlog_end': (int(retained[0]['binlog_start']) if retained
                       else int(entries[-1]['binlog_end'])),
        'manifest_sha256': _sha256_file(new_full / snap.MANIFEST_NAME),
        'snapshot_files': snap_info.get('total_files'),
        'snapshot_bytes': snap_info.get('total_bytes'),
        'snapshot_row_total': snap_info.get('row_total'),
        'state': prefix[-1].get('state') or {},
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'rebased_from': [e['id'] for e in prefix],
    }
    new_entries = [full_entry]
    for i, e in enumerate(retained, start=1):
        e2 = dict(e)
        old_id = int(e2.get('id') if e2.get('id') is not None else -1)
        e2['id'] = i
        if old_id != i:
            # 重新编号后同步改写增量文件内的 id，并刷新 sha256/size
            path = _incr_path(chain_dir, e2)
            data = _read_json(path)
            data['id'] = i
            raw = (json.dumps(data, ensure_ascii=False, indent=2) + '\n'
                   ).encode('utf-8')
            tmp = path.with_suffix('.json.tmp')
            tmp.write_bytes(raw)
            os.replace(tmp, path)
            e2['size'] = len(raw)
            e2['sha256'] = _sha256_bytes(raw)
            full_entry['binlog_start'] = int(e2['binlog_start'])
            full_entry['binlog_end'] = int(e2['binlog_start'])
        new_entries.append(e2)
    old_entries = list(entries)
    manifest['entries'] = new_entries
    manifest['keep'] = int(keep)
    _save_manifest(chain_dir, manifest)
    # manifest 落盘后删除被淘汰文件（失败只告警，不影响链完整性）
    removed = []
    keep_files = {e.get('file') for e in new_entries if e.get('kind') == 'incr'}
    keep_dirs = {e.get('dir') for e in new_entries if e.get('kind') == 'full'}
    for e in old_entries:
        try:
            if e.get('kind') == 'full' and e.get('dir') not in keep_dirs:
                p = _full_dir(chain_dir, e)
                if p.exists():
                    shutil.rmtree(p)
                    removed.append(p.name)
            if e.get('kind') == 'incr' and e.get('file') not in keep_files:
                p = _incr_path(chain_dir, e)
                if p.exists():
                    p.unlink()
                    removed.append(p.name)
        except OSError:
            pass
    report = {
        'status': 'ok', 'action': 'prune', 'keep': keep,
        'old_entries': len(old_entries), 'new_entries': len(new_entries),
        'retained_increments': len(retained),
        'new_full': new_full_name, 'removed_files': removed[:20],
        'restore_duration_s': restore_report['duration_s'],
    }
    if not quiet:
        print('[chain] prune 完成: 保留基线 + %d 增量（rebase %s，删除 %d 个旧文件）'
              % (len(retained), new_full_name, len(removed)))
    return report


# ==================== bench ====================

def _build_dataset(data_dir, rows: int) -> None:
    data_dir = Path(data_dir)
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(str(data_dir))
    try:
        ex = Executor(db)
        res = ex.execute_sql(
            'CREATE TABLE t (id INT PRIMARY KEY AUTO_INCREMENT, v INT)', {})
        if isinstance(res, dict) and res.get('type') == 'error':
            raise ChainError(f'建表失败: {res}')
        batch = 500
        for start in range(1, rows + 1, batch):
            end = min(rows, start + batch - 1)
            values = ', '.join('(%d)' % i for i in range(start, end + 1))
            res = ex.execute_sql('INSERT INTO t (v) VALUES ' + values, {})
            if isinstance(res, dict) and res.get('type') == 'error':
                raise ChainError(f'导入失败: {res}')
        db.checkpoint(force=True)
    finally:
        db.shutdown()


def bench(rows: int = 10_000, changes: int = 1_000, out: str = '') -> dict:
    _quiet()
    work = Path(tempfile.mkdtemp(prefix='lightdb_chain_bench_'))
    try:
        data_dir = work / 'db'
        _build_dataset(data_dir, rows)
        chain_dir = work / 'chain'
        t0 = time.perf_counter()
        init_report = chain_init(data_dir, chain_dir)
        init_s = time.perf_counter() - t0
        # 变更：更新 changes/2 行 + 新增 changes/2 行（含 AUTO_INCREMENT）
        db = Database(str(data_dir))
        try:
            ex = Executor(db)
            half = max(1, changes // 2)
            for i in range(1, half + 1):
                res = ex.execute_sql(
                    'UPDATE t SET v = %d WHERE id = %d' % (1000 + i, i), {})
                if isinstance(res, dict) and res.get('type') == 'error':
                    raise ChainError(f'更新失败: {res}')
            for i in range(half):
                ex.execute_sql('INSERT INTO t (v) VALUES (%d)'
                               % (rows + i + 1), {})
            db.checkpoint(force=True)
        finally:
            db.shutdown()
        t0 = time.perf_counter()
        incr_report = chain_incr(data_dir, chain_dir)
        incr_s = time.perf_counter() - t0
        target = work / 'restored'
        t0 = time.perf_counter()
        restore_report = chain_restore(chain_dir, target)
        restore_s = time.perf_counter() - t0
        incr_size = int(incr_report.get('size') or 0)
        baseline_bytes = int((init_report.get('baseline_bytes') or 0))
        report = {
            'schema': 'lightdb-incremental-backup-bench/1',
            'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'rows': rows, 'changes': changes,
            'baseline': {
                'bytes': baseline_bytes,
                'duration_s': round(init_s, 3),
                'snapshot_dir_bytes': baseline_bytes,
                'row_total': init_report.get('row_total'),
            },
            'increment': {
                'bytes': incr_size,
                'duration_s': round(incr_s, 3),
                'stats': incr_report.get('stats') or {},
                'ratio_vs_baseline': (round(incr_size / baseline_bytes, 4)
                                      if baseline_bytes else None),
            },
            'restore': {
                'duration_s': round(restore_s, 3),
                'applied_increments': restore_report.get('applied_increments'),
                'row_total': restore_report.get('row_total'),
                'wal_zero': restore_report.get('wal_zero'),
                'state_verified': restore_report.get('state_verified'),
            },
        }
        if out:
            p = Path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                         encoding='utf-8')
            report['_out'] = str(p)
        print('[chain] bench: %d 行 → 基线 %.1f KB/%.2fs；增量 %.1f KB/%.2fs；'
              '恢复 %.2fs（WAL=0=%s）'
              % (rows, baseline_bytes / 1024.0, init_s, incr_size / 1024.0,
                 incr_s, restore_s, report['restore']['wal_zero']))
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ==================== CLI ====================

def _make_parser():
    ap = argparse.ArgumentParser(
        description='LightDB 增量备份链（init/incr/restore/prune/bench）')
    sub = ap.add_subparsers(dest='command', required=True)
    p_init = sub.add_parser('init', help='创建全量基线链')
    p_init.add_argument('--data-dir', required=True)
    p_init.add_argument('--chain-dir', required=True)
    p_init.add_argument('--json', default='')
    p_incr = sub.add_parser('incr', help='生成一个增量段')
    p_incr.add_argument('--data-dir', required=True)
    p_incr.add_argument('--chain-dir', required=True)
    p_incr.add_argument('--keep', type=int, default=None)
    p_incr.add_argument('--json', default='')
    p_rest = sub.add_parser('restore', help='恢复基线+增量到目标目录')
    p_rest.add_argument('--chain-dir', required=True)
    p_rest.add_argument('--target', required=True)
    p_rest.add_argument('--to', type=int, default=None,
                        help='恢复到第 N 个增量（0=仅基线）')
    p_rest.add_argument('--latest', action='store_true', default=True)
    p_rest.add_argument('--force', action='store_true')
    p_rest.add_argument('--json', default='')
    p_prune = sub.add_parser('prune', help='保留最新 N 个增量并 rebase')
    p_prune.add_argument('--chain-dir', required=True)
    p_prune.add_argument('--keep', type=int, required=True)
    p_prune.add_argument('--json', default='')
    p_bench = sub.add_parser('bench', help='10k 基线/增量大小与恢复耗时')
    p_bench.add_argument('--rows', type=int, default=10_000)
    p_bench.add_argument('--changes', type=int, default=1_000)
    p_bench.add_argument('--json', default='')
    return ap


def _emit(report: dict, json_path: str) -> None:
    if json_path:
        p = Path(json_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                     encoding='utf-8')
        print('JSON: %s' % p)


def main(argv=None) -> int:
    args = _make_parser().parse_args(argv)
    _quiet()
    try:
        if args.command == 'init':
            report = chain_init(args.data_dir, args.chain_dir)
        elif args.command == 'incr':
            report = chain_incr(args.data_dir, args.chain_dir, keep=args.keep)
        elif args.command == 'restore':
            report = chain_restore(
                args.chain_dir, args.target,
                to=(None if args.to is None else args.to),
                latest=(args.latest if args.to is None else False),
                force=args.force)
        elif args.command == 'prune':
            report = chain_prune(args.chain_dir, args.keep)
        elif args.command == 'bench':
            report = bench(args.rows, args.changes, args.json)
            return 0
        else:                                        # pragma: no cover
            return 2
    except ChainVersionError as exc:
        print(json.dumps({'status': 'error', 'action': args.command,
                          'type': 'version_incompatible',
                          'error': str(exc)}, ensure_ascii=False))
        return 3
    except ChainIntegrityError as exc:
        print(json.dumps({'status': 'error', 'action': args.command,
                          'type': 'chain_integrity', 'error': str(exc)},
                         ensure_ascii=False))
        return 4
    except ChainUnsupported as exc:
        print(json.dumps({'status': 'error', 'action': args.command,
                          'type': 'unsupported_change', 'error': str(exc)},
                         ensure_ascii=False))
        return 5
    except ChainError as exc:
        print(json.dumps({'status': 'error', 'action': args.command,
                          'type': 'chain_error', 'error': str(exc)},
                         ensure_ascii=False))
        return 1
    _emit(report, getattr(args, 'json', '') or '')
    return 0


if __name__ == '__main__':
    sys.exit(main())
