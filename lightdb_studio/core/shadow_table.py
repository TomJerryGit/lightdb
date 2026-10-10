"""
影子表
- 沙盒环境：不影响生产的测试表
- 从真实表克隆（可选脱敏）
- 生命周期自动管理
"""
import json
import re
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Callable, Optional   # ★ 修复：补 Dict

SHADOW_FILE = Path.home() / '.lightdb_studio' / 'shadow_tables.json'


@dataclass
class ShadowTable:
    shadow_id: str = ""
    name: str = ""
    source_table: str = ""
    connection: str = ""
    database: str = ""
    row_count: int = 0
    masked_columns: List[str] = field(default_factory=list)
    created_by: str = ""
    ttl_hours: int = 24
    expires_at: float = 0
    status: str = "active"
    created_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.shadow_id:
            self.shadow_id = f"sh_{uuid.uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = now
        if not self.expires_at:
            self.expires_at = now + self.ttl_hours * 3600

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def remaining_hours(self) -> float:
        return max(0, (self.expires_at - time.time()) / 3600)


class ShadowTableManager:
    """影子表管理器"""

    def __init__(self, conn_provider=None, masking_engine=None):
        self.file = SHADOW_FILE
        self.conn_provider = conn_provider
        self.masking_engine = masking_engine
        self.shadows: Dict[str, ShadowTable] = {}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for sid, sd in data.get('shadows', {}).items():
                self.shadows[sid] = ShadowTable(**sd)
        except Exception:
            pass

    def save(self):
        data = {
            'shadows': {s: asdict(sh) for s, sh in self.shadows.items()},
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def create_shadow(self, source_table: str, connection: str,
                      sample_size: int = 1000,
                      ttl_hours: int = 24,
                      masked_columns: List[str] = None,
                      created_by: str = '',
                      progress_cb: Callable = None) -> ShadowTable:
        conn = self.conn_provider(connection) \
            if self.conn_provider else None
        if not conn:
            raise ValueError(f"连接 {connection} 不可用")

        suffix = uuid.uuid4().hex[:6]
        shadow_name = f"{source_table}_shadow_{suffix}"

        # 复制结构
        try:
            _, r = conn.query(f"SHOW CREATE TABLE `{source_table}`")
            if not r or len(r[0]) < 2:
                raise RuntimeError("无法获取表结构")
            create_sql = r[0][1]

            create_sql = re.sub(
                rf'CREATE\s+TABLE\s+`?{re.escape(source_table)}`?',
                f'CREATE TABLE `{shadow_name}`',
                create_sql, flags=re.IGNORECASE, count=1)

            create_sql = re.sub(r'ENGINE\s*=\s*\w+', 'ENGINE=InnoDB',
                                 create_sql, flags=re.IGNORECASE)

            conn.execute(create_sql)
        except Exception as e:
            raise RuntimeError(f"创建结构失败: {e}")

        # 复制数据
        row_count = 0
        masked_set = set(masked_columns or [])

        try:
            cols, rows = conn.query(
                f"SELECT * FROM `{source_table}` LIMIT {sample_size}")

            if rows:
                row_count = len(rows)

                if masked_set and self.masking_engine:
                    masked_rows = []
                    for row in rows:
                        new_row = list(row)
                        for i, cname in enumerate(cols):
                            if cname in masked_set:
                                rule = self.masking_engine.get_strategy(cname)
                                if rule:
                                    new_row[i] = self.masking_engine.mask_value(
                                        new_row[i], rule)
                        masked_rows.append(new_row)
                    rows = masked_rows

                if progress_cb:
                    progress_cb(0, len(rows), 'inserting')

                cols_str = ', '.join(f"`{c}`" for c in cols)
                ph = ', '.join(['%s'] * len(cols))
                insert_sql = (f"INSERT INTO `{shadow_name}` "
                               f"({cols_str}) VALUES ({ph})")

                batch = 500
                for i in range(0, len(rows), batch):
                    conn.execute_many(insert_sql, rows[i:i + batch])
                    if progress_cb:
                        progress_cb(min(i + batch, len(rows)),
                                     len(rows), 'inserting')

        except Exception:
            pass

        shadow = ShadowTable(
            name=shadow_name,
            source_table=source_table,
            connection=connection,
            database=conn.config.database or '',
            row_count=row_count,
            masked_columns=list(masked_set),
            created_by=created_by,
            ttl_hours=ttl_hours,
        )
        self.shadows[shadow.shadow_id] = shadow
        self.save()
        return shadow

    def drop_shadow(self, shadow_id: str) -> bool:
        shadow = self.shadows.get(shadow_id)
        if not shadow:
            return False

        conn = self.conn_provider(shadow.connection) \
            if self.conn_provider else None
        if conn:
            try:
                conn.execute(f"DROP TABLE IF EXISTS `{shadow.name}`")
            except Exception:
                pass

        shadow.status = 'dropped'
        self.save()
        return True

    def cleanup_expired(self) -> int:
        n = 0
        for sid, shadow in list(self.shadows.items()):
            if shadow.status == 'active' and shadow.is_expired():
                self.drop_shadow(sid)
                n += 1
        return n

    def list_active(self) -> List[ShadowTable]:
        return [s for s in self.shadows.values()
                if s.status == 'active' and not s.is_expired()]

    def stats(self) -> dict:
        active = self.list_active()
        return {
            'total': len(self.shadows),
            'active': len(active),
            'expired': sum(1 for s in self.shadows.values()
                            if s.is_expired() and s.status == 'active'),
            'total_rows': sum(s.row_count for s in active),
        }