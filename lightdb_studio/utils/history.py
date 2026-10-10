"""查询历史持久化"""
import json
import time
from pathlib import Path
from collections import deque

HISTORY_FILE = Path.home() / '.lightdb_studio' / 'history.json'


class QueryHistory:
    def __init__(self, max_size=500):
        self.path = HISTORY_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_size = max_size
        self.entries = deque(maxlen=max_size)
        self.favorites = []
        self.load()

    def load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for e in data.get('entries', []):
                self.entries.append(e)
            self.favorites = data.get('favorites', [])
        except Exception:
            pass

    def save(self):
        data = {
            'entries': list(self.entries),
            'favorites': self.favorites,
        }
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def add(self, sql, conn_name='', success=True, elapsed=0.0):
        sql = sql.strip()
        if not sql:
            return
        if self.entries and self.entries[-1]['sql'] == sql:
            return
        self.entries.append({
            'sql': sql,
            'conn': conn_name,
            'ts': time.time(),
            'success': success,
            'elapsed': elapsed,
        })
        self.save()

    def add_favorite(self, sql, name=''):
        sql = sql.strip()
        if any(f['sql'] == sql for f in self.favorites):
            return
        self.favorites.append({
            'name': name or sql[:40],
            'sql': sql,
            'ts': time.time(),
        })
        self.save()

    def remove_favorite(self, sql):
        self.favorites = [f for f in self.favorites if f['sql'] != sql]
        self.save()

    def clear(self):
        self.entries.clear()
        self.save()

    def recent(self, n=100):
        return list(self.entries)[-n:][::-1]

    def search(self, keyword):
        kw = keyword.lower()
        return [e for e in self.entries if kw in e['sql'].lower()]