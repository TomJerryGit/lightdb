"""生产可观测性：慢查询日志 / 运行期指标 / Prometheus 渲染（task-70）

为什么单独成模块
================

* 指标与慢查询是**横切能力**：DB/Executor/网络层都要喂数据，但它们不能
  各自发明一套计数。这里集中定义：
  - ``ServerRuntimeMetrics``：网络/语句级真实计数（连接、QPS、错误码），
    由 `net/server.py` 在真实路径上打点；
  - ``SlowQueryLog``：慢查询环形缓冲 + 可选落盘与轮转，由
    `sql/executor.py` 在每条语句结束后按真实耗时记录；
  - ``render_prometheus``：把上面两者 + DB 的真实 WAL/checkpoint/binlog/
    备份/内存线程数据渲染成 Prometheus 文本格式（`/metrics`）。

设计原则（与仓库既有"不许编造"规范一致）
========================================

1. **数值来自真实采集**：连接数取 `MySQLServer.connection_stats()`，WAL 取
   各表 `wal.size_bytes()`，checkpoint 取 `Database.checkpoint_stats`，
   错误按真实 ERR 包 errno 计数，内存/线程取 psutil/threading 实测值；
2. **取不到的如实说明**：psutil 不可用、备份目录不存在等情况输出 0 并附
   `#` 注释说明来源，不编造一个"看起来正常"的数字；
3. 慢查询阈值/文件/轮转全部用**环境变量**配置（本任务范围不含 config.py，
   不新增 Config 字段），未设置时用下列默认值；
4. SQL 文本落盘前**截断**并对口令类字面量做 `***` 脱敏（慢查询日志不是
   审计：审计按既定契约保留原文，`SHOW AUDIT LOG` 行为不变）。

环境变量
========

慢查询日志：
  ``LIGHTDB_SLOW_QUERY_MS``      阈值毫秒（默认 500）
  ``LIGHTDB_SLOWLOG_ENABLED``    是否落盘（默认 1；0=只进内存环形缓冲）
  ``LIGHTDB_SLOWLOG_FILE``       文件路径（默认 ``<data_dir>/slow.log``）
  ``LIGHTDB_SLOWLOG_MAX_BYTES``  单文件阈值（默认 10MB，超过后轮转）
  ``LIGHTDB_SLOWLOG_KEEP``       保留的历史文件数（默认 3：slow.log.1..3）
  ``LIGHTDB_SLOWLOG_BUFFER``     内存环形缓冲条数（默认 200）
  ``LIGHTDB_SLOWLOG_SQL_MAX``    SQL 落盘/展示截断字符数（默认 500）
  ``LIGHTDB_SLOWLOG_FSYNC``      每条是否 fsync（默认 0；慢查询稀少时可开）
审计轮转（由 ``sql/executor.py::_AuditSink`` 读取）：
  ``LIGHTDB_AUDIT_MAX_BYTES``    审计文件阈值（默认 10MB）
  ``LIGHTDB_AUDIT_MAX_RECORDS``  审计文件条数阈值（默认 100000）
  ``LIGHTDB_AUDIT_KEEP``         保留的历史文件数（默认 5）
指标端点：
  ``LIGHTDB_METRICS_PORT``       端口（默认 0=不启动；>0 时随 MySQLServer 启动）
  ``LIGHTDB_METRICS_HOST``       监听地址（默认 127.0.0.1）
  ``LIGHTDB_METRICS_PREFIX``     指标名前缀（默认 ``lightdb``）

慢查询日志的读取方式
====================

* 进程内/测试：``executor.slowlog.rows()`` / ``executor.slowlog.snapshot()``；
* SQL：``SELECT * FROM __SLOW_QUERY_LOG__``（虚拟表，列：
  ts / user / elapsed_ms / cmd / success / errno / sqlstate / sql）；
* 文件：``slow.log``（JSONL，每条含 ts/user/elapsed_ms/cmd/success/errno/
  sql），轮转文件 ``slow.log.1`` …。

为什么不做 ``SHOW SLOW QUERIES``：parser 不在本任务可改范围；虚拟表 +
文件两种读法已经覆盖同一份数据，语义更明确。
"""
import json
import os
import re
import threading
import time
from collections import OrderedDict, deque

from ..logger import logger


# ==================== 环境变量工具 ====================

def env_int(name: str, default: int, minimum: int = None) -> int:
    raw = os.environ.get(name)
    try:
        val = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        val = default
    if minimum is not None and val < minimum:
        val = minimum
    return val


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ('0', 'false', 'off', 'no', '')


def slow_threshold_ms() -> int:
    """慢查询阈值（毫秒）：``LIGHTDB_SLOW_QUERY_MS``，默认 500"""
    return env_int('LIGHTDB_SLOW_QUERY_MS', 500, minimum=0)


# ==================== SQL 截断 / 脱敏 ====================

#: 口令类字面量：慢查询日志/指标标签里一律替换成 '***'（不泄露凭据）
_PASSWORD_RE = re.compile(
    r"""(IDENTIFIED\s+(?:WITH\s+\w+\s+)?BY\s*"""
    r"""|PASSWORD\s*=\s*"""
    r"""|SET\s+PASSWORD\s+FOR\s+\S+\s*=\s*"""
    r"""|ALTER\s+USER\s+\S+\s+IDENTIFIED\s+BY\s*)"""
    r"""('(?:[^']|'')*'|"(?:[^"]|"")*")""",
    re.IGNORECASE)


def redact_sql(sql: str) -> str:
    """把 SQL 里的口令字面量脱敏（``IDENTIFIED BY '...'`` / ``PASSWORD='...'``）

    慢查询日志会落盘，不能把 ``CREATE USER ... IDENTIFIED BY '明文'``
    原样写进去（审计日志另有既定契约，`SHOW AUDIT LOG` 行为不变）。
    """
    text = str(sql or '')
    try:
        return _PASSWORD_RE.sub(lambda m: m.group(1) + "'***'", text)
    except Exception:                            # pragma: no cover - 防御
        return text


def normalize_sql_label(sql: str, max_len: int = 80) -> str:
    """指标标签用的 SQL 归一化：压空白 + 字面量替换成 ?（降低基数）"""
    text = str(sql or '')
    text = re.sub(r"'(?:[^']|'')*'", '?', text)
    text = re.sub(r'"(?:[^"]|"")*"', '?', text)
    text = re.sub(r'\b\d+\b', '0', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_len]


def _escape_label(value) -> str:
    """Prometheus label value 转义（反斜杠/双引号/换行）"""
    s = str(value if value is not None else '')
    return (s.replace('\\', '\\\\')
             .replace('"', '\\"')
             .replace('\n', '\\n')
             .replace('\r', '\\r'))


# ==================== 慢查询日志 ====================

class SlowQueryLog:
    """慢查询环形缓冲 + 可选落盘（大小轮转、保留 N 个历史文件）

    * 只记录 **elapsed_ms >= 阈值** 的语句（成功与失败都记，失败带 errno）；
    * 内存环形缓冲供 ``SELECT * FROM __SLOW_QUERY_LOG__`` 与指标 top 榜；
    * 落盘为 JSONL，单文件超过 ``max_bytes`` 轮转为 ``slow.log.1``…，
      仅保留 ``keep`` 个历史文件（默认 3）；写盘失败只告警并计数，
      内存记录不丢；
    * 线程安全（RLock）；不持有常驻文件句柄（Windows 上数据目录可删除）。

    崩溃安全说明：每行一次 append+flush（可选 fsync）；轮转用
    ``os.replace`` 从旧到新重命名，任何一步崩溃都不会让已有内容消失，
    最坏情况是历史文件编号有空洞（下次写入继续追加到当前文件）。
    """

    _AGG_MAX = 1000

    def __init__(self, data_dir: str = None, path: str = None,
                 threshold_ms: int = None, enabled: bool = None,
                 max_bytes: int = None, keep: int = None,
                 buffer_size: int = None, sql_max: int = None):
        self.threshold_ms = (slow_threshold_ms() if threshold_ms is None
                             else int(threshold_ms))
        env_path = os.environ.get('LIGHTDB_SLOWLOG_FILE') or ''
        if path is None:
            path = env_path or os.path.join(str(data_dir or '.'), 'slow.log')
        self.path = str(path)
        self.enabled = (env_flag('LIGHTDB_SLOWLOG_ENABLED', True)
                        if enabled is None else bool(enabled))
        self.max_bytes = env_int(
            'LIGHTDB_SLOWLOG_MAX_BYTES', 10 * 1024 * 1024,
            minimum=1024) if max_bytes is None else max(1024, int(max_bytes))
        self.keep = env_int(
            'LIGHTDB_SLOWLOG_KEEP', 3, minimum=1) if keep is None else max(1, int(keep))
        buf = env_int('LIGHTDB_SLOWLOG_BUFFER', 200, minimum=1) \
            if buffer_size is None else max(1, int(buffer_size))
        self.sql_max = env_int('LIGHTDB_SLOWLOG_SQL_MAX', 500, minimum=32) \
            if sql_max is None else max(32, int(sql_max))
        self.fsync = env_flag('LIGHTDB_SLOWLOG_FSYNC', False)

        self._lock = threading.RLock()
        self.records = deque(maxlen=buf)
        #: 归一化 SQL -> {count,total_ms,max_ms,last_ts,cmd}
        self._agg = OrderedDict()
        self.total = 0
        self.max_elapsed_ms = 0.0
        self.write_errors = 0
        self.rotations = 0
        self._file_bytes = 0
        if self.enabled:
            try:
                if os.path.exists(self.path):
                    self._file_bytes = os.path.getsize(self.path)
            except OSError:                       # pragma: no cover - 防御
                self._file_bytes = 0

    # ---------- 记录 ----------
    def should_record(self, elapsed_ms: float) -> bool:
        return float(elapsed_ms) >= self.threshold_ms

    def record(self, user: str, sql: str, elapsed_ms: float,
               cmd: str = '', success: bool = True, errno=None,
               sqlstate: str = None, ts: float = None) -> bool:
        """记录一条慢查询；返回是否真的记录（未达阈值/异常时 False）"""
        elapsed_ms = float(elapsed_ms)
        if not self.should_record(elapsed_ms):
            return False
        entry = {
            'ts': float(ts if ts is not None else time.time()),
            'user': str(user or ''),
            'elapsed_ms': round(elapsed_ms, 3),
            'cmd': str(cmd or '')[:40],
            'success': bool(success),
            'errno': int(errno) if isinstance(errno, int) else errno,
            'sql': redact_sql(str(sql or ''))[:self.sql_max],
        }
        line = None
        with self._lock:
            self.records.append(entry)
            self.total += 1
            if elapsed_ms > self.max_elapsed_ms:
                self.max_elapsed_ms = elapsed_ms
            key = normalize_sql_label(entry['sql'])
            agg = self._agg.get(key)
            if agg is None:
                agg = {'count': 0, 'total_ms': 0.0, 'max_ms': 0.0,
                       'last_ts': 0.0, 'cmd': entry['cmd']}
                self._agg[key] = agg
                while len(self._agg) > self._AGG_MAX:
                    self._agg.popitem(last=False)
            agg['count'] += 1
            agg['total_ms'] += elapsed_ms
            agg['max_ms'] = max(agg['max_ms'], elapsed_ms)
            agg['last_ts'] = entry['ts']
            agg['cmd'] = entry['cmd']
            if self.enabled:
                line = json.dumps(entry, ensure_ascii=False) + '\n'
        if line is not None:
            self._append(line)
        return True

    def _append(self, line: str):
        with self._lock:
            try:
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(self.path, 'a', encoding='utf-8') as f:
                    f.write(line)
                    f.flush()
                    if self.fsync:
                        os.fsync(f.fileno())
                self._file_bytes += len(line.encode('utf-8'))
            except Exception as e:                # pragma: no cover - 磁盘故障
                self.write_errors += 1
                logger.warn(f"慢查询日志落盘失败（内存记录保留）: {e}")
                return
            if self._file_bytes >= self.max_bytes:
                self._rotate_locked()

    # ---------- 轮转 ----------
    def _rotate_locked(self):
        """slow.log -> slow.log.1 -> … -> slow.log.N（超出 keep 的删除）

        只在拿锁时调用；任何一步失败都保留当前文件（不丢记录）。
        """
        try:
            oldest = f'{self.path}.{self.keep}'
            if os.path.exists(oldest):
                os.remove(oldest)
            for i in range(self.keep - 1, 0, -1):
                src = f'{self.path}.{i}'
                dst = f'{self.path}.{i + 1}'
                if os.path.exists(src):
                    os.replace(src, dst)
            if os.path.exists(self.path):
                os.replace(self.path, f'{self.path}.1')
            # 轮转后立刻建回一个空的当前文件：运维/测试看到的"当前日志"
            # 始终存在（下一次写入直接追加，不需要等第一条记录才创建）
            try:
                with open(self.path, 'a', encoding='utf-8'):
                    pass
            except OSError as e:                  # pragma: no cover - 防御
                logger.warn(f"轮转后创建空当前文件失败: {e}")
            self._file_bytes = 0
            self.rotations += 1
        except Exception as e:                    # pragma: no cover - 磁盘故障
            self.write_errors += 1
            logger.warn(f"慢查询日志轮转失败（继续写当前文件）: {e}")

    # ---------- 读取 ----------
    def rows(self, limit: int = None):
        """(ts 文本, user, elapsed_ms, cmd, success, errno, sql) 列表"""
        with self._lock:
            recs = list(self.records)
        if limit is not None:
            recs = recs[-int(limit):]
        out = []
        for r in recs:
            try:
                tstr = time.strftime('%Y-%m-%d %H:%M:%S',
                                     time.localtime(float(r['ts'])))
            except (TypeError, ValueError):        # pragma: no cover - 防御
                tstr = str(r.get('ts', ''))
            out.append((tstr, r.get('user', ''), r.get('elapsed_ms', 0.0),
                        r.get('cmd', ''), 1 if r.get('success') else 0,
                        r.get('errno'), r.get('sql', '')))
        return out

    def recent(self, limit: int = 50):
        with self._lock:
            return list(self.records)[-int(limit):]

    def snapshot(self) -> dict:
        """指标用的快照（真实计数 + top 榜；无数据时返回空/0）"""
        with self._lock:
            total = self.total
            max_ms = self.max_elapsed_ms
            top = sorted(self._agg.items(),
                         key=lambda kv: (kv[1]['count'], kv[1]['max_ms']),
                         reverse=True)[:10]
            file_bytes = self._file_bytes
            rotations = self.rotations
            write_errors = self.write_errors
            recent = len(self.records)
        return {
            'threshold_ms': self.threshold_ms,
            'total': total,
            'max_elapsed_ms': max_ms,
            'recent': recent,
            'top': [
                {'sql': key, 'count': v['count'],
                 'avg_ms': (v['total_ms'] / v['count']) if v['count'] else 0.0,
                 'max_ms': v['max_ms'], 'cmd': v['cmd'],
                 'last_ts': v['last_ts']}
                for key, v in top
            ],
            'file': self.path,
            'file_bytes': file_bytes,
            'max_bytes': self.max_bytes,
            'keep': self.keep,
            'rotations': rotations,
            'write_errors': write_errors,
            'enabled': self.enabled,
        }

    def close(self):
        """慢查询日志不持有常驻句柄；close 仅清空内存缓冲之外的状态"""
        return


# ==================== 运行期指标（网络/语句/错误） ====================

class ServerRuntimeMetrics:
    """MySQL 服务端真实计数：连接、语句、错误码，以及 QPS 滑动窗口

    打点位置（都在真实路径上）：
    * ``on_connection_accepted``：`MySQLServer.verify_request` 准入通过后；
    * ``on_connection_rejected``/``on_auth_failure``：准入拒绝 / 登录失败；
    * ``on_query``：`MySQLHandler._dispatch` 收到 COM_QUERY / COM_STMT_EXECUTE；
    * ``on_error``：`MySQLHandler._send_packet` 发出 ERR 包时（解析真实 errno），
      因此覆盖鉴权/权限/执行/语法所有错误分支。
    """

    def __init__(self):
        self.start_time = time.time()
        self._lock = threading.Lock()
        self.connections_total = 0
        self.connections_rejected = 0
        self.auth_failures = 0
        self.queries_total = 0
        self.errors_total = 0
        self.errors_by_code = {}
        self._qps_buckets = {}          # 秒级时间戳 -> 语句数
        self._qps_last_prune = 0

    # ---------- 打点 ----------
    def on_connection_accepted(self):
        with self._lock:
            self.connections_total += 1

    def on_connection_rejected(self):
        with self._lock:
            self.connections_rejected += 1

    def on_auth_failure(self):
        with self._lock:
            self.auth_failures += 1

    def on_query(self, count: int = 1):
        now = int(time.time())
        with self._lock:
            self.queries_total += int(count)
            self._qps_buckets[now] = self._qps_buckets.get(now, 0) + int(count)
            if now - self._qps_last_prune >= 5:
                self._qps_last_prune = now
                cutoff = now - 300
                for ts in [t for t in self._qps_buckets if t < cutoff]:
                    self._qps_buckets.pop(ts, None)

    def on_error(self, code):
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = 1105
        with self._lock:
            self.errors_total += 1
            self.errors_by_code[code] = self.errors_by_code.get(code, 0) + 1

    # ---------- 读取 ----------
    def qps(self, window_seconds: int = 5) -> float:
        now = int(time.time())
        start = now - max(1, int(window_seconds)) + 1
        with self._lock:
            n = sum(v for ts, v in self._qps_buckets.items() if ts >= start)
        return round(n / float(max(1, int(window_seconds))), 3)

    def uptime_seconds(self) -> float:
        return round(time.time() - self.start_time, 3)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                'connections_total': self.connections_total,
                'connections_rejected': self.connections_rejected,
                'auth_failures': self.auth_failures,
                'queries_total': self.queries_total,
                'errors_total': self.errors_total,
                'errors_by_code': dict(self.errors_by_code),
                'uptime_seconds': round(time.time() - self.start_time, 3),
            }


# ==================== Prometheus 渲染 ====================

def _metric(lines, name, value, mtype='gauge', help_text=None,
            labels=None):
    """追加一条 Prometheus 指标行（TYPE/HELP 由 `_type_line` 单独输出）"""
    if labels:
        label_text = ','.join(
            f'{k}="{_escape_label(v)}"' for k, v in labels.items())
        lines.append(f'{name}{{{label_text}}} {value}')
    else:
        lines.append(f'{name} {value}')


def _type_line(lines, name, mtype, help_text):
    lines.append(f'# HELP {name} {help_text}')
    lines.append(f'# TYPE {name} {mtype}')


def _backup_stats(data_dir: str):
    """备份/快照产物真实统计（扫描备份目录；不存在则 0 + 说明）

    来源：``LIGHTDB_BACKUP_DIR`` > ``<data_dir>/../backups``（仓库/部署默认
    布局）。只做只读扫描，不创建目录；统计 ``*.sql/*.dump`` 文件与
    ``physical_*`` / ``snapshot_*`` / ``snap_*`` 目录的真实大小。
    """
    env_dir = os.environ.get('LIGHTDB_BACKUP_DIR') or ''
    candidates = []
    if env_dir:
        candidates.append(env_dir)
    if data_dir:
        candidates.append(os.path.join(os.path.dirname(
            os.path.abspath(str(data_dir))), 'backups'))
        candidates.append(os.path.join(str(data_dir), 'backups'))
    for cand in candidates:
        if not cand or not os.path.isdir(cand):
            continue
        files = 0
        total = 0
        dirs = 0
        try:
            for entry in os.scandir(cand):
                try:
                    if entry.is_file():
                        if entry.name.endswith(('.sql', '.dump', '.json')):
                            files += 1
                            total += entry.stat().st_size
                    elif entry.is_dir() and (
                            entry.name.startswith(('physical_', 'snapshot_',
                                                   'snap_', 'backup_'))):
                        dirs += 1
                        size = 0
                        for root, _d, names in os.walk(entry.path):
                            for n in names:
                                try:
                                    size += os.path.getsize(
                                        os.path.join(root, n))
                                except OSError:
                                    continue
                        total += size
                except OSError:
                    continue
        except OSError:
            continue
        return {'dir': cand, 'files': files, 'dirs': dirs,
                'bytes': total, 'found': True}
    return {'dir': candidates[0] if candidates else '',
            'files': 0, 'dirs': 0, 'bytes': 0, 'found': False}


def _wal_stats(db):
    """各表 WAL 真实字节数 + 总量（表/WAL 不可用时返回空）"""
    out = {}
    try:
        for name, tbl in list(getattr(db, 'tables', {}).items()):
            try:
                out[str(name)] = int(tbl.wal.size_bytes())
            except Exception:                    # pragma: no cover - 防御
                continue
    except Exception:                            # pragma: no cover - 防御
        return {}
    return out


def _replica_status(server):
    """增量复制原型的真实状态快照（未配置/非原型时返回 None）

    数据源：目标库 `Database.replication_status()` → `ReplicaPrototype.
    show_status()`（与状态文件/`SHOW SLAVE STATUS` 同一份快照）。
    """
    db = getattr(server, 'db', None)
    getter = getattr(db, 'replication_status', None) if db is not None else None
    if not callable(getter):
        return None
    try:
        status = getter()
    except Exception:                            # pragma: no cover - 防御
        return None
    if isinstance(status, dict) and status.get('Replica_Is_Prototype'):
        return status
    return None


#: `/metrics` 的复制度量名 → SHOW 快照键/类型（无数据统一输出 0）
_REPLICA_METRICS = (
    ('lightdb_replica_lag_lsn', 'Replica_Lag_LSN', 'gauge',
     'Master max LSN minus applied LSN (bytes behind, 0 when caught up)'),
    ('lightdb_replica_lag_seconds', 'Replica_Lag_Seconds', 'gauge',
     'Seconds since last successful apply while behind (0 when caught up)'),
    ('lightdb_replica_events_behind', 'Replica_Events_Behind', 'gauge',
     'Events not yet applied (real COUNT on master)'),
    ('lightdb_replica_applied_lsn', 'Replica_Applied_LSN', 'gauge',
     'Last applied global LSN (persisted checkpoint)'),
    ('lightdb_replica_master_lsn', 'Replica_Master_LSN', 'gauge',
     'Master max readable global LSN'),
    ('lightdb_replica_applied_events_total', 'Replica_Applied_Events',
     'counter', 'Events applied since replication state was created'),
    ('lightdb_replica_apply_errors_total', 'Replica_Apply_Errors',
     'counter', 'Apply failures (checkpoint retained, retried)'),
    ('lightdb_replica_last_apply_ts', 'Replica_Last_Apply_Ts', 'gauge',
     'Unix timestamp of last successful apply (0 = no data)'),
    ('lightdb_replica_caught_up', 'Replica_Caught_Up', 'gauge',
     '1 = fully caught up, 0 = behind/not running'),
    ('lightdb_replica_retries_total', 'Replica_Retries', 'counter',
     'Follow-mode retries (backoff) since process start'),
)


def _render_replica_metrics(lines, prefix, status):
    """渲染增量复制指标（无数据输出 0 + 注释，不编造）"""
    for name, key, mtype, help_text in _REPLICA_METRICS:
        _type_line(lines, name, mtype, help_text)
        try:
            value = status.get(key, 0) if status else 0
            value = float(value) if mtype == 'gauge' else int(value or 0)
        except Exception:                        # pragma: no cover - 防御
            value = 0
        lines.append(f'{name} {value}')
    if status:
        lines.append(
            f'# 增量复制: mode={status.get("Replica_Mode")} '
            f'following={status.get("Replica_Following")} '
            f'last_error={str(status.get("Last_Error") or "")[:120]!r}')
    else:
        lines.append(
            '# 增量复制未配置/无数据：以上为 0'
            '（未注册 ReplicaPrototype 或状态不可读）')



def render_prometheus(server, prefix: str = None) -> str:
    """把一台 MySQLServer 的真实运行数据渲染成 Prometheus text 格式

    ``server`` 需具备：``runtime_metrics`` / ``db`` / ``executor`` /
    ``start_time`` / ``connection_stats()``（见 `net/server.py`）。任何
    子项取不到都输出 0 并附 ``#`` 说明，不静默省略、不编造。
    """
    prefix = prefix or os.environ.get('LIGHTDB_METRICS_PREFIX') or 'lightdb'
    lines = []
    lines.append(f'# LightDB production metrics (prefix={prefix})')
    # ---------- 服务存在性 ----------
    _type_line(lines, f'{prefix}_up', 'gauge', 'Server is up (1)')
    lines.append(f'{prefix}_up 1')

    stats = getattr(server, 'runtime_metrics', None)
    snap = stats.snapshot() if stats is not None else {}
    conn = {}
    try:
        conn = server.connection_stats()
    except Exception:                            # pragma: no cover - 防御
        conn = {}

    # ---------- 连接 ----------
    _type_line(lines, f'{prefix}_connections_active', 'gauge',
               'Currently admitted connections')
    lines.append(f'{prefix}_connections_active {int(conn.get("active_connections", 0))}')
    _type_line(lines, f'{prefix}_connections_total',
               'counter', 'Accepted connections since start')
    lines.append(f'{prefix}_connections_total {int(snap.get("connections_total", 0))}')
    _type_line(lines, f'{prefix}_connections_rejected_total', 'counter',
               'Connections rejected by admission (count/whitelist)')
    lines.append(
        f'{prefix}_connections_rejected_total '
        f'{int(snap.get("connections_rejected", 0))}')
    _type_line(lines, f'{prefix}_auth_failures_total', 'counter',
               'Failed login attempts')
    lines.append(
        f'{prefix}_auth_failures_total {int(snap.get("auth_failures", 0))}')
    if conn.get('active_by_ip'):
        _type_line(lines, f'{prefix}_connections_active_by_ip', 'gauge',
                   'Active connections per client IP')
        for ip, n in sorted(conn['active_by_ip'].items()):
            _metric(lines, f'{prefix}_connections_active_by_ip', n,
                    labels={'ip': ip})

    # ---------- 语句 / QPS / 错误 ----------
    _type_line(lines, f'{prefix}_queries_total', 'counter',
               'Statements received (COM_QUERY + COM_STMT_EXECUTE)')
    lines.append(f'{prefix}_queries_total {int(snap.get("queries_total", 0))}')
    _type_line(lines, f'{prefix}_errors_total', 'counter',
               'Error packets sent (all error paths, counted by errno)')
    lines.append(f'{prefix}_errors_total {int(snap.get("errors_total", 0))}')
    _type_line(lines, f'{prefix}_errors_by_code_total', 'counter',
               'Error packets by MySQL errno')
    errors_by_code = snap.get('errors_by_code') or {}
    if errors_by_code:
        for code, n in sorted(errors_by_code.items(),
                              key=lambda kv: (-kv[1], kv[0])):
            _metric(lines, f'{prefix}_errors_by_code_total', n,
                    labels={'errno': code})
    else:
        lines.append(f'# 无错误记录（errors_by_code 为空）')
    # QPS 滑动窗口（真实按秒桶统计）
    _type_line(lines, f'{prefix}_qps', 'gauge',
               'Queries per second over a sliding window')
    for window in (5, 60):
        try:
            q = stats.qps(window) if stats is not None else 0.0
        except Exception:                        # pragma: no cover - 防御
            q = 0.0
        _metric(lines, f'{prefix}_qps', q, labels={'window': f'{window}s'})
    _type_line(lines, f'{prefix}_uptime_seconds', 'gauge',
               'Server uptime in seconds')
    lines.append(
        f'{prefix}_uptime_seconds {float(snap.get("uptime_seconds", 0.0))}')

    # ---------- 慢查询 ----------
    executor = getattr(server, 'executor', None)
    db = getattr(server, 'db', None)
    slow = getattr(executor, 'slowlog', None)
    if slow is None and db is not None:
        slow = getattr(db, 'slowlog', None)
    slow_snap = slow.snapshot() if slow is not None else {}
    _type_line(lines, f'{prefix}_slow_queries', 'gauge',
               'Slow queries recorded (ring buffer + file)')
    lines.append(f'{prefix}_slow_queries {int(slow_snap.get("total", 0))}')
    _type_line(lines, f'{prefix}_slow_query_max_ms', 'gauge',
               'Max slow-query elapsed time in ms')
    lines.append(
        f'{prefix}_slow_query_max_ms {float(slow_snap.get("max_elapsed_ms", 0.0))}')
    _type_line(lines, f'{prefix}_slow_query_threshold_ms', 'gauge',
               'Configured slow-query threshold in ms')
    lines.append(
        f'{prefix}_slow_query_threshold_ms {int(slow_snap.get("threshold_ms", 0))}')
    _type_line(lines, f'{prefix}_slow_query_top_count', 'gauge',
               'Top slow queries by occurrence (normalized SQL prefix)')
    top = slow_snap.get('top') or []
    if top:
        for rank, item in enumerate(top, start=1):
            _metric(lines, f'{prefix}_slow_query_top_count', item['count'],
                    labels={'rank': rank, 'cmd': item['cmd'],
                            'sql': item['sql']})
        _type_line(lines, f'{prefix}_slow_query_top_max_ms', 'gauge',
                   'Max elapsed time of each top slow query')
        for rank, item in enumerate(top, start=1):
            _metric(lines, f'{prefix}_slow_query_top_max_ms',
                    round(float(item['max_ms']), 3),
                    labels={'rank': rank, 'cmd': item['cmd'],
                            'sql': item['sql']})
    else:
        lines.append(f'# 无慢查询（阈值 {slow_snap.get("threshold_ms", "?")}ms）')
    if slow is not None:
        _type_line(lines, f'{prefix}_slowlog_file_bytes', 'gauge',
                   'Current slow log file size in bytes')
        lines.append(
            f'{prefix}_slowlog_file_bytes {int(slow_snap.get("file_bytes", 0))}')
        _type_line(lines, f'{prefix}_slowlog_rotations_total', 'counter',
                   'Slow log rotations performed')
        lines.append(
            f'{prefix}_slowlog_rotations_total '
            f'{int(slow_snap.get("rotations", 0))}')
        _type_line(lines, f'{prefix}_slowlog_write_errors_total', 'counter',
                   'Slow log write/rotate failures (records kept in memory)')
        lines.append(
            f'{prefix}_slowlog_write_errors_total '
            f'{int(slow_snap.get("write_errors", 0))}')

    # ---------- WAL / checkpoint ----------
    db = getattr(server, 'db', None)
    wal = _wal_stats(db) if db is not None else {}
    _type_line(lines, f'{prefix}_wal_bytes', 'gauge',
               'Current per-table WAL size in bytes')
    if wal:
        for name, size in sorted(wal.items()):
            _metric(lines, f'{prefix}_wal_bytes', size,
                    labels={'table': name})
    else:
        lines.append(f'# 无表/WAL 不可用（wal_bytes 为空）')
    _type_line(lines, f'{prefix}_wal_bytes_total', 'gauge',
               'Total WAL size across tables')
    lines.append(f'{prefix}_wal_bytes_total {sum(wal.values())}')
    cp = getattr(db, 'checkpoint_stats', None) or {}
    _type_line(lines, f'{prefix}_checkpoints_total', 'counter',
               'Database.checkpoint() calls performed')
    lines.append(f'{prefix}_checkpoints_total {int(cp.get("calls", 0))}')
    _type_line(lines, f'{prefix}_checkpoint_tables_total', 'counter',
               'Tables checkpointed (WAL segments retired)')
    lines.append(
        f'{prefix}_checkpoint_tables_total {int(cp.get("tables", 0))}')
    _type_line(lines, f'{prefix}_tables', 'gauge',
               'Number of tables in the database')
    try:
        n_tables = len(getattr(db, 'tables', {}) or {})
    except Exception:                            # pragma: no cover - 防御
        n_tables = 0
    lines.append(f'{prefix}_tables {n_tables}')

    # ---------- binlog（存在才输出；不因 scrape 而创建文件） ----------
    binlog = getattr(db, '_binlog_log', None) if db is not None else None
    _type_line(lines, f'{prefix}_binlog_events', 'gauge',
               'Data events written to the append-only binlog')
    if binlog is not None:
        try:
            lines.append(
                f'{prefix}_binlog_events {int(binlog.data_event_count)}')
            _type_line(lines, f'{prefix}_binlog_position_bytes', 'gauge',
                       'Current binlog file position in bytes')
            lines.append(
                f'{prefix}_binlog_position_bytes {int(binlog.current_pos)}')
        except Exception:                        # pragma: no cover - 防御
            lines.append(f'{prefix}_binlog_events 0')
            lines.append(f'# binlog 状态读取失败（按 0 输出）')
    else:
        lines.append(f'{prefix}_binlog_events 0')
        lines.append('# binlog 未启用/未创建（LIGHTDB_BINLOG_ENABLED=0 或尚无写入）')

    # ---------- 备份/快照 ----------
    data_dir = str(getattr(db, 'data_dir', '') or '')
    backup = _backup_stats(data_dir) if data_dir else {
        'dir': '', 'files': 0, 'dirs': 0, 'bytes': 0, 'found': False}
    _type_line(lines, f'{prefix}_backup_files', 'gauge',
               'Backup dump files found in backup dir')
    lines.append(f'{prefix}_backup_files {int(backup["files"])}')
    _type_line(lines, f'{prefix}_backup_bytes', 'gauge',
               'Total backup/snapshot bytes found')
    lines.append(f'{prefix}_backup_bytes {int(backup["bytes"])}')
    _type_line(lines, f'{prefix}_snapshot_dirs', 'gauge',
               'Snapshot/physical-backup directories found')
    lines.append(f'{prefix}_snapshot_dirs {int(backup["dirs"])}')
    if not backup['found']:
        lines.append(
            f'# 备份目录不存在（{backup["dir"] or "(未配置)"}）；'
            f'LIGHTDB_BACKUP_DIR 可指定。以上为 0 表示"未找到产物"，不是采集失败')

    # ---------- 连接池（task-71；真实计数；未启用时 0 + 注释） ----------
    pool = getattr(server, 'pool', None)
    _type_line(lines, f'{prefix}_pool_workers', 'gauge',
               'Connection-pool worker threads by state')
    if pool is not None:
        try:
            ps = pool.snapshot()
        except Exception as e:                   # pragma: no cover - 防御
            ps = {}
            lines.append(f'# 连接池指标读取失败: {e}')
        for state, key in (('busy', 'workers_busy'),
                           ('idle', 'workers_idle'),
                           ('total', 'workers_total')):
            _metric(lines, f'{prefix}_pool_workers', ps.get(key, 0),
                    labels={'state': state})
        for name, key, mtype in (
                (f'{prefix}_pool_workers_created_total', 'workers_created',
                 'counter'),
                (f'{prefix}_pool_assignments_total', 'assignments',
                 'counter'),
                (f'{prefix}_pool_reused_total', 'reused', 'counter'),
                (f'{prefix}_pool_rejected_total', 'rejected', 'counter'),
                (f'{prefix}_pool_queue_timeouts_total', 'queue_timeouts',
                 'counter'),
                (f'{prefix}_pool_idle_shrinks_total', 'idle_shrinks',
                 'counter'),
                (f'{prefix}_pool_dead_workers_total', 'dead_workers',
                 'counter'),
                (f'{prefix}_pool_jobs_queued', 'jobs_queued', 'gauge'),
                (f'{prefix}_pool_jobs_pending_accept',
                 'jobs_pending_accept', 'gauge'),
                (f'{prefix}_pool_long_running', 'long_running', 'gauge'),
        ):
            _type_line(lines, name, mtype, f'Connection pool metric {key}')
            lines.append(f'{name} {ps.get(key, 0)}')
        _type_line(lines, f'{prefix}_pool_max_workers', 'gauge',
                   'Configured pool max worker threads')
        lines.append(
            f'{prefix}_pool_max_workers {ps.get("max_workers", 0)}')
        lines.append(
            f'# 连接池策略: overflow={ps.get("overflow")} '
            f'min_idle={ps.get("min_idle")} '
            f'idle_timeout={ps.get("idle_timeout")}s '
            f'queue_timeout={ps.get("queue_timeout")}s')
    else:
        for state in ('busy', 'idle', 'total'):
            _metric(lines, f'{prefix}_pool_workers', 0,
                    labels={'state': state})
        lines.append('# 连接池未启用（LIGHTDB_POOL_ENABLED=0）：以上为 0')

    # ---------- 增量复制（task-81；无数据输出 0 + 注释） ----------
    _render_replica_metrics(lines, prefix, _replica_status(server))

    # ---------- 内存 / 线程 ----------
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        rss = int(proc.memory_info().rss)
        _type_line(lines, f'{prefix}_process_memory_rss_bytes', 'gauge',
                   'Process RSS in bytes (psutil)')
        lines.append(f'{prefix}_process_memory_rss_bytes {rss}')
        threads_ps = int(proc.num_threads())
        _type_line(lines, f'{prefix}_process_threads', 'gauge',
                   'Process thread count (psutil)')
        lines.append(f'{prefix}_process_threads {threads_ps}')
    except Exception as e:                       # pragma: no cover - 环境差异
        lines.append(f'# psutil 不可用（{type(e).__name__}: {e}）；'
                     f'memory/threads 用 threading 兜底')
        _type_line(lines, f'{prefix}_process_threads', 'gauge',
                   'Process thread count (threading fallback)')
        lines.append(
            f'{prefix}_process_threads {threading.active_count()}')
        _type_line(lines, f'{prefix}_process_memory_rss_bytes', 'gauge',
                   'Process RSS in bytes (unavailable -> 0)')
        lines.append(f'{prefix}_process_memory_rss_bytes 0')

    return '\n'.join(lines) + '\n'


def metrics_snapshot_json(server) -> dict:
    """``/metrics?format=json`` 用的结构化快照（测试/排障用）"""
    stats = getattr(server, 'runtime_metrics', None)
    executor = getattr(server, 'executor', None)
    slow = getattr(executor, 'slowlog', None)
    db = getattr(server, 'db', None)
    out = {
        'runtime': stats.snapshot() if stats is not None else {},
        'connections': {},
        'slow': slow.snapshot() if slow is not None else {},
        'wal': _wal_stats(db) if db is not None else {},
        'checkpoint': getattr(db, 'checkpoint_stats', {}) if db is not None else {},
        'tables': len(getattr(db, 'tables', {}) or {}) if db is not None else 0,
    }
    try:
        out['connections'] = server.connection_stats()
    except Exception:                            # pragma: no cover - 防御
        pass
    pool = getattr(server, 'pool', None)
    out['pool'] = pool.snapshot() if pool is not None else None
    replica = _replica_status(server)
    out['replica'] = ({
        'present': True,
        'lag_lsn': replica.get('Replica_Lag_LSN', 0),
        'lag_seconds': replica.get('Replica_Lag_Seconds', 0),
        'events_behind': replica.get('Replica_Events_Behind', 0),
        'applied_lsn': replica.get('Replica_Applied_LSN', 0),
        'master_lsn': replica.get('Replica_Master_LSN', 0),
        'applied_events': replica.get('Replica_Applied_Events', 0),
        'apply_errors': replica.get('Replica_Apply_Errors', 0),
        'last_apply_ts': replica.get('Replica_Last_Apply_Ts', 0),
        'caught_up': bool(replica.get('Replica_Caught_Up', 0)),
        'following': bool(replica.get('Replica_Following', 0)),
        'last_error': replica.get('Last_Error', ''),
    } if replica else {
        'present': False, 'lag_lsn': 0, 'lag_seconds': 0.0,
        'events_behind': 0, 'applied_lsn': 0, 'master_lsn': 0,
        'applied_events': 0, 'apply_errors': 0, 'last_apply_ts': 0,
        'caught_up': True, 'following': False, 'last_error': '',
    })
    return out
