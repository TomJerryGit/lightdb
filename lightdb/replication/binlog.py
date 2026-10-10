"""
二进制日志 (Binlog) - ROW 格式
用于主从复制、PITR 恢复

事件类型:
  INSERT / UPDATE / DELETE / DDL / COMMIT / BEGIN
"""
import json
import threading
import time
from pathlib import Path


class BinlogEvent:
    __slots__ = ('pos', 'txn_id', 'type', 'table', 'data', 'ts')

    def __init__(self, pos, txn_id, etype, table, data, ts=None):
        self.pos = pos
        self.txn_id = txn_id
        self.type = etype
        self.table = table
        self.data = data
        self.ts = ts if ts is not None else time.time()

    def to_json(self):
        return json.dumps({
            'pos': self.pos, 'txn_id': self.txn_id,
            'type': self.type, 'table': self.table,
            'data': self.data, 'ts': self.ts,
        }, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, line):
        o = json.loads(line)
        return cls(o['pos'], o['txn_id'], o['type'],
                   o['table'], o['data'], o.get('ts'))


class Binlog:
    """Binlog 管理器"""
    MAX_FILE_SIZE = 64 * 1024 * 1024  # 64 MB

    def __init__(self, data_dir: str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

        # 定位当前日志文件
        self.index = 1
        existing = sorted(self.dir.glob('binlog.*'))
        if existing:
            last = existing[-1]
            try:
                self.index = int(last.suffix[1:])
            except Exception:
                self.index = 1

        self.file = self.dir / f'binlog.{self.index:06d}'
        self.pos = 0
        self.size = 0

        if self.file.exists():
            self.size = self.file.stat().st_size
            with open(self.file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = BinlogEvent.from_json(line)
                        self.pos = max(self.pos, ev.pos)
                    except Exception:
                        pass

        self.fh = open(self.file, 'a', encoding='utf-8')

    # ============ 写 ============
    def append(self, txn_id: int, etype: str, table, data) -> int:
        with self.lock:
            self.pos += 1
            ev = BinlogEvent(self.pos, txn_id, etype, table, data)
            line = ev.to_json() + '\n'
            self.fh.write(line)
            self.fh.flush()
            self.size += len(line.encode('utf-8'))

            if self.size >= self.MAX_FILE_SIZE:
                self._rotate()

            return self.pos

    def _rotate(self):
        self.fh.close()
        self.index += 1
        self.file = self.dir / f'binlog.{self.index:06d}'
        self.fh = open(self.file, 'a', encoding='utf-8')
        self.size = 0
        self.pos = 0

    # ============ 读 ============
    def read_from(self, start_pos: int, limit: int = 1000):
        """读取 pos >= start_pos 的事件"""
        events = []
        if not self.file.exists():
            return events
        with self.lock:
            with open(self.file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = BinlogEvent.from_json(line)
                    except Exception:
                        continue
                    if ev.pos >= start_pos:
                        events.append(ev)
                        if len(events) >= limit:
                            break
        return events

    def current_pos(self) -> int:
        return self.pos

    def current_file(self) -> str:
        return self.file.name

    def flush(self):
        with self.lock:
            self.fh.flush()
            try:
                import os
                os.fsync(self.fh.fileno())
            except Exception:
                pass

    def close(self):
        with self.lock:
            try:
                self.fh.close()
            except Exception:
                pass