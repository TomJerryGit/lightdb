"""
数字孪生
- 数据库镜像（快照级）
- 实时对比
- 变更预演
"""
import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Dict, Optional, Callable


TWIN_FILE = Path.home() / '.lightdb_studio' / 'digital_twin.json'


@dataclass
class TwinSnapshot:
    """孪生快照"""
    snapshot_id: str = ""
    twin_id: str = ""
    timestamp: float = 0
    label: str = ""
    # 结构 + 数据
    tables: Dict = field(default_factory=dict)
    # {table_name: {'columns': [...], 'row_count': N, 'hash': '...'}}
    total_tables: int = 0
    total_rows: int = 0

    def __post_init__(self):
        if not self.snapshot_id:
            self.snapshot_id = f"snap_{uuid.uuid4().hex[:8]}"
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class DigitalTwin:
    """数字孪生"""
    twin_id: str = ""
    name: str = ""
    source_connection: str = ""
    source_database: str = ""
    # 镜像连接（可以是同一数据库的不同 schema，或影子环境）
    target_connection: str = ""
    # 快照
    snapshots: List[TwinSnapshot] = field(default_factory=list)
    # 同步策略
    sync_mode: str = "on_demand"       # on_demand / interval / realtime
    sync_interval: int = 3600
    # 状态
    status: str = "active"
    last_sync: float = 0
    created_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.twin_id:
            self.twin_id = f"twin_{uuid.uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = now


@dataclass
class TwinDiff:
    """孪生差异"""
    diff_id: str = ""
    twin_id: str = ""
    snap_a: str = ""
    snap_b: str = ""
    # 差异
    tables_added: List[str] = field(default_factory=list)
    tables_removed: List[str] = field(default_factory=list)
    tables_changed: List[Dict] = field(default_factory=list)
    # {table, old_rows, new_rows, schema_diff, hash_diff}
    # 时间
    computed_at: float = 0
    severity: str = "info"           # info / warning / critical

    def __post_init__(self):
        if not self.diff_id:
            self.diff_id = f"diff_{uuid.uuid4().hex[:6]}"
        if not self.computed_at:
            self.computed_at = time.time()


class DigitalTwinManager:
    """数字孪生管理器"""

    def __init__(self, conn_provider=None):
        self.file = TWIN_FILE
        self.conn_provider = conn_provider
        self.twins: Dict[str, DigitalTwin] = {}
        self.diffs: List[TwinDiff] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for tid, td in data.get('twins', {}).items():
                snaps = [TwinSnapshot(**s)
                          for s in td.pop('snapshots', [])]
                t = DigitalTwin(**td)
                t.snapshots = snaps
                self.twins[tid] = t
            for dd in data.get('diffs', [])[-500:]:
                self.diffs.append(TwinDiff(**dd))
        except Exception:
            pass

    def save(self):
        data = {
            'twins': {
                t: {
                    **asdict(tw),
                    'snapshots': [asdict(s) for s in tw.snapshots],
                }
                for t, tw in self.twins.items()
            },
            'diffs': [asdict(d) for d in self.diffs[-200:]],
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 创建孪生 ====================

    def create_twin(self, name: str, source_connection: str,
                    source_database: str = '',
                    target_connection: str = '',
                    sync_mode: str = 'on_demand',
                    sync_interval: int = 3600) -> DigitalTwin:
        t = DigitalTwin(
            name=name,
            source_connection=source_connection,
            source_database=source_database,
            target_connection=target_connection,
            sync_mode=sync_mode,
            sync_interval=sync_interval,
        )
        self.twins[t.twin_id] = t
        self.save()
        return t

    def remove_twin(self, twin_id: str):
        if twin_id in self.twins:
            del self.twins[twin_id]
            self.save()

    # ==================== 快照 ====================

    def take_snapshot(self, twin_id: str,
                      label: str = '') -> Optional[TwinSnapshot]:
        """拍摄当前快照"""
        twin = self.twins.get(twin_id)
        if not twin:
            return None

        conn = self.conn_provider(twin.source_connection) \
            if self.conn_provider else None
        if not conn:
            return None

        if twin.source_database:
            try:
                conn.switch_database(twin.source_database)
            except Exception:
                pass

        snap = TwinSnapshot(
            twin_id=twin_id,
            label=label or f"快照 {len(twin.snapshots) + 1}",
        )

        try:
            tables = conn.get_tables()
            total_rows = 0

            for t in tables:
                try:
                    cols = conn.get_columns(t)
                    _, r = conn.query(
                        f"SELECT COUNT(*) FROM `{t}`")
                    row_count = r[0][0] if r else 0

                    # 结构 hash
                    schema_str = '|'.join(
                        f"{c['name']}:{c.get('type', '')}"
                        for c in cols)
                    schema_hash = __import__('hashlib').md5(
                        schema_str.encode()).hexdigest()[:16]

                    snap.tables[t] = {
                        'columns': [c['name'] for c in cols],
                        'column_count': len(cols),
                        'row_count': row_count,
                        'schema_hash': schema_hash,
                    }
                    total_rows += row_count
                except Exception:
                    snap.tables[t] = {
                        'columns': [],
                        'row_count': 0,
                        'schema_hash': '',
                        'error': 'failed',
                    }

            snap.total_tables = len(snap.tables)
            snap.total_rows = total_rows

            twin.snapshots.append(snap)
            twin.last_sync = time.time()
            # 只保留最近 20 个
            if len(twin.snapshots) > 20:
                twin.snapshots = twin.snapshots[-20:]

            self.save()
            return snap
        except Exception:
            return None

    # ==================== 对比 ====================

    def compare(self, twin_id: str,
                snap_a_id: str = None,
                snap_b_id: str = None) -> Optional[TwinDiff]:
        """对比两个快照"""
        twin = self.twins.get(twin_id)
        if not twin or len(twin.snapshots) < 2:
            return None

        if snap_a_id:
            a = next((s for s in twin.snapshots
                       if s.snapshot_id == snap_a_id), None)
        else:
            a = twin.snapshots[-2]

        if snap_b_id:
            b = next((s for s in twin.snapshots
                       if s.snapshot_id == snap_b_id), None)
        else:
            b = twin.snapshots[-1]

        if not a or not b:
            return None

        diff = TwinDiff(
            twin_id=twin_id,
            snap_a=a.snapshot_id,
            snap_b=b.snapshot_id,
        )

        tables_a = set(a.tables.keys())
        tables_b = set(b.tables.keys())

        diff.tables_added = list(tables_b - tables_a)
        diff.tables_removed = list(tables_a - tables_b)

        severity = 'info'
        for t in tables_a & tables_b:
            ta = a.tables[t]
            tb = b.tables[t]
            changes = {}

            if ta.get('schema_hash') != tb.get('schema_hash'):
                changes['schema'] = {
                    'old_columns': ta.get('columns', []),
                    'new_columns': tb.get('columns', []),
                }
                severity = 'critical'

            row_delta = tb.get('row_count', 0) - ta.get('row_count', 0)
            if row_delta != 0:
                pct = (row_delta / max(1, ta.get('row_count', 1))) * 100
                changes['rows'] = {
                    'old': ta.get('row_count', 0),
                    'new': tb.get('row_count', 0),
                    'delta': row_delta,
                    'pct': round(pct, 2),
                }
                if abs(pct) > 50:
                    severity = 'warning'

            if changes:
                diff.tables_changed.append({
                    'table': t,
                    'changes': changes,
                })

        if diff.tables_added or diff.tables_removed:
            severity = 'warning'

        diff.severity = severity
        self.diffs.append(diff)
        self.save()
        return diff

    # ==================== 变更预演 ====================

    def preview_change(self, twin_id: str, sql: str) -> Dict:
        """
        变更预演：在孪生上执行 SQL，观察影响
        返回预估影响（不实际执行）
        """
        twin = self.twins.get(twin_id)
        if not twin:
            return {'error': '孪生不存在'}

        conn = self.conn_provider(twin.target_connection or
                                    twin.source_connection) \
            if self.conn_provider else None
        if not conn:
            return {'error': '连接不可用'}

        # 分析 SQL
        import re
        up = sql.strip().upper()

        result = {
            'sql': sql,
            'previewed_at': time.time(),
            'estimated_impact': {},
            'warnings': [],
            'dry_run_safe': True,
        }

        # 解析目标表
        m = re.search(
            r'(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM|DROP\s+TABLE|ALTER\s+TABLE)\s+[`"]?(\w+)',
            up)
        if m:
            target_table = m.group(1)
            result['target_table'] = target_table

            # 估算影响行数
            if up.startswith('UPDATE') or up.startswith('DELETE'):
                where_m = re.search(r'WHERE\s+(.+?)(?:\s+ORDER|\s+LIMIT|$)', up)
                try:
                    if where_m:
                        where_clause = sql[sql.upper().find('WHERE'):]
                        _, r = conn.query(
                            f"SELECT COUNT(*) FROM `{target_table}` "
                            f"{where_clause}")
                        count = r[0][0] if r else 0
                        result['estimated_impact']['rows'] = count
                    else:
                        _, r = conn.query(
                            f"SELECT COUNT(*) FROM `{target_table}`")
                        count = r[0][0] if r else 0
                        result['estimated_impact']['rows'] = count
                        result['warnings'].append(
                            f"⚠️ 无 WHERE 条件，将影响整表 {count} 行")
                except Exception as e:
                    result['warnings'].append(f"无法估算: {e}")

            # DROP/TRUNCATE 警告
            if 'DROP' in up or 'TRUNCATE' in up:
                result['warnings'].append(
                    "🚨 破坏性操作！不可恢复")
                result['dry_run_safe'] = False

        return result

    # ==================== 实时监控 ====================

    def live_diff(self, twin_id: str) -> Dict:
        """
        实时对比：当前状态 vs 上次快照
        """
        twin = self.twins.get(twin_id)
        if not twin or not twin.snapshots:
            return {'error': '无快照'}

        last = twin.snapshots[-1]
        conn = self.conn_provider(twin.source_connection) \
            if self.conn_provider else None
        if not conn:
            return {'error': '连接不可用'}

        if twin.source_database:
            try:
                conn.switch_database(twin.source_database)
            except Exception:
                pass

        result = {
            'twin_id': twin_id,
            'baseline': last.snapshot_id,
            'baseline_label': last.label,
            'changes': [],
            'summary': {
                'tables_added': 0,
                'tables_removed': 0,
                'tables_changed': 0,
                'rows_delta': 0,
            },
        }

        try:
            tables_now = set(conn.get_tables())
            tables_then = set(last.tables.keys())

            added = tables_now - tables_then
            removed = tables_then - tables_now

            result['summary']['tables_added'] = len(added)
            result['summary']['tables_removed'] = len(removed)

            for t in added:
                result['changes'].append({
                    'table': t,
                    'type': 'table_added',
                })

            for t in removed:
                result['changes'].append({
                    'table': t,
                    'type': 'table_removed',
                })

            for t in tables_now & tables_then:
                try:
                    _, r = conn.query(
                        f"SELECT COUNT(*) FROM `{t}`")
                    now_count = r[0][0] if r else 0
                    old_count = last.tables[t].get('row_count', 0)

                    if now_count != old_count:
                        delta = now_count - old_count
                        result['changes'].append({
                            'table': t,
                            'type': 'rows_changed',
                            'old': old_count,
                            'new': now_count,
                            'delta': delta,
                        })
                        result['summary']['tables_changed'] += 1
                        result['summary']['rows_delta'] += delta
                except Exception:
                    pass
        except Exception as e:
            result['error'] = str(e)

        return result

    # ==================== 统计 ====================

    def stats(self) -> dict:
        by_severity = {}
        for d in self.diffs:
            by_severity[d.severity] = by_severity.get(d.severity, 0) + 1

        return {
            'total_twins': len(self.twins),
            'active_twins': sum(1 for t in self.twins.values()
                                 if t.status == 'active'),
            'total_snapshots': sum(len(t.snapshots)
                                     for t in self.twins.values()),
            'total_diffs': len(self.diffs),
            'diffs_by_severity': by_severity,
        }