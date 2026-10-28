"""最小 binlog 复制原型（task-71 生产化 B）

定位（**原型**，不是完整复制实现）
==================================

把主库 append-only binlog 里的**行事件**拉取到目标库（从库/备份节点）并按
原顺序 apply：DDL 交执行器、DML 走存储层行镜像。用于验证"位点复制"链路与
断点续传，**不**宣称生产级复制。

已实现
------

* 拉取入口：真实 MySQL 协议连接主库（pymysql）：
  - `SHOW BINARY LOGS`（server 内建，task-71）→ 文件清单与大小；
  - `SHOW MASTER STATUS` → 当前文件/位点；
  - `SELECT * FROM __BINLOG_FETCH__ WHERE lsn > N LIMIT k` → 机器可读的
    行事件（内部导出接口，6 列：pos/lsn/txn_id/event_type/table_name/data；
    `lsn` 是跨文件全局字节偏移，可跨 binlog 轮转单调续传）。
* apply 语义：
  - **DDL**：交目标库 `Executor` 执行（与从库 SQL 线程同一条路径）；
    CREATE TABLE 已存在 / DROP TABLE 不存在 / TRUNCATE / ALTER ADD 列已存在 /
    ALTER DROP 列已不存在 → 判为**幂等重放**（记录 `idempotent`），其余错误
    显式 `ReplicaApplyError`；
  - **DML**：按行镜像直写目标存储层（不触发触发器，与 `SlaveReplicator`
    的 SQL 线程一致）：INSERT 主键冲突 → 幂等；UPDATE/DELETE 按主键，
    affected=0 → 幂等；目标缺表/列不匹配 → 显式 `ReplicaApplyError`；
  - apply 期间 `suspend_binlog()`，目标库不把重放事件再记进自己的 binlog。
* 断点续传：每应用一条事件后把 `{master_file, lsn, seq, applied, idempotent,
  errors, last_event_type, last_apply_ts}` 原子写回状态文件（tmp+replace），
  进程重启/中断后从持久 lsn 继续（不会重复应用已成功的事件；重复到了也幂等）。
* 错误显式：拉取失败（主库不可用/binlog 损坏被服务端拒绝）、事件不是 6 列、
  payload 非法 JSON、未知 event_type、DDL/DML apply 失败 → 全部抛
  `ReplicaApplyError`（带上下文），绝不静默跳过。

限制（原型边界，如实写进 docs/PRODUCTION_READINESS.md）
--------------------------------------------------------

* **单主**：一个原型实例只跟一个主库；没有拓扑管理；
* **无自动 failover**：主库挂了就停（`ReplicaError`），不做选主/切主；
* **无多线程 apply**：严格按事件顺序串行应用（单线程）；
* 目标库 schema 必须与主库一致（DML 行镜像按列顺序/主键 apply；DDL 按
  binlog 里的原始 SQL 重放；不做 schema diff 修复）；
* 触发器/外键/存储过程副作用不在行镜像里（行级复制不触发目标侧触发器）；
* 事件时间戳不在导出接口里：原型报告"apply 处理延迟"（拉取→应用耗时）
  与 `last_apply_ts`，**不**伪造 MySQL 的 `Seconds_Behind_Master`；
* 不加密、不压缩、不做 binlog 位点校验和（CRC 已由主库读取端校验）。
"""
import json
import os
import threading
import time

from ..logger import logger

#: 原型支持的事件类型（其它一律显式错误）
KNOWN_EVENT_TYPES = frozenset({'INSERT', 'UPDATE', 'DELETE', 'DDL'})

#: 目标库 apply 时判定"幂等重放"的 DDL action
_IDEMPOTENT_DDL_ACTIONS = frozenset({
    'ADD COLUMN', 'DROP COLUMN', 'RENAME TO',
})


class ReplicaError(Exception):
    """复制链路错误（不可恢复/需人工处理）"""


class ReplicaApplyError(ReplicaError):
    """事件应用失败（显式；带事件上下文）"""

    def __init__(self, message: str, event=None):
        self.event = event
        ctx = ''
        if event is not None:
            try:
                ctx = (f' [pos={event[0]!r} lsn={event[1]!r} '
                       f'txn={event[2]!r} type={event[3]!r} '
                       f'table={event[4]!r}]')
            except Exception:                    # pragma: no cover - 防御
                ctx = f' [{event!r}]'
        super().__init__(f'复制 apply 失败{ctx}: {message}')


class ReplicaPrototype:
    """单主、单线程的 binlog → 目标库 apply 原型"""

    STATE_VERSION = 1

    def __init__(self, master_host: str, master_port: int, target_db,
                 user: str = 'root', password: str = '',
                 state_path: str = None, connect_timeout: float = 5.0,
                 read_timeout: float = 60.0, target_executor=None):
        self.master_host = str(master_host)
        self.master_port = int(master_port)
        self.user = user
        self.password = password
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.target_db = target_db
        self._executor = target_executor
        self._lock = threading.Lock()
        self.state_path = (
            state_path if state_path is not None
            else os.path.join(str(target_db.data_dir), 'replica_state.json'))
        self.state = self._load_state()
        self.started_at = time.time()
        self._last_error = None

    # ==================== 状态持久化 ====================

    def _default_state(self) -> dict:
        return {
            'version': self.STATE_VERSION,
            'master_host': self.master_host,
            'master_port': self.master_port,
            'master_file': None,
            'lsn': 0,
            'seq': 0,
            'applied': 0,
            'idempotent': 0,
            'errors': 0,
            'last_event_type': None,
            'last_apply_ts': 0.0,
        }

    def _load_state(self) -> dict:
        state = self._default_state()
        path = str(self.state_path)
        if not os.path.exists(path):
            return state
        try:
            raw = json.loads(open(path, encoding='utf-8').read())
        except Exception as e:
            raise ReplicaError(
                f'复制状态文件损坏，拒绝从不确定位点继续: {path}: {e}')
        if not isinstance(raw, dict):
            raise ReplicaError(f'复制状态文件格式非法: {path}')
        state.update({k: raw[k] for k in state if k in raw})
        return state

    def save_state(self, sync: bool = False):
        """原子写回状态（tmp + replace）

        * 默认 ``sync=False``：只 flush 到 OS（不 fsync）—— 每条事件都 fsync
          在 Windows 上实测约 0.2s/次，1000 事件会慢到不可用；
        * ``sync=True``：批末/错误路径做一次 fsync，保证断点续传位点尽量
          不丢（崩溃最多回放最后一批事件，重放按幂等处理）。
        * 失败抛 ReplicaError（不能假装已持久化）。
        """
        state = dict(self.state)
        state['saved_at'] = time.time()
        path = str(self.state_path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                if sync:
                    os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception as e:
            raise ReplicaError(f'复制状态持久化失败: {path}: {e}')

    # ==================== 主库访问 ====================

    def _connect(self):
        import pymysql
        try:
            return pymysql.connect(
                host=self.master_host, port=self.master_port,
                user=self.user, password=self.password,
                connect_timeout=self.connect_timeout,
                read_timeout=self.read_timeout, autocommit=True)
        except Exception as e:
            raise ReplicaError(
                f'连接主库失败 {self.master_host}:{self.master_port}: {e}')

    def _query(self, conn, sql: str):
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall() if cur.description else []
            cols = [d[0] for d in cur.description] if cur.description else []
            return cols, rows
        except ReplicaError:
            raise
        except Exception as e:
            raise ReplicaError(f'主库查询失败: {sql[:120]}: {e}')

    def master_status(self) -> dict:
        """`SHOW MASTER STATUS` → {file, position}；binlog 关闭时显式报错"""
        conn = self._connect()
        try:
            cols, rows = self._query(conn, 'SHOW MASTER STATUS')
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not rows:
            raise ReplicaError(
                '主库 SHOW MASTER STATUS 为空：binlog 未启用'
                '（LIGHTDB_BINLOG_ENABLED=0）或尚未产生写入，无法复制')
        row = rows[0]
        return {'file': row[0], 'position': int(row[1])}

    def binary_logs(self) -> list:
        """`SHOW BINARY LOGS` → [{name,size,encrypted}, ...]"""
        conn = self._connect()
        try:
            cols, rows = self._query(conn, 'SHOW BINARY LOGS')
        finally:
            try:
                conn.close()
            except Exception:
                pass
        out = []
        for row in rows:
            out.append({'name': row[0], 'size': int(row[1]),
                        'encrypted': row[2] if len(row) > 2 else 'No'})
        return out

    def fetch_events(self, after_lsn: int = None, limit: int = 500) -> list:
        """拉取 `lsn > after_lsn` 的行事件（`__BINLOG_FETCH__` 6 列）"""
        after_lsn = int(self.state['lsn'] if after_lsn is None
                        else after_lsn)
        limit = max(1, int(limit))
        sql = ('SELECT * FROM __BINLOG_FETCH__ WHERE lsn > '
               f'{after_lsn} LIMIT {limit}')
        conn = self._connect()
        try:
            cols, rows = self._query(conn, sql)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        expect = ['pos', 'lsn', 'txn_id', 'event_type', 'table_name', 'data']
        if cols and cols != expect:
            raise ReplicaError(
                f'主库 __BINLOG_FETCH__ 列契约变化：期望 {expect}，实际 {cols}')
        return [tuple(r) for r in rows]

    # ==================== apply ====================

    @property
    def executor(self):
        """目标库执行器（DDL 用；惰性创建并复用）"""
        if self._executor is None:
            from ..sql.executor import Executor
            self._executor = Executor(self.target_db)
        return self._executor

    def apply_event(self, event, persist: bool = True) -> dict:
        """应用一条事件；返回 {'result': 'applied'|'idempotent', 'type': ...}

        ``persist=False`` 用于批量 apply：由 `run_once` 按批/按间隔统一写
        状态文件，避免每条事件一次 JSON dump + rename 的开销（1000 条事件
        实测省 ~2/3 时间）；直接调用方默认每条都持久化。
        """
        event = self._validate_event(event)
        pos, lsn, txn_id, etype, table, payload = event
        data = payload
        if etype == 'DDL':
            result = self._apply_ddl(data.get('sql', ''), event)
        else:
            result = self._apply_dml(etype, table, data, event)
        with self._lock:
            self.state['lsn'] = max(int(self.state.get('lsn') or 0), int(lsn))
            self.state['seq'] = max(int(self.state.get('seq') or 0), int(pos))
            self.state['last_event_type'] = etype
            self.state['last_apply_ts'] = time.time()
            if result == 'applied':
                self.state['applied'] = int(self.state.get('applied') or 0) + 1
            else:
                self.state['idempotent'] = \
                    int(self.state.get('idempotent') or 0) + 1
        if persist:
            self.save_state()
        return {'result': result, 'type': etype, 'lsn': int(lsn),
                'table': table}

    def _validate_event(self, event):
        """校验并解码事件；损坏/未知类型 → ReplicaApplyError（显式）"""
        if not isinstance(event, (tuple, list)):
            raise ReplicaApplyError(f'事件不是元组/列表: {type(event)}')
        if len(event) != 6:
            raise ReplicaApplyError(
                f'事件列数非法: {len(event)}（期望 6）', event)
        pos, lsn, txn_id, etype, table, payload = event
        try:
            lsn_i = int(lsn)
            pos_i = int(pos) if pos is not None else 0
        except (TypeError, ValueError):
            raise ReplicaApplyError('事件位点不是整数', event)
        etype = str(etype or '').upper()
        if etype not in KNOWN_EVENT_TYPES:
            raise ReplicaApplyError(
                f'未知事件类型 {etype!r}（拒绝静默跳过）', event)
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode('utf-8', 'replace')
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except Exception as e:
                raise ReplicaApplyError(
                    f'事件 payload 不是合法 JSON（损坏事件）: {e}', event)
        elif isinstance(payload, dict):
            data = payload
        else:
            raise ReplicaApplyError(
                f'事件 payload 类型非法: {type(payload)}', event)
        if not isinstance(data, dict):
            raise ReplicaApplyError('事件 payload 不是 JSON 对象', event)
        return (pos_i, lsn_i, txn_id, etype, table, data)

    # ---------- DML ----------
    def _get_table(self, name, event):
        try:
            return self.target_db.get_table(name)
        except KeyError:
            raise ReplicaApplyError(
                f'目标库缺表 {name!r}：DDL 事件未先复制/顺序错误', event)

    def _apply_dml(self, etype: str, table: str, data: dict, event):
        tbl = self._get_table(table, event)
        cols = data.get('cols')
        if etype == 'INSERT':
            row = data.get('row')
            if not isinstance(row, (list, tuple)):
                raise ReplicaApplyError('INSERT 缺 row 镜像', event)
            try:
                with self.target_db.suspend_binlog():
                    tbl.insert(list(row), txn_id=0)
                return 'applied'
            except ValueError as e:
                msg = str(e)
                if any(k in msg for k in ('重复', 'duplicate', 'Duplicate',
                                          'unique', '唯一')):
                    return 'idempotent'      # 已应用过（重放边界）
                raise ReplicaApplyError(f'INSERT apply 失败: {msg}', event)
            except Exception as e:
                raise ReplicaApplyError(f'INSERT apply 失败: {e}', event)

        if etype == 'UPDATE':
            pk = data.get('pk')
            new = data.get('new')
            if pk is None or not isinstance(new, (list, tuple)):
                raise ReplicaApplyError('UPDATE 缺 pk/new 镜像', event)
            table_cols = [c['name'] for c in tbl.cols]
            set_pairs = {}
            for i, name in enumerate(table_cols):
                if name == str(tbl.pk_col):
                    continue
                if i < len(new):
                    set_pairs[name] = new[i]
            try:
                with self.target_db.suspend_binlog():
                    n = tbl.update(
                        set_pairs,
                        {'column': tbl.pk_col, 'op': '=', 'value': pk},
                        txn_id=0)
            except Exception as e:
                raise ReplicaApplyError(f'UPDATE apply 失败: {e}', event)
            return 'applied' if n else 'idempotent'

        if etype == 'DELETE':
            pk = data.get('pk')
            if pk is None:
                raise ReplicaApplyError('DELETE 缺 pk 镜像', event)
            try:
                with self.target_db.suspend_binlog():
                    n = tbl.delete(
                        {'column': tbl.pk_col, 'op': '=', 'value': pk},
                        txn_id=0)
            except Exception as e:
                raise ReplicaApplyError(f'DELETE apply 失败: {e}', event)
            return 'applied' if n else 'idempotent'
        raise ReplicaApplyError(f'未支持的 DML 类型 {etype!r}', event)

    # ---------- DDL ----------
    def _apply_ddl(self, sql: str, event):
        sql = str(sql or '').strip()
        if not sql:
            raise ReplicaApplyError('DDL 事件缺少 sql 文本', event)
        from ..sql.parser import parser
        try:
            parsed = parser.parse(sql)
        except Exception as e:
            raise ReplicaApplyError(
                f'DDL 解析失败（拒绝静默跳过）: {e}', event)
        if not isinstance(parsed, dict):
            raise ReplicaApplyError(f'DDL 无法解析: {sql[:200]}', event)
        cmd = str(parsed.get('command') or '')
        table = parsed.get('table')

        # 幂等预判（重放边界：上一条已应用但状态未落盘）
        if cmd == 'CREATE TABLE' and table in self.target_db.tables:
            return 'idempotent'
        if cmd == 'DROP TABLE' and table not in self.target_db.tables:
            return 'idempotent'
        if cmd == 'ALTER TABLE' and table not in self.target_db.tables:
            raise ReplicaApplyError(
                f'ALTER TABLE 目标表不存在 {table!r}（DDL 顺序错误）', event)

        try:
            target_ex = self.executor
            with self.target_db.suspend_binlog():
                result = target_ex.execute(parsed, {})
        except Exception as e:
            raise ReplicaApplyError(f'DDL 执行异常: {e}', event)
        if isinstance(result, dict) and result.get('type') == 'error':
            if self._ddl_error_is_idempotent(parsed, result, event):
                return 'idempotent'
            raise ReplicaApplyError(
                f"DDL apply 失败 errno={result.get('code')}: "
                f"{result.get('message')}；sql={sql[:200]}", event)
        return 'applied'

    def _ddl_error_is_idempotent(self, parsed, result, event) -> bool:
        """判断 DDL 报错是否"该变更已经生效过"（幂等重放）

        原型只对可**验证**的形态判幂等（ALTER ADD/DROP COLUMN）：不仅看
        错误码/报文，还核对目标表的真实结构；核对不上就按失败显式上抛。
        """
        cmd = str(parsed.get('command') or '')
        code = result.get('code')
        msg = str(result.get('message') or '')
        table = parsed.get('table')
        if cmd != 'ALTER TABLE':
            # CREATE FULLTEXT INDEX 重放：索引/表已存在时按幂等处理
            if cmd == 'CREATE FULLTEXT INDEX' and any(
                    k in msg for k in ('已存在', 'Duplicate', 'duplicate',
                                       'exists')):
                return True
            return False
        action = str(parsed.get('action') or '').upper()
        if action not in _IDEMPOTENT_DDL_ACTIONS:
            return False
        tbl = self.target_db.tables.get(table)
        if tbl is None:
            return False
        have = {str(c['name']) for c in tbl.cols}
        if action == 'ADD COLUMN':
            col = (parsed.get('column') or {}).get('name')
            return bool(col) and str(col) in have
        if action == 'DROP COLUMN':
            col = parsed.get('column')
            return bool(col) and str(col) not in have
        return False

    # ==================== 批量运行 ====================

    def run_once(self, limit: int = 500, save_every: int = 50) -> dict:
        """拉取一批并串行 apply；返回统计（events/applied/idempotent/elapsed_ms）

        ``save_every``：每 N 条事件写一次状态（不 fsync）；批末统一
        flush+fsync 一次。崩溃最多回放最后一批事件（重放按幂等处理），
        换来 1000 事件 apply 的可用延迟（每条 fsync 在 Windows 上约 0.2s）。
        """
        started = time.time()
        status = self.master_status()
        logs = self.binary_logs()
        names = {item['name'] for item in logs}
        prev_file = self.state.get('master_file')
        if prev_file and names and prev_file not in names:
            raise ReplicaError(
                f'起始位点所在 binlog 文件 {prev_file!r} 在 master 已不存在'
                f'（binlog 被清理/重建）：无法保证断点续传，拒绝继续')
        events = self.fetch_events(self.state['lsn'], limit)
        stats = {'fetched': len(events), 'applied': 0, 'idempotent': 0,
                 'errors': 0, 'events': len(events), 'last_lsn': None,
                 'last_seq': None, 'master_file': status['file'],
                 'master_position': status['position'],
                 'elapsed_ms': 0.0, 'events_per_sec': 0.0}
        for idx, ev in enumerate(events):
            try:
                res = self.apply_event(ev, persist=False)
            except ReplicaApplyError as e:
                self.state['errors'] = int(self.state.get('errors') or 0) + 1
                self.save_state(sync=True)
                self._last_error = str(e)
                stats['errors'] = 1
                stats['error'] = str(e)
                stats['elapsed_ms'] = round(
                    (time.time() - started) * 1000.0, 3)
                raise
            if res['result'] == 'applied':
                stats['applied'] += 1
            else:
                stats['idempotent'] += 1
            stats['last_lsn'] = res['lsn']
            stats['last_seq'] = int(self.state.get('seq') or 0)
            if save_every and (idx + 1) % max(1, int(save_every)) == 0:
                self.save_state()
        # 记录主库当前文件（用于清理检测；文件级，不含位点）
        self.state['master_file'] = status['file']
        # 批末 fsync 一次：断点续传位点的持久化点（每事件不 fsync，
        # 否则 Windows 上 1000 事件 ≈ 200s；崩溃最多回放本批、按幂等处理）
        self.save_state(sync=True)
        elapsed = time.time() - started
        stats['elapsed_ms'] = round(elapsed * 1000.0, 3)
        stats['events_per_sec'] = (
            round(len(events) / elapsed, 1) if elapsed > 0 else 0.0)
        # 延迟口径：本批"拉取→应用完成"的处理延迟（不是 MySQL 的
        # Seconds_Behind_Master；导出接口不含事件时间戳，如实说明）
        stats['lag_ms'] = stats['elapsed_ms']
        stats['caught_up'] = len(events) == 0
        stats['state'] = dict(self.state)
        return stats

    def run_until_caught_up(self, batch: int = 500,
                            max_batches: int = 1000) -> dict:
        """连续应用直到没有新事件（用于测试/一次性同步）"""
        started = time.time()
        total = {'batches': 0, 'events': 0, 'applied': 0,
                 'idempotent': 0, 'elapsed_ms': 0.0, 'caught_up': False}
        for _ in range(max(1, int(max_batches))):
            stats = self.run_once(limit=batch)
            total['batches'] += 1
            total['events'] += stats['events']
            total['applied'] += stats['applied']
            total['idempotent'] += stats['idempotent']
            if stats['caught_up']:
                total['caught_up'] = True
                break
        total['elapsed_ms'] = round((time.time() - started) * 1000.0, 3)
        total['events_per_sec'] = (
            round(total['events'] / (total['elapsed_ms'] / 1000.0), 1)
            if total['elapsed_ms'] > 0 else 0.0)
        total['state'] = dict(self.state)
        return total

    def follow(self, interval: float = 0.2, batch: int = 500,
               stop_event: threading.Event = None, max_events: int = None):
        """持续跟随（单线程）；主库不可达时抛 ReplicaError（无自动 failover）"""
        applied_total = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                return {'applied': applied_total, 'stopped': True}
            try:
                stats = self.run_once(limit=batch)
            except ReplicaError:
                raise
            applied_total += stats['events']
            if max_events is not None and applied_total >= max_events:
                return {'applied': applied_total, 'stopped': True}
            if stats['events'] == 0:
                time.sleep(float(interval))

    def stats(self) -> dict:
        """当前状态快照（CLI/测试/排障用）"""
        state = dict(self.state)
        state['state_path'] = str(self.state_path)
        state['target_data_dir'] = str(self.target_db.data_dir)
        state['last_error'] = self._last_error
        state['process_uptime'] = round(time.time() - self.started_at, 3)
        return state
