"""
从库复制管理器
- IO 线程：从主库拉取 binlog → 中继日志
- SQL 线程：从中继日志重放到本地
"""
import json
import threading
import time
from collections import deque

from ..logger import logger


class RelayLog:
    """中继日志（内存 + 磁盘）"""

    def __init__(self, data_dir: str):
        from pathlib import Path
        self.path = Path(data_dir) / 'relay.log'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.queue = deque()
        self.lock = threading.RLock()
        self.fh = open(self.path, 'a', encoding='utf-8')

    def append(self, event_tuple):
        with self.lock:
            self.queue.append(event_tuple)
            self.fh.write(json.dumps(event_tuple, ensure_ascii=False) + '\n')
            self.fh.flush()

    def pop_all(self):
        with self.lock:
            events = list(self.queue)
            self.queue.clear()
            return events

    def close(self):
        with self.lock:
            try:
                self.fh.close()
            except Exception:
                pass


class SlaveReplicator:
    """从库侧"""

    def __init__(self, db, master_host: str, master_port: int,
                 user: str = 'root', password: str = ''):
        self.db = db
        self.master_host = master_host
        self.master_port = master_port
        self.user = user
        self.password = password

        self.running = False
        self.io_thread = None
        self.sql_thread = None
        self.last_pos = 0
        self.relay = RelayLog(str(db.data_dir))

        self.stats = {
            'io_events': 0,
            'sql_events': 0,
            'errors': 0,
            'last_error': None,
            'last_sync_ts': 0,
        }

    # ============ 生命周期 ============
    def start(self):
        if self.running:
            return
        self.running = True
        self.io_thread = threading.Thread(target=self._io_loop, daemon=True,
                                          name='SlaveIO')
        self.sql_thread = threading.Thread(target=self._sql_loop, daemon=True,
                                           name='SlaveSQL')
        self.io_thread.start()
        self.sql_thread.start()
        logger.info(f"从库复制已启动 (主库 {self.master_host}:{self.master_port})")

    def stop(self):
        self.running = False
        if self.io_thread:
            self.io_thread.join(timeout=3)
        if self.sql_thread:
            self.sql_thread.join(timeout=3)
        self.relay.close()
        logger.info("从库复制已停止")

    # ============ IO 线程 ============
    def _io_loop(self):
        import pymysql
        while self.running:
            try:
                conn = pymysql.connect(
                    host=self.master_host,
                    port=self.master_port,
                    user=self.user,
                    password=self.password,
                    autocommit=True,
                    connect_timeout=5,
                )
                cur = conn.cursor()

                while self.running:
                    sql = (f"SELECT * FROM __BINLOG_FETCH__ "
                           f"WHERE pos >= {self.last_pos + 1} LIMIT 500")
                    try:
                        cur.execute(sql)
                        rows = cur.fetchall()
                    except Exception as e:
                        self.stats['errors'] += 1
                        self.stats['last_error'] = str(e)
                        break

                    for r in rows:
                        # r = (pos, txn_id, type, table, data_json)
                        self.relay.append(r)
                        self.stats['io_events'] += 1

                    if not rows:
                        time.sleep(0.1)

                try:
                    conn.close()
                except Exception:
                    pass

            except Exception as e:
                self.stats['errors'] += 1
                self.stats['last_error'] = str(e)
                logger.warn(f"从库IO线程异常: {e}")
                time.sleep(1)

    # ============ SQL 线程 ============
    def _sql_loop(self):
        while self.running:
            events = self.relay.pop_all()
            if not events:
                time.sleep(0.05)
                continue

            for ev in events:
                try:
                    self._apply_event(ev)
                    self.last_pos = ev[0]
                    self.stats['sql_events'] += 1
                    self.stats['last_sync_ts'] = time.time()
                except Exception as e:
                    self.stats['errors'] += 1
                    self.stats['last_error'] = str(e)
                    logger.error(f"从库重放失败 pos={ev[0]}: {e}")

    def _apply_event(self, ev):
        pos, txn_id, etype, table, data_json = ev
        data = json.loads(data_json) if isinstance(data_json, str) else data_json

        if etype == 'INSERT':
            tbl = self.db.get_table(table)
            row = data.get('row', [])
            try:
                tbl.insert(row, txn_id=0)
            except ValueError:
                # 主键已存在（重复投递）
                pass

        elif etype == 'UPDATE':
            tbl = self.db.get_table(table)
            pk = data['pk']
            new = data.get('new', [])
            # 生成 set_pairs（除主键外的所有列）
            set_pairs = {}
            for i, c in enumerate(tbl.cols):
                if i != tbl.pk_idx and i < len(new):
                    set_pairs[c['name']] = new[i]
            tbl.update(set_pairs,
                       {'column': tbl.pk_col, 'op': '=', 'value': pk},
                       txn_id=0)

        elif etype == 'DELETE':
            tbl = self.db.get_table(table)
            pk = data['pk']
            tbl.delete({'column': tbl.pk_col, 'op': '=', 'value': pk},
                       txn_id=0)

        elif etype == 'DDL':
            sql = data.get('sql', '')
            from ..sql.parser import parser
            from ..sql.executor import Executor
            try:
                p = parser.parse(sql)
                if p:
                    ex = Executor(self.db)
                    ex.execute(p, {})
            except Exception as e:
                logger.warn(f"DDL 重放失败: {e}")

        elif etype in ('COMMIT', 'BEGIN'):
            pass

    # ============ 状态 ============
    def status(self) -> dict:
        return {
            'running': self.running,
            'master_host': self.master_host,
            'master_port': self.master_port,
            'last_pos': self.last_pos,
            **self.stats,
        }