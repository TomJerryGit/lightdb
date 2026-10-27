"""二进制一致性快照备份 / 恢复（P2）。

设计目标
--------
* **一致性**：快照前先对每张表做 checkpoint（脏页 fsync + BTree 根 fsync +
  WAL 截断），确认所有 `<table>.wal` 归零后才做文件级拷贝；拷贝的
  `.dat` / `.btroot` / `.free` / `.autoinc` / `_meta.json` / `root.password`
  是"同一时刻"的可直接恢复状态。
* **可校验**：生成的 `manifest.json` 记录 lightdb 版本、快照格式版本、
  每张表的行数/列定义/`SHOW CREATE`/首尾样本行/自增计数，以及每个数据
  文件的 size + SHA256；恢复前先校验 manifest 与全部文件哈希，**校验失败
  绝不触碰目标目录**。
* **不与运行中服务打架**：默认检测 127.0.0.1 上是否有服务端口在监听；
  检测到则**拒绝**（`SnapshotRefusedError`，退出码 3），只有显式
  `force=True`（CLI `--force`）才继续并记录 WARNING。
* **恢复后自检**：逐表行数 / 关键值样本 / `SHOW CREATE` / 自增下一号一致，
  且 WAL 归零；失败显式报错（不静默返回"成功"）。
* **单表快照**：`table=<name>` 只拷该表文件 + 过滤后的 `_meta.json`，
  可恢复到全新数据目录（只含该表）。
* **在线热备（quiesce）**：`online=True` 时在拷贝窗口内阻断写入：
  * 进程内模式（`live_db=<server 的 Database 实例>`）：按表名排序获取
    所有目标表的 `Table._lock`（写锁）→ `db.checkpoint()`（WAL 落盘）
    → 拷贝文件 → 释放锁；窗口内服务写入被阻塞，释放后继续。
  * 跨进程模式（`server_pid=...` / 自动探测监听端口）：用 OS 级
    `Process.suspend()` 冻结服务进程 → 原样拷贝（crash-consistent 镜像）
    → 恢复进程 → 在**快照副本**上做 WAL 恢复 + checkpoint，得到 WAL=0 的
    确定恢复点。冻结窗口只含拷贝，不含哈希/恢复。
  * 两种模式都在 manifest 的 `online` 段记录阻塞窗口、checkpoint、拷贝耗时。

兼容策略（跨版本）
------------------
* 快照格式（`schema` + `format_version`）不兼容 → 无条件 `ManifestError`，
  即使传 `--allow-version-mismatch` 也不放行；
* 仅 lightdb 版本不同（同格式）→ 默认拒绝；显式
  `--allow-version-mismatch` 允许并写入 WARNING（报告
  `version_check.warnings`），恢复后照常逐表自检。

边界（详见 docs/SNAPSHOT_BACKUP.md）
-----------------------------------
* 离线 `--force` 不冻结服务：只保证"本工具 checkpoint 后文件一致"，不能
  保证与并发写入的实时性——生产环境优先 `--online` 或停机。
* 跨进程 online 依赖操作系统进程暂停权限；受限沙箱/无权限时显式失败
  （`OnlineQuiesceError`），不会假装 quiesce 成功。
* 端口检测只覆盖显式传入/默认 8888；自定义端口的服务请用
  `--server-port` 告知，否则可能漏检。
* 数据目录里除数据文件外的文件（审计日志、备份目录等）不进入快照。
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import platform
import shutil
import socket
import sys
import time
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:               # 允许脚本/独立运行
    sys.path.insert(0, str(REPO_ROOT))

SCHEMA = 'lightdb-binary-snapshot/1'
FORMAT_VERSION = 1
MANIFEST_NAME = 'manifest.json'
DATA_SUBDIR = 'data'
DEFAULT_SERVER_PORTS = (8888,)
#: 快照包含的每表文件后缀（与 lightdb/db/table.py 的落盘文件一致）
TABLE_FILE_SUFFIXES = ('.dat', '.btroot', '.free', '.wal', '.autoinc')
#: 全局文件（库级元数据 / 兼容旧库的 root 口令文件）
GLOBAL_FILES = ('_meta.json', 'root.password')
SAMPLE_FIRST = 3
SAMPLE_LAST = 3


# ==================== 异常 ====================

class SnapshotError(Exception):
    """快照/恢复的通用错误。"""


class SnapshotRefusedError(SnapshotError):
    """检测到服务在监听 / 目标被占用等"拒绝执行"情形（CLI 退出码 3）。"""


class ManifestError(SnapshotError):
    """manifest 缺失、JSON 非法、schema/format 不兼容、字段缺失。"""


class VersionMismatchError(SnapshotError):
    """lightdb 版本不兼容（默认要求精确匹配）。"""


class SnapshotIntegrityError(SnapshotError):
    """快照文件缺失/大小不符/SHA256 不匹配，或恢复后自检不一致。"""


class OnlineQuiesceError(SnapshotError):
    """在线快照无法建立/完成 quiesce 窗口（无进程、被沙箱拒绝、锁超时等）。"""


# ==================== 基础工具 ====================

def lightdb_version() -> str:
    """当前 lightdb 版本（manifest 的 `lightdb_version` 来源）。"""
    try:
        import lightdb
        return str(lightdb.__version__)
    except Exception:                                # pragma: no cover
        return 'unknown'


def sha256_file(path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


def _jsonable(value):
    """把引擎行值转成可 JSON 序列化/可逐值比较的形式（无损）。"""
    if isinstance(value, bytes):
        return {'__bytes_hex__': value.hex()}
    if isinstance(value, bytearray):
        return {'__bytes_hex__': bytes(value).hex()}
    if isinstance(value, memoryview):
        return {'__bytes_hex__': value.tobytes().hex()}
    if isinstance(value, Decimal):
        return {'__decimal__': str(value)}
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return {'__iso__': value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _table_file_names(data_dir: Path, table: str) -> list:
    names = []
    for suffix in TABLE_FILE_SUFFIXES:
        p = data_dir / (table + suffix)
        if p.is_file():
            names.append(p.name)
    return names


def _selected_files(data_dir: Path, tables) -> list:
    """返回本次快照要拷贝的文件名（不含路径）。"""
    files = [n for n in GLOBAL_FILES if (data_dir / n).is_file()]
    for t in tables:
        files.extend(_table_file_names(data_dir, t))
    return files


def find_running_servers(server_ports=None, timeout: float = 0.2,
                         host: str = '127.0.0.1') -> list:
    """探测给定端口上是否有服务在监听（默认 8888）。

    只做 TCP connect —— 不猜测进程名、不读注册表。返回
    `[{'host':..., 'port':...}, ...]`（空列表 = 未检测到）。
    """
    ports = [int(p) for p in (server_ports or DEFAULT_SERVER_PORTS)]
    env_port = os.environ.get('LIGHTDB_PORT')
    if env_port and server_ports is None:
        try:
            ports.append(int(env_port))
        except ValueError:
            pass
    found = []
    for port in sorted(set(ports)):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            if s.connect_ex((host, port)) == 0:
                found.append({'host': host, 'port': port})
        finally:
            s.close()
    return found


def _refuse_if_running(server_ports, force: bool, warnings: list,
                       action: str) -> list:
    running = find_running_servers(server_ports)
    if not running:
        return running
    detail = ', '.join('%s:%s' % (r['host'], r['port']) for r in running)
    if not force:
        raise SnapshotRefusedError(
            f'检测到 LightDB 服务正在监听 {detail}，拒绝{action}（可能拷到不一致'
            f'的中间状态）。请先停止服务；确需在线{action}请显式 --force（风险自负）')
    warnings.append(
        f'WARNING: 服务正在监听 {detail}，--force 强制{action}；并发写入下'
        f'不保证与实时状态一致，生产环境请停机操作')
    return running


# ==================== 在线热备（quiesce）后端 ====================

def detect_server_pid(server_ports=None) -> "int | None":
    """用 psutil 找监听给定端口的进程 PID（找不到/无权限时返回 None）。"""
    try:
        import psutil
    except Exception:                                # pragma: no cover
        return None
    ports = set(int(p) for p in (server_ports or DEFAULT_SERVER_PORTS))
    env_port = os.environ.get('LIGHTDB_PORT')
    if env_port and server_ports is None:
        try:
            ports.add(int(env_port))
        except ValueError:
            pass
    try:
        conns = psutil.net_connections(kind='tcp')
    except Exception:                                # pragma: no cover
        return None
    for conn in conns:
        try:
            if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                continue
            if conn.laddr.port in ports and conn.pid and conn.pid != os.getpid():
                return int(conn.pid)
        except Exception:                            # pragma: no cover
            continue
    return None


def suspend_process(pid: int, timeout: float = 10.0) -> dict:
    """OS 级暂停一个进程（跨进程 online quiesce 后端）。

    受限沙箱/令牌可能禁止 PROCESS_SUSPEND_RESUME —— 此时抛
    `OnlineQuiesceError` 显式失败，绝不假装暂停成功。
    """
    if int(pid) == os.getpid():
        raise OnlineQuiesceError('拒绝暂停当前进程自身')
    try:
        import psutil
    except Exception as exc:                         # pragma: no cover
        raise OnlineQuiesceError(f'缺少 psutil，无法做进程级 quiesce: {exc}')
    try:
        proc = psutil.Process(int(pid))
    except Exception as exc:                         # noqa: BLE001
        raise OnlineQuiesceError(f'找不到服务进程 pid={pid}: {exc}') from exc
    try:
        proc.suspend()
    except Exception as exc:                         # noqa: BLE001
        raise OnlineQuiesceError(
            f'暂停服务进程 pid={pid} 失败（{type(exc).__name__}: {exc}）；'
            f'受限沙箱可能禁止 PROCESS_SUSPEND_RESUME。可改用进程内 live_db '
            f'方式，或停止服务后做离线快照（--force 属不安全兜底）'
        ) from exc
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if proc.status() == psutil.STATUS_STOPPED:
                break
        except Exception:                            # pragma: no cover
            break
        time.sleep(0.01)
    return {'pid': int(pid), 'backend': 'process-suspend', 'suspended': True}


def resume_process(pid: int) -> bool:
    """恢复被 suspend_process 暂停的进程；返回是否成功。"""
    try:
        import psutil
        psutil.Process(int(pid)).resume()
        return True
    except Exception:                                # pragma: no cover
        return False


def _copy_raw_files(data_dir: Path, tables, data_out: Path) -> list:
    """把选中的物理文件原样拷贝到快照 data/（不做哈希，尽量缩短阻塞窗口）。"""
    names = _selected_files(data_dir, tables)
    for name in names:
        shutil.copy2(data_dir / name, data_out / name)
    return names


def _read_tables_from_meta(data_dir: Path) -> list:
    """不打开数据库、直接读 `_meta.json` 拿表名（跨进程 online 用）。"""
    try:
        meta = json.loads((data_dir / '_meta.json').read_text(encoding='utf-8'))
    except Exception as exc:                         # noqa: BLE001
        raise SnapshotError(f'读取 _meta.json 失败: {exc}') from exc
    return sorted((meta.get('tables') or {}).keys())


class _TableLocks:
    """进程内 quiesce：按名字排序获取所有目标表的 `Table._lock`（写锁）。"""

    def __init__(self, live_db, tables, quiesce_wait: float = 10.0):
        self.live_db = live_db
        self.tables = sorted(tables)
        self.timeout = float(quiesce_wait)
        self.locks = []
        self.wait_s = 0.0

    def __enter__(self):
        t0 = time.perf_counter()
        try:
            for name in self.tables:
                lock = self.live_db.get_table(name)._lock
                if not lock.acquire(timeout=self.timeout):
                    raise OnlineQuiesceError(
                        f'等待表 {name} 的写锁超时（{self.timeout:.1f}s）：'
                        f'有长事务/DDL 正在执行，无法建立 quiesce 窗口')
                self.locks.append(lock)
        except BaseException:
            self.release()
            raise
        finally:
            self.wait_s = time.perf_counter() - t0
        return self

    def release(self):
        while self.locks:
            lock = self.locks.pop()
            try:
                lock.release()
            except RuntimeError:                     # pragma: no cover
                pass

    def __exit__(self, *exc):
        self.release()
        return False


# ==================== manifest ====================

def _result_rows(result, what: str) -> list:
    if isinstance(result, dict) and result.get('type') == 'error':
        raise SnapshotError(f'{what} 失败: {result}')
    if not isinstance(result, dict) or result.get('type') != 'resultset':
        raise SnapshotError(f'{what} 返回了非结果集: {result!r}')
    return result.get('rows') or []


def _collect_table_info(db, executor, tname: str) -> dict:
    """采集单表：行数 / 列定义 / SHOW CREATE / 首尾样本 / 自增计数。"""
    from lightdb.db.database import describe_columns

    tbl = db.get_table(tname)
    count_rows = _result_rows(
        executor.execute_sql(f'SELECT COUNT(*) FROM {tname}'),
        f'COUNT(*) FROM {tname}')
    row_count = int(count_rows[0][0])
    cols = describe_columns(tbl)
    create_rows = _result_rows(
        executor.execute_sql(f'SHOW CREATE TABLE {tname}'),
        f'SHOW CREATE TABLE {tname}')
    show_create = create_rows[0][1] if create_rows else ''
    pk = [c['name'] for c in cols if c.get('key') == 'PRI']
    samples = {}
    if pk:
        order = ', '.join(pk)
        samples['first_sql'] = (f'SELECT * FROM {tname} ORDER BY {order} '
                                f'LIMIT {SAMPLE_FIRST}')
        samples['last_sql'] = (f'SELECT * FROM {tname} ORDER BY {order} DESC '
                               f'LIMIT {SAMPLE_LAST}')
    else:
        samples['first_sql'] = f'SELECT * FROM {tname} LIMIT {SAMPLE_FIRST}'
        samples['last_sql'] = None
    first = [_jsonable(list(r)) for r in _result_rows(
        executor.execute_sql(samples['first_sql']), '样本查询')]
    last = []
    if samples.get('last_sql'):
        last = [_jsonable(list(r)) for r in _result_rows(
            executor.execute_sql(samples['last_sql']), '样本查询')]
    auto_next = {}
    if hasattr(tbl, 'auto_increment_next_values'):
        try:
            auto_next = {str(k): int(v)
                         for k, v in (tbl.auto_increment_next_values() or {}).items()}
        except Exception:                            # pragma: no cover
            auto_next = {}
    return {
        'name': tname,
        'row_count': row_count,
        'columns': _jsonable(cols),
        'primary_key': pk,
        'show_create': show_create,
        'auto_increment_next': auto_next,
        'sample_first': first,
        'sample_last': last,
        'sample_sql': samples,
    }


def _write_manifest(snapshot_dir: Path, manifest: dict):
    target = snapshot_dir / MANIFEST_NAME
    tmp = snapshot_dir / (MANIFEST_NAME + '.tmp')
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    os.replace(tmp, target)


def read_manifest(snapshot_dir) -> dict:
    snapshot_dir = Path(snapshot_dir)
    path = snapshot_dir / MANIFEST_NAME
    if not path.is_file():
        raise ManifestError(f'快照目录里没有 {MANIFEST_NAME}: {snapshot_dir}')
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:                         # noqa: BLE001
        raise ManifestError(f'manifest 不是合法 JSON（{path}）: {exc}') from exc
    if not isinstance(manifest, dict):
        raise ManifestError('manifest 顶层必须是 JSON 对象')
    if manifest.get('schema') != SCHEMA:
        raise ManifestError(f'不支持的 manifest schema: {manifest.get("schema")!r}'
                            f'（本工具: {SCHEMA}）')
    if int(manifest.get('format_version') or 0) != FORMAT_VERSION:
        raise ManifestError(f'不支持的快照 format_version: '
                            f'{manifest.get("format_version")!r}'
                            f'（本工具: {FORMAT_VERSION}）')
    for key in ('lightdb_version', 'created_at', 'tables', 'files'):
        if key not in manifest:
            raise ManifestError(f'manifest 缺少字段: {key}')
    return manifest


def validate_manifest(manifest: dict, version: str = None,
                      allow_version_mismatch: bool = False) -> dict:
    """跨版本兼容校验。

    策略：
    * **格式不兼容**（`schema` / `format_version`）由 `read_manifest` 直接
      `ManifestError` —— 即使 `--allow-version-mismatch` 也不放行；
    * **仅 lightdb 版本不同**（同格式）：默认拒绝；显式
      `allow_version_mismatch=True` 时放行并返回 WARNING（报告链路上抛出），
      恢复后仍必须通过逐表自检。
    """
    expected = str(version or lightdb_version())
    snap_ver = str(manifest.get('lightdb_version') or '')
    warnings = []
    mismatch = snap_ver != expected
    if mismatch:
        if not allow_version_mismatch:
            raise VersionMismatchError(
                f'快照的 lightdb 版本 {snap_ver!r} 与当前版本 {expected!r} 不一致'
                f'（物理页/元数据格式可能变化）；确认格式兼容时可加 '
                f'--allow-version-mismatch（仅放行**格式兼容**的版本差异）')
        warnings.append(
            f'WARNING: 快照 lightdb 版本 {snap_ver!r} != 当前 {expected!r}；'
            f'已由 --allow-version-mismatch 显式放行（schema/format 兼容），'
            f'恢复后会逐表自检；若物理格式实际不兼容会在打开时显式报错')
    return {'expected_version': expected, 'snapshot_version': snap_ver,
            'format_compatible': True, 'mismatch_allowed': bool(mismatch),
            'warnings': warnings}


# ==================== 备份 ====================

def create_snapshot(data_dir, out_dir, *, table: str = None, force: bool = False,
                    server_ports=None, check_servers: bool = True,
                    version: str = None, quiet: bool = False,
                    online: bool = False, live_db=None, server_pid: int = None,
                    quiesce_wait: float = 10.0) -> dict:
    """创建一致性二进制快照；返回报告 dict（含 manifest 与耗时）。

    `online=True` 时走在线热备（quiesce）：
    * `live_db` 给出服务端同一个 `Database` 实例 → 进程内写锁 quiesce；
    * 否则用 `server_pid` / 自动探测监听端口 → 跨进程 OS 级 suspend quiesce。
    """
    warnings = []
    t_start = time.perf_counter()
    data_dir = Path(data_dir).resolve()
    out_dir = Path(out_dir).resolve()
    if not data_dir.is_dir():
        raise SnapshotError(f'数据目录不存在: {data_dir}')
    if out_dir == data_dir or data_dir in out_dir.parents:
        raise SnapshotError('快照输出目录不能位于数据目录内部')
    if check_servers and not online:
        _refuse_if_running(server_ports, force, warnings, '快照')

    # 输出目录：非空目录默认拒绝（避免覆盖旧快照），--force 时清空
    if out_dir.exists():
        if any(out_dir.iterdir()):
            if not force:
                raise SnapshotError(
                    f'快照输出目录已存在且非空: {out_dir}（--force 覆盖）')
            shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data_out = out_dir / DATA_SUBDIR
    data_out.mkdir(parents=True, exist_ok=True)

    if online:
        return _create_online_snapshot(
            data_dir, out_dir, data_out, table=table, live_db=live_db,
            server_pid=server_pid, server_ports=server_ports,
            warnings=warnings, t_start=t_start, quiet=quiet,
            quiesce_wait=quiesce_wait)

    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    try:
        db = Database(str(data_dir))
    except Exception as exc:                         # noqa: BLE001
        raise SnapshotError(f'打开数据目录失败（可能被其它进程占用）: {exc}') from exc

    manifest_tables = []
    try:
        all_tables = sorted(db.tables.keys())
        if table is not None:
            if table not in db.tables:
                raise SnapshotError(f'表不存在: {table!r}（现有: {all_tables}）')
            targets = [table]
        else:
            targets = all_tables
        if not targets:
            raise SnapshotError('数据目录里没有任何表，拒绝生成空快照')

        executor = Executor(db)
        wal_before = {t: (data_dir / f'{t}.wal').stat().st_size
                      if (data_dir / f'{t}.wal').is_file() else 0
                      for t in targets}
        checked = db.checkpoint(force=False)
        wal_after = {}
        for t in targets:
            p = data_dir / f'{t}.wal'
            wal_after[t] = p.stat().st_size if p.is_file() else 0
        non_zero = {t: n for t, n in wal_after.items() if n}
        if non_zero:
            raise SnapshotError(
                f'checkpoint 后仍有非空 WAL（存在未提交事务？）: {non_zero}；'
                f'为确保一致，拒绝快照')
        for t in targets:
            manifest_tables.append(_collect_table_info(db, executor, t))
        users_meta = list((getattr(db, 'users', {}) or {}).keys())
    finally:
        db.shutdown()

    # 关闭后 WAL 必须仍为 0（shutdown 只会再落一次元数据/关闭 WAL）
    final_wal = {}
    for t in targets:
        p = data_dir / f'{t}.wal'
        final_wal[t] = p.stat().st_size if p.is_file() else 0
    non_zero = {t: n for t, n in final_wal.items() if n}
    if non_zero:
        raise SnapshotError(f'shutdown 后 WAL 非零，快照中止: {non_zero}')

    # ---- 拷贝文件 ----
    file_records = []
    names = _selected_files(data_dir, targets)
    if table is not None or True:
        source_meta = data_dir / '_meta.json'
        filtered_meta = None
        if table is not None:
            try:
                filtered_meta = json.loads(source_meta.read_text(encoding='utf-8'))
            except Exception as exc:                 # noqa: BLE001
                raise SnapshotError(f'读取 _meta.json 失败: {exc}') from exc
            filtered_meta['tables'] = {
                table: filtered_meta['tables'][table]}
            filtered_meta['views'] = {}
            filtered_meta['pending_ddl'] = None
    for name in names:
        src = data_dir / name
        dst = data_out / name
        if name == '_meta.json' and table is not None:
            dst.write_text(json.dumps(filtered_meta, ensure_ascii=False,
                                      indent=2) + '\n', encoding='utf-8')
        else:
            shutil.copy2(src, dst)
        file_records.append({
            'name': name,
            'path': f'{DATA_SUBDIR}/{name}',
            'size': dst.stat().st_size,
            'sha256': sha256_file(dst),
        })

    total_bytes = sum(r['size'] for r in file_records)
    table_by_name = {t['name']: t for t in manifest_tables}
    for tinfo in manifest_tables:
        tinfo['files'] = [r['name'] for r in file_records
                          if _belongs_to_table(r['name'], tinfo['name'])]
        if not tinfo['files']:
            raise SnapshotError(f'表 {tinfo["name"]} 没有任何落盘文件（异常）')

    manifest = {
        'schema': SCHEMA,
        'format_version': FORMAT_VERSION,
        'lightdb_version': lightdb_version(),
        'created_at': _datetime.datetime.now().astimezone().isoformat(
            timespec='seconds'),
        'source': {
            'data_dir': str(data_dir),
            'host': platform.node(),
            'platform': platform.platform(),
            'python': platform.python_version(),
        },
        'mode': 'single_table' if table is not None else 'full',
        'table_filter': table,
        'checkpoint': {
            'tables_checked': int(checked),
            'tables_total': len(targets),
            'wal_before_bytes': wal_before,
            'wal_after_bytes': wal_after,
            'all_wal_zero': True,
            'strategy': '每表 Table.checkpoint()（脏页 fsync + BTree 根 fsync + '
                        'WAL truncate）后 shutdown，再文件级拷贝',
        },
        'tables': manifest_tables,
        'tables_count': len(manifest_tables),
        'row_total': sum(t['row_count'] for t in manifest_tables),
        'files': file_records,
        'total_files': len(file_records),
        'total_bytes': total_bytes,
        'users': users_meta,
        'online': {'enabled': False, 'backend': None,
                   'note': '离线快照：无 quiesce 窗口'},
        'warnings': warnings,
        'limitations': [
            '运行中服务不会被冻结：--force 仅保证本工具 checkpoint 后文件一致，'
            '不保证与并发写入的实时性；生产请停机。',
            '端口检测只覆盖传入的 --server-port（默认 8888），自定义端口需显式告知。',
            '数据目录中的审计日志/备份等非数据文件不进入快照。',
        ],
    }
    _write_manifest(out_dir, manifest)
    duration = time.perf_counter() - t_start
    report = {
        'status': 'ok',
        'action': 'snapshot',
        'snapshot_dir': str(out_dir),
        'manifest_path': str(out_dir / MANIFEST_NAME),
        'lightdb_version': manifest['lightdb_version'],
        'mode': manifest['mode'],
        'table_filter': table,
        'tables': [{'name': t['name'], 'row_count': t['row_count'],
                    'files': t['files']}
                   for t in manifest_tables],
        'tables_count': manifest['tables_count'],
        'row_total': manifest['row_total'],
        'total_files': manifest['total_files'],
        'total_bytes': total_bytes,
        'wal_zero': True,
        'online': manifest['online'],
        'warnings': warnings,
        'duration_s': duration,
    }
    if not quiet:
        print('[snapshot] 完成: %s（%d 表 / %d 行 / %d 文件 / %.1f KB / %.2fs）'
              % (out_dir, manifest['tables_count'], manifest['row_total'],
                 manifest['total_files'], total_bytes / 1024.0, duration))
        for w in warnings:
            print('[snapshot] ' + w)
    return report


# ==================== 在线热备（quiesce）主流程 ====================

def _finalize_online_copy(data_out: Path, table):
    """在**快照副本**上做 WAL 恢复 + checkpoint + 采集元数据。

    * 进程内写锁模式：拷贝时 WAL 已为 0，这里只是采集 + 幂等 checkpoint；
    * 跨进程 suspend 模式：拷贝的是 crash-consistent 镜像，这里先用引擎
      WAL 恢复（redo 已提交 / undo 未提交）→ checkpoint 截断 WAL →
      得到 WAL=0 的确定恢复点，再采集行数/SHOW CREATE/样本/自增。
    返回 (manifest_tables, users_meta, CheckpointDump)。
    """
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    if table is not None:
        meta_path = data_out / '_meta.json'
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        except Exception as exc:                     # noqa: BLE001
            raise SnapshotError(f'读取快照副本 _meta.json 失败: {exc}') from exc
        meta['tables'] = {table: meta['tables'][table]}
        meta['views'] = {}
        meta['pending_ddl'] = None
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n',
                             encoding='utf-8')

    try:
        db = Database(str(data_out))
    except Exception as exc:                         # noqa: BLE001
        raise SnapshotIntegrityError(
            f'在线快照副本无法被引擎打开（WAL 恢复失败/镜像不一致）: {exc}') from exc
    manifest_tables = []
    try:
        checked = db.checkpoint(force=False)
        executor = Executor(db)
        for tname in sorted(db.tables.keys()):
            manifest_tables.append(_collect_table_info(db, executor, tname))
        users_meta = list((getattr(db, 'users', {}) or {}).keys())
    finally:
        db.shutdown()
    if not manifest_tables:
        raise SnapshotError('在线快照副本里没有任何表')
    wal_after = {}
    for tinfo in manifest_tables:
        p = data_out / f'{tinfo["name"]}.wal'
        wal_after[tinfo['name']] = p.stat().st_size if p.is_file() else 0
    non_zero = {t: n for t, n in wal_after.items() if n}
    if non_zero:
        raise SnapshotIntegrityError(
            f'在线快照副本最终的 WAL 非零（一致性未达成）: {non_zero}')
    return manifest_tables, users_meta, {'tables_checked': int(checked),
                                         'wal_after_bytes': wal_after}


def _build_online_manifest(data_dir: Path, data_out: Path, table,
                           manifest_tables, users_meta, checkpoint_info,
                           online_info, warnings) -> tuple:
    """组装在线快照 manifest（结构同离线，额外带 `online` 段）。"""
    names = _selected_files(data_out, [t['name'] for t in manifest_tables])
    file_records = []
    for name in names:
        p = data_out / name
        file_records.append({
            'name': name,
            'path': f'{DATA_SUBDIR}/{name}',
            'size': p.stat().st_size,
            'sha256': sha256_file(p),
        })
    for tinfo in manifest_tables:
        tinfo['files'] = [r['name'] for r in file_records
                          if _belongs_to_table(r['name'], tinfo['name'])]
        if not tinfo['files']:
            raise SnapshotError(f'表 {tinfo["name"]} 没有任何落盘文件（异常）')
    total_bytes = sum(r['size'] for r in file_records)
    checkpoint_info.setdefault(
        'strategy', 'quiesce 窗口内 checkpoint（WAL 落盘/截断）后文件拷贝；'
                    '跨进程模式在副本上 WAL 恢复+checkpoint')
    manifest = {
        'schema': SCHEMA,
        'format_version': FORMAT_VERSION,
        'lightdb_version': lightdb_version(),
        'created_at': _datetime.datetime.now().astimezone().isoformat(
            timespec='seconds'),
        'source': {
            'data_dir': str(data_dir),
            'host': platform.node(),
            'platform': platform.platform(),
            'python': platform.python_version(),
        },
        'mode': 'single_table' if table is not None else 'full',
        'table_filter': table,
        'checkpoint': dict(checkpoint_info),
        'online': dict(online_info),
        'tables': manifest_tables,
        'tables_count': len(manifest_tables),
        'row_total': sum(t['row_count'] for t in manifest_tables),
        'files': file_records,
        'total_files': len(file_records),
        'total_bytes': total_bytes,
        'users': users_meta,
        'warnings': warnings,
        'limitations': [
            '在线快照以 quiesce 窗口换取一致性：窗口内写入阻塞，窗口时长见 '
            'online.blocked_window_s；窗口过长会影响客户端时延。',
            '跨进程 suspend 模式是"冻结服务进程 + 拷贝 crash-consistent 镜像"'
            '（随后在副本上 WAL 恢复），不依赖服务端 checkpoint 接口；'
            '需要 OS 进程暂停权限。',
            '数据目录中的审计日志/备份等非数据文件不进入快照。',
        ],
    }
    return manifest, file_records, total_bytes


def _create_online_snapshot(data_dir: Path, out_dir: Path, data_out: Path, *,
                            table, live_db, server_pid, server_ports,
                            warnings, t_start, quiet,
                            quiesce_wait: float) -> dict:
    from lightdb.db.database import Database      # noqa: F401 (保证引擎可用)
    from lightdb.sql.executor import Executor     # noqa: F401

    if live_db is not None:
        live_dir = Path(getattr(live_db, 'data_dir', '') or '').resolve()
        if live_dir != data_dir:
            raise SnapshotError(
                f'live_db 的数据目录 {live_dir} 与 --data-dir {data_dir} 不一致')
        all_tables = sorted(live_db.tables.keys())
    else:
        all_tables = _read_tables_from_meta(data_dir)
    if table is not None:
        if table not in all_tables:
            raise SnapshotError(f'表不存在: {table!r}（现有: {all_tables}）')
        targets = [table]
    else:
        targets = all_tables
    if not targets:
        raise SnapshotError('数据目录里没有任何表，拒绝生成空快照')

    online_info = {
        'enabled': True,
        'backend': None,
        'quiesced_pid': None,
        'blocked_window_s': 0.0,
        'checkpoint_s': 0.0,
        'copy_s': 0.0,
        'lock_wait_s': 0.0,
        'suspend_s': 0.0,
        'resume_ok': None,
        'recovered_on_copy': False,
        'wal_bytes_before_checkpoint': {},
    }

    if live_db is not None:
        # ---------- 进程内写锁 quiesce ----------
        online_info['backend'] = 'in-process-write-lock'
        online_info['quiesced_pid'] = os.getpid()
        online_info['server_running'] = True
        deadline = time.time() + max(0.1, float(quiesce_wait))
        last_err = ''
        succeeded = False
        while time.time() < deadline:
            locks = _TableLocks(live_db, targets, quiesce_wait)
            locks.__enter__()
            try:
                online_info['lock_wait_s'] = locks.wait_s
                pending = [t for t in targets
                           if getattr(live_db.get_table(t), '_read_views', None)]
                if pending:
                    last_err = f'表 {pending} 上存在未提交事务'
                    continue
                t_mark = time.perf_counter()
                online_info['wal_bytes_before_checkpoint'] = {
                    t: ((data_dir / f'{t}.wal').stat().st_size
                        if (data_dir / f'{t}.wal').is_file() else 0)
                    for t in targets}
                checked = live_db.checkpoint(force=False)
                if int(checked) < len(targets):
                    last_err = (f'仅 {checked}/{len(targets)} 张表 checkpoint '
                                f'成功（存在未提交事务？）')
                    continue
                online_info['checkpoint_s'] = time.perf_counter() - t_mark
                wal_after = {}
                for t in targets:
                    p = data_dir / f'{t}.wal'
                    wal_after[t] = p.stat().st_size if p.is_file() else 0
                non_zero = {t: n for t, n in wal_after.items() if n}
                if non_zero:
                    last_err = f'写锁窗口内 checkpoint 后 WAL 仍非零: {non_zero}'
                    continue
                t_copy = time.perf_counter()
                _copy_raw_files(data_dir, targets, data_out)
                online_info['copy_s'] = time.perf_counter() - t_copy
                online_info['blocked_window_s'] = time.perf_counter() - t_mark
                online_info['wal_after_bytes'] = wal_after
                succeeded = True
            finally:
                locks.release()
        if not succeeded:
            raise OnlineQuiesceError(
                f'未能在 {quiesce_wait:.1f}s 内建立一致的进程内 quiesce 窗口：'
                f'{last_err}（等待事务结束后重试，或停机做离线快照）')
    else:
        # ---------- 跨进程 OS 级 suspend quiesce ----------
        pid = server_pid or detect_server_pid(server_ports)
        if pid is None:
            raise OnlineQuiesceError(
                '在线快照需要能定位服务进程：请用 --server-pid 指定，或改用'
                '进程内 live_db=... 方式（引擎没有跨进程 SQL quiesce 协议）')
        online_info['backend'] = 'process-suspend'
        online_info['quiesced_pid'] = int(pid)
        online_info['server_running'] = True
        t0 = time.perf_counter()
        suspend_process(pid)
        online_info['suspend_s'] = time.perf_counter() - t0
        try:
            t_copy = time.perf_counter()
            _copy_raw_files(data_dir, targets, data_out)
            online_info['copy_s'] = time.perf_counter() - t_copy
        finally:
            resumed = resume_process(pid)
            online_info['resume_ok'] = bool(resumed)
            online_info['blocked_window_s'] = time.perf_counter() - t0
        if not resumed:
            raise OnlineQuiesceError(
                f'严重: 恢复服务进程 pid={pid} 失败，请手工 resume'
                f'（psutil.Process({pid}).resume()）')
        online_info['recovered_on_copy'] = True
        online_info['wal_bytes_before_checkpoint'] = {
            t: ((data_out / f'{t}.wal').stat().st_size
                if (data_out / f'{t}.wal').is_file() else 0)
            for t in targets}

    # ---------- 副本后处理：WAL 恢复/checkpoint + 采集 + 哈希 ----------
    online_info['blocked_window_s'] = round(online_info['blocked_window_s'], 4)
    manifest_tables, users_meta, cp = _finalize_online_copy(data_out, table)
    checkpoint_info = {
        'tables_checked': cp['tables_checked'],
        'tables_total': len(targets),
        'wal_after_bytes': cp['wal_after_bytes'],
        'all_wal_zero': True,
        'online': True,
        'strategy': ('quiesce 窗口内 Table.checkpoint()（脏页 fsync + BTree 根 '
                     'fsync + WAL truncate）→ 文件拷贝 → 释放写锁/恢复进程；'
                     '跨进程模式在快照副本上再做 WAL 恢复 + checkpoint'),
    }
    if live_db is not None:
        checkpoint_info['wal_before_bytes'] = \
            online_info.get('wal_bytes_before_checkpoint') or {}
    manifest, file_records, total_bytes = _build_online_manifest(
        data_dir, data_out, table, manifest_tables, users_meta,
        checkpoint_info, online_info, warnings)
    _write_manifest(out_dir, manifest)
    duration = time.perf_counter() - t_start
    report = {
        'status': 'ok',
        'action': 'snapshot',
        'snapshot_dir': str(out_dir),
        'manifest_path': str(out_dir / MANIFEST_NAME),
        'lightdb_version': manifest['lightdb_version'],
        'mode': manifest['mode'],
        'table_filter': table,
        'tables': [{'name': t['name'], 'row_count': t['row_count'],
                    'files': t['files']} for t in manifest_tables],
        'tables_count': manifest['tables_count'],
        'row_total': manifest['row_total'],
        'total_files': manifest['total_files'],
        'total_bytes': total_bytes,
        'wal_zero': True,
        'online': online_info,
        'warnings': warnings,
        'duration_s': duration,
    }
    if not quiet:
        print('[snapshot] 在线快照完成: %s（%d 表 / %d 行 / %.1f KB）'
              % (out_dir, manifest['tables_count'], manifest['row_total'],
                 total_bytes / 1024.0))
        print('[snapshot] quiesce 后端=%s 阻塞窗口=%.3fs（checkpoint %.3fs + '
              '拷贝 %.3fs，锁等待 %.3fs）总耗时 %.2fs'
              % (online_info['backend'], online_info['blocked_window_s'],
                 online_info['checkpoint_s'], online_info['copy_s'],
                 online_info['lock_wait_s'], duration))
        for w in warnings:
            print('[snapshot] ' + w)
    return report


def _belongs_to_table(filename: str, table: str) -> bool:
    """判断落盘文件是否属于某表（表名可能含点等，按后缀精确匹配）。"""
    for suffix in TABLE_FILE_SUFFIXES:
        if filename == table + suffix:
            return True
    return False


# ==================== 恢复 ====================

def _verify_snapshot_files(snapshot_dir: Path, manifest: dict) -> list:
    """恢复前校验：所有记录的文件存在、大小与 SHA256 一致。"""
    data_dir = snapshot_dir / DATA_SUBDIR
    problems = []
    seen = set()
    for record in manifest.get('files') or []:
        name = record.get('name') or ''
        path = snapshot_dir / str(record.get('path') or f'{DATA_SUBDIR}/{name}')
        if not path.is_file():
            problems.append(f'缺少文件: {record.get("path")}')
            continue
        size = path.stat().st_size
        want = record.get('size')
        want = -1 if want is None else int(want)
        if size != want:
            problems.append(f'大小不符: {name}（manifest={want} '
                            f'实际={size}）')
        actual = sha256_file(path)
        if actual != str(record.get('sha256') or ''):
            problems.append(f'SHA256 不符: {name}（manifest='
                            f'{str(record.get("sha256"))[:16]}… 实际={actual[:16]}…）')
        seen.add(path.name)
    for tinfo in manifest.get('tables') or []:
        for name in tinfo.get('files') or []:
            if name not in seen:
                problems.append(f'表 {tinfo.get("name")} 的文件未在 files 清单中: {name}')
    return problems


def _verify_restored(target: Path, manifest: dict) -> dict:
    """恢复后自检：逐表行数 / SHOW CREATE / 首尾样本 / 自增计数 / WAL=0。"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    problems = []
    db = Database(str(target))
    try:
        executor = Executor(db)
        tables_report = {}
        expected_names = [t['name'] for t in manifest.get('tables') or []]
        actual_names = sorted(db.tables.keys())
        if actual_names != sorted(expected_names):
            problems.append(f'表集合不一致: 期望 {sorted(expected_names)} '
                            f'实际 {actual_names}')
        for tinfo in manifest.get('tables') or []:
            tname = tinfo['name']
            if tname not in db.tables:
                problems.append(f'缺少表: {tname}')
                continue
            count_rows = _result_rows(
                executor.execute_sql(f'SELECT COUNT(*) FROM {tname}'),
                f'COUNT(*) FROM {tname}')
            row_count = int(count_rows[0][0])
            if row_count != int(tinfo['row_count']):
                problems.append(f'{tname}: 行数 {row_count} != manifest '
                                f'{tinfo["row_count"]}')
            create_rows = _result_rows(
                executor.execute_sql(f'SHOW CREATE TABLE {tname}'),
                f'SHOW CREATE TABLE {tname}')
            show_create = create_rows[0][1] if create_rows else ''
            if show_create != tinfo.get('show_create'):
                problems.append(f'{tname}: SHOW CREATE 不一致')
            sample_sql = tinfo.get('sample_sql') or {}
            for key, expect in (('first_sql', tinfo.get('sample_first') or []),
                                ('last_sql', tinfo.get('sample_last') or [])):
                sql = sample_sql.get(key)
                if not sql:
                    continue
                actual = [_jsonable(list(r)) for r in _result_rows(
                    executor.execute_sql(sql), f'{tname} 样本查询')]
                if actual != expect:
                    problems.append(
                        f'{tname}: 样本不一致（{key}）: 期望 {expect!r} 实际 {actual!r}')
            tbl = db.get_table(tname)
            if hasattr(tbl, 'auto_increment_next_values'):
                actual_auto = {str(k): int(v) for k, v in
                               (tbl.auto_increment_next_values() or {}).items()}
                expect_auto = {str(k): int(v) for k, v in
                               (tinfo.get('auto_increment_next') or {}).items()}
                if actual_auto != expect_auto:
                    problems.append(f'{tname}: 自增下一号不一致: '
                                    f'期望 {expect_auto} 实际 {actual_auto}')
            tables_report[tname] = {
                'row_count': row_count,
                'show_create_ok': show_create == tinfo.get('show_create'),
                'samples_ok': True,
            }
    finally:
        db.shutdown()

    # shutdown 后 WAL 必须归零
    wal_sizes = {}
    for tname in [t['name'] for t in manifest.get('tables') or []]:
        p = target / f'{tname}.wal'
        wal_sizes[tname] = p.stat().st_size if p.is_file() else 0
    non_zero = {t: n for t, n in wal_sizes.items() if n}
    if non_zero:
        problems.append(f'WAL 未归零: {non_zero}')

    if problems:
        raise SnapshotIntegrityError(
            '恢复后自检失败:\n  - ' + '\n  - '.join(problems[:20]))
    return {'tables': tables_report, 'wal_sizes': wal_sizes,
            'wal_zero': True, 'tables_verified': len(tables_report)}


def restore_snapshot(snapshot_dir, target_data_dir, *, force: bool = False,
                     server_ports=None, check_servers: bool = True,
                     verify: bool = True, version: str = None,
                     allow_version_mismatch: bool = False,
                     quiet: bool = False) -> dict:
    """从快照恢复到目标数据目录（先校验，再拷贝，再自检）。"""
    warnings = []
    t_start = time.perf_counter()
    snapshot_dir = Path(snapshot_dir).resolve()
    target = Path(target_data_dir).resolve()
    if not snapshot_dir.is_dir():
        raise SnapshotError(f'快照目录不存在: {snapshot_dir}')
    manifest = read_manifest(snapshot_dir)
    version_check = validate_manifest(manifest, version,
                                      allow_version_mismatch)
    warnings.extend(version_check.get('warnings') or [])
    if check_servers:
        _refuse_if_running(server_ports, force, warnings, '恢复')
    if target == snapshot_dir or snapshot_dir in target.parents or \
            target in snapshot_dir.parents:
        raise SnapshotError('目标数据目录与快照目录不能互相嵌套')

    # 先做完整校验，失败时绝不动目标目录
    problems = _verify_snapshot_files(snapshot_dir, manifest)
    if problems:
        raise SnapshotIntegrityError(
            '快照文件校验失败（未触碰目标目录）:\n  - '
            + '\n  - '.join(problems[:20]))

    replaced_to = None
    if target.exists():
        entries = list(target.iterdir())
        if entries:
            if not force:
                raise SnapshotError(
                    f'目标目录已存在且非空: {target}（--force 会将旧目录改名保留'
                    f'后恢复，不会删除旧数据）')
            replaced_to = target.parent / ('%s.replaced-%s'
                                           % (target.name,
                                              time.strftime('%Y%m%d-%H%M%S')))
            os.replace(target, replaced_to)
            warnings.append(f'目标目录非空：旧目录已改名为 {replaced_to}')
    target.mkdir(parents=True, exist_ok=True)

    data_src = snapshot_dir / DATA_SUBDIR
    for record in manifest.get('files') or []:
        src = snapshot_dir / str(record.get('path'))
        dst = target / str(record.get('name'))
        shutil.copy2(src, dst)
    # 拷贝后再校验一次（防止拷贝过程中被篡改/截断）
    problems = []
    for record in manifest.get('files') or []:
        dst = target / str(record.get('name'))
        if not dst.is_file():
            problems.append(f'拷贝后缺少: {record.get("name")}')
            continue
        want = record.get('size')
        want = -1 if want is None else int(want)
        if dst.stat().st_size != want:
            problems.append(f'拷贝后大小不符: {record.get("name")}')
        elif sha256_file(dst) != str(record.get('sha256') or ''):
            problems.append(f'拷贝后 SHA256 不符: {record.get("name")}')
    if problems:
        raise SnapshotIntegrityError('恢复拷贝校验失败:\n  - '
                                     + '\n  - '.join(problems[:20]))

    verify_report = None
    if verify:
        verify_report = _verify_restored(target, manifest)
    duration = time.perf_counter() - t_start
    report = {
        'status': 'ok',
        'action': 'restore',
        'snapshot_dir': str(snapshot_dir),
        'target_data_dir': str(target),
        'manifest_path': str(snapshot_dir / MANIFEST_NAME),
        'lightdb_version': manifest.get('lightdb_version'),
        'expected_version': version_check['expected_version'],
        'version_check': version_check,
        'mode': manifest.get('mode'),
        'table_filter': manifest.get('table_filter'),
        'tables_restored': [t['name'] for t in manifest.get('tables') or []],
        'row_total': manifest.get('row_total'),
        'total_files': manifest.get('total_files'),
        'total_bytes': manifest.get('total_bytes'),
        'verified': bool(verify),
        'verification': verify_report,
        'replaced_to': str(replaced_to) if replaced_to else None,
        'warnings': warnings,
        'duration_s': duration,
    }
    if not quiet:
        print('[snapshot] 恢复完成: %s -> %s（%d 表 / %d 行 / %d 文件 / %.2fs）'
              % (snapshot_dir, target, len(report['tables_restored']),
                 manifest.get('row_total') or 0,
                 manifest.get('total_files') or 0, duration))
        if verify_report:
            print('[snapshot] 自检: %d 表行数/关键值/SHOW CREATE/自增/WAL=0 一致'
                  % verify_report['tables_verified'])
        for w in warnings:
            print('[snapshot] ' + w)
    return report


def describe_snapshot(snapshot_dir) -> dict:
    """只读查看快照 manifest（供 CLI/排障用）。"""
    snapshot_dir = Path(snapshot_dir)
    manifest = read_manifest(snapshot_dir)
    return {
        'snapshot_dir': str(snapshot_dir),
        'schema': manifest.get('schema'),
        'format_version': manifest.get('format_version'),
        'lightdb_version': manifest.get('lightdb_version'),
        'created_at': manifest.get('created_at'),
        'mode': manifest.get('mode'),
        'table_filter': manifest.get('table_filter'),
        'online': manifest.get('online'),
        'tables': [{'name': t['name'], 'row_count': t['row_count']}
                   for t in manifest.get('tables') or []],
        'tables_count': manifest.get('tables_count'),
        'row_total': manifest.get('row_total'),
        'total_files': manifest.get('total_files'),
        'total_bytes': manifest.get('total_bytes'),
        'warnings': manifest.get('warnings') or [],
    }
