"""
标准负载定义（类 Sysbench）
"""
import random
import string
import time
from dataclasses import dataclass, field
from typing import List, Dict, Callable


# ==================== 表结构 ====================

BENCH_TABLE_DDL = """
CREATE TABLE {table} (
    id INT PRIMARY KEY,
    k INT NOT NULL,
    c VARCHAR(120) NOT NULL,
    pad VARCHAR(60) NOT NULL
)
"""


# ==================== 数据生成 ====================

def random_string(n: int) -> str:
    return ''.join(random.choices(string.ascii_letters, k=n))


def generate_rows(start: int, count: int) -> List[tuple]:
    rows = []
    for i in range(start, start + count):
        rows.append((
            i,
            random.randint(1, 10 ** 9),
            random_string(120),
            random_string(60),
        ))
    return rows


# ==================== 工作负载基类 ====================

@dataclass
class WorkloadResult:
    name: str
    queries: int = 0
    errors: int = 0
    total_ms: float = 0
    latencies_ms: List[float] = field(default_factory=list)
    # 分位数
    p50: float = 0
    p95: float = 0
    p99: float = 0
    p999: float = 0
    min_lat: float = 0
    max_lat: float = 0
    avg_lat: float = 0
    # 吞吐
    qps: float = 0
    tps: float = 0

    def compute_stats(self, elapsed_s: float):
        if not self.latencies_ms:
            return
        sorted_lat = sorted(self.latencies_ms)
        n = len(sorted_lat)
        self.min_lat = sorted_lat[0]
        self.max_lat = sorted_lat[-1]
        self.avg_lat = sum(sorted_lat) / n
        self.p50 = sorted_lat[int(n * 0.5)]
        self.p95 = sorted_lat[int(n * 0.95)]
        self.p99 = sorted_lat[int(n * 0.99)]
        self.p999 = sorted_lat[min(int(n * 0.999), n - 1)]
        self.qps = self.queries / elapsed_s if elapsed_s > 0 else 0

    def to_dict(self) -> Dict:
        return {
            'name': self.name,
            'queries': self.queries,
            'errors': self.errors,
            'total_ms': self.total_ms,
            'qps': round(self.qps, 2),
            'tps': round(self.tps, 2),
            'latency_ms': {
                'min': round(self.min_lat, 3),
                'avg': round(self.avg_lat, 3),
                'p50': round(self.p50, 3),
                'p95': round(self.p95, 3),
                'p99': round(self.p99, 3),
                'p999': round(self.p999, 3),
                'max': round(self.max_lat, 3),
            },
        }


class BaseWorkload:
    """工作负载基类"""
    name = "base"
    description = ""

    def __init__(self, conn, table: str = 'sbtest1'):
        self.conn = conn
        self.table = table
        self._max_id = 0

    def setup(self, rows: int):
        """准备数据"""
        cur = self.conn.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {self.table}")
        cur.execute(BENCH_TABLE_DDL.format(table=self.table))

        # 批量插入
        batch_size = 1000
        for start in range(1, rows + 1, batch_size):
            count = min(batch_size, rows - start + 1)
            batch = generate_rows(start, count)
            placeholders = ','.join(['(%s,%s,%s,%s)'] * len(batch))
            sql = f"INSERT INTO {self.table} VALUES {placeholders}"
            flat = [v for row in batch for v in row]
            cur.execute(sql, flat)

        self._max_id = rows
        self.conn.commit()

    def execute_once(self) -> bool:
        """执行一次（子类实现）"""
        raise NotImplementedError

    def cleanup(self):
        try:
            cur = self.conn.cursor()
            cur.execute(f"DROP TABLE IF EXISTS {self.table}")
            self.conn.commit()
        except Exception:
            pass


# ==================== 具体负载 ====================

class PointSelect(BaseWorkload):
    name = "point_select"
    description = "主键点查（最基础）"

    def execute_once(self) -> bool:
        pk = random.randint(1, self._max_id)
        cur = self.conn.cursor()
        cur.execute(f"SELECT c FROM {self.table} WHERE id = %s", (pk,))
        cur.fetchone()
        return True


class RangeScan(BaseWorkload):
    name = "range_scan"
    description = "范围扫描（100 行）"

    def execute_once(self) -> bool:
        start = random.randint(1, max(1, self._max_id - 100))
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT c FROM {self.table} WHERE id BETWEEN %s AND %s",
            (start, start + 100))
        cur.fetchall()
        return True


class InsertWorkload(BaseWorkload):
    name = "insert"
    description = "单行插入"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._next_id = 0

    def setup(self, rows: int):
        super().setup(rows)
        self._next_id = rows + 1

    def execute_once(self) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            f"INSERT INTO {self.table} VALUES (%s,%s,%s,%s)",
            (self._next_id, random.randint(1, 10**9),
             random_string(120), random_string(60)))
        self._next_id += 1
        return True


class UpdateWorkload(BaseWorkload):
    name = "update"
    description = "非索引列更新"

    def execute_once(self) -> bool:
        pk = random.randint(1, self._max_id)
        cur = self.conn.cursor()
        cur.execute(
            f"UPDATE {self.table} SET c = %s WHERE id = %s",
            (random_string(120), pk))
        return True


class DeleteInsert(BaseWorkload):
    name = "delete"
    description = "删除+插入（保持行数）"

    def execute_once(self) -> bool:
        pk = random.randint(1, self._max_id)
        cur = self.conn.cursor()
        cur.execute(f"DELETE FROM {self.table} WHERE id = %s", (pk,))
        cur.execute(
            f"INSERT INTO {self.table} VALUES (%s,%s,%s,%s)",
            (pk, random.randint(1, 10**9),
             random_string(120), random_string(60)))
        return True


class OLTPReadOnly(BaseWorkload):
    name = "oltp_ro"
    description = "OLTP 只读混合（SELECT 100%）"

    def execute_once(self) -> bool:
        r = random.random()
        if r < 0.5:
            pk = random.randint(1, self._max_id)
            cur = self.conn.cursor()
            cur.execute(f"SELECT c FROM {self.table} WHERE id = %s", (pk,))
            cur.fetchone()
        elif r < 0.9:
            start = random.randint(1, max(1, self._max_id - 10))
            cur = self.conn.cursor()
            cur.execute(
                f"SELECT c FROM {self.table} WHERE id BETWEEN %s AND %s",
                (start, start + 10))
            cur.fetchall()
        else:
            cur = self.conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) FROM {self.table} "
                f"WHERE k = %s", (random.randint(1, 1000),))
            cur.fetchone()
        return True


class OLTPReadWrite(BaseWorkload):
    name = "oltp_rw"
    description = "OLTP 读写混合（10% 写）"

    def execute_once(self) -> bool:
        r = random.random()
        cur = self.conn.cursor()
        if r < 0.6:
            pk = random.randint(1, self._max_id)
            cur.execute(f"SELECT c FROM {self.table} WHERE id = %s", (pk,))
            cur.fetchone()
        elif r < 0.85:
            start = random.randint(1, max(1, self._max_id - 10))
            cur.execute(
                f"SELECT c FROM {self.table} WHERE id BETWEEN %s AND %s",
                (start, start + 10))
            cur.fetchall()
        elif r < 0.92:
            pk = random.randint(1, self._max_id)
            cur.execute(
                f"UPDATE {self.table} SET c = %s WHERE id = %s",
                (random_string(120), pk))
        elif r < 0.97:
            pk = random.randint(1, self._max_id)
            cur.execute(f"DELETE FROM {self.table} WHERE id = %s", (pk,))
            cur.execute(
                f"INSERT INTO {self.table} VALUES (%s,%s,%s,%s)",
                (pk, random.randint(1, 10**9),
                 random_string(120), random_string(60)))
        else:
            cur.execute(
                f"INSERT INTO {self.table} VALUES (%s,%s,%s,%s)",
                (self._max_id + random.randint(1, 10000),
                 random.randint(1, 10**9),
                 random_string(120), random_string(60)))
        return True


class JoinWorkload(BaseWorkload):
    name = "join"
    description = "两表 JOIN"

    def setup(self, rows: int):
        super().setup(rows)
        # 副表
        cur = self.conn.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {self.table}_2")
        cur.execute(BENCH_TABLE_DDL.format(table=f"{self.table}_2"))
        # 每 10 行一条
        batch = [(i, i % 1000, 'x', 'y')
                 for i in range(1, rows + 1, 10)]
        for i in range(0, len(batch), 1000):
            chunk = batch[i:i+1000]
            ph = ','.join(['(%s,%s,%s,%s)'] * len(chunk))
            cur.execute(
                f"INSERT INTO {self.table}_2 VALUES {ph}",
                [v for row in chunk for v in row])
        self.conn.commit()

    def execute_once(self) -> bool:
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT a.id, a.c, b.c
            FROM {self.table} a
            JOIN {self.table}_2 b ON a.id = b.id
            WHERE a.id BETWEEN %s AND %s
            LIMIT 10
        """, (random.randint(1, self._max_id - 100),
              random.randint(1, self._max_id)))
        cur.fetchall()
        return True


# ==================== 注册表 ====================

WORKLOADS = {
    'point_select': PointSelect,
    'range_scan': RangeScan,
    'insert': InsertWorkload,
    'update': UpdateWorkload,
    'delete': DeleteInsert,
    'oltp_ro': OLTPReadOnly,
    'oltp_rw': OLTPReadWrite,
    'join': JoinWorkload,
}


def get_workload(name: str):
    return WORKLOADS.get(name)


def list_workloads():
    print("\n可用工作负载：\n")
    for name, cls in WORKLOADS.items():
        print(f"  {name:20s}  {cls.description}")
    print()