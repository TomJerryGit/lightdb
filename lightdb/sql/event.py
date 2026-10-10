"""
事件调度器
- CREATE EVENT ... ON SCHEDULE EVERY n SECOND/MINUTE/HOUR/DAY
- DO statement
"""
import json
import threading
import time
from pathlib import Path

from .pl import PLParser, PLInterpreter, PLContext


class EventDef:
    def __init__(self, name, schedule_seconds, body, enabled=True, comment=''):
        self.name = name
        self.schedule_seconds = schedule_seconds
        self.body = body
        self.enabled = enabled
        self.comment = comment
        self.last_run = 0
        self.next_run = time.time() + schedule_seconds
        self.run_count = 0

    def to_dict(self):
        return {
            'name': self.name,
            'schedule_seconds': self.schedule_seconds,
            'body': self.body,
            'enabled': self.enabled,
            'comment': self.comment,
            'last_run': self.last_run,
            'next_run': self.next_run,
            'run_count': self.run_count,
        }

    @classmethod
    def from_dict(cls, d):
        e = cls(d['name'], d['schedule_seconds'], d['body'],
                d.get('enabled', True), d.get('comment', ''))
        e.last_run = d.get('last_run', 0)
        e.next_run = d.get('next_run', time.time() + e.schedule_seconds)
        e.run_count = d.get('run_count', 0)
        return e


class EventScheduler:
    def __init__(self, data_dir, executor):
        self.path = Path(data_dir) / '_events.json'
        self.executor = executor
        self.events = {}     # name -> EventDef
        self.running = False
        self.thread = None
        self.lock = threading.RLock()
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for n, d in data.items():
                self.events[n] = EventDef.from_dict(d)
        except Exception:
            pass

    def save(self):
        data = {n: e.to_dict() for n, e in self.events.items()}
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def create(self, name, schedule_seconds, body, enabled=True, comment=''):
        with self.lock:
            if name in self.events:
                del self.events[name]
            e = EventDef(name, schedule_seconds, body, enabled, comment)
            self.events[name] = e
            self.save()
            return e

    def drop(self, name):
        with self.lock:
            if name in self.events:
                del self.events[name]
                self.save()
                return True
            return False

    def list_all(self):
        return list(self.events.values())

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True, name='EventScheduler')
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)

    def _loop(self):
        while self.running:
            try:
                now = time.time()
                with self.lock:
                    to_run = [e for e in self.events.values()
                              if e.enabled and e.next_run <= now]
                for e in to_run:
                    self._run_event(e)
            except Exception:
                pass
            time.sleep(1.0)

    def _run_event(self, e):
        from ..logger import logger
        try:
            body = e.body
            if body.strip().upper().startswith('BEGIN'):
                up = body.strip().upper()
                if up.startswith('BEGIN') and up.endswith('END'):
                    body = body.strip()[5:-3].strip()
            p = PLParser(body)
            stmts = p.parse_program()
            ctx = PLContext(self.executor, {'user': 'system'}, self.executor.db)
            interp = PLInterpreter(ctx)
            interp.run(stmts)
            logger.info(f"事件 {e.name} 执行成功")
        except Exception as ex:
            logger.error(f"事件 {e.name} 执行失败: {ex}")
        finally:
            e.last_run = time.time()
            e.run_count += 1
            e.next_run = time.time() + e.schedule_seconds
            self.save()