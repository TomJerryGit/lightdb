"""
逻辑备份 + 恢复
- dump: 导出所有表为 SQL 脚本
- restore: 从 SQL 脚本恢复
- PITR: 基于 binlog 恢复到指定时间点
"""
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from ..logger import logger


class BackupManager:
    def __init__(self, db, backup_dir: str):
        self.db = db
        self.dir = Path(backup_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ============ 逻辑备份 ============
    def dump(self, output_file: str = None) -> str:
        """导出为 SQL 脚本"""
        if output_file is None:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = str(self.dir / f"dump_{ts}.sql")

        lines = []
        lines.append(f"-- LightDB Backup")
        lines.append(f"-- Generated: {datetime.now().isoformat()}")
        lines.append(f"-- Tables: {len(self.db.tables)}")
        lines.append("")

        # 每个表
        for tname, tbl in self.db.tables.items():
            lines.append(f"-- ========== Table: {tname} ==========")

            # CREATE TABLE
            col_defs = []
            for c in tbl.cols:
                d = f"  `{c['name']}` {c['type']}"
                if c.get('primary'):
                    d += ' PRIMARY KEY'
                elif c.get('unique'):
                    d += ' UNIQUE'
                if c.get('notnull'):
                    d += ' NOT NULL'
                col_defs.append(d)

            lines.append(f"CREATE TABLE {tname} (")
            lines.append(',\n'.join(col_defs))
            lines.append(");")
            lines.append("")

            # INSERT
            for pk, pid, sid in tbl.bt.scan():
                page = tbl.bp.get_page(pid)
                rb = page.get_row(sid)
                r = tbl._decode_row(rb)
                if r is None:
                    continue
                vals = ', '.join(self._sql_literal(v) for v in r)
                lines.append(f"INSERT INTO {tname} VALUES ({vals});")

            lines.append("")

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        logger.info(f"备份完成: {output_file}")
        return output_file

    def _sql_literal(self, v):
        if v is None:
            return 'NULL'
        if isinstance(v, bool):
            return '1' if v else '0'
        if isinstance(v, (int, float)):
            return str(v)
        s = str(v).replace("'", "''")
        return f"'{s}'"

    # ============ 恢复 ============
    def restore(self, dump_file: str):
        """从 SQL 脚本恢复"""
        from ..sql.parser import parser
        from ..sql.executor import Executor

        if not os.path.exists(dump_file):
            raise FileNotFoundError(dump_file)

        with open(dump_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # 逐条执行（简单按 ; 分割）
        ex = Executor(self.db)
        statements = self._split_statements(content)

        ok = 0
        fail = 0
        for stmt in statements:
            stmt = stmt.strip()
            if not stmt or stmt.startswith('--'):
                continue
            try:
                p = parser.parse(stmt)
                if p:
                    r = ex.execute(p, {'_no_binlog': True})
                    if isinstance(r, dict) and r.get('type') == 'error':
                        fail += 1
                        logger.warn(f"恢复失败: {stmt[:80]} → {r['message']}")
                    else:
                        ok += 1
            except Exception as e:
                fail += 1
                logger.warn(f"恢复异常: {stmt[:80]} → {e}")

        logger.info(f"恢复完成: 成功={ok}, 失败={fail}")
        return ok, fail

    def _split_statements(self, content: str):
        """按分号切分 SQL（忽略字符串内的分号）"""
        stmts = []
        buf = ''
        in_str = None
        for ch in content:
            if in_str:
                buf += ch
                if ch == in_str:
                    in_str = None
            elif ch in ("'", '"'):
                in_str = ch
                buf += ch
            elif ch == ';':
                stmts.append(buf)
                buf = ''
            else:
                buf += ch
        if buf.strip():
            stmts.append(buf)
        return stmts

    # ============ 物理备份（文件复制）============
    def physical_backup(self, output_dir: str = None) -> str:
        """复制数据目录（需要停库或冻结）"""
        if output_dir is None:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_dir = str(self.dir / f"physical_{ts}")

        # 先 flush
        for tbl in self.db.tables.values():
            tbl.flush()
        self.db.binlog.flush()

        shutil.copytree(str(self.db.data_dir), output_dir)
        logger.info(f"物理备份完成: {output_dir}")
        return output_dir

    # ============ PITR (时间点恢复) ============
    def pitr(self, dump_file: str, target_time: float):
        """
        时间点恢复:
        1. 恢复 dump 基础数据
        2. 应用 dump 之后的 binlog 到 target_time
        """
        # 步骤1
        self.restore(dump_file)

        # 步骤2: 读取 binlog
        from ..replication.binlog import Binlog
        binlog = self.db.binlog
        events = binlog.read_from(1, limit=100000)

        applied = 0
        for ev in events:
            if ev.ts > target_time:
                break
            try:
                self._apply_binlog_event(ev)
                applied += 1
            except Exception as e:
                logger.warn(f"PITR应用失败 pos={ev.pos}: {e}")

        logger.info(f"PITR完成: 应用 {applied} 条binlog")
        return applied

    def _apply_binlog_event(self, ev):
        tbl_name = ev.table
        if not tbl_name or tbl_name not in self.db.tables:
            return
        tbl = self.db.tables[tbl_name]
        if ev.type == 'INSERT':
            tbl.insert(ev.data.get('row', []), txn_id=0)
        elif ev.type == 'UPDATE':
            new = ev.data.get('new', [])
            set_pairs = {}
            for i, c in enumerate(tbl.cols):
                if i != tbl.pk_idx and i < len(new):
                    set_pairs[c['name']] = new[i]
            tbl.update(set_pairs,
                       {'column': tbl.pk_col, 'op': '=', 'value': ev.data['pk']},
                       txn_id=0)
        elif ev.type == 'DELETE':
            tbl.delete({'column': tbl.pk_col, 'op': '=', 'value': ev.data['pk']},
                       txn_id=0)