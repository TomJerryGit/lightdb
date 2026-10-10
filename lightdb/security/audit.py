"""
审计日志
- 记录所有 DDL / DCL / DML（可配置级别）
- 输出到文件 + 内存环形缓冲
"""
import json
import threading
import time
from collections import deque
from pathlib import Path


class AuditLogger:
    LEVELS = {
        'NONE': 0,
        'DCL': 1,       # GRANT, CREATE USER 等
        'DDL': 2,       # CREATE/DROP/ALTER
        'DML': 3,       # INSERT/UPDATE/DELETE
        'ALL': 4,       # SELECT 也记
    }

    def __init__(self, data_dir: str, level: str = 'DDL'):
        self.path = Path(data_dir) / 'audit.log'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.level = self.LEVELS.get(level.upper(), 2)
        self.lock = threading.RLock()
        self.recent = deque(maxlen=2000)
        self.fh = open(self.path, 'a', encoding='utf-8')
        self.stats = {'total': 0, 'by_type': {}}

    def set_level(self, level: str):
        with self.lock:
            self.level = self.LEVELS.get(level.upper(), 2)

    def record(self, user: str, cmd: str, sql: str, success: bool,
               error: str = None, elapsed: float = 0.0):
        """记录一条审计日志"""
        lvl = self._classify(cmd)
        if lvl > self.level:
            return

        entry = {
            'ts': time.time(),
            'user': user,
            'cmd': cmd,
            'sql': sql[:500],
            'success': success,
            'error': (error or '')[:200],
            'elapsed_ms': round(elapsed * 1000, 3),
        }

        with self.lock:
            self.recent.append(entry)
            self.fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
            self.fh.flush()
            self.stats['total'] += 1
            self.stats['by_type'][cmd] = self.stats['by_type'].get(cmd, 0) + 1

    def _classify(self, cmd: str) -> int:
        up = cmd.upper()
        if up in ('GRANT', 'REVOKE', 'CREATE USER', 'DROP USER', 'ALTER USER',
                  'SET PASSWORD'):
            return self.LEVELS['DCL']
        if up.startswith('CREATE') or up.startswith('DROP') or up.startswith('ALTER') \
                or up in ('TRUNCATE',):
            return self.LEVELS['DDL']
        if up in ('INSERT', 'UPDATE', 'DELETE'):
            return self.LEVELS['DML']
        return self.LEVELS['ALL']

    def recent_records(self, n: int = 50):
        with self.lock:
            return list(self.recent)[-n:]

    def close(self):
        with self.lock:
            try:
                self.fh.close()
            except Exception:
                pass