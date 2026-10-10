"""
触发器
- BEFORE/AFTER INSERT/UPDATE/DELETE
- FOR EACH ROW
- NEW.column / OLD.column
"""
import json
import time
from pathlib import Path

from .pl import PLParser, PLInterpreter, PLContext


class Trigger:
    def __init__(self, name, table, timing, event, body, order=0):
        self.name = name
        self.table = table
        self.timing = timing.upper()    # BEFORE / AFTER
        self.event = event.upper()      # INSERT / UPDATE / DELETE
        self.body = body
        self.order = order
        self.created_at = time.time()

    def to_dict(self):
        return {
            'name': self.name, 'table': self.table,
            'timing': self.timing, 'event': self.event,
            'body': self.body, 'order': self.order,
            'created_at': self.created_at,
        }

    @classmethod
    def from_dict(cls, d):
        t = cls(d['name'], d['table'], d['timing'], d['event'],
                d['body'], d.get('order', 0))
        t.created_at = d.get('created_at', time.time())
        return t


class TriggerManager:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / '_triggers.json'
        self.triggers = {}   # table -> [Trigger, ...]
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for table, tlist in data.items():
                self.triggers[table] = [Trigger.from_dict(d) for d in tlist]
        except Exception:
            pass

    def save(self):
        data = {
            table: [t.to_dict() for t in tlist]
            for table, tlist in self.triggers.items()
        }
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def create(self, name, table, timing, event, body, order=0):
        # 检查同名
        for tlist in self.triggers.values():
            for t in tlist:
                if t.name == name:
                    raise ValueError(f"触发器 '{name}' 已存在")
        t = Trigger(name, table, timing, event, body, order)
        self.triggers.setdefault(table, []).append(t)
        self.triggers[table].sort(key=lambda x: x.order)
        self.save()
        return t

    def drop(self, name):
        for table, tlist in self.triggers.items():
            for i, t in enumerate(tlist):
                if t.name == name:
                    del tlist[i]
                    self.save()
                    return True
        return False

    def list_all(self):
        result = []
        for tlist in self.triggers.values():
            result.extend(tlist)
        return result

    def fire(self, timing, event, table, old_row=None, new_row=None,
             executor=None, session=None):
        """触发某表上的触发器"""
        tlist = self.triggers.get(table, [])
        if not tlist:
            return

        # 准备列名映射（按行位置）
        tbl = executor.db.get_table(table)
        col_names = [c['name'] for c in tbl.cols]
        old_dict = dict(zip(col_names, old_row)) if old_row else None
        new_dict = dict(zip(col_names, new_row)) if new_row else None

        for trig in tlist:
            if trig.timing != timing or trig.event != event:
                continue
            try:
                self._run_trigger(trig, old_dict, new_dict, executor, session)
            except Exception as e:
                from ..logger import logger
                logger.error(f"触发器 {trig.name} 执行失败: {e}")

    def _run_trigger(self, trig, old_dict, new_dict, executor, session):
        body = trig.body
        if body.strip().upper().startswith('BEGIN'):
            # 去外层 BEGIN/END
            up = body.strip().upper()
            if up.startswith('BEGIN') and up.endswith('END'):
                body = body.strip()[5:-3].strip()

        p = PLParser(body)
        stmts = p.parse_program()

        ctx = PLContext(executor, session, executor.db)
        ctx.new_row = new_dict
        ctx.old_row = old_dict

        interp = PLInterpreter(ctx)
        interp.run(stmts)