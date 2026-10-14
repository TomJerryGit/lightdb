"""
逻辑备份 + 恢复
- dump: 导出所有表为 SQL 脚本（格式规范见 `lightdb/sql/logical_dump.py`）
- restore: 从 SQL 脚本恢复（**先建表再灌数据**，执行完读回校验）
- PITR: 基于 binlog 恢复到指定时间点

历史缺陷（2026-10 修复，`tests/test_batch8.py::test_logical_backup`）：
dump 里 `CREATE TABLE` 前面有一行 `-- ========== Table: users ==========`，
而 restore 的切分器对"以 `--` 开头的语句"直接 `continue`，于是建表语句被
整条丢掉；10 条 INSERT 全部报「表 users 不存在」，却只记了 fail 计数、
不抛错，最终调用方拿到的是"成功恢复 0 行"。现在：
1. 切分器只把**纯注释块**当注释，前导注释会被剥掉、语句照常执行；
2. dump 自带 `DROP TABLE IF EXISTS` + `CREATE TABLE IF NOT EXISTS`；
3. 任何语句失败 → 抛 `RestoreError`（绝不静默继续）；
4. 恢复后读回校验行数 / 主键 / 列结构 / 逐行可解码。
"""
import os
import shutil
from datetime import datetime
from pathlib import Path

from ..logger import logger
from ..sql.logical_dump import (BackupError, RestoreError, build_dump,
                                parse_dump, restore_dump, table_schemas,
                                verify_restore)

__all__ = ['BackupManager', 'BackupError', 'RestoreError']


class BackupManager:
    def __init__(self, db, backup_dir: str):
        self.db = db
        self.dir = Path(backup_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ============ 逻辑备份 ============
    def dump(self, output_file: str = None, tables=None,
             no_data: bool = False, no_create: bool = False,
             include_header: bool = True) -> str:
        """导出为 SQL 脚本（lightdb-logical-dump v1）

        `include_header=False` 用于兼容"只想要裸语句"的老调用方；
        行数哨兵（`-- lightdb:rows`）始终写入，恢复后靠它做行数校验。
        """
        if output_file is None:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = str(self.dir / f"dump_{ts}.sql")

        content = build_dump(
            self.db, tables=tables, no_data=no_data,
            no_create=no_create, include_header=include_header)

        tmp = str(output_file) + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)
        # 原子替换：备份文件半截比没有备份更危险（恢复端会读到截断的
        # INSERT）。先写 .tmp 再 replace，与 Database.save_meta 同款。
        os.replace(tmp, output_file)

        logger.info(f"备份完成: {output_file}")
        return output_file

    def dump_tables(self):
        """本次会导出的表结构（供调用方打印/校验）"""
        return table_schemas(self.db)

    def _sql_literal(self, v):
        """保留此方法以兼容旧调用方；真实实现已收敛到
        `lightdb.sql.logical_dump.sql_literal`（转义规则只有一份）"""
        from ..sql.logical_dump import sql_literal
        return sql_literal(v)

    # ============ 恢复 ============
    def restore(self, dump_file: str, strict: bool = True,
                verify: bool = True, drop_extra: bool = True,
                executor=None):
        """从 SQL 脚本恢复

        返回 `(ok, fail)`（沿用老调用方签名）。**失败即抛
        `RestoreError`**：`strict=True`（默认）时不会出现"fail>0 但
        返回值被忽略 → 数据库其实是空的"这种静默失败。需要"尽力恢复
        并把错误收集起来"的场景请显式传 `strict=False`。
        """
        if not os.path.exists(dump_file):
            raise FileNotFoundError(dump_file)

        ok, fail, report = restore_dump(
            self.db, dump_file=dump_file, executor=executor,
            strict=strict, verify=verify, drop_extra=drop_extra)
        if report is not None and not report.ok:
            logger.warn(f"恢复校验未通过: {report.errors}")
        logger.info(f"恢复完成: 成功={ok}, 失败={fail}")
        return ok, fail

    def verify(self, dump_file: str):
        """只做校验：解析 dump 并读回比对（不写数据）"""
        with open(dump_file, 'r', encoding='utf-8') as f:
            plan = parse_dump(f.read())
        return verify_restore(self.db, plan)

    def plan(self, dump_file: str):
        """解析 dump（不执行），返回 DumpPlan"""
        with open(dump_file, 'r', encoding='utf-8') as f:
            return parse_dump(f.read())

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
                # PITR 追 binlog 失败必须让调用方知道：静默跳过等于
                # 恢复到错误的时间点
                raise RestoreError(
                    f"PITR 应用 binlog pos={ev.pos} 失败: {e}") from e

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
