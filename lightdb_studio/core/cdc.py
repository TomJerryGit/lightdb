"""
变更数据捕获（Change Data Capture）
- 基于时间戳的增量同步
- 基于 Binlog 的实时同步
- 冲突解决
"""
import json
import re
import threading
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Callable, Optional

CDC_FILE = Path.home() / '.lightdb_studio' / 'cdc_tasks.json'


@dataclass
class CDCTask:
    name: str = ""
    source_connection: str = ""
    source_table: str = ""
    target_connection: str = ""
    target_table: str = ""
    mode: str = "poll"          # poll / binlog
    # 增量键
    key_column: str = "id"
    timestamp_column: str = ""
    # 状态
    last_key: str = ""
    last_timestamp: float = 0
    last_sync: float = 0
    total_synced: int = 0
    # 冲突解决
    conflict_strategy: str = "update"   # skip / update / error
    enabled: bool = True
    interval: int = 60          # 轮询间隔（秒）
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class CDCEvent:
    task_name: str = ""
    event_type: str = ""        # insert / update / delete
    key: str = ""
    data: Dict = field(default_factory=dict)
    timestamp: float = 0
    success: bool = True
    error: str = ""


class CDCEngine:
    """CDC 引擎"""

    def __init__(self, conn_provider: Callable = None):
        """
        conn_provider(name) -> DBConnection
        """
        self.file = CDC_FILE
        self.conn_provider = conn_provider
        self.tasks: List[CDCTask] = []
        self.events: List[CDCEvent] = []
        self._running_tasks: Dict[str, bool] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._callbacks: List[Callable] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.tasks = [CDCTask(**t) for t in data.get('tasks', [])]
        except Exception:
            pass

    def save(self):
        data = {'tasks': [asdict(t) for t in self.tasks]}
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 任务管理 ====================

    def add_task(self, task: CDCTask):
        self.tasks.append(task)
        self.save()

    def remove_task(self, name: str):
        self.stop_task(name)
        self.tasks = [t for t in self.tasks if t.name != name]
        self.save()

    def get_task(self, name: str) -> Optional[CDCTask]:
        for t in self.tasks:
            if t.name == name:
                return t
        return None

    # ==================== 启动/停止 ====================

    def start_task(self, name: str):
        task = self.get_task(name)
        if not task or task.name in self._running_tasks:
            return
        self._running_tasks[name] = True
        thread = threading.Thread(
            target=self._poll_loop, args=(task,),
            daemon=True, name=f'CDC-{name}')
        self._threads[name] = thread
        thread.start()

    def stop_task(self, name: str):
        if name in self._running_tasks:
            self._running_tasks[name] = False
        if name in self._threads:
            self._threads[name].join(timeout=3)
            del self._threads[name]

    def stop_all(self):
        for name in list(self._running_tasks.keys()):
            self.stop_task(name)

    def is_running(self, name: str) -> bool:
        return self._running_tasks.get(name, False)

    # ==================== 轮询同步 ====================

    def _poll_loop(self, task: CDCTask):
        while self._running_tasks.get(task.name):
            try:
                self.sync_once(task)
            except Exception as e:
                self._log_event(CDCEvent(
                    task_name=task.name,
                    event_type='error',
                    error=str(e),
                    success=False,
                ))
            # 等待间隔或提前退出
            for _ in range(task.interval):
                if not self._running_tasks.get(task.name):
                    break
                time.sleep(1)

    def sync_once(self, task: CDCTask) -> Dict:
        """执行一次同步"""
        source = self.conn_provider(task.source_connection) \
            if self.conn_provider else None
        target = self.conn_provider(task.target_connection) \
            if self.conn_provider else None

        if not source or not target:
            return {'ok': False, 'error': '连接不可用'}

        # 构造增量查询
        where_clause = ""
        if task.timestamp_column and task.last_timestamp > 0:
            ts = time.strftime('%Y-%m-%d %H:%M:%S',
                                time.localtime(task.last_timestamp))
            where_clause = f" WHERE `{task.timestamp_column}` > '{ts}'"
        elif task.last_key:
            where_clause = f" WHERE `{task.key_column}` > '{task.last_key}'"

        sql = (f"SELECT * FROM `{task.source_table}`"
               f"{where_clause} "
               f"ORDER BY `{task.key_column}` "
               f"LIMIT 1000")

        try:
            cols, rows = source.query(sql)
        except Exception as e:
            return {'ok': False, 'error': str(e)}

        if not rows:
            task.last_sync = time.time()
            return {'ok': True, 'synced': 0}

        # 检查目标表是否存在
        try:
            target_cols = target.get_columns(task.target_table)
            target_col_names = [c['name'] for c in target_cols]
        except Exception:
            return {'ok': False, 'error': '目标表不存在'}

        # 逐行同步
        key_idx = cols.index(task.key_column) \
            if task.key_column in cols else 0

        inserted = 0
        updated = 0
        skipped = 0
        errors = 0

        for row in rows:
            row_dict = dict(zip(cols, row))
            key_val = row[key_idx]

            # 检查目标是否已存在
            try:
                _, r = target.query(
                    f"SELECT COUNT(*) FROM `{task.target_table}` "
                    f"WHERE `{task.key_column}` = %s",
                    (key_val,))
                exists = r[0][0] > 0 if r else False
            except Exception:
                exists = False

            if exists:
                if task.conflict_strategy == 'skip':
                    skipped += 1
                    continue
                elif task.conflict_strategy == 'update':
                    set_pairs = {c: row_dict[c] for c in cols
                                 if c in target_col_names
                                 and c != task.key_column}
                    if set_pairs:
                        set_str = ', '.join(
                            f"`{k}` = %s" for k in set_pairs)
                        params = list(set_pairs.values()) + [key_val]
                        try:
                            target.execute(
                                f"UPDATE `{task.target_table}` "
                                f"SET {set_str} "
                                f"WHERE `{task.key_column}` = %s",
                                tuple(params))
                            updated += 1
                        except Exception:
                            errors += 1
                else:   # error
                    errors += 1
            else:
                # 插入
                insert_cols = [c for c in cols
                               if c in target_col_names]
                insert_vals = [row_dict[c] for c in insert_cols]
                col_str = ', '.join(f"`{c}`" for c in insert_cols)
                placeholders = ', '.join(['%s'] * len(insert_cols))
                try:
                    target.execute(
                        f"INSERT INTO `{task.target_table}` "
                        f"({col_str}) VALUES ({placeholders})",
                        tuple(insert_vals))
                    inserted += 1
                except Exception:
                    errors += 1

            # 记录事件
            self._log_event(CDCEvent(
                task_name=task.name,
                event_type='sync',
                key=str(key_val),
                timestamp=time.time(),
            ))

        # 更新状态
        last_row = rows[-1]
        task.last_key = str(last_row[key_idx])
        if task.timestamp_column and task.timestamp_column in cols:
            ts_idx = cols.index(task.timestamp_column)
            try:
                from datetime import datetime
                ts_str = str(last_row[ts_idx])[:19]
                task.last_timestamp = datetime.strptime(
                    ts_str, '%Y-%m-%d %H:%M:%S').timestamp()
            except Exception:
                pass
        task.last_sync = time.time()
        task.total_synced += inserted + updated

        self.save()

        return {
            'ok': True,
            'inserted': inserted,
            'updated': updated,
            'skipped': skipped,
            'errors': errors,
            'total': len(rows),
        }

    # ==================== 事件日志 ====================

    def _log_event(self, event: CDCEvent):
        if not event.timestamp:
            event.timestamp = time.time()
        self.events.append(event)
        if len(self.events) > 10000:
            self.events = self.events[-5000:]

        for cb in self._callbacks:
            try:
                cb(event)
            except Exception:
                pass

    def on_event(self, callback):
        self._callbacks.append(callback)

    def get_recent_events(self, task_name: str = None,
                          limit: int = 100) -> List[CDCEvent]:
        events = self.events
        if task_name:
            events = [e for e in events if e.task_name == task_name]
        return list(reversed(events[-limit:]))

    def stats(self) -> Dict:
        return {
            'total_tasks': len(self.tasks),
            'running_tasks': sum(1 for n in self._running_tasks.values()
                                  if n),
            'total_events': len(self.events),
            'total_synced': sum(t.total_synced for t in self.tasks),
        }