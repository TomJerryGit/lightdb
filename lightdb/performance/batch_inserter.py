"""
批量插入 - 减少 WAL/落盘次数
通过合并事务日志 + 延迟 flush，一次写多行
"""
import time
from ..logger import logger


class BatchInserter:
    def __init__(self, db):
        self.db = db

    def insert_many(self, table_name: str, rows: list,
                    txn_id: int = 0, batch_size: int = 500) -> int:
        """
        批量插入 rows（列表的列表）
        - 一次持锁
        - 一次 flush
        """
        tbl = self.db.get_table(table_name)
        if not rows:
            return 0

        n = 0
        t0 = time.time()

        with tbl._lock:
            # 逐行插入（内部不做 fsync）
            for row in rows:
                full = list(row) + [None] * (len(tbl.cols) - len(row))
                try:
                    tbl.insert(full, txn_id=txn_id)
                    n += 1
                except ValueError as e:
                    # 主键冲突跳过
                    logger.warn(f"批量插入跳过: {e}")

            # 一次 flush
            tbl.flush()

        elapsed = time.time() - t0
        rate = n / elapsed if elapsed > 0 else 0
        logger.info(f"批量插入 {n} 行到 {table_name}，耗时 {elapsed:.3f}s ({rate:.0f} rows/s)")
        return n

    def insert_many_fast(self, table_name: str, rows: list,
                         txn_id: int = 0) -> int:
        """
        极速插入 - 直接写 B+Tree，跳过部分校验
        适用于 bulk load（数据可信）
        """
        tbl = self.db.get_table(table_name)
        if not rows:
            return 0

        n = 0
        t0 = time.time()

        with tbl._lock:
            for row in rows:
                full = list(row) + [None] * (len(tbl.cols) - len(row))
                pk = full[tbl.pk_idx]
                if pk is None:
                    pk = tbl._next_pk
                    full[tbl.pk_idx] = pk

                # 直接写
                rb = tbl._encode_row(full, xmin=txn_id)
                pid, sid = tbl._heap_insert(rb)
                if pid is None:
                    continue
                tbl.bt.insert(pk, pid, sid)
                tbl._next_pk = max(tbl._next_pk, pk + 1)
                n += 1

            tbl.flush()

        elapsed = time.time() - t0
        rate = n / elapsed if elapsed > 0 else 0
        logger.info(f"批量快插 {n} 行 ({rate:.0f} rows/s)")
        return n