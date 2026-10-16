"""
表对象 - 集成 MVCC + 行级锁（修复版）

修复:
- MVCC add_undo / find_visible_version 传 table=self.name
- WAL 写入顺序
- notnull / primary 检查
- 主键重复检测

事务原子性 / 持久化 / 崩溃恢复批次（本文件）:
- **真 ROLLBACK**: 每条 UPDATE/DELETE 把自己的物理镜像
  (UndoAction) 压进 mvcc.physical_undo[txn_id]，abort 时由
  abort_txn() 倒序回放，把旧行**写回原页原槽位**；INSERT 的
  回滚是删除该行并摘掉索引项。旧实现只丢 MVCC 版本链节点，
  页上被覆写/物理删除的数据不会回来，ROLLBACK 是假的。
- **xmax 不再恒为 0**: DELETE / UPDATE 淘汰旧版本时把真实
  事务号写进旧行的 xmax，读路径按 read_view 过滤（见
  _is_visible()）。autocommit 没有真实事务号，用高位合成号
  （_next_autocommit_tid），mvcc 侧有 SYNTHETIC_TXN_FLOOR
  把它们当已提交。
- **锁不泄漏**: autocommit 语句不再用 txn_id=0 拿锁却永不释放
  （database.py 只 release 真实 tid）。_stmt_tid() 为每条
  autocommit 语句分配独立合成事务号，语句结束立刻
  lock_manager.release()。
- **崩溃恢复**: recover_from_wal() 用 WAL.analyze() 做
  redo(已提交) + undo(loser，倒序)，全部幂等；恢复成功后才
  truncate WAL。
- **log-before-data**: WAL 记录用 log() 写入缓冲后，立刻
  flush_to_os() 交给 OS，**然后**才改页。这样即使脏页被驱逐落盘
  后进程被硬杀，日志也一定读得回来，不会出现"页改了、日志没了"
  而无法撤销。
- **WAL 增长控制**: 干净 shutdown（Table.close）在刷完页之后
  truncate WAL；否则 WAL 会无限增长、下次启动还要做全量重放。
- **在线 checkpoint / WAL 轮转（本批次）**: 不再只在 shutdown 时才
  压缩 WAL。每条 autocommit 语句成功收尾后按**阈值**检查
  （`wal_checkpoint_bytes` / `wal_checkpoint_records`，可用
  `Table` 类属性或环境变量 `LIGHTDB_WAL_CHECKPOINT_BYTES` /
  `LIGHTDB_WAL_CHECKPOINT_RECORDS` 调整），达到就执行 checkpoint：
      1) 确认本表没有未提交事务（`_read_views` 为空），否则**推迟**；
      2) `BufferPool.flush()`：脏页写盘 + fsync；
      3) BTree 根指针落盘（fsync）；
      4) `WAL.truncate()`：段落清零，LSN 保持单调。
  顺序不可颠倒：**数据先落盘、日志后退休**，否则崩溃后无法 redo。
  未提交事务在 #1 被挡住，它的旧行镜像不会随轮转消失。
  另提供显式入口 `Table.checkpoint(force=...)` / `Database.checkpoint()`。
- **堆页定位缓存**: `_heap_insert` 原先每次从第 1 页扫到最后一页找
  插入目标，代价 O(总页数)/行。缓冲区只有 500 帧、而 .dat 涨到几千页
  之后，这种全扫会变成"每插一行驱逐一批脏页"的 LRU 颠簸（实测把
  10 万行量级的写入拖到小时级）。现在缓存"最后一个堆页"，页满了
  才去 `_space_pages`（vacuum 紧凑化过的页）里复用空洞，否则新建页。
  纯性能改动，不改变落盘格式。

持久化版本链批次（本文件，页/行布局变更）:
- **行头扩展**: ROW_HEADER 13 → 21 字节，新增 prev_page / prev_slot
  两个 uint32 指向前一版本在堆里的物理位置：
      [0:4]   xmin      创建该版本的事务号
      [4:8]   xmax      淘汰该版本（更新/删除）的事务号，0 = 仍存活
      [8]     flags     保留
      [9:13]  dlen      JSON 载荷长度
      [13:17] prev_page 前一版本所在堆页；0 = 无前驱（版本链起点）
      [17:21] prev_slot 前一版本槽位（prev_page == 0 时无意义）
  **旧数据文件不兼容**：旧行头只有 13 字节，解码时 dlen 之后的 8 字节
  会被当成 prev 指针（读到 JSON 载荷里的字节），版本链会指向垃圾位置。
  实测数据目录都是新建的（各测试自己的 data_* 子目录），因此不做迁移；
  若要读旧文件必须重建表。
- **UPDATE 不再原地覆写**: 旧版本行头 xmax 就地写成本事务号（旧行
  数据 + 它自己的 prev 指针原样保留），新版本另写一槽，行头
  xmin = 本事务号、prev = 旧版本位置。索引（BTree，pk -> (page,slot)
  单一键）仍指向**最新**版本，于是「索引头 + prev 链」构成一条
  持久化的版本链（方案 A，见 _read_version）。
- **DELETE 不再摘索引**: 只把行头 xmax 置为真实事务号（墓碑），
  索引项**保留**指向该墓碑。否则重启后老快照连墓碑的位置都找不到，
  「删掉的行老快照仍应看得见」无法实现。
- **重启后老快照可见**: 版本链在页上，重启后 ReadView 只要拿到
  同样的 (txn_id, active, min_id, max_id, aborted) 就能沿链回溯到
  被覆盖前的旧值（tests/test_mvcc_persistence.py 实测）。
- **vacuum**: Table.vacuum(horizon) / Database.vacuum() 回收
  「对所有活跃快照都不可见」的旧版本，并缝合 prev 链。

性能说明: autocommit 语句的隐式 COMMIT 走 WAL.commit_async()
（只 flush 到 OS，不 fsync）。早期版本对每条 autocommit INSERT
都 fsync，实测 3000 行插入要 200s+（14 rows/s）。真正要求
掉电不丢的是显式 COMMIT / abort / flush / close，它们仍然 fsync。

已知限制:
- 合成事务号是进程内递减的高位 uint32（>= SYNTHETIC_TXN_FLOOR），
  重启后从 0xFFFFFFFF 重新开始，理论上会与上一进程写进页里的
  合成号重复；因为这类号一律按"已提交"处理，重复不会破坏可见性。
- 没有页 LSN / checkpoint 联动，恢复是"日志全量重放 + 幂等覆盖"，
  但 WAL **已被 checkpoint 限定在一个段落内**（默认 1 MB / 5000 条），
  所以重启重放的量是有界的；恢复耗时不再随运行时长增长。
- checkpoint 只在本表没有未提交事务时才执行；一个长时间不提交的
  显式事务会把 WAL 轮转推迟到它结束（安全优先，此时 WAL 仍会增长）。
- 非整型主键不建索引（_rebuild_index 只收 isinstance(pk, int)），
  与本次修复无关，保持原状。
- **没有 vacuum 时空间只增不减**：每次 UPDATE 都会多留一个旧版本行、
  每次 DELETE 都会留墓碑，堆页只增不回收，直到显式调用 vacuum()。
- **不做 vacuum 时版本链会变长**：链越长，老快照的回溯扫描越慢
  （O(版本数) 次单槽读取）。
- SQL 层（lightdb/sql/plan_executor.py，本批次不允许改动）的
  SeqScan 只判断索引头那一行的可见性、不走 prev 链，所以
  **SQL 语句读不到老快照的旧版本**；Python API 的
  Table.select(..., read_view=...) 才能沿链回溯。
  （JOIN 走的 PlanExecutor 一定带 read_view，所以墓碑不会"复活"成
   可见行；DELETE 后索引里留着墓碑，这一点依赖读路径的可见性判断。）
- 写路径把"本读视图看得见的版本"当作淘汰对象。行锁保证同一主键
  不会被并发改写，所以它总是索引头；只有拿"重启后重建的历史快照"
  去写时它才可能是链中间的版本，此时索引前移会让中间版本之后更新的
  版本暂时不可达（读老快照 + 写同一行 = 不支持）。
- 主键复用（DELETE 后重新 INSERT 同一主键）会把新版本挂到墓碑后面，
  于是老快照仍读到被删的旧值 —— 语义上等价于"重插",符合 MVCC。
"""
import json
import os
import re
import struct
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..storage.page import PAGE_TYPE_HEAP
from ..storage.buffer_pool import BufferPool
from ..storage.btree import BTree
from ..storage.wal import WAL, is_implicit_commit_tid
from ..storage.lock_manager import (
    LockManager, LockType, DeadlockError)
from ..storage.mvcc import MVCCManager, UndoAction
from ..logger import logger


# 行头布局（见文件头说明）：
#   xmin(4) + xmax(4) + flags(1) + dlen(4) + prev_page(4) + prev_slot(4)
#   13 字节是"旧布局前缀"，前 13 字节的语义没变，外部按
#   struct.unpack_from('<IIBI', rb, 0) 读 xmin/xmax/dlen 的代码仍然可用。
ROW_HEADER = 21
ROW_HEADER_LEGACY13 = 13

# prev_page == 0 表示"没有前一版本"。
# 安全性: BufferPool.new_page 的页号从 0（元数据页）开始递增，
# 堆页永远是 >= 1，所以 0 不可能是一个合法堆页号。
NO_PREV = 0

# WAL 段落阈值（checkpoint 触发条件）。None 表示取环境变量/下面的默认值。
# 默认 1 MB：既让 WAL 有界（目标 ≤ 32 MB 的 1/32），又让崩溃恢复要
# 重放的段落足够小（实测 ~1 MB 段落恢复约 0.5 s，要求 < 2 s）。
WAL_CHECKPOINT_BYTES_DEFAULT = 1 * 1024 * 1024
WAL_CHECKPOINT_RECORDS_DEFAULT = 5000
WAL_CHECKPOINT_BYTES_ENV = 'LIGHTDB_WAL_CHECKPOINT_BYTES'
WAL_CHECKPOINT_RECORDS_ENV = 'LIGHTDB_WAL_CHECKPOINT_RECORDS'

# autocommit 语句用的合成事务号。
#
# 行头里 xmin/xmax 是 struct '<I'（uint32），所以事务号必须落在
# [0, 2**32) 内，不能用负数。这里从 2**32-1 往下分配，形成一段
# 独立的高位区间，与 mvcc 从 1 开始递增的真实事务号永不冲突；
# 配套地在 MVCCManager 里加了一个"合成号恒视为已提交"的阈值判断。
SYNTHETIC_TXN_BASE = 0xFFFFFFFF
_synth_tid = SYNTHETIC_TXN_BASE
_synth_tid_lock = threading.Lock()


def _next_autocommit_tid() -> int:
    """分配一个 autocommit 语句专用的合成事务号（高位区间递减）"""
    global _synth_tid
    with _synth_tid_lock:
        _synth_tid -= 1
        if _synth_tid <= SYNTHETIC_TXN_BASE // 2:
            # 极端情况下回绕，正常 workload 到不了这里
            _synth_tid = SYNTHETIC_TXN_BASE
        return _synth_tid


def coerce(value, col_type: str):
    """类型转换

    `col_type` 现在可能是**带参数**的真实类型（`VARCHAR(20)`、
    `DECIMAL(10,2)`、`INT(11) UNSIGNED`）——解析器不再把长度抹掉
    （元数据诚实化的要求：`SHOW COLUMNS` 必须报真实声明）。因此比较前
    先取基类型名：`VARCHAR(20)` → `VARCHAR`。
    """
    if value is None:
        return None
    raw = str(col_type or '').strip().upper()
    m = re.match(r'^([A-Z_][A-Z0-9_]*)', raw)
    if not m:
        # 类型名本身无法解析：显式报错，不要掉进最后的 `return str(value)`
        # 分支把任意值都当字符串接受（那是静默的类型欺骗）。
        raise ValueError(f"无法识别的列类型: {col_type!r}")
    t = m.group(1)
    try:
        if t in ('INT', 'BIGINT'):
            return int(value)
        if t in ('FLOAT', 'DOUBLE', 'DECIMAL'):
            return float(value)
        if t == 'BOOLEAN':
            if isinstance(value, str):
                return value.upper() in ('1', 'TRUE', 'YES', 'ON')
            return bool(value)
        return str(value)
    except (ValueError, TypeError):
        raise ValueError(f"无法将 {value!r} 转换为 {col_type}")


class Table:
    """表对象"""

    #: checkpoint 触发阈值（测试/调用方可以直接改类属性；None = 用环境变量/默认值）
    wal_checkpoint_bytes = None
    wal_checkpoint_records = None

    def __init__(self, name: str, cols: List[Dict], data_dir: str,
                 lock_manager: LockManager = None,
                 mvcc: MVCCManager = None):
        self.name = name
        self.cols = cols
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 最后一个堆页的缓存（见 _heap_insert；纯性能优化，不改变语义）
        self._last_heap_pid = None
        # vacuum 紧凑化过、仍有可复用空闲空间的页号（后进先出复用）
        self._space_pages = []
        # 最近一次 vacuum 在页内真正释放出的字节数（供巡检/日志）
        self.last_vacuum_freed_bytes = 0

        self.lock_manager = lock_manager or LockManager()
        self.mvcc = mvcc or MVCCManager()

        self.pk_idx = next(
            (i for i, c in enumerate(cols) if c.get('primary')), 0)
        self.pk_col = cols[self.pk_idx]['name']
        self.pk_type = cols[self.pk_idx]['type']

        self.heap_path = str(self.data_dir / f"{name}.dat")
        self.btroot_path = self.data_dir / f"{name}.btroot"
        self.wal_path = str(self.data_dir / f"{name}.wal")

        self.bp = BufferPool(self.heap_path, capacity=500)
        self.wal = WAL(self.wal_path)

        root_id = self._load_btree_root()
        self.bt = BTree(self.bp, root_page_id=root_id)
        self._btroot_saved = root_id
        #: 上一次**同步写过**的根页号（None = 盘上那份的持久性未知）
        self._btroot_synced = None
        self._save_btree_root(self.bt.root_page_id)

        self._rebuild_index()
        self._lock = threading.RLock()
        self._next_pk = self._calc_next_pk()

        # 事务号 -> ReadView（供写路径的可见性判断 + 恢复用）
        self._read_views: Dict[int, Any] = {}

        # 崩溃恢复期的"版本位置重映射"（见 _resolve_old_loc），空闲时为空
        self._recovery_remap: Dict[tuple, tuple] = {}

        # 分区元数据（CREATE TABLE ... PARTITION BY ...）：None = 未分区。
        # `_partition_spec` 是解析器给的原始描述（随 Database 元数据持久化），
        # `_partition` 是 PartitionManager 实例（SHOW PARTITIONS / prune 用）。
        self._partition_spec = None
        self._partition = None
        self._partition_col_idx = None

        # 全文索引（CREATE FULLTEXT INDEX）：列名 -> InvertedIndex
        self._fulltext = None

        # CHECK 约束（语法/求值在 `sql/checks.py`，这里只持有定义）：
        #   * 列级约束随列定义走（`cols[i]['check']`）——它本来就在元数据
        #     的 `cols` 里；旧实现只是**从来没人读它**，于是 `CHECK` 完全
        #     不生效；
        #   * 表级约束（含 `CONSTRAINT 名字 CHECK (...)`）由
        #     `Database.create_table` 规范化命名后 `set_checks()` 挂上，
        #     随 `_meta.json` 的 `checks` 一起持久化（重启后仍生效）。
        self._checks = []
        self._refresh_check_flag()

    # ==================== CHECK 约束 ====================

    def _refresh_check_flag(self):
        """缓存"这张表有没有 CHECK"，避免每行写入都重扫列定义"""
        self._has_checks = bool(self._checks) or any(
            (c.get('check') or {}).get('expr') for c in self.cols)

    def set_checks(self, checks):
        """挂上表级 CHECK 约束（定义已由 `Database.create_table` 校验+命名）"""
        self._checks = [dict(c) for c in (checks or [])
                        if isinstance(c, dict) and c.get('expr')]
        self._refresh_check_flag()
        return self._checks

    def check_constraints(self):
        """本表全部 CHECK 约束（列级在前、表级在后，顺序稳定）"""
        out = []
        for c in self.cols:
            ck = c.get('check') or {}
            if ck.get('expr'):
                out.append({'name': ck.get('name') or '',
                            'expr': ck['expr'],
                            'column': c['name']})
        for ck in self._checks:
            if ck.get('expr'):
                out.append({'name': ck.get('name') or '',
                            'expr': ck['expr'],
                            'column': ck.get('column')})
        return out

    def _validate_checks(self, row):
        """写入前校验全部 CHECK；违规抛 `CheckConstraintError`（ValueError）

        语义（与 MySQL 一致）：

        * 表达式求值为 **UNKNOWN**（NULL 参与比较）→ **放行**；
        * 求值为 FALSE → 抛错，文案带 `ERROR 3819 (HY000)` 与列/取值；
        * 求值过程出错（列解析不到、函数报错等）→ 同样显式抛错，
          **绝不**"算不出来就当通过"。
        """
        if not getattr(self, '_has_checks', False):
            return
        from ..sql.checks import (
            CheckConstraintError, RowResolver, evaluate_check,
            format_violation)
        names = [c['name'] for c in self.cols]
        for spec in self.check_constraints():
            resolver = RowResolver(names, row, self.name)
            verdict = evaluate_check(spec['expr'], resolver,
                                     context=self.name)
            if verdict is False:
                col = spec.get('column')
                has_value = bool(col) and col in names
                raise CheckConstraintError(
                    format_violation(
                        self.name,
                        spec['name'] or f"{self.name}_chk",
                        spec['expr'], col,
                        row[names.index(col)] if has_value else None,
                        has_value),
                    table=self.name, constraint=spec.get('name'),
                    column=col)

    def _apply_set_pairs(self, old_row, set_pairs):
        """按 SET 列表算出新行（写入路径与"写入前整体校验"共用一份语义）"""
        new = list(old_row)
        for col, val in set_pairs.items():
            ci = self._col_idx(col)
            if ci == self.pk_idx:
                raise ValueError("不允许修改主键")
            new[ci] = coerce(val, self.cols[ci]['type'])
        return new

    def _validate_update_checks(self, pks, set_pairs, read_view):
        """UPDATE 的写入前整体校验（任一目标行违规 → 整条语句不写任何行）"""
        for pk in pks:
            anchor = self._read_version(pk, read_view)
            if anchor is None:
                continue
            _, _xmin, _xmax, apid, asid = anchor
            page = self.bp.get_page(apid)
            if page is None:
                continue
            r = self._decode_row_ver(page.get_row(asid))
            if r is None:
                continue
            self._validate_checks(self._apply_set_pairs(r[0], set_pairs))

    # ==================== 生成列 ====================

    def _apply_generated(self, values):
        """按表达式计算生成列的值（GENERATED ALWAYS AS (expr) [STORED]）

        本引擎把生成列按 **STORED** 语义处理：写入时就地算好并随行落盘，
        读路径不需要再求值，SELECT 到的就是真实存储值（VINTUAL 的"读时
        计算"在纯 Python 引擎里没有收益，且会让索引/恢复路径各写一份）。
        表达式由 `sql/generated.py` 求值（支持 UPPER/LOWER/CONCAT/ABS/
        COALESCE/算术），解析失败或求值失败都**显式抛错**，不写 NULL 了事。
        """
        if not any(c.get('generated') for c in self.cols):
            return values
        from ..sql.generated import eval_expr as _gen_eval
        values = list(values)
        col_names = [c['name'] for c in self.cols]
        for i, c in enumerate(self.cols):
            gen = c.get('generated')
            if not gen:
                continue
            try:
                values[i] = _gen_eval(gen['expr'], values, col_names)
            except Exception as e:
                raise ValueError(
                    f"生成列 {c['name']} 的表达式 {gen['expr']} 求值失败: {e}")
        return values

    # ==================== 分区 ====================

    def set_partition(self, spec):
        """挂上分区描述（spec 为 parser._parse_partition 的返回值）

        RANGE / LIST / HASH 都映射到 `sql/partition.py` 的 PartitionManager，
        这样 `_partition.prune(where)` / `Partition.match(pk)` 与执行器、用例
        （tests/test_batch13.py 读 `tbl._partition.partitions`）共用一份实现。
        """
        self._partition_spec = spec
        if not spec:
            self._partition = None
            self._partition_col_idx = None
            return None
        from ..sql.partition import PartitionManager
        pm = PartitionManager()
        ptype = str(spec.get('type', '')).upper()
        if ptype == 'HASH':
            pm.define_hash(spec.get('col'), int(spec.get('num') or 4))
        elif ptype == 'RANGE':
            pm.define_range(
                spec.get('col'),
                [(r.get('lo'), r.get('hi'), r.get('name'))
                 for r in (spec.get('ranges') or [])])
        elif ptype == 'LIST':
            pm.define_list(
                spec.get('col'),
                [(l.get('values'), l.get('name'))
                 for l in (spec.get('lists') or [])])
        else:
            raise ValueError(f"不支持的分区类型: {ptype}")
        self._partition = pm
        names = [c['name'] for c in self.cols]
        col = spec.get('col')
        if col in names:
            self._partition_col_idx = names.index(col)
        return pm

    # ==================== 全文索引 ====================

    def create_fulltext_index(self, columns, name=None, if_not_exists=False):
        """在列上建全文索引（倒排索引），并对**现有行**建索引

        `columns` 可以是单列名字符串或列名列表。索引挂在表对象上，
        之后每次 insert / delete / update 都会同步维护（见 _index_fulltext /
        _unindex_fulltext），保证 MATCH ... AGAINST 的结果来自真实数据。
        索引本身不持久化（是派生数据）：重开库后需重新 CREATE FULLTEXT INDEX，
        这一点在交付说明里登记。
        """
        from ..sql.fulltext import FullTextManager
        cols = [columns] if isinstance(columns, str) else list(columns or [])
        if not cols:
            raise ValueError("全文索引至少需要一列")
        names = [c['name'] for c in self.cols]
        for c in cols:
            if c not in names:
                raise KeyError(f"列 {c} 不存在")
        if if_not_exists and self._fulltext is not None \
                and all(c in self._fulltext.indexes for c in cols):
            return False
        if self._fulltext is None:
            self._fulltext = FullTextManager()
        for c in cols:
            self._fulltext.create_index(c)
        # 用现有行回填索引（真数据，不是空索引）
        with self._lock:
            for pk, page_id, slot_id in self.bt.scan():
                page = self.bp.get_page(page_id)
                rb = page.get_row(slot_id) if page is not None else None
                vals = self._decode_row(rb) if rb is not None else None
                if vals is None:
                    continue
                for c in cols:
                    self._fulltext.indexes[c].add(pk, vals[names.index(c)])
        return True

    def has_fulltext_index(self, column) -> bool:
        return (self._fulltext is not None
                and column in self._fulltext.indexes)

    def fulltext_search(self, column, query):
        if not self.has_fulltext_index(column):
            raise KeyError(f"列 {column} 上没有全文索引")
        return self._fulltext.search(column, query)

    def _index_fulltext(self, pk, row):
        if self._fulltext is None:
            return
        try:
            self._fulltext.index_row(
                pk, row, [c['name'] for c in self.cols])
        except Exception as e:                   # pragma: no cover
            logger.warn(f"全文索引写入失败 {self.name}/{pk}: {e}")

    def _unindex_fulltext(self, pk):
        if self._fulltext is None:
            return
        try:
            self._fulltext.remove_row(pk)
        except Exception as e:                   # pragma: no cover
            logger.warn(f"全文索引删除失败 {self.name}/{pk}: {e}")

    # ==================== 事务辅助 ====================

    def _stmt_tid(self, txn_id: int) -> int:
        """
        归一化事务号。
        调用方传 0 表示 autocommit（executor 在没有显式事务时不带
        txn_id）。此时分配一个**独立合成事务号**，这样：
          - 行头的 xmin/xmax 拿到真实非零值，不再恒为 0；
          - 锁归属于这个合成号，语句结束就能精确 release，
            不会像 txn_id=0 那样永远泄漏。
        """
        return txn_id if txn_id else _next_autocommit_tid()

    def _cleanup_stmt(self, tid: int, txn_id: int,
                      pks=None, read_view=None):
        """
        autocommit 语句收尾。

        必须做两件事，缺一不可：
        1) 写 COMMIT 记录（fsync）。autocommit 的 WAL 记录如果只有
           INSERT/UPDATE/DELETE 而没有 COMMIT，崩溃恢复的
           WAL.analyze() 会把它当成 **loser 事务** 去 undo，于是
           "已提交但页还没刷" 的 autocommit 写入会被错误地撤销
           （实测：干净 shutdown 后重开只剩显式事务那一行）。
        2) 释放行锁。旧实现用 txn_id=0 拿锁、database.py 只 release
           真实 tid，锁永久泄漏。
        """
        if txn_id:
            return
        try:
            self.wal.commit_async(tid)
        except Exception as e:
            logger.warn(f"autocommit COMMIT 落盘失败: {e}")
        self.lock_manager.release(tid)
        self._read_views.pop(tid, None)

    def register_read_view(self, txn_id: int, read_view):
        if txn_id:
            self._read_views[txn_id] = read_view

    def _is_visible(self, xmin, xmax, read_view) -> bool:
        """
        读路径可见性。
        read_view 为 None（autocommit 读）时按"已提交即可见"处理：
        xmax == 0 可见；xmax 非 0 说明该行已被某事务删除/更新，
        而本读没有快照 → 不可见（删除已生效）。
        注意：负数 xmax 是 autocommit 删除的标记，同样不可见。
        """
        if read_view is None:
            return xmax == 0
        return read_view.is_visible(xmin, xmax)

    # ==================== 持久化版本链 ====================

    def _chain_versions(self, start_loc, limit: int = 4096):
        """
        从 start_loc 沿 prev 指针往回走，逐个 yield
        (page_id, slot_id, rb, values, xmin, xmax, prev_page, prev_slot)。

        为什么需要: 索引只有 pk -> (page,slot) 单一定位，指向最新版本；
        被 UPDATE 覆盖/被 DELETE 墓碑化的旧版本只能靠行头里的
        prev_page/prev_slot 回溯 —— 这张"链"是**落在页上**的，
        所以重启后仍然可走（这正是本次改造的核心）。

        limit 是防御性上界（断链/人为损坏时不至于死循环），
        同时用 seen 集合防止环。
        """
        loc = start_loc
        seen = set()
        n = 0
        while loc is not None and n < limit:
            if loc in seen:
                return
            seen.add(loc)
            n += 1
            pid, sid = loc
            page = self.bp.get_page(pid)
            rb = page.get_row(sid) if page is not None else None
            r = self._decode_row_ver(rb)
            if r is None:
                return
            values, xmin, xmax, pp, ps = r
            yield pid, sid, rb, values, xmin, xmax, pp, ps
            if pp == NO_PREV or pp is None:
                return
            loc = (pp, ps)

    def _read_version(self, pk, read_view):
        """
        沿版本链找对 read_view 可见的**最新**版本。

        可见性规则（与 ReadView.is_visible 一致）:
          xmin 已提交且对本读视图可见，且
          (xmax == 0 或 xmax 对本读视图不可见 —— 删除/更新它的事务
           未提交、或已回滚) → 该版本可见。
        已回滚事务写下的新版本在链上会被跳过（xmax == xmin == 事务号
        对任何读视图都不可见），回滚后的旧版本（xmax 被复位成 0）
        自然重新可见。

        返回 (values, xmin, xmax, page_id, slot_id) 或 None。
        """
        loc = self.bt.search(pk)
        if loc is None:
            return None
        for pid, sid, _rb, values, xmin, xmax, _pp, _ps in \
                self._chain_versions(loc):
            if self._is_visible(xmin, xmax, read_view):
                return values, xmin, xmax, pid, sid
        return None

    def _visible_pks(self, read_view):
        """全表扫描：返回 [(pk, values, xmin, xmax, pid, sid)]（当前读视图可见的版本）"""
        out = []
        for k, pid, sid in self.bt.scan():
            for vpid, vsid, _rb, values, xmin, xmax, _pp, _ps in \
                    self._chain_versions((pid, sid)):
                if self._is_visible(xmin, xmax, read_view):
                    out.append((k, values, xmin, xmax, vpid, vsid))
                    break
        return out

    # ==================== 元数据 ====================

    def _load_btree_root(self):
        if self.btroot_path.exists():
            try:
                return int(self.btroot_path.read_text().strip())
            except Exception:
                return None
        return None

    def _save_btree_root(self, root_id: int, sync: bool = False):
        """
        写 BTree 根页号。

        - `sync=True`（**checkpoint / close / 恢复收尾**用）：确保这个根
          页号**已经 fsync**。原因：checkpoint 以"根指针已落盘"为前提
          退休 WAL，只写进 OS 缓冲的话，掉电后根指针可能仍是旧值，
          已 fsync 的数据页会变成"找不到根的孤儿页"（索引不可达）。
          若该值此前已经同步写过（`_btroot_synced`），直接跳过 ——
          根页号只在 B+Tree 分裂/合并时变，绝大多数 checkpoint/close
          因此不需要产生任何 syscall。
        - `sync=False`（普通 flush 用）：**只在根页号变化时**才写，且不
          fsync。无条件 open+write 会让每个事务多一次文件写，实测把
          test_txn_atomicity 一类用例从 18s 拖到 36s。
        """
        if sync and root_id == self._btroot_synced:
            return
        if not sync and getattr(self, '_btroot_saved', None) == root_id:
            return
        data = str(root_id).encode('utf-8')
        with open(self.btroot_path, 'wb') as f:
            f.write(data)
            f.flush()
            if sync:
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        self._btroot_saved = root_id
        if sync:
            self._btroot_synced = root_id

    def _rebuild_index(self):
        """
        索引重建：只在 BTree 为空时扫描 heap 页重建。

        新布局下页上同一个 pk 会有多个版本（活版本 + 若干墓碑），
        索引必须指向**版本链的头**（最新的那个版本），否则
        _read_version 从索引出发会走不到链尾的老版本。

        链头的判定不靠事务号大小：autocommit 的合成事务号是从
        0xFFFFFFFF 往下递减的高位号，数值上比真实事务号大得多，
        按 max(xmin) 选头会选错。这里按 prev 指针判定：
        **没有被本 pk 任何其它版本当作 prev 的版本就是链头**。
        若页上出现断链（人为损坏），退化为取 xmin 最大者。

        墓碑（xmax != 0）**也要建索引**：DELETE 后索引项必须保留指
        向墓碑，重启后老快照才可能沿链找到被删除的旧值；可见性由
        _read_version/_is_visible 负责，已删除的行不会"复活"。
        """
        if self.bt.count() > 0:
            return

        versions = {}       # pk -> [(pid, sid, xmin, prev_page, prev_slot)]
        for pid in range(1, self.bp.total_pages()):
            page = self.bp.get_page(pid)
            if page is None or page.page_type != PAGE_TYPE_HEAP:
                continue
            for sid, rb in page.iter_rows():
                r = self._decode_row_ver(rb)
                if not r:
                    continue
                row, xmin, _xmax, pp, ps = r
                pk = row[self.pk_idx]
                if isinstance(pk, int):
                    versions.setdefault(pk, []).append(
                        (pid, sid, xmin, pp, ps))

        for pk, vers in versions.items():
            referenced = {(v[3], v[4]) for v in vers if v[3] != NO_PREV}
            heads = [(v[0], v[1], v[2]) for v in vers
                     if (v[0], v[1]) not in referenced]
            if not heads:
                heads = [(v[0], v[1], v[2]) for v in vers]
            pid, sid, _xmin = max(heads, key=lambda h: h[2])
            self.bt.insert(pk, pid, sid)

    def _calc_next_pk(self) -> int:
        max_pk = 0
        for k, _, _ in self.bt.scan():
            if isinstance(k, int) and k > max_pk:
                max_pk = k
        return max_pk + 1

    def _col_idx(self, name: str) -> int:
        for i, c in enumerate(self.cols):
            if c['name'] == name:
                return i
        raise KeyError(f"列 {name} 不存在")

    # ==================== 行编解码 ====================

    def _encode_row(self, values: List,
                    xmin: int = 0, xmax: int = 0,
                    prev_page: int = NO_PREV,
                    prev_slot: int = 0) -> bytes:
        """
        编码一个版本行。布局见文件头（21 字节行头 + JSON 载荷）。

        prev_page/prev_slot: 本版本的前一版本在堆里的位置，
        0/0 表示版本链起点（本行是该 pk 的第一个版本）。
        """
        jd = json.dumps(
            values, ensure_ascii=False, default=str).encode('utf-8')
        return struct.pack(
            '<IIBIII', xmin, xmax, 0, len(jd),
            prev_page, prev_slot) + jd

    def _decode_row_ver(self, rb: bytes):
        """
        解出新布局的完整版本信息:
            (values, xmin, xmax, prev_page, prev_slot)
        返回 None 表示行字节非法/被截断。
        """
        if not rb or len(rb) < ROW_HEADER:
            return None
        try:
            xmin, xmax, _flags, dlen, pp, ps = struct.unpack_from(
                '<IIBIII', rb, 0)
            if len(rb) < ROW_HEADER + dlen:
                return None
            values = json.loads(
                rb[ROW_HEADER:ROW_HEADER + dlen].decode('utf-8'))
            return values, xmin, xmax, pp, ps
        except Exception:
            return None

    def _decode_row_full(self, rb: bytes):
        """
        兼容旧调用方的三元组接口 (values, xmin, xmax)。

        lightdb/sql/plan_executor.py、lightdb/performance/parallel.py
        以及 tests/ 都按这个形状解包，因此**不能**改成四元组；
        需要 prev 指针的内部路径请用 _decode_row_ver。
        """
        r = self._decode_row_ver(rb)
        if r is None:
            return None
        return r[0], r[1], r[2]

    def _decode_row(self, rb: bytes):
        r = self._decode_row_full(rb)
        return r[0] if r else None

    # ==================== 写操作 ====================

    def insert(self, values: List, txn_id: int = 0,
               read_view=None) -> int:
        """插入一行

        txn_id=0 表示 autocommit：内部会分配合成事务号，
        WAL 仍按 0 号记录（WAL.find_committed_transactions 把 0 号
        当隐式提交），锁则挂在合成号上并在语句结束释放。
        """
        with self._lock:
            tid = self._stmt_tid(txn_id)
            if txn_id:
                self.register_read_view(txn_id, read_view)
            owned = bool(txn_id)
            ok = False
            try:
                n = self._insert_locked(
                    values, tid, read_view, owned)
                ok = True
                return n
            finally:
                self._cleanup_stmt(tid, txn_id)
                if ok:
                    self._safe_maybe_checkpoint()

    def _safe_maybe_checkpoint(self):
        """阈值 checkpoint 的调用壳：checkpoint 失败不能影响语句结果"""
        try:
            self._maybe_checkpoint()
        except Exception as e:                    # pragma: no cover - 防御
            logger.warn(f"checkpoint {self.name} 失败（忽略，WAL 保持原样）: {e}")

    def _column_default(self, col: dict):
        """列的**真实**默认值（没有声明 DEFAULT 时为 None）

        `cols[i]['default']` 由解析器按 MySQL 取值形态解析：
        数字存字符串（交给 `coerce` 按列类型转）、字符串是 Python str、
        当前时间函数存表达式原文（这里真的求值，不假装）。
        """
        if not col.get('has_default'):
            return None
        raw = col.get('default')
        if raw is None:
            return None
        if col.get('generated'):
            return raw
        t = str(col.get('type') or '').strip().upper()
        m = re.match(r'^([A-Z_][A-Z0-9_]*)', t)
        base = m.group(1) if m else t
        if base in ('INT', 'BIGINT', 'FLOAT', 'DOUBLE', 'DECIMAL',
                    'BOOLEAN'):
            # 数字字面量（解析器存的是原文）；转不了就交给 coerce 报错
            try:
                return coerce(raw, col.get('type'))
            except (ValueError, TypeError):
                return raw
        if base in ('DATETIME', 'DATE', 'TIME'):
            # `DEFAULT CURRENT_TIMESTAMP` 一族：真的求值（本引擎的
            # 「当前时间」就是 `sql/functions/datetime_funcs.py::now()`，
            # 与 `NOW()` 同一个实现），绝不把函数名当字符串存进数据里。
            try:
                from ..sql.functions.datetime_funcs import now as _now
                return _now()
            except Exception:
                return raw
        return raw

    def _insert_locked(self, values: List, tid: int,
                       read_view, owned: bool) -> int:
        # 补齐列 —— 用**真实的列默认值**补，而不是一律补 None。
        #
        # 解析器现在真的解析并保存 `DEFAULT`（`cols[i]['default']`），
        # 所以 `SHOW COLUMNS` 报出的 Default 必须真的会被用上；否则那张
        # 表就是"元数据说有默认值、引擎却不用"的又一处不诚实。显式传入
        # 的 NULL 仍然按原子值处理（MySQL：显式 NULL 不进默认值）。
        if len(values) < len(self.cols):
            values = list(values) + [
                self._column_default(self.cols[i])
                for i in range(len(values), len(self.cols))]

        # 生成列（GENERATED ALWAYS AS (...) STORED）：写入前按表达式真算，
        # 用户给的值被覆盖（MySQL 里给生成列赋值本来就是错误，这里不静默
        # 保留用户值，而是以表达式结果为准）。
        values = self._apply_generated(values)

        # 类型转换 + 约束检查
        row = []
        for i, c in enumerate(self.cols):
            v = coerce(values[i], c['type'])
            if c.get('notnull') and v is None:
                raise ValueError(
                    f"列 {c['name']} 不允许为空")
            row.append(v)

        # 主键
        pk = row[self.pk_idx]
        if pk is None:
            pk = self._next_pk
            row[self.pk_idx] = pk

        # CHECK 约束：两条写入路径（SQL `INSERT` 与 `Table.insert` 直写）
        # 都汇到这里，因此这一处校验覆盖全部入口（SQL `UPDATE` 见
        # `_update_locked`）。放在拿锁 / 写 WAL **之前** —— 违规时不会留下
        # 任何半条记录。违规抛 `CheckConstraintError`（ValueError 子类），
        # SQL 层据此返回 `ERROR 3819 (HY000)`。
        self._validate_checks(row)

        # 加锁
        self._acquire_row_lock(tid, pk)

        # 检查主键重复 + 找"前一版本"（新布局：索引头可能就是墓碑）
        prev_page, prev_slot = NO_PREV, 0
        loc = self.bt.search(pk)
        if loc is not None:
            page = self.bp.get_page(loc[0])
            rb = page.get_row(loc[1]) if page is not None else None
            r = self._decode_row_ver(rb)
            if r is not None:
                _, old_xmin, old_xmax = r[0], r[1], r[2]
                # 只有"当前可见"的行才算重复；已被本事务或
                # 其他事务标记删除的历史版本可以复用主键。
                visible = self._is_visible(
                    old_xmin, old_xmax, read_view)
                if visible:
                    raise ValueError(f"主键重复: {pk}")
                # ★ 复用墓碑/不可见旧版本的主键时，把新版本接到它后面，
                #   否则索引项被覆盖后，老快照就再也找不到被删的旧值。
                prev_page, prev_slot = loc[0], loc[1]

        # WAL（log-before-data）。flush 到 OS 必须发生在改页**之前**：
        # 否则脏页可能被驱逐落盘，而日志还留在 Python 缓冲里 ——
        # 崩溃后就出现"页改了、日志没了"，恢复无法撤销。
        self.wal.log(tid, 'INSERT', {
            'table': self.name, 'pk': pk, 'row': row,
            'prev': [prev_page, prev_slot],
        })
        self.wal.flush_to_os()

        # 写堆：新版本行头 xmin = 本事务号，prev 指向被复用的旧版本
        rb = self._encode_row(row, xmin=tid, xmax=0,
                              prev_page=prev_page, prev_slot=prev_slot)
        page_id, slot_id = self._heap_insert(rb)
        if page_id is None:
            raise RuntimeError("存储空间不足")

        # 写索引：指向最新版本（覆盖旧的索引项）
        self.bt.insert(pk, page_id, slot_id)
        self._next_pk = max(self._next_pk, pk + 1)
        # 全文索引与数据同步（有全文索引时才做事）
        self._index_fulltext(pk, row)

        # 物理 undo：本事务插入的行，回滚时删除
        if owned:
            self.mvcc.push_undo(tid, UndoAction(
                'INSERT', self.name, pk, txn_id=tid,
                page_id=page_id, slot_id=slot_id))
        return 1

    def _acquire_row_lock(self, tid, pk):
        """拿行级 X 锁，超时/死锁统一转 RuntimeError"""
        try:
            ok = self.lock_manager.acquire(
                tid, (self.name, pk), LockType.X, timeout=5.0)
            if not ok:
                raise RuntimeError("获取锁超时")
        except DeadlockError as e:
            raise RuntimeError(str(e))

    def _heap_insert(self, rb: bytes):
        """
        往堆里插入一行，返回 (page_id, slot_id)。

        选页顺序：
          1) 缓存的"最后一个堆页"（页号最大的堆页，与原实现一致）；
          2) 该页放不下时，从后往前找**还有足够空闲空间**的堆页
             —— 这些空间来自 vacuum 的页内紧凑化（Page.compact），
             没有它，回收出来的空洞永远复用不到（插入是追加式的）；
          3) 都放不下才新建页。

        定位仍是纯优化：不改变落盘格式，也不改变"能放下就放下"的
        语义；唯一变化是真空化过的旧页会被重新利用，
        .dat 不再只增不减。
        """
        need = len(rb) + 4          # 行数据 + 一个槽位项
        last = None
        cached = self._last_heap_pid
        if cached is not None:
            last = self.bp.get_page(cached)
            if last is None or last.page_type != PAGE_TYPE_HEAP:
                last = None
        if last is None:
            for pid in range(1, self.bp.total_pages()):
                p = self.bp.get_page(pid)
                if p and p.page_type == PAGE_TYPE_HEAP:
                    last = p
            if last is None:
                last = self.bp.new_page(PAGE_TYPE_HEAP)

        sid = last.insert_row(rb)
        if sid is None:
            target = self._find_heap_page_with_space(need)
            if target is None:
                target = self.bp.new_page(PAGE_TYPE_HEAP)
            sid = target.insert_row(rb)
            if sid is None:
                return None, None
            last = target
        self._last_heap_pid = last.page_id
        return last.page_id, sid

    def _find_heap_page_with_space(self, need: int):
        """
        在"真空化过的页"里找一个能容纳 need 字节的（没有则 None）。

        ★ 只查 `_space_pages`（vacuum 紧凑化后登记过的页），**不做全表
        页扫描**：全扫在缓冲区只有 500 帧、而 .dat 有几千页时会引发
        LRU 颠簸（每插一行驱逐一个脏页），实测把 10 万行量级的插入
        拖成小时级。登记表里放不下的页在检查时被剔除，因此稳态是
        O(1)（每页被选中后一直用到放不下为止）。
        """
        pages = self._space_pages
        while pages:
            pid = pages[-1]
            p = self.bp.get_page(pid)
            if (p is None or p.page_type != PAGE_TYPE_HEAP
                    or p.free_space() < need):
                pages.pop()
                continue
            return p
        return None

    def update(self, set_pairs: Dict,
               where: Optional[Dict],
               txn_id: int = 0,
               read_view=None) -> int:
        """更新

        **不再原地覆写**：把目标旧版本的行头 xmax 写成本事务号（旧行
        数据与它自己的 prev 指针原样留在堆里），然后**另写一个新版本**
        （xmin = 本事务号，prev = 旧版本位置），索引指向新版本。
        旧行完整字节同时压入物理 undo，回滚时按原页原槽位写回并把
        新版本清掉。xmax 用真实事务号，供读视图过滤并发未提交的更新。
        """
        with self._lock:
            tid = self._stmt_tid(txn_id)
            owned = bool(txn_id)
            if txn_id:
                self.register_read_view(txn_id, read_view)
            ok = False
            try:
                n = self._update_locked(
                    set_pairs, where, tid, read_view, owned)
                ok = True
                return n
            finally:
                self._cleanup_stmt(tid, txn_id)
                if ok:
                    self._safe_maybe_checkpoint()

    def _update_locked(self, set_pairs, where, tid,
                       read_view, owned) -> int:
        pks = self._find_pks(where, read_view)

        # CHECK 约束：**先对全部目标行整体校验，再逐行写**。若一边写一边
        # 校验，第 N 行违规时前 N-1 行已经落盘（autocommit 下退不回来），
        # 语句报错却留下部分更新 —— 那是另一种"假成功"。
        if getattr(self, '_has_checks', False):
            self._validate_update_checks(pks, set_pairs, read_view)

        n = 0
        for pk in pks:
            self._acquire_row_lock(tid, pk)
            undo_act = None

            loc = self.bt.search(pk)
            if loc is None:
                continue
            head_loc = loc

            # 要淘汰的是"本读视图看得见的那一版"（正常情况就是索引头）
            anchor = self._read_version(pk, read_view)
            if anchor is None:
                continue
            _, old_xmin, old_xmax, apid, asid = anchor
            page = self.bp.get_page(apid)
            if page is None:
                continue
            rb = page.get_row(asid)
            r = self._decode_row_ver(rb)
            if r is None:
                continue
            old_row, old_pp, old_ps = r[0], r[3], r[4]

            # ★ MVCC 加表名（内存版本链，供本进程内的读视图回查）
            self.mvcc.add_undo(
                pk, old_xmin, tid, old_row,
                table=self.name)

            new = self._apply_set_pairs(old_row, set_pairs)

            # ★ 旧版本墓碑化：
            #   - xmax 写真实事务号（并发读者靠它挡住未提交的更新）；
            #   - 只改 xmax，旧版本自己的 prev 指针必须原样保留，
            #     否则链就断在这里、更老的历史版本再也走不到。
            #   若该版本已经被别的事务标记淘汰（xmax != 0，正常情况
            #   下会被行锁挡住），保留原有标记不覆盖。
            old_ver = self._encode_row(
                old_row, xmin=old_xmin,
                xmax=tid if old_xmax == 0 else old_xmax,
                prev_page=old_pp, prev_slot=old_ps)
            # ★ 新版本：xmin = 本事务号，prev 指向刚墓碑化的旧版本
            nrb = self._encode_row(
                new, xmin=tid, xmax=0,
                prev_page=apid, prev_slot=asid)

            self.wal.log(tid, 'UPDATE', {
                'table': self.name, 'pk': pk,
                'old': old_row, 'new': new,
                'old_loc': [apid, asid],
                'old_b': self._encode_row(
                    old_row, xmin=old_xmin, xmax=0,
                    prev_page=old_pp, prev_slot=old_ps).hex(),
                'tomb_b': old_ver.hex(),
                'ver_b': nrb.hex(),
            })
            self.wal.flush_to_os()      # 先落日志，再改页

            if owned:
                undo_act = self.mvcc.push_undo(tid, UndoAction(
                    'UPDATE', self.name, pk, txn_id=tid, rb=rb,
                    page_id=apid, slot_id=asid))

            # 旧版本墓碑写回**原槽位**（长度不变，原地覆盖即可）
            if not page.update_row_inplace(asid, old_ver):
                page.restore_row(asid, old_ver)

            np, ns = self._heap_insert(nrb)
            if np is None:
                raise RuntimeError("存储空间不足")
            # 索引指向新版本（版本链的头）。正常情况 anchor 就是索引头
            # （行锁保证同一主键不会被并发改写），这里把索引前移即可。
            # 若 anchor 是链中间的旧版本（只有"重建出来的历史快照"去写
            # 才会遇到），索引前移会让中间那版之后更新的版本暂时不可达
            # —— 属于已知限制，见文件头"已知限制"。
            if (np, ns) != head_loc:
                self.bt.delete(pk)
                self.bt.insert(pk, np, ns)
            # 全文索引跟随最新版本（旧版本的行值不再作为命中来源）
            self._unindex_fulltext(pk)
            self._index_fulltext(pk, new)

            # 回滚时还要把这个新版本清掉（否则页上留一个不可见死行）
            if owned and undo_act is not None:
                undo_act.new_page_id = np
                undo_act.new_slot_id = ns
            n += 1
        return n

    def delete(self, where: Optional[Dict],
               txn_id: int = 0, read_view=None) -> int:
        """删除

        **不物理删行、也不摘索引项**：只把目标版本行头的 xmax 写成
        真实事务号（墓碑），行数据与 prev 链保留在堆里；索引继续指向
        这个墓碑，这样重启后老快照沿链仍能读到被删除的旧值。
        同时把旧行完整字节压入物理 undo，回滚时按原页原槽位复活。
        """
        with self._lock:
            tid = self._stmt_tid(txn_id)
            owned = bool(txn_id)
            if txn_id:
                self.register_read_view(txn_id, read_view)
            ok = False
            try:
                n = self._delete_locked(
                    where, tid, read_view, owned)
                ok = True
                return n
            finally:
                self._cleanup_stmt(tid, txn_id)
                if ok:
                    self._safe_maybe_checkpoint()

    def _delete_locked(self, where, tid, read_view, owned) -> int:
        pks = self._find_pks(where, read_view)
        n = 0
        for pk in pks:
            self._acquire_row_lock(tid, pk)

            anchor = self._read_version(pk, read_view)
            if anchor is None:
                continue
            _, old_xmin, old_xmax, apid, asid = anchor

            page = self.bp.get_page(apid)
            if page is None:
                continue
            rb = page.get_row(asid)
            r = self._decode_row_ver(rb)
            if r is None:
                continue
            old_row, old_pp, old_ps = r[0], r[3], r[4]

            # ★ MVCC 加表名（内存版本链）
            self.mvcc.add_undo(
                pk, old_xmin, tid, old_row,
                table=self.name)

            # ★ 旧版本行头写入真实删除事务号（autocommit 为合成号），
            #   然后把它作为"墓碑"留在页上：索引项**保留**指向它，
            #   对看得见删除事务的读视图不可见，对看不见的（老快照）
            #   仍然可见 —— 这就是"删掉的行老快照还能读到"的实现。
            old_ver = self._encode_row(
                old_row, xmin=old_xmin,
                xmax=tid if old_xmax == 0 else old_xmax,
                prev_page=old_pp, prev_slot=old_ps)

            self.wal.log(tid, 'DELETE', {
                'table': self.name, 'pk': pk,
                'old': old_row,
                # old_b = 事务前的**干净前像**（xmax=0），撤销时原样写回
                'old_b': self._encode_row(
                    old_row, xmin=old_xmin, xmax=0,
                    prev_page=old_pp, prev_slot=old_ps).hex(),
                'tomb_b': old_ver.hex(),
                'old_loc': [apid, asid],
            })
            self.wal.flush_to_os()      # 先落日志，再改页

            if owned:
                self.mvcc.push_undo(tid, UndoAction(
                    'DELETE', self.name, pk, txn_id=tid, rb=rb,
                    page_id=apid, slot_id=asid))

            if not page.update_row_inplace(asid, old_ver):
                page.restore_row(asid, old_ver)
            # ★ 不调用 bt.delete(pk)：索引项必须留着指向墓碑
            # 全文索引则要摘掉：被删除的行不应再被 MATCH 命中
            self._unindex_fulltext(pk)
            n += 1
        return n

    # ==================== 读操作 ====================
    #
    # 读路径统一走 _read_version()：索引头 -> prev 链，取"对读视图
    # 可见的最新版本"。内存里的 mvcc.undo_log 只作为本进程内的补充
    # （页上没有的历史版本），重启后靠页上的链。
    # 扫描代价: 单主键等值查找是 O(链长) 次单槽读取（无 vacuum 时
    # 链会随更新次数增长）；全表扫描是 O(索引项数 × 链长)。

    def _find_pks(self, where: Optional[Dict],
                  read_view=None) -> List:
        if where is None:
            return [v[0] for v in self._visible_pks(read_view)]

        # 主键等值
        if 'column' in where:
            col, op, val = (where['column'], where['op'],
                            where['value'])
            if col == self.pk_col and op == '=':
                try:
                    pk = coerce(val, self.pk_type)
                    v = self._read_version(pk, read_view)
                    if v is None:
                        return []
                    if self._match(v[0], where):
                        return [pk]
                    return []
                except Exception:
                    return []

        # 全表
        result = []
        for k, values, _xmin, _xmax, _pid, _sid in \
                self._visible_pks(read_view):
            if self._match(values, where):
                result.append(k)
        return result

    def _match(self, row, where):
        if 'and' in where:
            return all(self._match(row, c)
                       for c in where['and'])
        if 'or' in where:
            return any(self._match(row, c)
                       for c in where['or'])

        ci = self._col_idx(where['column'])
        l = row[ci]
        op = where['op']
        try:
            r = coerce(where['value'], self.cols[ci]['type'])
        except Exception:
            r = where['value']

        if l is None:
            return op == '=' and r is None
        if r is None:
            return op == '!='
        if op == '=': return l == r
        if op == '!=': return l != r
        if op == '<': return l < r
        if op == '>': return l > r
        if op == '<=': return l <= r
        if op == '>=': return l >= r
        if op == 'LIKE':
            pat = str(r).replace('%', '.*').replace('_', '.')
            return bool(re.match('^' + pat + '$', str(l)))
        return False

    def select(self, columns, where=None, order_by=None,
               limit=None, read_view=None):
        with self._lock:
            pks = self._find_pks(where, read_view)
            rows = []
            for pk in pks:
                # _find_pks 已经按可见性挑选过，这里只需取回那一版的行值
                v = self._read_version(pk, read_view)
                if v is not None:
                    rows.append(v[0])

        has_agg = any(
            re.match(r'\w+\s*\(', c.strip())
            for c in columns if c != '*')

        if columns == ['*']:
            out_cols = [c['name'] for c in self.cols]
            out_rows = rows
        elif has_agg:
            out_cols = [c.upper().strip() for c in columns]
            out_rows = [self._aggregate(rows, columns)]
        else:
            out_cols = [c.strip() for c in columns]
            out_rows = [
                [r[self._col_idx(c.strip())] for c in columns]
                for r in rows
            ]

        if order_by:
            col, desc = order_by
            try:
                i = out_cols.index(col)
                out_rows.sort(
                    key=lambda r: (r[i] is None, r[i]),
                    reverse=desc)
            except Exception:
                pass

        if limit:
            out_rows = out_rows[:limit]

        return out_cols, out_rows

    def _aggregate(self, rows, columns):
        result = []
        for col in columns:
            col = col.strip()
            m = re.match(
                r'(\w+)\s*\(\s*(\*|\w+)\s*\)',
                col, re.IGNORECASE)
            if not m:
                result.append(
                    rows[0][self._col_idx(col)] if rows else None)
                continue
            fn, arg = m.group(1).upper(), m.group(2)
            if fn == 'COUNT':
                if arg == '*':
                    result.append(len(rows))
                else:
                    ci = self._col_idx(arg)
                    result.append(sum(
                        1 for r in rows if r[ci] is not None))
            elif fn in ('SUM', 'AVG', 'MIN', 'MAX'):
                ci = self._col_idx(arg)
                vals = [r[ci] for r in rows if r[ci] is not None]
                if not vals:
                    result.append(None)
                elif fn == 'SUM': result.append(sum(vals))
                elif fn == 'AVG': result.append(
                    sum(vals) / len(vals))
                elif fn == 'MIN': result.append(min(vals))
                elif fn == 'MAX': result.append(max(vals))
            else:
                result.append(None)
        return result

    @property
    def row_count(self) -> int:
        return self.bt.count()

    # ==================== 事务回滚 / 恢复 ====================

    def abort_txn(self, txn_id: int) -> int:
        """
        真回滚：倒序回放本事务的物理 undo 镜像。

        - INSERT -> 删掉该行（堆 + 索引）
        - UPDATE -> 把事务前的旧行字节写回原页原槽位，并把索引项
          指回原位（若之前因空间不足搬到了新页）
        - DELETE -> 按原页原槽位复活旧行，重建索引项

        倒序（LIFO）是必须的：同一行被改多次时只有逆序才能回到
        事务前状态。
        """
        actions = self.mvcc.pop_undo(txn_id, table=self.name)
        restored = 0
        for act in actions:
            try:
                if self._undo_one(act):
                    restored += 1
            except Exception as e:
                logger.warn(f"回滚 {act.op} pk={act.pk} 失败: {e}")
        return restored

    def rollback_statement(self, tid, marks) -> int:
        """**语句级回滚**：只撤销保存点之后追加的物理 undo 镜像

        用在「用户显式事务里的多值 INSERT 中途失败」：不写 ABORT、不释放
        锁 / 读视图、不动事务状态（用户仍可继续执行，或整体 ROLLBACK），
        只让**这一条语句**失效。

        为什么还要写补偿 WAL：显式事务提交时 `wal.commit(tid)` 之前的所有
        INSERT 记录都会被崩溃恢复的 redo 重放，于是在进程内被撤销掉的行会
        在重启后"复活"。这里为每条被撤销的 INSERT 追加一条与
        `Table.delete` **完全同形**的 DELETE 记录（LSN 在 INSERT 之后）：
        redo 按 LSN 顺序 INSERT→DELETE 后该行被墓碑化 → 对所有读视图不可见。
        若该事务后来整体 ROLLBACK，恢复侧按逆 LSN 先撤销 DELETE（复活）
        再撤销 INSERT（删除），净效果仍然是"没有这一行"。

        代价（如实记录）：进程内被撤销的行是**物理删除**（槽位释放、索引项
        退回被复用的旧版本），而崩溃后 redo 出来的是**墓碑**——两者对外都
        不可见，但物理形态不同；`_next_pk` 在进程内按 `_calc_next_pk()`
        重算（不留下"已消耗"的空洞）。
        """
        actions = self.mvcc.pop_undo_after(tid, marks)
        restored = 0
        for act in actions:
            try:
                if act.op == 'INSERT':
                    self._log_statement_undo_delete(tid, act)
                if self._undo_one(act):
                    restored += 1
            except Exception as e:
                logger.warn(f"语句级回滚 {act.op} pk={act.pk} 失败: {e}")
        if actions:
            logger.info(
                f"语句级回滚 {self.name}: 撤销 {restored}/{len(actions)} 条"
                f"（事务 {tid} 保持打开）")
        return restored

    def _log_statement_undo_delete(self, tid, act) -> bool:
        """给"语句级回滚掉的 INSERT"补一条 DELETE WAL 记录（见上）"""
        page = (self.bp.get_page(act.page_id)
                if act.page_id is not None else None)
        rb = page.get_row(act.slot_id) if page is not None else None
        r = self._decode_row_ver(rb)
        if r is None:
            return False
        values, xmin, _xmax, pp, ps = r
        self.wal.log(tid, 'DELETE', {
            'table': self.name, 'pk': act.pk,
            'old': values,
            'old_b': self._encode_row(
                values, xmin=xmin, xmax=0,
                prev_page=pp, prev_slot=ps).hex(),
            'tomb_b': self._encode_row(
                values, xmin=xmin, xmax=tid,
                prev_page=pp, prev_slot=ps).hex(),
            'old_loc': [act.page_id, act.slot_id],
        })
        self.wal.flush_to_os()          # WAL 先于数据（与 _delete_locked 一致）
        return True

    def _undo_one(self, act) -> bool:
        if act.op == 'INSERT':
            # 事务插入的行 -> 删除。
            # 新布局下 INSERT 可能复用了某个墓碑的主键（新版本 prev
            # 指向墓碑），所以不能简单 bt.delete(pk)：必须把索引退回
            # 被复用的旧版本，否则那个墓碑就再也找不到了。
            prev_loc = None
            page = (self.bp.get_page(act.page_id)
                    if act.page_id is not None else None)
            rb = page.get_row(act.slot_id) if page is not None else None
            r = self._decode_row_ver(rb)
            if r is not None and r[3] != NO_PREV:
                prev_loc = (r[3], r[4])

            loc = self.bt.search(act.pk)
            if loc is not None and (act.page_id is None or
                                    loc == (act.page_id, act.slot_id)):
                if prev_loc is not None:
                    self.bt.insert(act.pk, prev_loc[0], prev_loc[1])
                else:
                    self.bt.delete(act.pk)
            if page is not None:
                page.delete_row(act.slot_id)
            self._next_pk = self._calc_next_pk()
            return True

        if act.rb is None:
            return False

        if act.op == 'UPDATE':
            # 先清掉本事务写入的新版本（页上不可见的死行）
            self._discard_dead_version(
                act.new_page_id, act.new_slot_id, act.page_id,
                act.slot_id)
            # 旧行写回原位置
            if not self._put_row_back(act.page_id, act.slot_id,
                                      act.rb):
                np, ns = self._heap_insert(act.rb)
                if np is None:
                    return False
                act.page_id, act.slot_id = np, ns
            # 索引指回原位置
            cur = self.bt.search(act.pk)
            if cur is None or cur != (act.page_id, act.slot_id):
                self.bt.delete(act.pk)
                self.bt.insert(act.pk, act.page_id, act.slot_id)
            return True

        if act.op == 'DELETE':
            # 复活被删的行
            if not self._put_row_back(act.page_id, act.slot_id,
                                      act.rb):
                np, ns = self._heap_insert(act.rb)
                if np is None:
                    return False
                act.page_id, act.slot_id = np, ns
            if self.bt.search(act.pk) is None:
                self.bt.insert(act.pk, act.page_id, act.slot_id)
            self._next_pk = max(self._next_pk, act.pk + 1)
            return True

        return False

    def _discard_dead_version(self, page_id, slot_id,
                              keep_page_id, keep_slot_id):
        """
        丢弃本事务 UPDATE 写出的新版本（回滚时用）。

        注意不要清到旧版本所在槽位：新版本可能和旧版本在同一个
        槽位（例如页满时 heap_insert 又选中同一页），此时跳过。
        """
        if page_id is None:
            return
        if (page_id, slot_id) == (keep_page_id, keep_slot_id):
            return
        page = self.bp.get_page(page_id)
        if page is not None:
            page.delete_row(slot_id)

    def _put_row_back(self, page_id, slot_id, rb: bytes) -> bool:
        """
        把整行字节写回指定的 (page_id, slot_id)。
        依赖 Page.restore_row：槽位空着（被 delete_row 清过）就重新挂上，
        已有数据且空间够就原地覆盖。返回 False 表示该页放不下。
        """
        if page_id is None:
            return False
        page = self.bp.get_page(page_id)
        if page is None:
            return False
        s = slot_id if slot_id is not None else 0
        return page.restore_row(s, rb)

    def recover_from_wal(self):
        """
        崩溃恢复：redo(已提交) + undo(loser)，成功落盘后才 truncate WAL。

        模拟场景是"WAL 已落盘、页未刷盘"：
        - loser（没写 COMMIT 就崩了）的页改动可能已经在盘上，
          必须用 WAL 里的旧行镜像撤销；
        - 已提交事务的页改动可能没落盘，必须按 WAL 重放。
        两个阶段的操作都幂等（覆盖式写 + 按主键定位），
        所以重复执行或页已落盘都不会出错。

        ★ truncate 是有条件的：任何一条 redo/undo 失败、或恢复结果刷盘
        失败，都保留 WAL 原样，让下次启动可以重试；否则一次恢复失败就会
        把已提交的数据永久丢掉（旧实现无条件 truncate）。
        """
        try:
            committed_ops, loser_ops = self.wal.analyze()
        except Exception as e:
            logger.warn(f"WAL 分析失败，跳过恢复 {self.name}: {e}")
            return 0

        # ★ loser 事务号登记进 MVCC 的 aborted 集合：它们写下的新版本
        #   从此被判为不可见（可见性规则的第 2 条），回滚语义在重启后
        #   依然成立，不依赖页上的物理清理是否彻底。
        loser_tids = {tid for _lsn, tid, _t, _d in loser_ops
                      if not is_implicit_commit_tid(tid)}
        for t in loser_tids:
            try:
                self.mvcc.mark_aborted(t)
            except Exception:
                pass

        # 恢复期的"版本位置重映射"表（见 _resolve_old_loc）
        self._recovery_remap = {}

        replayed = 0
        failed = 0
        for lsn, tid, rtype, data in committed_ops:
            try:
                if self._redo_op(tid, rtype, data):
                    replayed += 1
            except Exception as e:
                failed += 1
                logger.warn(f"重放 lsn={lsn} {rtype} 失败: {e}")

        # loser 必须**倒序**撤销
        undone = 0
        for lsn, tid, rtype, data in reversed(loser_ops):
            try:
                if self._undo_wal_op(rtype, data, tid=tid):
                    undone += 1
            except Exception as e:
                failed += 1
                logger.warn(f"撤销 lsn={lsn} {rtype} 失败: {e}")
        self._recovery_remap = {}

        if replayed or undone:
            logger.info(
                f"崩溃恢复 {self.name}: redo {replayed} 条, "
                f"undo {undone} 条")

        if failed:
            logger.warn(
                f"{self.name}: {failed} 条 WAL 操作恢复失败，"
                f"保留 WAL 不截断（下次启动重试）")
            try:
                self.flush()
            except Exception:
                pass
            return replayed

        # 恢复结果先落盘（含根指针 fsync），落盘成功之后才允许截断/轮转 WAL
        try:
            self.flush()
            self._retire_wal(sync_truncate=False)
        except Exception as e:
            logger.warn(
                f"{self.name}: 恢复结果刷盘失败，保留 WAL 不截断: {e}")
            return replayed
        return replayed

    # ---------- 恢复期的"版本位置重映射" ----------
    #
    # WAL 里记录的 old_loc 是**写日志那一刻**旧版本的物理位置。
    # 崩溃时脏页可能只落了一部分，重放时新版本被 _heap_insert 放到
    # 别的槽位，后续记录里的 old_loc 就可能指不到东西。_resolve_old_loc
    # 会去版本链上按"内容相同"找回该版本的真实位置，并登记进
    # _recovery_remap，后续记录的 prev 指针随之改写。
    # 代价: 仅在某条记录的位置对不上时才做一次链上查找（O(链长)），
    # 正常（位置吻合）路径是 O(1) 的一次单槽读取。

    def _rec_loc(self, loc):
        if not loc:
            return None
        return self._recovery_remap.get(
            (loc[0], loc[1]), (loc[0], loc[1]))

    def _remap_row_prev(self, rb: bytes) -> bytes:
        """把行镜像里的 prev 指针按恢复期重映射表改写"""
        if not rb or len(rb) < ROW_HEADER or not self._recovery_remap:
            return rb
        pp, ps = struct.unpack_from('<II', rb, 13)
        if pp == NO_PREV:
            return rb
        np_, ns_ = self._rec_loc((pp, ps))
        if (np_, ns_) == (pp, ps):
            return rb
        b = bytearray(rb)
        struct.pack_into('<II', b, 13, np_, ns_)
        return bytes(b)

    @staticmethod
    def _same_content(rb_a, rb_b) -> bool:
        """两个行镜像的 xmin/xmax/载荷是否一致（忽略 prev 指针）"""
        if rb_a is None or rb_b is None:
            return False
        if rb_a == rb_b:
            return True
        try:
            xa = struct.unpack_from('<II', rb_a, 0)
            xb = struct.unpack_from('<II', rb_b, 0)
        except Exception:
            return False
        if xa != xb:
            return False
        da = json.loads(rb_a[ROW_HEADER:].decode('utf-8'))
        db_ = json.loads(rb_b[ROW_HEADER:].decode('utf-8'))
        return da == db_

    @staticmethod
    def _same_row_payload(rb_a, rb_b) -> bool:
        """
        两个行镜像是否"同一个事务写的同一份内容"：
        只比 xmin 与 JSON 载荷，**忽略 xmax 和 prev**。

        redo 的幂等判定用: 同一条 INSERT/UPDATE 日志写出的版本，
        xmin 与载荷唯一确定它；xmax 之后可能被别的已提交事务改成墓碑，
        prev 可能被恢复期的位置重映射改写，都不影响"这条日志已重放"。
        """
        if rb_a is None or rb_b is None:
            return False
        try:
            xa = struct.unpack_from('<I', rb_a, 0)[0]
            xb = struct.unpack_from('<I', rb_b, 0)[0]
        except Exception:
            return False
        if xa != xb:
            return False
        try:
            da = json.loads(rb_a[ROW_HEADER:].decode('utf-8'))
            db_ = json.loads(rb_b[ROW_HEADER:].decode('utf-8'))
        except Exception:
            return False
        return da == db_

    def _resolve_old_loc(self, pk, old_loc, old_b):
        """
        把 WAL 记录的旧版本位置解析成**当前页上的真实位置**。
        返回 None 表示这条日志没有位置信息（旧格式）。
        """
        if not old_loc:
            return None
        loc = self._rec_loc(old_loc)
        want = bytes.fromhex(old_b) if old_b else None
        if want is None:
            return loc
        page = self.bp.get_page(loc[0])
        cur = page.get_row(loc[1]) if page is not None else None
        if self._same_content(cur, want):
            return loc
        # 位置对不上：沿版本链找内容相同的版本（可能是本进程重放时
        # 重新分配的槽位），找到就登记映射，后续记录跟着改写。
        head = self.bt.search(pk)
        if head is not None:
            for pid, sid, rb, _v, _xm, _xx, _pp, _ps in \
                    self._chain_versions(head):
                if self._same_content(rb, want):
                    self._recovery_remap[(old_loc[0], old_loc[1])] = \
                        (pid, sid)
                    return (pid, sid)
        return loc

    def _redo_op(self, tid, rtype, data) -> bool:
        """
        重放一条已提交的操作（幂等）。

        新布局下的语义:
        - INSERT: 写入新版本（xmin = tid），prev 指向被复用的旧版本
        - UPDATE: **旧版本 xmax 置位（墓碑化）+ 新版本另写一槽**
          （xmin = tid，prev = 旧版本位置），索引指向新版本
        - DELETE: 旧版本 xmax 置位（墓碑化），**索引项保留**指向墓碑
        """
        if rtype == 'INSERT':
            pk = data['pk']
            prev = data.get('prev') or [NO_PREV, 0]
            ploc = self._rec_loc(prev) if prev[0] else None
            rb = self._encode_row(
                data['row'], xmin=tid, xmax=0,
                prev_page=(ploc[0] if ploc else NO_PREV),
                prev_slot=(ploc[1] if ploc else 0))
            return self._redo_put_img(pk, rb)
        if rtype == 'UPDATE':
            return self._redo_update(tid, data)
        if rtype == 'DELETE':
            return self._redo_delete(tid, data)
        return False

    def _redo_update(self, tid, data) -> bool:
        """重放已提交的 UPDATE（幂等，见 _redo_op 说明）"""
        pk = data['pk']
        old_b = data.get('old_b')
        old_loc = self._resolve_old_loc(pk, data.get('old_loc'), old_b)

        # 新版本：prev 指向**解析后**的旧版本位置
        nrb = self._encode_row(
            data['new'], xmin=tid, xmax=0,
            prev_page=(old_loc[0] if old_loc else NO_PREV),
            prev_slot=(old_loc[1] if old_loc else 0))

        # 1) 旧版本墓碑化：只在它当前还是"事务前的干净前像"时写，
        #    避免把已经推进到更新状态的版本砸回墓碑。
        if old_loc is not None and old_b is not None:
            page = self.bp.get_page(old_loc[0])
            cur = page.get_row(old_loc[1]) if page is not None else None
            tomb = (bytes.fromhex(data['tomb_b'])
                    if data.get('tomb_b')
                    else self._with_xmax(bytes.fromhex(old_b), tid))
            tomb = self._remap_row_prev(tomb)
            if cur is not None and cur != tomb and \
                    self._same_content(cur, bytes.fromhex(old_b)):
                if not page.update_row_inplace(old_loc[1], tomb):
                    page.restore_row(old_loc[1], tomb)

        # 2) 新版本另写一槽（幂等：链上已有同样字节就不再写）
        return self._redo_put_img(pk, nrb)

    def _redo_delete(self, tid, data) -> bool:
        """
        重放已提交的 DELETE（幂等）。

        新布局下 DELETE 不摘索引项：墓碑必须留在索引里，重启后
        老快照才能沿链找到被删的旧值。所以这里把 old_loc 处的行
        墓碑化，并保证索引指向它。
        """
        pk = data['pk']
        old_b = data.get('old_b')
        old_loc = self._resolve_old_loc(pk, data.get('old_loc'), old_b)
        if old_loc is None:
            old_loc = self.bt.search(pk)
        if old_loc is None:
            return False
        page = self.bp.get_page(old_loc[0])
        if page is None:
            return False
        if old_b is not None or data.get('tomb_b'):
            tomb = (bytes.fromhex(data['tomb_b'])
                    if data.get('tomb_b')
                    else self._with_xmax(bytes.fromhex(old_b), tid))
            tomb = self._remap_row_prev(tomb)
            cur = page.get_row(old_loc[1])
            if cur is not None and cur != tomb and \
                    self._same_content(cur, bytes.fromhex(old_b)):
                if not page.update_row_inplace(old_loc[1], tomb):
                    page.restore_row(old_loc[1], tomb)
        # 索引仍然指向这个墓碑（幂等：已经指向就不动）。
        # 若索引头已经是一个"活版本"（例如页上已经存在 DELETE 之后的
        # 重插版本），绝不能把索引改指墓碑 —— 那会让重插的行消失。
        loc = self.bt.search(pk)
        if loc is None:
            self.bt.insert(pk, old_loc[0], old_loc[1])
        elif loc != (old_loc[0], old_loc[1]):
            page2 = self.bp.get_page(loc[0])
            head_rb = page2.get_row(loc[1]) if page2 is not None else None
            hr = self._decode_row_ver(head_rb)
            if hr is None or hr[2] != 0:
                self.bt.insert(pk, old_loc[0], old_loc[1])
        return True

    @staticmethod
    def _with_xmax(rb: bytes, xmax: int) -> bytes:
        """把行头里的 xmax 换掉（用于旧格式 WAL 的兼容）"""
        b = bytearray(rb)
        struct.pack_into('<I', b, 4, xmax)
        return bytes(b)

    def _redo_put_img(self, pk, rb: bytes) -> bool:
        """
        幂等地把一个"最新版本镜像"写到该主键上：
        - 链上已经存在**同一事务写的同一份内容**（xmin + 载荷相同）
          → 这条日志已经重放过，直接返回；
        - 否则**追加**到堆上并把索引指向它（绝不原地覆写别人，
          否则会把还没重放的后续版本砸掉）。

        为什么不按"完全相同的字节"判定: xmax 之后可能被别的已提交
        事务改成墓碑，prev 可能被恢复期的位置重映射改写，字节比不相等
        但版本其实早就在页上了 —— 若再追加一份就成了重复版本，
        还会让后续 loser undo 找不到真正的旧版本位置。
        """
        loc = self.bt.search(pk)
        if loc is not None:
            page = self.bp.get_page(loc[0])
            cur = page.get_row(loc[1]) if page is not None else None
            if cur == rb:
                return True
            if self._same_row_payload(cur, rb):
                return True
            for _pid, _sid, rb2, _v, _xm, _xx, _pp, _ps in \
                    self._chain_versions(loc):
                if self._same_row_payload(rb2, rb):
                    return True
        np, ns = self._heap_insert(rb)
        if np is None:
            return False
        self.bt.insert(pk, np, ns)
        if isinstance(pk, int):
            self._next_pk = max(self._next_pk, pk + 1)
        return True

    def _redo_put(self, pk, values, xmin=0) -> bool:
        """兼容入口：把 values 作为新版本幂等写入（无 prev）"""
        return self._redo_put_img(
            pk, self._encode_row(values, xmin=xmin, xmax=0))

    def _kill_loser_successors(self, loc, tid) -> int:
        """
        把 loser 事务写在 loc 之上的"新版本"标记为死（xmax = tid）。

        为什么需要: loser 的 UPDATE 在页上留下了一个 xmin = loser 号、
        xmax = 0 的新版本；进程内 ROLLBACK 会物理删掉它，但崩溃恢复
        只能靠 WAL。把它的 xmax 置成 loser 号，配合 MVCC 里
        mark_aborted(tid)，即使它因为断链被走到也会被判为不可见。

        代价: 一次全堆页扫描（只在恢复 loser 事务时调用）；
        只处理 prev == loc 且 xmin == tid 的版本；同一行被同一 loser
        改多次时，更深的后继版本虽然不在这里处理，但它们的 xmin 也是
        loser 号，靠 aborted 集合同样不可见。
        """
        if loc is None:
            return 0
        n = 0
        target = (loc[0], loc[1])
        for pid in range(1, self.bp.total_pages()):
            page = self.bp.get_page(pid)
            if page is None or page.page_type != PAGE_TYPE_HEAP:
                continue
            for sid, rb in list(page.iter_rows()):
                r = self._decode_row_ver(rb)
                if r is None:
                    continue
                if r[1] != tid or r[2] != 0 or (r[3], r[4]) != target:
                    continue
                b = bytearray(rb)
                struct.pack_into('<I', b, 4, tid)
                if not page.update_row_inplace(sid, bytes(b)):
                    page.restore_row(sid, bytes(b))
                n += 1
        return n

    def _undo_wal_op(self, rtype, data, tid=0) -> bool:
        """
        用 WAL 里的旧行镜像撤销 loser 事务的一条操作（幂等）。

        新布局下 UPDATE 的撤销要把 loser 写出的**新版本**判为不可见
        （xmax = loser 号，配合 mvcc.mark_aborted），并把**旧版本**
        的前像（xmax = 0、prev 保留）写回原位、索引指回它；DELETE 的
        撤销把墓碑还原成 xmax = 0 的活版本。
        """
        pk = data.get('pk')
        old_b = data.get('old_b')
        old_loc = data.get('old_loc')

        if rtype == 'INSERT':
            loc = self.bt.search(pk)
            if loc is None:
                return False
            page = self.bp.get_page(loc[0])
            rb = page.get_row(loc[1]) if page is not None else None
            r = self._decode_row_ver(rb)
            # 只撤销"本 loser 插入的那一版"，别误删别的版本
            if r is None or (tid and r[1] != tid):
                return False
            prev = (r[3], r[4]) if r[3] != NO_PREV else None
            if prev is not None:
                # 该主键原本有旧版本（复用了墓碑/不可见版本的主键）→
                # 索引退回旧版本，绝不能把索引项直接删掉
                self.bt.insert(pk, prev[0], prev[1])
            else:
                self.bt.delete(pk)
            if page is not None:
                page.delete_row(loc[1])
            self._next_pk = self._calc_next_pk()
            return True

        if old_b is None:
            return False
        rb = self._remap_row_prev(bytes.fromhex(old_b))
        page_id, slot_id = (old_loc[0], old_loc[1]) if old_loc else (None, None)
        resolved = self._resolve_old_loc(pk, old_loc, old_b)
        if resolved is not None:
            page_id, slot_id = resolved

        if rtype == 'UPDATE':
            # ★ 先把 loser 写下的新版本标记为死
            self._kill_loser_successors(
                (page_id, slot_id) if page_id is not None else None, tid)
            if page_id is None:
                return self._redo_put(
                    pk, data['old'], xmin=self._row_xmin(rb))
            if not self._put_row_back(page_id, slot_id, rb):
                np, ns = self._heap_insert(rb)
                if np is None:
                    return False
                page_id, slot_id = np, ns
                self._recovery_remap[(old_loc[0], old_loc[1])] = (np, ns)
            cur = self.bt.search(pk)
            if cur is None or cur != (page_id, slot_id):
                self.bt.delete(pk)
                self.bt.insert(pk, page_id, slot_id)
            return True

        if rtype == 'DELETE':
            if page_id is None:
                return self._redo_put(
                    pk, data['old'], xmin=self._row_xmin(rb))
            if not self._put_row_back(page_id, slot_id, rb):
                np, ns = self._heap_insert(rb)
                if np is None:
                    return False
                page_id, slot_id = np, ns
                self._recovery_remap[(old_loc[0], old_loc[1])] = (np, ns)
            if self.bt.search(pk) is None:
                self.bt.insert(pk, page_id, slot_id)
            self._next_pk = max(self._next_pk, int(pk) + 1)
            return True

        return False

    @staticmethod
    def _row_xmin(rb: bytes) -> int:
        try:
            return struct.unpack_from('<I', rb, 0)[0]
        except Exception:
            return 0

    # ==================== vacuum（版本回收）====================

    def vacuum(self, horizon: int = None) -> int:
        """
        最小可用 vacuum：回收"对任何快照都不可能再被读到"的旧版本。

        判定: 版本 V 可回收 ⟺ V.xmax != 0 且
              (V.xmax 是隐式提交号 或 V.xmax <= horizon)。

        - xmax != 0 表示 V 已被某个事务淘汰（更新/删除）；
        - 任何快照 RV 的 max_id 都 > 本进程最小的活跃事务号，
          所以 xmax <= horizon 时 RV.is_visible 对 V 恒为 False；
        - horizon = None 时取 mvcc.oldest_active_txn()（没有活跃事务
          时就是下一个待分配号），是本进程能做到的最保守上界。

        ★ 隐式提交号（autocommit 语句分配的合成高位号，见
        wal.is_implicit_commit_tid）必须单独放行，否则 **autocommit
        的 UPDATE/DELETE 留下的旧版本永远回收不了**：合成号是
        0xF0000000 以上的巨数，`xmax <= horizon`（horizon 只有几十）
        恒为假 —— 这正是"`Database.vacuum()` 回收 0"的根因。
        语义上这种号一律被 ReadView 视为"已提交删除"
        （mvcc.ReadView.is_visible 第 119 行），对**所有**读视图都
        不可见，因此回收它总是安全的。

        回收时**必须缝合 prev 链**：把"prev 指向被删版本"的后继版本
        的 prev 改写成被删版本的 prev，否则链会断在空洞上，
        更老的版本就再也读不到了。若某个主键的版本被删光，索引项
        一并摘除；若索引头被删掉，索引改指剩下的最新版本。

        ★ 回收后对相关页做**页内紧凑化**（Page.compact）：把存活行
        压到底部，空洞变成连续的空闲区，后续 insert 可以直接复用
        （实测 2000 行 × 20 轮 UPDATE 后 vacuum 回收 20000 个版本、
        页内释放 596 KB；紧接着插 2000 行新数据，**堆页 +0**
        （183→183），而修复前是 +16 页（183→199））。
        **数据文件本身不会变小**（页号被索引/prev 指针引用，
        截断文件会让引用失效）；"可复用空间"才是这里能给的空间收益。
        `last_vacuum_freed_bytes` 暴露本次释放的字节数。

        返回回收的版本数。不会删除任何仍可能被读到的版本。
        """
        with self._lock:
            if horizon is None:
                horizon = self.mvcc.oldest_active_txn()
            self._recovery_remap = {}
            freed_bytes = 0

            # 1) 收集所有版本
            versions = {}       # pk -> [(pid, sid, rb, xmin, xmax, pp, ps)]
            for pid in range(1, self.bp.total_pages()):
                page = self.bp.get_page(pid)
                if page is None or page.page_type != PAGE_TYPE_HEAP:
                    continue
                for sid, rb in page.iter_rows():
                    r = self._decode_row_ver(rb)
                    if r is None:
                        continue
                    pk = r[0][self.pk_idx]
                    if not isinstance(pk, int):
                        continue
                    versions.setdefault(pk, []).append(
                        (pid, sid, rb, r[1], r[2], r[3], r[4]))

            removed = 0
            touched_pages = set()
            for pk, vers in versions.items():
                dead = set()
                for pid, sid, _rb, _xmin, xmax, _pp, _ps in vers:
                    if xmax != 0 and (is_implicit_commit_tid(xmax)
                                      or xmax <= horizon):
                        dead.add((pid, sid))
                if not dead:
                    continue

                # 位置 -> prev，用于缝合
                prev_of = {(v[0], v[1]): (v[5], v[6]) for v in vers}
                alive = [v for v in vers if (v[0], v[1]) not in dead]

                # 2) 缝合仍然存活的后继版本
                for pid, sid, rb, xmin, xmax, pp, ps in alive:
                    if pp == NO_PREV or (pp, ps) not in dead:
                        continue
                    seen = set()
                    while (pp, ps) in dead and (pp, ps) not in seen:
                        seen.add((pp, ps))
                        pp, ps = prev_of.get((pp, ps), (NO_PREV, 0))
                    page = self.bp.get_page(pid)
                    if page is None:
                        continue
                    b = bytearray(rb)
                    struct.pack_into('<II', b, 13, pp, ps)
                    if not page.update_row_inplace(sid, bytes(b)):
                        page.restore_row(sid, bytes(b))

                # 3) 删掉死版本的数据（槽位）
                for pid, sid in dead:
                    page = self.bp.get_page(pid)
                    if page is not None:
                        page.delete_row(sid)
                    touched_pages.add(pid)
                    removed += 1

                # 4) 索引：指向仍然存活链头；全被删光就摘掉索引项
                if not alive:
                    self.bt.delete(pk)
                    continue
                referenced = {(v[5], v[6]) for v in alive
                              if v[5] != NO_PREV}
                heads = [(v[0], v[1], v[3]) for v in alive
                         if (v[0], v[1]) not in referenced]
                if not heads:
                    heads = [(v[0], v[1], v[3]) for v in alive]
                hpid, hsid, _xm = max(heads, key=lambda h: h[2])
                self.bt.insert(pk, hpid, hsid)

            # 5) 页内紧凑化：把空洞变成可复用的连续空闲区。
            #    槽位号不变（索引与 prev 指针都按 (page, slot) 定位），
            #    只有行数据的偏移会变，读路径每次都会重新读槽位。
            for pid in touched_pages:
                page = self.bp.get_page(pid)
                if page is not None:
                    got = page.compact()
                    freed_bytes += got
                    if got > 0 and pid not in self._space_pages:
                        # 登记为"可复用页"，后续 insert 会优先填这些空洞
                        self._space_pages.append(pid)
            self.last_vacuum_freed_bytes = freed_bytes
            if freed_bytes:
                logger.info(
                    f"vacuum {self.name}: 回收 {removed} 个版本，"
                    f"页内释放 {freed_bytes} 字节可复用空间")
            return removed

    # ==================== checkpoint / WAL 轮转 ====================

    def wal_checkpoint_limits(self):
        """当前生效的 WAL 段落阈值 (字节, 条数)；0 表示该维度不限制"""
        b = self.wal_checkpoint_bytes
        if b is None:
            try:
                b = int(os.environ.get(
                    WAL_CHECKPOINT_BYTES_ENV,
                    WAL_CHECKPOINT_BYTES_DEFAULT))
            except (TypeError, ValueError):
                b = WAL_CHECKPOINT_BYTES_DEFAULT
        r = self.wal_checkpoint_records
        if r is None:
            try:
                r = int(os.environ.get(
                    WAL_CHECKPOINT_RECORDS_ENV,
                    WAL_CHECKPOINT_RECORDS_DEFAULT))
            except (TypeError, ValueError):
                r = WAL_CHECKPOINT_RECORDS_DEFAULT
        return b, r

    def checkpoint(self, force: bool = False) -> bool:
        """
        在线 checkpoint：把脏页安全落盘后**退休**（截断）WAL 段落。

        顺序（WAL-before-data 的反向退休规则）:
          1) 安全门: 本表存在未提交事务（`_read_views` 非空）时**拒绝**
             执行（返回 False），除非 force=True。原因：脏页里可能含
             未提交事务写的版本，把它们刷盘 + 截断 WAL 会让"未提交的
             行"在崩溃后既无法撤销、又被当成已提交永久留下。
          2) `BufferPool.flush()` —— 脏页写盘并 fsync（**数据先于日志**）。
          3) BTree 根指针落盘并 fsync（否则根指针掉电丢失 ⇒ 索引不可达）。
          4) `WAL.truncate()` —— 段落清零，LSN 保持单调。

        返回是否真的做了 checkpoint。幂等：WAL 已经为空时再调用，
        结果仍是"页已落盘、WAL 为空"，不会报错、不会丢数据。
        """
        with self._lock:
            if self._read_views and not force:
                return False
            self._retire_wal()
            return True

    def _retire_wal(self, sync_truncate: bool = True):
        """
        checkpoint 的落盘序列（顺序即正确性）:

          1) `BufferPool.flush()` —— 脏页写盘 + fsync（**数据先于日志**）
          2) BTree 根指针写盘 + fsync（否则根指针掉电丢失 ⇒ 索引不可达）
          3) `WAL.truncate()` —— 日志段落退休，LSN 保持单调

        只有在 #1/#2 都真正 fsync 之后才允许 #3；顺序颠倒就是
        "日志先于数据退休"，崩溃后无法 redo/undo。

        `sync_truncate=False` 用于**进程即将退出**的收尾（close /
        恢复结束）：数据页此刻已经 fsync，截断动作即使掉电丢失，
        下次启动也只是把同样的记录再幂等重放一遍，不会丢已提交数据；
        省下一次 fsync 只影响下次启动要重放的量。
        """
        self.bp.flush()
        self._save_btree_root(self.bt.root_page_id, sync=True)
        self.wal.truncate(sync=sync_truncate)

    def _maybe_checkpoint(self) -> bool:
        """按阈值判断是否需要 checkpoint（只在语句成功收尾后调用）"""
        limit_b, limit_r = self.wal_checkpoint_limits()
        if limit_b <= 0 and limit_r <= 0:
            return False
        if self._read_views:          # 有未提交事务：推迟，绝不刷未提交数据
            return False
        if not self.wal.needs_checkpoint(limit_b, limit_r):
            return False
        return self.checkpoint()

    def flush(self):
        self.bp.flush()
        self.wal.flush()
        self._save_btree_root(self.bt.root_page_id)

    def close(self):
        """
        干净关闭：先把脏页 + BTree 根落盘（数据已持久），
        再 rotate/truncate WAL。

        为什么可以 truncate：close() 意味着本实例不再写，页已经
        完整落盘（`_retire_wal` 还会把根指针 fsync），WAL 里已提交
        的改动不再需要重放；没提交的残片本来就要丢弃。
        不 truncate 的话 WAL 会无限增长，而且下次启动要对整份历史做全量 redo。
        """
        self.flush()
        try:
            self._retire_wal(sync_truncate=False)
        except Exception as e:
            logger.warn(f"关闭时 truncate WAL 失败: {e}")
        self.bp.close()
        self.wal.close()

    def stats(self) -> dict:
        return {
            'table': self.name,
            'rows': self.row_count,
            'pages': self.bp.total_pages(),
            'btree_height': self.bt.height(),
            **self.bp.stats(),
        }