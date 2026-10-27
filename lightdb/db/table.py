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
import bisect
import json
import os
import re
import struct
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..storage.page import (
    PAGE_TYPE_HEAP, PAGE_TYPE_BTREE_LEAF, PAGE_TYPE_BTREE_INTERNAL,
    PAGE_HEADER_SIZE, PAGE_SIZE)
from ..storage.buffer_pool import BufferPool
from ..storage.btree import BTree
from ..storage.freelist import FreePageRegistry
from ..storage.pgscan import scan_pages, prev_of, xmin_xmax
from ..storage.wal import WAL, is_implicit_commit_tid
from ..storage.lock_manager import (
    LockManager, LockType, DeadlockError)
from ..storage.mvcc import MVCCManager, UndoAction
from ..logger import logger
from .errors import DdlError
from .typecheck import NotNullViolation, validate_value


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
#: P5：**时间维度**的 checkpoint 阈值（秒）。0/None = 关闭该维度。
#: 与大小/条数维度一起构成"WAL 有界"的三道触发条件；时间触发在下一次
#: 写入收尾时检查（本引擎没有后台线程，空闲时不做无谓刷盘）。
WAL_CHECKPOINT_SECONDS_DEFAULT = 60.0
WAL_CHECKPOINT_SECONDS_ENV = 'LIGHTDB_WAL_CHECKPOINT_SECONDS'

#: P6.2：AUTO_INCREMENT **崩溃持久化**的区间预留大小。
#:
#: 语义：每次持久化一个"预留上界 R"（文件 `<name>.autoinc`，原子写 + fsync），
#: 之后从内存计数器分配号码；只要分配号 < R 就无需再写盘。硬杀重启时从
#: `max(meta 计数, 文件预留上界)` 继续 —— **绝不重用**任何已分配号，代价是
#: 最多跳过一个预留区间（默认 1000 个号）。这样每 N 次插入才有一次 fsync，
#: 批量插入吞吐几乎不受影响（量化见 tests/test_auto_increment_durability.py）。
#: 交付权衡：预留越大，fsync 次数越少、吞吐越好；崩溃重启后跳号上界
#: 也越大。默认 10000：实测本机一次 fsync≈55ms，默认块下 4000 行批量
#: 插入只有 1 次预留写盘（约 +55ms，见 test_auto_increment_durability.py
#: 的吞吐量化），而硬杀最多跳过 1 万个号（允许跳号、禁止复用）。
AUTO_INCREMENT_BLOCK_DEFAULT = 10000
AUTO_INCREMENT_BLOCK_ENV = 'LIGHTDB_AUTO_INCREMENT_BLOCK'

# 一个堆页剩余多少字节才值得登记为"可复用页"（页级回收）。
# 太小（例如只剩 4 字节）的页登记进来只会让 `_find_heap_page_with_space`
# 白查一次再剔除；64 字节足够放一行窄行（含 21 字节行头 + 一个槽位项）。
MIN_REUSABLE_PAGE_BYTES = 64

# 前向搬页的**松弛系数**（阶段 2 第 3 项）。
#
# 搬页时每个目标页只使用 `_page_move_capacity()` 的
# `1 - RELOCATE_SLACK_RATIO` 部分来装搬入行，剩下的就是**搬完之后留在
# 页上的可复用空闲空间**。理想值（0）是把活行压到最少页数，但那样每页
# 几乎塞满（实测 500 行 → 5 页、每页仅剩 26 字节），**真空化释放的空间
# 就没有以"可复用空闲页"的形式留下来** —— 紧接着插入 1500 行时只能一路
# 新建页，堆页从 5 涨到 17（阶段 2.1 的既有断言
# `heap_after - heap_before <= 1` 就是这么被打破的）。
#
# 0.5 是实测取值（同窗成对测量，见交付说明的"空间数字"一节）:
#   * "20000 行删 90% → vacuum" 场景: 文件 1257472 → 569344 B（回收 54.7%），
#     可复用空闲 ~234 KB；
#   * "6000 行删 75% → vacuum → 重启 → 再插 1500 行" 场景:
#     文件 376832 → 245760 B（回收 34.8%），可复用 ~67 KB，**堆页 +0**；
#   * "5000 行删 80% → vacuum → 再插 1000 行" 场景:
#     文件 311296 → 163840 B（回收 47.4%），**堆页 +0**；
#   * "500 行 × 20 轮 UPDATE → vacuum → 再插 1500 行" 场景:
#     堆页增长 <= 1（阶段 2.1 的既有断言）。
# 取值越大 → 文件缩得越多、可复用空间越少，两者此消彼长；
# 另外 `_relocate_forward` 还有一道"收益门槛"（只搬到 heap_floor 之下），
# 保证任何情况下都不会出现"越搬越大"。
RELOCATE_SLACK_RATIO = 0.5

#: 是否启用**前向搬页**（页级回收里最激进的一步；见 `_relocate_forward`）。
#: 默认 False —— 本批次实测: 搬页在大表上会把文件变大（每页留"可复用余量"
#: 需要更多页），而真正的收益（索引页密集化 + 尾部截断）不依赖它。
#: 打开后仍受"实测收益门槛 + 整份回滚"保护，不会越回收越大。
#: 是否启用**前向搬页**（页级回收里最激进的一步；见 `_relocate_forward`）。
#:
#: **默认 False —— 本批次实测它没能在目标场景里给出收益**，因此交付时关闭。
#: 实测（`ENABLE_PAGE_RELOCATION=True`）: 计划生成阶段返回
#: `moved=0 / heap_floor=未下降`（`_reloc_dbg={'rows':2000, 'heap_pages':187,
#: 'target_pages':38}`），即 187 个堆页里"每页只占 2048 字节软容量"的打包
#: 约束下找不到足够的落点，最终一行都没搬，文件大小不变。
#: 打开它不会更差（自带"实测收益门槛 + 整份回滚"，见 `_reclaim_pages`），
#: 但也不会更好，所以不把它当成交付能力。
#:
#: 仍然生效的页级回收路径（不依赖搬页）:
#: 索引页密集化（`_rebuild_btree`）+ 尾部截断（`trim_tail`）+ 空闲页
#: 持久化登记（`FreePageRegistry`）。
ENABLE_PAGE_RELOCATION = False

#: 是否启用 B+Tree **原地重建**（索引页密集化）。
#:
#: **默认 False —— 本批次实测它在"索引密集但页号靠后"的场景会丢数据**:
#: 新树被追加到文件尾（页号高于旧树），随后"截回旧长度"会把新树自己的页
#: 截掉，根指针指向不存在的页 ⇒ 重启后整表读不到（实测 dump 从 2000 行
#: 变 0 行）。虽然加了"页数不少于旧树就整份回退"的收益门槛，但
#: "新树更小却落在更高页号"这条路径没有被门槛覆盖，风险不可接受，
#: 因此关闭。`_rebuild_btree` 实现与门槛都保留，供后续在
#: `BufferPool` 支持"按空洞分配 + 截断只丢尾部空洞"之后再启用。
ENABLE_BTREE_REBUILD = False

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


class _OmittedDefault:
    """内部哨兵：INSERT 列清单里**没给**、且该列也没有声明 DEFAULT

    为什么需要它：修复 C 要求"主键收到 NULL → 1048"，而本引擎对
    `INSERT INTO t (v) VALUES (...)`（省略 INT 主键）有**自取主键**的
    既有能力（`_next_pk`，见 tests/test_insert_atomicity.py ⑤ 与
    tests/test_metadata_honesty.py 的 AUTO_INCREMENT 用例）。两者在
    `Table.insert` 的入参里都是 `None`，必须区分：

    * `INSERT INTO t (v) VALUES (7)`：主键列**没出现**在列清单里 ——
      executor 用 `Table._column_default()` 补位，这里返回本哨兵；
    * `INSERT INTO t (id, v) VALUES (NULL, 7)` / `VALUES (NULL, 7)`：
      用户**显式**写了 NULL —— 原样是 `None`，主键/NOT NULL 列拒绝。

    哨兵在 `_insert_locked` 里、任何 WAL/页写入与生成列求值之前被解析成
    `None`，因此不会进入行载荷、不会落盘、也不会出现在读路径。
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return '<OMITTED>'

    def __str__(self) -> str:
        # 只用于极端防御路径（有人把它当值展示）：语义上等价于 NULL。
        return 'NULL'


#: 单例哨兵（见 `_OmittedDefault`；只做身份比较 `is _OMITTED`）
_OMITTED = _OmittedDefault()


def coerce(value, col_type: str):
    """类型转换 + 声明式类型约束校验（P1 修复 C）

    `col_type` 现在可能是**带参数**的真实类型（`VARCHAR(20)`、
    `DECIMAL(10,2)`、`INT(11) UNSIGNED`）——解析器不再把长度抹掉
    （元数据诚实化的要求：`SHOW COLUMNS` 必须报真实声明）。

    旧实现只做 `int()/float()/bool()/str()`；现在把"范围 / 长度 / 日期 /
    精度"规则集中到 `db/typecheck.py`（`validate_value`），这里保持薄：
    合法值返回转换后的值（返回类型与旧实现一致），非法值抛
    `TypeCheckError` —— 异常自带 `errno` / `sqlstate`（1264/1406/1292/
    1366…），执行器据此返回**确定的**语义码，不再按报文猜。
    """
    if value is None:
        return None
    return validate_value(value, col_type)


class UniqueViolationError(ValueError):
    """唯一约束冲突（写入路径检查出来的，对应 MySQL 1062）

    为什么是 `ValueError` 子类：`sql/executor.py` 的 `ValueError` 分支按
    报文归类错误码（`Duplicate entry` / `唯一` → 1062）。本异常自带
    `errno=1062` / `sqlstate='23000'`，即使将来改成按类型分派也能直接
    使用；报文同样包含 `Duplicate entry ... for key ...` 的 MySQL 形态。
    """

    errno = 1062
    sqlstate = '23000'

    def __init__(self, message: str, key: str = None, entry=None):
        super().__init__(str(message))
        self.key = key
        self.entry = entry


class Table:
    """表对象"""

    #: checkpoint 触发阈值（测试/调用方可以直接改类属性；None = 用环境变量/默认值）
    wal_checkpoint_bytes = None
    wal_checkpoint_records = None
    #: P5：时间阈值（秒；None = 取环境变量/默认 60s；0 = 关闭）
    wal_checkpoint_seconds = None
    #: P6.2：自增号预留区间大小（None = 取环境变量/默认 1000）
    auto_increment_block = None
    #: P5.1：WAL 段大小 / checkpoint 后保留的最近归档段数
    #: （None = 取环境变量 LIGHTDB_WAL_SEGMENT_BYTES /
    #:  LIGHTDB_WAL_KEEP_SEGMENTS 或 WAL 模块默认值）
    wal_segment_bytes = None
    wal_keep_segments = None

    def __init__(self, name: str, cols: List[Dict], data_dir: str,
                 lock_manager: LockManager = None,
                 mvcc: MVCCManager = None,
                 unique_indexes: List[Dict] = None,
                 secondary_indexes: List[Dict] = None,
                 requested_engine: str = 'LightDB',
                 auto_increment_next: Dict[str, int] = None,
                 auto_increment_start: int = None):
        self.name = name
        self.cols = cols
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        #: E2：`CREATE/ALTER ... ENGINE=` 请求的引擎名（兼容别名）。
        #: 实际存储始终是 LightDB 原生行存；这里只持久化并如实回显，
        #: **不实现** InnoDB/MyISAM/MEMORY/CSV 的任何语义差异。
        self.requested_engine = requested_engine or 'LightDB'

        # 最后一个堆页的缓存（见 _heap_insert；纯性能优化，不改变语义）
        self._last_heap_pid = None
        # vacuum 紧凑化过、仍有可复用空闲空间的页号（后进先出复用）
        # ★ 阶段 2 第 3 项: 这份登记表现在**持久化**在 `<name>.free` 里
        #   （FreePageRegistry），不再只活在内存中 —— 否则重启后
        #   `_heap_insert` 不知道任何旧页还能放东西，只能追加新页，
        #   `.dat` 会因为一次重启白白长回去。
        self.freelist = FreePageRegistry(self.data_dir / f"{name}.free")
        self.freelist.load()
        #: 兼容阶段 2.1 的调用方（同一个 list 对象，不是拷贝）
        self._space_pages = self.freelist.pages
        # 最近一次 vacuum 在页内真正释放出的字节数（供巡检/日志）
        self.last_vacuum_freed_bytes = 0
        # 最近一次 vacuum 的页级统计（截断/复用/上限），见 vacuum_stats()
        self._last_vacuum_pages = {}
        # 最近一次 trim_tail 的细节（页数/字节/扫描摘要）
        self._last_trim = {}
        # 最近一次 vacuum 用的"活跃读视图可见性上界"（inf = 没有活跃读视图）
        self._view_floor = float('inf')

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
        # P5.1：WAL 命名多段轮转。段大小 / checkpoint 保留段数可配置：
        #   Table.wal_segment_bytes / LIGHTDB_WAL_SEGMENT_BYTES
        #   Table.wal_keep_segments / LIGHTDB_WAL_KEEP_SEGMENTS
        # None = 由 WAL 读取环境变量/默认值（活动段 <table>.wal，归档段
        # <table>-wal-NNNNNN.log）。
        self.wal = WAL(self.wal_path,
                       segment_bytes=self.wal_segment_bytes,
                       keep_segments=self.wal_keep_segments)

        root_id = self._load_btree_root()
        self.bt = BTree(self.bp, root_page_id=root_id)
        self._btroot_saved = root_id
        #: 上一次**同步写过**的根页号（None = 盘上那份的持久性未知）
        self._btroot_synced = None
        self._save_btree_root(self.bt.root_page_id)

        self._rebuild_index()
        self._lock = threading.RLock()
        self._next_pk = self._calc_next_pk()
        # P6：AUTO_INCREMENT 状态。
        # `_auto_next` = 每列"下一个待分配号"（列名 → int），随表元数据
        # 持久化（`Database.save_meta`）。INSERT 省略/NULL 时在表锁内分配，
        # 显式值推进计数器；回滚**不回收**已分配号（MySQL 语义）。
        # P6.2：`_auto_reserved` = 已**持久化**的预留上界（排他），分配号
        # 必须 < 上界；不足时先原子写 `<name>.autoinc` + fsync 再继续。
        self._autoinc_path = self.data_dir / f"{name}.autoinc"
        self._auto_next = {}
        self._auto_reserved = {}
        self._load_autoinc_reservations()
        for _k, _v in (auto_increment_next or {}).items():
            try:
                self._auto_next[str(_k)] = int(_v)
            except (TypeError, ValueError):
                continue
        self._auto_start_pending = (
            int(auto_increment_start) if auto_increment_start else None)
        self._init_auto_increment_state()
        # 启动时校验持久化的空闲页登记表: 越界（文件曾经被截断过）、
        # 已经不是堆页（页号被 B+Tree 复用）、其实没空间的项一律丢弃。
        # 丢弃只损失复用机会，绝不改变"能放下就放下"的语义。
        self._validate_free_pages()

        # 事务号 -> ReadView（供写路径的可见性判断 + 恢复用）
        self._read_views: Dict[int, Any] = {}

        # P5.1：非唯一二级索引的**真实内存映射**（懒建）：
        #   `_secondary_maps[name] = {'spec','map': {key: set(pk)}, 'sorted'}`
        # 写路径只标脏（`_secondary_dirty=True`），下一次可安全使用索引的
        # 查询时按 `_live_rows()` 重建；有活跃事务/读视图时**安全回退**
        # 全表扫描（索引映射不承担 MVCC 可见性判断）。
        self._secondary_maps = None
        self._secondary_dirty = True
        self._index_stats = {
            'lookups': 0, 'fallbacks': 0, 'rebuilds': 0, 'ordered': 0}

        # P5：checkpoint 状态（供 SHOW WAL STATUS / 日志观测）。
        # `_last_checkpoint_ts` 在时间阈值判定中使用；`_checkpoint_count`
        # 是**真实完成**的 checkpoint 次数（幂等重复调用也会计数）。
        self._last_checkpoint_ts = time.time()
        self._checkpoint_count = 0
        self._last_checkpoint_reason = ''
        self._last_checkpoint_wal_bytes = 0
        self._last_checkpoint_wal_records = 0

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

        # 唯一性约束：表级 `UNIQUE KEY/INDEX 名字 (列...)`（含
        # `CONSTRAINT 名字 UNIQUE`）**与列级 `col ... UNIQUE`（单列）**。
        # 本引擎没有独立唯一索引结构，唯一性由写入路径（INSERT / UPDATE）
        # 扫描强制，因此这份声明必须随 `_meta.json` 持久化、重启后仍在
        # （见 `Database._load` / `save_meta`）。
        #
        # 两类声明落成同一份规格：
        #   * 表级：{'name': 索引名, 'columns': [列...]}
        #   * 列级：{'name': 列名, 'columns': [列名], 'origin': 'column'}
        #     —— MySQL 给列级 UNIQUE 自动建的索引名就是列名。
        # 列级规格由 `_sync_column_unique_indexes()` 从 `cols[i]['unique']`
        # 推导，因此老 `_meta.json`（只有 `unique=true`、没有
        # `unique_indexes` 条目）打开后也会自动补上并强制。
        self._unique_indexes = []
        self.set_unique_indexes(unique_indexes)

        #: E2：**非唯一**索引声明（`CREATE INDEX` / 内联 `KEY idx (列)`）。
        #: 本引擎没有二级 B+Tree，因此它们不加速查询（planner 仍全表扫描），
        #: 但声明必须持久化并在 `SHOW INDEX` / `SHOW CREATE TABLE` 如实
        #: 报出（Index_type=`SCAN-ASSIST`，不谎称 BTREE）；`Cardinality`
        #: 给出真实的不同非 NULL 键值个数。
        self._secondary_indexes = []
        self.set_secondary_indexes(secondary_indexes)

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

    # ==================== 唯一性约束（表级 UNIQUE / 列级 UNIQUE） ====================
    #
    # 设计取舍（**正确性优先**）：本引擎只有主键一个真实 B+Tree，没有
    # 二级索引。表级 `UNIQUE KEY k (列...)` 与列级 `col ... UNIQUE` 如果
    # "建索引"就要再写一套索引结构 + 崩溃恢复，本批次选择**写入路径唯一性
    # 检查**（全表扫描当前版本，O(n)）：
    #
    #   * 两类声明都进 `_unique_indexes`，走同一套校验；
    #   * 声明随 `_meta.json` 持久化 → 重启后仍强制；
    #   * INSERT / UPDATE（含多行 UPDATE）都在**写任何一页之前**整体校验，
    #     冲突抛 `UniqueViolationError`（1062），不会留下半套写入；
    #   * `SHOW INDEX` 会列出它们，但 `Index_type` 如实写 `UNIQUE-CHECK`
    #     （不是 BTREE），并由 `Cardinality` 给出真实的不同键值个数；
    #   * 多个 NULL 允许共存（与 MySQL 唯一索引语义一致；NULL 不参与
    #     唯一性判定）。
    #
    # 列级 UNIQUE 的索引名按 MySQL 惯例取**列名**（`email`），因此
    # 重复 INSERT 的报文是 `Duplicate entry 'x' for key 'email'`。

    def set_unique_indexes(self, specs):
        """设置唯一性约束（表级显式声明 + 列级 UNIQUE 推导）

        列名不存在的条目在这里仍然保留（不静默丢声明）：定义期
        `Database.create_table` 会先校验，运行期 `_col_idx` 找不到列会
        抛 `KeyError` —— 两种都是显式失败，而不是"约束悄悄没了"。
        """
        out = []
        for spec in (specs or []):
            if not isinstance(spec, dict):
                continue
            columns = [str(c) for c in (spec.get('columns') or []) if str(c)]
            if not columns:
                continue
            name = str(spec.get('name') or columns[0])
            item = {'name': name, 'columns': columns}
            if spec.get('origin') == 'column':
                item['origin'] = 'column'
            out.append(item)
        self._unique_indexes = out
        self._sync_column_unique_indexes()
        self._secondary_dirty = True
        return self.unique_indexes()

    def _sync_column_unique_indexes(self):
        """把列级 `UNIQUE` 同步成等价的唯一索引规格（幂等）

        `email VARCHAR(50) UNIQUE` 在 MySQL 里等价于自动建一个名为
        `email` 的唯一索引；这里把它注册进同一份 `_unique_indexes`，
        于是 INSERT / UPDATE 走同一套 `_validate_unique_*`、`SHOW INDEX`
        走同一份 `index_specs()`、`_meta.json` 走同一份 `unique_indexes`。

        以 `cols` 为**唯一事实来源**：元数据里可能已有新版写出的
        `origin='column'` 规格，也可能只有老版的 `unique=true`（没有
        `unique_indexes` 条目）——两者都在这里重新推导，避免"列已删/已
        改、规格还在"的残影。表级规格原样保留；若与列级自动名（列名）
        撞名：
          * 列完全相同 → 视为同一条约束，由列级规格接管（DDL 只写内联
            UNIQUE，不再重复写 UNIQUE KEY）；
          * 列不同 → 表级规格改用 `<名字>_2`（列级自动名保持 MySQL 惯例
            的列名，且保证 SHOW CREATE TABLE 仍能往返）。
        """
        table_specs = [dict(s) for s in self._unique_indexes
                       if s.get('origin') != 'column']
        # 列级自动名（列名）优先保留：老元数据里可能有一条表级规格恰好
        # 用了某个 UNIQUE 列的名字（旧版允许这种声明，因为列级 UNIQUE
        # 当时不参与索引名冲突检查）。若列不同，表级规格让名（改成
        # `<名字>_2`），否则 SHOW CREATE 会写出两个同名约束、无法恢复。
        col_unique = {str(c['name']).lower(): str(c['name'])
                      for c in self.cols if c.get('unique')}
        taken = set(col_unique)
        kept_specs = []
        for spec in table_specs:
            name = str(spec['name'])
            low = name.lower()
            if low in col_unique:
                if list(spec.get('columns') or []) == [col_unique[low]]:
                    # 与列级自动推导完全同一条约束：由列级规格接管
                    continue
                base, n = name, 2
                while f"{base}_{n}".lower() in taken:
                    n += 1
                name = f"{base}_{n}"
                spec['name'] = name
                low = name.lower()
            taken.add(low)
            kept_specs.append(spec)
        by_name = {str(s['name']).lower(): s for s in kept_specs}
        derived = []
        for c in self.cols:
            if not c.get('unique'):
                continue
            cname = str(c.get('name') or '')
            if not cname:
                continue
            auto = {'name': cname, 'columns': [cname], 'origin': 'column'}
            hit = by_name.get(cname.lower())
            if hit is not None:
                # 正常情况下 col_unique 已保证不会撞名；防御旧/手工元数据
                if list(hit.get('columns') or []) == [cname]:
                    kept_specs.remove(hit)
                    by_name.pop(cname.lower(), None)
                else:
                    base, n = cname, 2
                    while f"{base}_{n}".lower() in by_name:
                        n += 1
                    auto['name'] = f"{base}_{n}"
            derived.append(auto)
            by_name[auto['name'].lower()] = auto
        self._unique_indexes = kept_specs + derived

    def unique_indexes(self):
        """全部唯一性约束（表级 + 列级；深拷贝，防止调用方改内部状态）

        表级条目是 `{'name','columns'}`；列级条目额外带
        `'origin': 'column'`。只想要表级声明（`SHOW CREATE TABLE` 已把
        列级内联成 `UNIQUE`，不能重复写 `UNIQUE KEY`）用
        `table_unique_indexes()`。
        """
        out = []
        for s in self._unique_indexes:
            item = {'name': s['name'], 'columns': list(s['columns'])}
            if s.get('origin') == 'column':
                item['origin'] = 'column'
            out.append(item)
        return out

    def table_unique_indexes(self):
        """表级 `UNIQUE KEY/INDEX` 声明（不含列级自动推导的规格）"""
        return [s for s in self.unique_indexes()
                if s.get('origin') != 'column']

    # ==================== E2：非唯一索引（持久化的声明元数据） ====================

    def set_secondary_indexes(self, specs):
        """设置非唯一索引声明（独立 CREATE INDEX 与内联 KEY 共用）

        结构统一为 `{'name', 'columns', 'unique': False}`；列不存在/
        重名等合法性由 `Database.create_index` 在定义期校验（这里只做
        结构归一化，避免旧元数据里的脏条目直接进入 SHOW 输出）。
        """
        out = []
        for spec in (specs or []):
            if not isinstance(spec, dict):
                continue
            columns = [str(c) for c in (spec.get('columns') or []) if str(c)]
            if not columns:
                continue
            name = str(spec.get('name') or columns[0])
            out.append({'name': name, 'columns': columns,
                        'unique': False})
        self._secondary_indexes = out
        self._secondary_dirty = True
        return self.secondary_indexes()

    def secondary_indexes(self):
        """全部非唯一索引声明（深拷贝）"""
        return [{'name': s['name'], 'columns': list(s['columns']),
                 'unique': False} for s in self._secondary_indexes]

    def _index_cardinality(self, columns):
        """索引列上**真实**的不同非 NULL 键值个数（不是估计值）"""
        try:
            idxs = [self._col_idx(c) for c in columns]
        except KeyError:
            return 0
        seen = set()
        for _pk, values in self._live_rows():
            tup = self._key_tuple(values, idxs)
            if self._key_has_null(tup):
                continue
            seen.add(tup)
        return len(seen)

    def unique_columns(self):
        """被任意唯一性约束覆盖的列名集合（表级 + 列级）"""
        out = set()
        for spec in self._unique_indexes:
            out.update(spec['columns'])
        return out

    def _live_rows(self):
        """全表扫描"当前仍存活"的版本：返回 `[(pk, values)]`

        判据是行头 `xmax == 0`（未被更新/删除墓碑化），**不看**读视图：
        唯一性是表级不变量（表级 UNIQUE 与列级 UNIQUE 共用），必须把别的
        会话**未提交**的插入也算进来（否则两个事务各插一行相同唯一值就能
        都提交）。代价是对未提交写入偏保守：对方若回滚，本次插入会报
        1062 而不会重复。
        """
        out = []
        for pk, pid, sid in self.bt.scan():
            for _vpid, _vsid, _rb, values, _xmin, xmax, _pp, _ps in \
                    self._chain_versions((pid, sid)):
                if xmax == 0:
                    out.append((pk, values))
                    break
        return out

    @staticmethod
    def _key_tuple(values, idxs):
        return tuple(values[i] for i in idxs)

    @staticmethod
    def _key_has_null(tup):
        """MySQL 语义：唯一索引里的 NULL 不参与唯一性判定（可多行共存）"""
        return any(v is None for v in tup)

    def _raise_unique(self, spec, tup):
        # 报文保持 MySQL 的 `Duplicate entry '<值>' for key '<键名>'`
        # 形态（1062 的归类同时靠 `Duplicate entry` 与 `唯一` 两个词，
        # 见 `Executor._storage_errno`）。
        shown = '-'.join('NULL' if v is None else str(v) for v in tup)
        raise UniqueViolationError(
            f"Duplicate entry '{shown}' for key '{spec['name']}'"
            f"（唯一约束冲突：列 {list(spec['columns'])} 的取值组合已存在）",
            key=spec['name'], entry=tup)

    def _find_unique_conflict(self, spec, tup, exclude_pks=()):
        idxs = [self._col_idx(c) for c in spec['columns']]
        for pk, values in self._live_rows():
            if pk in exclude_pks:
                continue
            if self._key_tuple(values, idxs) == tup:
                return pk
        return None

    def _validate_unique_insert(self, row):
        """INSERT 前的唯一性检查（在任何 WAL / 页写入之前）

        覆盖表级 `UNIQUE KEY` 与列级 `col ... UNIQUE`（后者已由
        `_sync_column_unique_indexes` 注册成同名唯一规格）。
        """
        if not self._unique_indexes:
            return
        for spec in self._unique_indexes:
            idxs = [self._col_idx(c) for c in spec['columns']]
            tup = self._key_tuple(row, idxs)
            if self._key_has_null(tup):
                continue
            if self._find_unique_conflict(spec, tup) is not None:
                self._raise_unique(spec, tup)

    def _validate_unique_update(self, pks, set_pairs):
        """UPDATE 前的**整体**唯一性检查（与 CHECK 同样的"先全查再写"）

        两条判据缺一不可：
        1. 本条 UPDATE 改出来的多个新行之间不能撞同一个唯一值
           （`UPDATE t SET email='x'` 命中两行 → 1062，MySQL 同样拒绝）；
        2. 新值不能与**未被本条 UPDATE 命中**的存活行相同。

        候选行用 `allow_pk_change=True` 算：唯一列恰好也是主键时
        （`CREATE TABLE t2 (a INT, UNIQUE KEY u (a))` 的首列会被自动选为
        主键），冲突必须在"不允许修改主键"之前被认出并报 1062 —— 用户
        看到的是"唯一冲突 + 键名/值"，不是一句与唯一性无关的通用 1105。
        无冲突时写入路径仍按主键不可改显式拒绝（1105），语义不变。
        """
        if not self._unique_indexes:
            return
        targets = set(pks)
        new_rows = {}
        for pk in pks:
            loc = self.bt.search(pk)
            if loc is None:
                continue
            current = None
            for _vpid, _vsid, _rb, values, _xmin, xmax, _pp, _ps in \
                    self._chain_versions(loc):
                if xmax == 0:
                    current = values
                    break
            if current is None:
                continue
            new_rows[pk] = self._apply_set_pairs(
                current, set_pairs, allow_pk_change=True)
        for spec in self._unique_indexes:
            idxs = [self._col_idx(c) for c in spec['columns']]
            seen = {}
            for pk, values in new_rows.items():
                tup = self._key_tuple(values, idxs)
                if self._key_has_null(tup):
                    continue
                if tup in seen:
                    self._raise_unique(spec, tup)
                seen[tup] = pk
            if not seen:
                continue
            for pk, values in self._live_rows():
                if pk in targets:
                    continue
                tup = self._key_tuple(values, idxs)
                if self._key_has_null(tup):
                    continue
                if tup in seen:
                    self._raise_unique(spec, tup)

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

    def _apply_set_pairs(self, old_row, set_pairs, allow_pk_change=False):
        """按 SET 列表算出新行（写入路径与"写入前整体校验"共用一份语义）

        `allow_pk_change` 只给**写入前的唯一性校验**用：本引擎的索引把
        "主键值 → 版本位置"当 key，改主键需要给 B+Tree 换 key（本批次
        不做，写入路径仍显式拒绝）。但校验阶段必须先按"用户写的 SET"
        算出候选行 —— 否则 `UPDATE t SET a=1 WHERE a=2`（a 同时是主键
        与表级 UNIQUE 列）会先撞上"不允许修改主键"的 1105，把真正的
        唯一冲突（1062）盖掉，排障方向完全错。

        ★ 修复 C：UPDATE 的 NOT NULL / 主键 NULL 校验也放在这里 ——
        `UPDATE t SET v = NULL`（v 是 NOT NULL 列）原来会静默写入 NULL
        （UPDATE 路径此前完全没有 NOT NULL 检查）。现在抛
        `NotNullViolation`（1048/23000），且因为它发生在**写 WAL/改页
        之前**（也在 `_validate_update_checks` / `_validate_unique_update`
        的写入前整体校验里先被调用一次），不会留下部分更新。
        """
        new = list(old_row)
        for col, val in set_pairs.items():
            ci = self._col_idx(col)
            c = self.cols[ci]
            v = coerce(val, c['type'])
            # 显式把 PK / NOT NULL 列改成 NULL：MySQL 是 1048，不是 1105。
            # 排在"不允许修改主键"之前：`SET id = NULL` 的语义错误是
            # "列不允许为空"，不是"不支持改主键"（后者是存储层限制）。
            if v is None and (c.get('notnull') or c.get('primary')):
                raise NotNullViolation(f"列 '{c['name']}' 不允许为空")
            if ci == self.pk_idx and not allow_pk_change:
                raise ValueError("不允许修改主键")
            new[ci] = v
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
        """
        读盘上的 B+Tree 根页号；不可用（不存在 / 内容非法 / 被标记为失效）
        时返回 None —— 调用方会因此**从堆页重建索引**。

        ★ 页号必须是 >= 1: 页 0 是元数据页，指向它的根是"看起来有效的
        垃圾"（`BTree.scan` 会从元数据页读不出叶子而直接结束扫描，
        表现为"整表查不到数据"）。vacuum 重建索引前会把根写成 -1 表示
        "此刻盘上没有有效索引"，重启时就靠这条判定走重建路径。
        """
        if self.btroot_path.exists():
            try:
                rid = int(self.btroot_path.read_text().strip())
                return rid if rid >= 1 else None
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

        ★ 链头判定**不能**用 `xmin` 最大者（本批次实测踩到的坑）:
        autocommit 的合成事务号（0xFFFFFFFF 往下）在数值上比真实事务号
        大得多，若一页上同时有"被 autocommit 删除的旧版本"和"活版本"，
        取 max(xmin) 会选中那个**已被清零槽位**的墓碑 —— 索引指向空槽位，
        那一行彻底读不到（完整性扫描器报 `('index', pk, loc, 'slot-empty')`）。
        正确判据与 `_reachable_locs` 一致: **没有被本 pk 任何其它版本
        当作 prev 的版本才是链头**；出现断链/人为损坏时才退化为取 xmin 最大者。
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
            # ★ 只给"槽位真实存在"的版本建索引项。
            #   页级回收会把死版本的槽位置零（`Page.compact` 之后该页甚至
            #   被截断），此时上面的 `versions` 是按**扫描顺序**收集的，
            #   可能仍包含刚被清零的墓碑。给它们建索引项 = 索引指向空槽位，
            #   该主键从此读不到（恢复路径实测: 800 个索引项里 200 个悬空）。
            page = self.bp.get_page(pid)
            if page is None or page.get_row(sid) is None:
                continue
            self.bt.insert(pk, pid, sid)

    def _calc_next_pk(self) -> int:
        max_pk = 0
        for k, _, _ in self.bt.scan():
            if isinstance(k, int) and k > max_pk:
                max_pk = k
        return max_pk + 1

    # ==================== P6：AUTO_INCREMENT ====================
    #
    # 语义（MySQL 对齐）：
    #   * 省略或显式 NULL → 分配 `下一个号`（每次分配后计数前进，不回退）；
    #   * 显式整数 → 若 >= 当前计数则把计数推进到 value+1（小值不缩减计数）；
    #   * 回滚 / 失败语句**不回收**已分配号（只留空洞，绝不重用）；
    #   * 计数随表元数据持久化（`Database.save_meta` 的
    #     `auto_increment_next`），重启后接着分配；
    #   * 分配在 `Table._lock` 内完成，因此多线程/多连接并发 INSERT
    #     不会拿到重复号。

    _AUTO_INT_TYPES = frozenset((
        'INT', 'BIGINT', 'TINYINT', 'SMALLINT', 'MEDIUMINT', 'INTEGER'))

    @classmethod
    def is_auto_increment_integer_type(cls, col_type) -> bool:
        m = re.match(r'^([A-Z_][A-Z0-9_]*)',
                     str(col_type or '').strip().upper())
        return bool(m) and m.group(1) in cls._AUTO_INT_TYPES

    def _max_existing_auto_value(self, idx):
        """该列现存可见行的最大整数（旧元数据缺计数时据此恢复，不重用）"""
        mx = None
        try:
            for _pk, values in self._live_rows():
                v = values[idx] if idx < len(values) else None
                if isinstance(v, bool) or not isinstance(v, int):
                    continue
                mx = v if mx is None else max(mx, v)
        except Exception:                         # pragma: no cover - 防御
            return None
        return mx

    # ---------- P6.2：自增号的崩溃持久化（区间预留） ----------

    def _auto_block_size(self) -> int:
        """预留区间大小（类属性 > 环境变量 > 默认 1000；最小 1）"""
        v = self.auto_increment_block
        if v is None:
            try:
                v = int(os.environ.get(
                    AUTO_INCREMENT_BLOCK_ENV,
                    AUTO_INCREMENT_BLOCK_DEFAULT))
            except (TypeError, ValueError):
                v = AUTO_INCREMENT_BLOCK_DEFAULT
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return AUTO_INCREMENT_BLOCK_DEFAULT

    def _load_autoinc_reservations(self):
        """读取 `<name>.autoinc`：每列**已持久化**的预留上界（排他）

        上界 R 的语义：`< R` 的号可能已经被分配，恢复时必须从 R 继续
        （允许跳号，但绝不重用）。文件不存在/损坏 → 空（启动时再按
        meta 计数 / 现存最大值兜底）。
        """
        self._auto_reserved = {}
        path = getattr(self, '_autoinc_path', None)
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        self._auto_reserved[str(k)] = max(0, int(v))
                    except (TypeError, ValueError):
                        continue
        except Exception as e:                    # pragma: no cover - 防御
            logger.warn(f"读取自增预留文件失败 {self.name}: {e}")

    def _write_autoinc_file(self, mapping: Dict[str, int]):
        """原子写 `<name>.autoinc`（tmp + fsync + os.replace）

        这是"INSERT 成功前持久化分配"的落盘点：写盘之后才允许把区间内的
        号发给调用方。每个预留区间只写一次，逐行插入不会 fsync。
        """
        try:
            payload = {str(k): int(v) for k, v in (mapping or {}).items()}
            if not payload:
                try:
                    self._autoinc_path.unlink()
                except OSError:
                    pass
                return
            self._autoinc_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._autoinc_path.with_suffix('.autoinc.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(self._autoinc_path))
        except Exception as e:                    # pragma: no cover - 防御
            logger.error(f"写自增预留文件失败 {self.name}: {e}")
            raise

    def _reserve_auto_block(self, idx: int, need_next: int) -> int:
        """确保 `need_next` 之前的号都已持久化；不足则写新的预留区间

        返回新的预留上界（排他）。调用方必须在**返回号码之前**调用本方法
        成功执行（写盘失败会抛异常，语句失败，而不是发出一个不持久号）。
        """
        name = self.cols[idx]['name']
        reserved = int(self._auto_reserved.get(name, 0) or 0)
        if need_next < reserved:
            return reserved
        block = self._auto_block_size()
        new_reserved = max(int(need_next), reserved) + block
        mapping = dict(self._auto_reserved)
        mapping[name] = new_reserved
        self._write_autoinc_file(mapping)
        self._auto_reserved[name] = new_reserved
        return new_reserved

    def _persist_auto_reservation(self, exact: bool = False):
        """把当前自增状态写盘（exact=True 时同时把预留上界收敛到真实下一号）

        调用点：`checkpoint()` / `close()` / TRUNCATE / ALTER AUTO_INCREMENT。
        exact=True 之后重启会从**精确**下一号开始（不跳号）；正常分配过程
        中则只按区间写盘（允许跳号，禁止复用）。

        注意：列上的 AUTO_INCREMENT 被 `MODIFY` 移除后，其计数条目**保留**
        在 `_auto_next`/`_auto_reserved` 里（并继续写 `.autoinc`）。这样：
          * 移除自增不会丢失"已分配号"信息；
          * 之后重新加回自增时从原计数继续（不复用旧号，也不退回 1）；
          * 若整表再无任何自增列且从未有过计数（普通表），才删除该文件。
        """
        keys = set(getattr(self, '_auto_next', {}) or {}) | \
            set(getattr(self, '_auto_reserved', {}) or {})
        if not keys:
            try:
                if self._autoinc_path.exists():
                    self._autoinc_path.unlink()
            except OSError:
                pass
            return
        if exact:
            mapping = {k: int(self._auto_next.get(k, 1) or 1) for k in keys}
            self._auto_reserved = dict(mapping)
        else:
            mapping = {
                k: max(int(self._auto_next.get(k, 1) or 1),
                       int(self._auto_reserved.get(k, 0) or 0))
                for k in keys}
        self._write_autoinc_file(mapping)

    def _init_auto_increment_state(self):
        """初始化自增列/计数（构造期）

        下一号 = max(meta 计数, 磁盘预留上界, 现存最大值+1, 起始号)；
        之后 `_auto_reserved` 取两者的较大值 —— 崩溃恢复后从预留上界继续，
        **不会重用**任何已分配号（最多跳过未用完的预留区间）。

        没有自增列的列名条目（`MODIFY` 移除自增后留下的计数）不参与分配，
        但**原样保留**，供之后重新加回自增时继续使用（不复用旧号）。
        """
        self._auto_cols = [i for i, c in enumerate(self.cols)
                           if c.get('auto_increment')]
        for i in self._auto_cols:
            name = self.cols[i]['name']
            nxt = int(self._auto_next.get(name, 1) or 1)
            reserved = int(self._auto_reserved.get(name, 0) or 0)
            nxt = max(nxt, reserved)
            mx = self._max_existing_auto_value(i)
            if mx is not None:
                nxt = max(nxt, mx + 1)
            if i == self.pk_idx and self._next_pk > nxt:
                nxt = self._next_pk
            self._auto_next[name] = max(1, nxt)
            self._auto_reserved[name] = max(reserved, nxt)
        if self._auto_start_pending is not None and self._auto_cols:
            name = self.cols[self._auto_cols[0]]['name']
            self._auto_next[name] = max(
                self._auto_next.get(name, 1), self._auto_start_pending)
        self._auto_start_pending = None

    def _sync_auto_columns(self):
        """列定义变化后同步自增列下标（ADD/DROP COLUMN、MODIFY 后调用）

        移除自增：只把该列从 `_auto_cols` 摘掉，**保留计数条目**（见
        `_persist_auto_reservation` 的说明）。
        重新加回自增：以现存数据最大值为下限抬升计数（避免把新号分配到
        已存在的显式值上；也不复用移除前的旧号）。
        """
        old_cols = set(getattr(self, '_auto_cols', []) or [])
        self._auto_cols = [i for i, c in enumerate(self.cols)
                           if c.get('auto_increment')]
        for i in self._auto_cols:
            name = self.cols[i]['name']
            self._auto_next.setdefault(name, 1)
            self._auto_reserved.setdefault(name, 0)
            if i not in old_cols:
                # 重新加回 AUTO_INCREMENT：不能被现存显式值撞号
                mx = self._max_existing_auto_value(i)
                floor = (mx + 1) if mx is not None else 1
                self._auto_next[name] = max(
                    int(self._auto_next.get(name, 1) or 1), floor)
                self._auto_reserved[name] = max(
                    int(self._auto_reserved.get(name, 0) or 0),
                    self._auto_next[name])

    def auto_increment_columns(self) -> List[str]:
        return [self.cols[i]['name'] for i in self._auto_cols]

    def auto_increment_next_values(self) -> Dict[str, int]:
        """持久化用的"每列下一个待分配号"快照"""
        return {k: int(v) for k, v in self._auto_next.items()}

    def auto_increment_reservations(self) -> Dict[str, int]:
        """已持久化的预留上界（排他）：`< R` 的号不可再用（诊断/测试用）"""
        return {k: int(v) for k, v in self._auto_reserved.items()}

    def next_auto_increment_value(self):
        """下一个将分配的自增号（没有自增列时 None；SHOW TABLE STATUS 用）"""
        if not self._auto_cols:
            return None
        name = self.cols[self._auto_cols[0]]['name']
        return int(self._auto_next.get(name, 1) or 1)

    def _allocate_auto_value(self, idx: int) -> int:
        """在表锁内分配下一个号（只前进，不回退；回滚也不回收）

        **先确保持久化**（区间没覆盖就写盘+fsync），再返回号码 ——
        进程随后在任何点被硬杀，重启都不会重用这个号。
        """
        name = self.cols[idx]['name']
        nxt = int(self._auto_next.get(name, 1) or 1)
        self._reserve_auto_block(idx, nxt + 1)
        self._auto_next[name] = nxt + 1
        if idx == self.pk_idx:
            self._next_pk = max(self._next_pk, nxt + 1)
        return nxt

    def _advance_auto_value(self, idx: int, value: int):
        """显式插入值推进计数器（大于当前才推进，绝不因小值回退）

        显式大值同样要先持久化：否则硬杀后重启会重新发出这些号。
        """
        name = self.cols[idx]['name']
        value = int(value)
        new_next = max(int(self._auto_next.get(name, 1) or 1), value + 1)
        if new_next > int(self._auto_reserved.get(name, 0) or 0):
            self._reserve_auto_block(idx, new_next)
        self._auto_next[name] = new_next
        if idx == self.pk_idx:
            self._next_pk = max(self._next_pk, value + 1)

    def set_auto_increment(self, value: int) -> int:
        """`ALTER TABLE ... AUTO_INCREMENT = n`：设置下一个待分配号

        不允许把计数设到"现存最大值 + 1"之下（MySQL 的取值下限语义）：
        返回**实际生效**的下一号（真实值，不装作已经设成更小的数）。
        设置结果立即持久化（写预留文件 + 由調用方 save_meta）。
        """
        with self._lock:
            if not self._auto_cols:
                raise DdlError(
                    f"Table '{self.name}' has no AUTO_INCREMENT column",
                    errno=1075, sqlstate='42000')
            idx = self._auto_cols[0]
            mx = self._max_existing_auto_value(idx)
            floor = (mx + 1) if mx is not None else 1
            new = max(1, int(value), floor)
            name = self.cols[idx]['name']
            self._auto_next[name] = new
            self._next_pk = max(self._next_pk, new)
            # 收敛预留：文件写 exact=new，重启从 new 开始（不跳号）
            mapping = dict(self._auto_next)
            self._auto_reserved = dict(mapping)
            self._write_autoinc_file(mapping)
            return new

    # ==================== 结构变更（ALTER TABLE：ADD / DROP COLUMN） ====================
    #
    # 行载荷是**按列位置编码的 JSON 数组**（见 `_encode_row`）。因此
    # "加一列 / 删一列"在存储层的真实动作就是"把每个行版本的数组改长度"
    # —— 只改 `self.cols` 元数据的话，`SHOW COLUMNS` 会说有这一列，
    # 而 `SELECT` 拿到的行仍然少/多一个字段（静默错位），这正是本批次
    # 要清掉的那类"看起来成功、其实没生效"。
    #
    # 下面两个方法把这件事做成**一次规划 + 一次执行**：
    #   * 规划阶段只读页、算好每一行"原地写"还是"搬到哪一页哪一个槽位"；
    #   * 执行阶段先搬（新位置写入成功后才清零老槽位），再原地改写，
    #     最后逐条改写索引项与 prev 指针。
    # `Page.insert_row` 的容量判据与 `free_space()` 完全等价
    # （`next_row_ptr - len >= PAGE_HEADER_SIZE + (n+1)*4`），所以规划出来
    # 的位置在执行阶段必然拿得到；拿不到就是页内容被别的东西改过，
    # 此时抛 `DdlError` 让整个 DDL 失败（不允许半套）。

    def iter_all_row_versions(self):
        """堆里**全部**行版本：[(pid, sid, values, xmin, xmax, pp, ps)]

        与 `_visible_pks`（只取索引可达、且对某读视图可见的版本）不同：
        这里把页上每个槽位都读出来。结构变更改的是**行载荷的列数**，
        墓碑与老版本也必须一起改 —— 否则重启后老快照沿 prev 链回溯时
        会拿到少一列/多一列的行（`row[pk_idx]` 错位甚至越界）。
        """
        out = []
        for pid in range(1, self.bp.total_pages()):
            page = self.bp.get_page(pid)
            if page is None or page.page_type != PAGE_TYPE_HEAP:
                continue
            for sid, rb in list(page.iter_rows()):
                r = self._decode_row_ver(rb)
                if r is None:
                    continue
                values, xmin, xmax, pp, ps = r
                out.append((pid, sid, values, xmin, xmax, pp, ps))
        return out

    def rewrite_row_payloads(self, transform, old_len: int, new_len: int,
                            zero_removed: bool = False,
                            always_transform: bool = False) -> dict:
        """把所有行版本的载荷按 `transform(values) -> new_values` 重写

        保证与语义：

        * **按行载荷长度幂等**：长度已是 `new_len` 的行原样保留；长度是
          `old_len` 的行走 `transform`；其它长度 → 显式 `DdlError`
          （列数对不上的行是损坏，绝不猜测补齐，那会掩盖数据问题）。
          `always_transform=True` 时**所有行版本**都走 transform（列数
          不变的类型变更 `ALTER ... MODIFY COLUMN` 用；转换必须幂等，
          崩溃恢复会再跑一次同一份 transform）。
          崩溃恢复会再次调用同一份 transform，靠这条判据判断"改过没有"。
        * 原地放不下的行搬到**本页的空闲区**（优先）、别的堆页，或新页；
          搬迁后索引项（pk → 链头位置）与版本链的 prev 指针**逐条改写**，
          Scheme A「索引指向最新版本」的语义不变。
        * `zero_removed=True` 时，被腾空的槽位与行尾残字节一律**清零**
          （`DROP COLUMN` 用：数据必须真的不可再从数据文件里读到）。
        * 页号/槽位只增不改：`self._next_pk`、`pk_idx` 都不受影响。

        返回统计 dict；表为空（一行都没有）时不做任何事。
        """
        snap = self.iter_all_row_versions()
        stats = {'rows': len(snap), 'in_place': 0, 'relocated': 0,
                 'new_pages': 0}
        if not snap:
            return stats

        # ---- 1) 算新载荷 + 新字节长度，并判"原地够不够" ----
        plans = []          # [pid, sid, nv, xmin, xmax, pp, ps, need]
        for pid, sid, values, xmin, xmax, pp, ps in snap:
            n = len(values)
            if not always_transform and n == new_len:
                nv = values
            elif always_transform or n == old_len:
                nv = transform(values)
            else:
                raise DdlError(
                    f"表 {self.name}：主键取值 {values[0]!r} 的行有 {n} 个"
                    f"字段，既不是变更前的 {old_len} 也不是变更后的 "
                    f"{new_len}；拒绝在列数不一致的表上做结构变更",
                    errno=1105)
            if len(nv) != new_len:      # transform 自己写错了 → 显式失败
                raise DdlError(
                    f"表 {self.name}：结构变更后的行有 {len(nv)} 个字段，"
                    f"期望 {new_len}", errno=1105)
            # prev 指针是定长 8 字节，因此这里算出的长度与最终写入的长度一致
            need = len(self._encode_row(nv)) + 4
            plans.append([pid, sid, nv, xmin, xmax, pp, ps, need])

        heap_pages = []
        for pid in range(1, self.bp.total_pages()):
            page = self.bp.get_page(pid)
            if page is not None and page.page_type == PAGE_TYPE_HEAP:
                heap_pages.append(pid)

        sim_free = {}
        sim_slots = {}

        def _sim(pid):
            if pid not in sim_free:
                p = self.bp.get_page(pid)
                if p is None or p.page_type != PAGE_TYPE_HEAP:
                    sim_free[pid] = -1
                    sim_slots[pid] = 0
                else:
                    sim_free[pid] = p.free_space()
                    sim_slots[pid] = p.n_slots
            return sim_free[pid]

        # ---- 2) 规划搬迁（动手之前一次算完） ----
        moves = {}          # (pid, sid) -> (npid, nsid)
        new_page_used = []
        new_page_slots = []
        for item in plans:
            pid, sid, need = item[0], item[1], item[7]
            page = self.bp.get_page(pid)
            cur = page.get_row(sid) if page is not None else None
            if cur is None:
                raise DdlError(
                    f"表 {self.name}：索引/槽位指向不存在的行 "
                    f"(page={pid}, slot={sid})，拒绝在其上做结构变更",
                    errno=1105)
            if len(cur) >= need - 4:
                continue                       # 原地写得下
            target = None
            for cand in [pid] + [x for x in heap_pages if x != pid]:
                if _sim(cand) >= need:
                    target = (cand, sim_slots[cand])
                    sim_slots[cand] += 1
                    sim_free[cand] -= need
                    break
            if target is None:
                cap = PAGE_SIZE - PAGE_HEADER_SIZE
                if not new_page_used or new_page_used[-1] + need > cap:
                    new_page_used.append(0)
                    new_page_slots.append(0)
                k = len(new_page_used) - 1
                target = ('NEW', k, new_page_slots[k])
                new_page_slots[k] += 1
                new_page_used[k] += need
            moves[(pid, sid)] = target

        # 新页在这里就真正分配出来：prev 指针要写**真实页号**，
        # 而"别的新页"可能就是本行前驱的落点。提前分配只会多出几个
        # 空堆页（合法、可复用），不会留下半套结构。
        allocated = {}
        for key, target in list(moves.items()):
            if target[0] != 'NEW':
                continue
            k = target[1]
            if k not in allocated:
                pg = self.bp.new_page(PAGE_TYPE_HEAP)
                if pg is None:                       # pragma: no cover
                    raise DdlError(
                        f"表 {self.name}：无法为新行分配堆页", errno=1105)
                allocated[k] = pg.page_id
                stats['new_pages'] += 1
            moves[key] = (allocated[k], target[2])

        def _remap_prev(pp, ps):
            if not pp or pp == NO_PREV:
                return NO_PREV, 0
            t = moves.get((pp, ps))
            return (pp, ps) if t is None else t

        # ---- 3) 执行：先搬（插新位置 → 成功后才清零老槽位），再原地改写 ----
        for item in plans:
            key = (item[0], item[1])
            target = moves.get(key)
            if target is None:
                continue
            npp, nps = _remap_prev(item[5], item[6])
            nrb = self._encode_row(
                item[2], xmin=item[3], xmax=item[4],
                prev_page=npp, prev_slot=nps)
            dst = self.bp.get_page(target[0])
            if dst is None or dst.page_type != PAGE_TYPE_HEAP:
                raise DdlError(
                    f"表 {self.name}：搬迁目标页 {target[0]} 不可用",
                    errno=1105)
            got = dst.insert_row(nrb)
            if got is None or got != target[1]:
                # 计划与实际判据等价；走到这里说明页内容在规划之后被改过
                raise DdlError(
                    f"表 {self.name}：搬迁到页 {target[0]} 槽位 "
                    f"{target[1]} 失败（实际 {got}）—— 结构变更中止",
                    errno=1105)
            src = self.bp.get_page(key[0])
            if src is not None:
                if zero_removed:
                    src.zero_slot(key[1])
                else:
                    src.delete_row(key[1])
            stats['relocated'] += 1

        for item in plans:
            key = (item[0], item[1])
            if key in moves:
                continue
            npp, nps = _remap_prev(item[5], item[6])
            nrb = self._encode_row(item[2], xmin=item[3], xmax=item[4],
                                   prev_page=npp, prev_slot=nps)
            page = self.bp.get_page(key[0])
            if page is None:
                raise DdlError(
                    f"表 {self.name}：页 {key[0]} 不可用", errno=1105)
            ok = (page.update_row_shrink(key[1], nrb) if zero_removed
                  else page.update_row_inplace(key[1], nrb))
            if not ok:                               # pragma: no cover - 防御
                raise DdlError(
                    f"表 {self.name}：原地改写 (page={key[0]}, "
                    f"slot={key[1]}) 失败 —— 结构变更中止", errno=1105)
            stats['in_place'] += 1

        # ---- 4) 索引项指向搬走了的链头 → 改指新位置 ----
        if moves:
            for pk, pid, sid in list(self.bt.scan()):
                t = moves.get((pid, sid))
                if t is not None:
                    self.bt.insert(pk, t[0], t[1])
        # 搬迁后有些页的登记项可能不再有空间（或页号已越界）→ 校验一遍。
        # 这里**不**主动 `_register_space_page`：那些页上还压着活跃行，
        # 登记成"可复用空闲页"会让 `integrity_scan` 的
        # `rows_on_free_pages` 出现假告警；这些空隙交给一次 VACUUM 回收。
        self._validate_free_pages()
        return stats

    def truncate_all(self) -> dict:
        """清空全部行，**保留表定义**（列 / CHECK / 分区 / 主键索引结构）

        MySQL 的 `TRUNCATE TABLE` 是 DDL：清数据 + 重置自增 + 不可回滚，
        与 `DELETE FROM t`（DML、逐行删除、可回滚、不重置自增）不同。
        本引擎的实现是：

        * 每个堆页的行槽位**清零**（`Page.zero_slot`，数据真的没了）；
        * 空页 `compact()` → `n_slots` 复位、行游标归位（页回到"全新页"）；
        * 主键 B+Tree 的键**逐条删除**（不是新建空树：新建会把树页追加到
          文件尾，文件白白变大，而"逐条删"留下的是可复用的空页）；
        * `_next_pk` 复位（MySQL 的 AUTO_INCREMENT 重置语义）。

        **不**删除/重建 `.dat`/`.wal` 文件：换文件句柄会让任何还持有本
        `Table` 对象的调用方（同一个 Executor / 统计管理器）读到旧文件。
        """
        with self._lock:
            for pid in range(1, self.bp.total_pages()):
                page = self.bp.get_page(pid)
                if page is None or page.page_type != PAGE_TYPE_HEAP:
                    continue
                n = 0
                for sid, _rb in list(page.iter_rows()):
                    page.zero_slot(sid)
                    n += 1
                page.compact()
            dropped = 0
            for pk, _pid, _sid in list(self.bt.scan()):
                if self.bt.delete(pk):
                    dropped += 1
            self._next_pk = 1
            # TRUNCATE 是 DDL：MySQL 同时把 AUTO_INCREMENT 重置回起点。
            # 也重置"曾被 MODIFY 移除自增"留下的计数条目（与重新加回自增
            # 的语义一致：空表从 1 开始）。
            names = set(self._auto_next) | set(self._auto_reserved)
            for i in self._auto_cols:
                names.add(self.cols[i]['name'])
            for name in names:
                self._auto_next[name] = 1
                self._auto_reserved[name] = 1
            try:
                # 重置也必须是**持久**的：否则硬杀后重启会从旧的预留上界
                # 继续，TRUNCATE 语义（回到 1）就没生效。
                self._persist_auto_reservation(exact=True)
            except Exception as e:                # pragma: no cover - 防御
                logger.warn(f"TRUNCATE 写自增预留失败 {self.name}: {e}")
            self._last_heap_pid = None
            # 全文索引是**派生**数据（不持久化），清空表就必须一起清空，
            # 否则 MATCH ... AGAINST 还能搜到已经删掉的行。这里保留索引
            # **定义**（列名），只把倒排表清空：清成 None 会让
            # `has_fulltext_index()` 变 False，之后 `MATCH` 会报"该列上没有
            # 全文索引" —— 那是把"表被清空"误报成"索引没了"。
            if self._fulltext is not None:
                from ..sql.fulltext import FullTextManager
                mgr = FullTextManager()
                for cname in list(self._fulltext.indexes):
                    mgr.create_index(cname)
                self._fulltext = mgr
            for pid in range(1, self.bp.total_pages()):
                page = self.bp.get_page(pid)
                if page is not None and page.page_type == PAGE_TYPE_HEAP:
                    self._register_space_page(pid)
            self._secondary_dirty = True
            return {'deleted_versions': dropped}

    def index_specs(self):
        """本表**真实存在 / 真实强制**的索引（`SHOW INDEX` 的唯一数据源）

        现状必须如实说明（宁可少报，也不"看起来有、其实没有"）：

        * **主键 B+Tree**：本引擎唯一的存储层索引（Scheme A：key = 主键
          取值，value = 该主键最新版本的 `(page, slot)`），插入时的主键
          重复检测也是它做的 → `Index_type='BTREE'`；
        * **表级 `UNIQUE KEY/INDEX 名字 (列...)`**：没有独立索引结构，
          唯一性由写入路径（INSERT / UPDATE）的整体检查强制 → 这里报出来
          （`Non_unique=0` 是真的），但 `Index_type='UNIQUE-CHECK'`
          **不谎称 BTREE**；`Cardinality` 是当前存活行里不同非 NULL 键值
          的**真实个数**（不是估计值）；
        * **列级 `col ... UNIQUE`**：等价于自动建一个名为列名的唯一索引
          （MySQL 惯例），由 `_sync_column_unique_indexes()` 注册进同一份
          `_unique_indexes` → 同样真实强制、同样报 `Non_unique=0` /
          `Index_type='UNIQUE-CHECK'`。

        返回 `[{'name','unique','columns','cardinality','index_type'}]`；
        多列索引会给出多个列名（`SHOW INDEX` 侧按 `Seq_in_index` 展开成
        多行）。
        """
        out = []
        pk_cols = [c['name'] for c in self.cols
                   if c.get('primary') and not c.get('implicit_pk')]
        if pk_cols:
            out.append({'name': 'PRIMARY', 'unique': True,
                        'columns': pk_cols,
                        'cardinality': self.bt.count(),
                        'index_type': 'BTREE'})
        for spec in self._unique_indexes:
            out.append({'name': spec['name'], 'unique': True,
                        'columns': list(spec['columns']),
                        'cardinality': self._unique_cardinality(spec),
                        'index_type': 'UNIQUE-CHECK'})
        # P5.1/task-59：单列与多列非唯一索引都有真实的**内存映射**
        # （多列按最左前缀等值/范围/ORDER BY 加速，重启后按数据重建），
        # 因此 Index_type 如实报 BTREE；Cardinality 是真实的不同非 NULL
        # 键值个数（多列按整组元组计，不是估计值）。
        for spec in self._secondary_indexes:
            cols = list(spec['columns'])
            out.append({'name': spec['name'], 'unique': False,
                        'columns': cols,
                        'cardinality': self._index_cardinality(cols),
                        'index_type': 'BTREE'})
        return out

    def _unique_cardinality(self, spec):
        """唯一约束的真实基数（不同非 NULL 键值个数，含未提交行）"""
        idxs = [self._col_idx(c) for c in spec['columns']]
        seen = set()
        for _pk, values in self._live_rows():
            tup = self._key_tuple(values, idxs)
            if self._key_has_null(tup):
                continue
            seen.add(tup)
        return len(seen)

    def refresh_schema_meta(self):
        """结构变更后刷新派生元数据（主键位置 / CHECK 缓存 / 分区列下标）

        `cols` 被换掉之后，`pk_idx` / `pk_col` / `pk_type` 可能指向错列
        （DROP COLUMN 删掉了主键**前面**的列时下标会整体前移），
        `_has_checks` 也要重算 —— 这些缓存不刷新就会出现"元数据说列 A
        是主键、实际按列 B 做主键"这种最隐蔽的错位。
        """
        idx = next((i for i, c in enumerate(self.cols)
                    if c.get('primary')), 0)
        self.pk_idx = idx
        self.pk_col = self.cols[idx]['name']
        self.pk_type = self.cols[idx]['type']
        self._refresh_check_flag()
        names = [c['name'] for c in self.cols]
        spec = getattr(self, '_partition_spec', None) or {}
        col = spec.get('col')
        self._partition_col_idx = names.index(col) if col in names else None
        # 列级 UNIQUE 同步（ADD/DROP COLUMN、崩溃恢复补完 DDL 后都走这里）：
        # 新加的 `UNIQUE` 列必须立刻参与写入校验，不能等下一次重启。
        if hasattr(self, '_unique_indexes'):
            self._sync_column_unique_indexes()
        # P6：列定义变化后同步自增列下标/计数键（ADD/DROP COLUMN 等）
        if hasattr(self, '_auto_next'):
            self._sync_auto_columns()
        # P5.1：结构变化会让二级索引映射失效（列位置/存在性变了）
        if hasattr(self, '_secondary_dirty'):
            self._secondary_dirty = True

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
        """列的**真实**默认值；没声明 DEFAULT 时返回内部哨兵 `_OMITTED`

        `cols[i]['default']` 由解析器按 MySQL 取值形态解析：
        数字存字符串（交给 `coerce` 按列类型转）、字符串是 Python str、
        当前时间函数存表达式原文（这里真的求值，不假装）。

        ★ 修复 C：没有声明 DEFAULT 的列返回 `_OMITTED` 而不是 `None`。
        调用方（`Executor._cmd_insert` 的列清单补位、`_insert_locked` 的
        短列表补位）因此能区分"这一列没出现在 INSERT 里"（可自取主键）
        与"用户显式写了 NULL"（PK / NOT NULL 列必须报 1048）。哨兵在
        `_insert_locked` 里解析成 `None` 后才进入行载荷，不会落盘。
        """
        if not col.get('has_default'):
            return _OMITTED
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
        #
        # 没声明 DEFAULT 的列补的是 `_OMITTED` 哨兵（见 `_column_default`），
        # 用来区分"列没出现在 INSERT 里"和"显式 NULL"。
        if len(values) < len(self.cols):
            values = list(values) + [
                self._column_default(self.cols[i])
                for i in range(len(values), len(self.cols))]
        else:
            values = list(values)

        # 记录"哪些列是 INSERT 里没给的"（哨兵 -> None），然后立刻把哨兵
        # 换成 None：生成列求值 / 类型转换 / 行载荷都只看见普通 NULL。
        omitted = {i for i, v in enumerate(values) if v is _OMITTED}
        if omitted:
            values = [None if v is _OMITTED else v for v in values]

        # 生成列（GENERATED ALWAYS AS (...) STORED）：写入前按表达式真算，
        # 用户给的值被覆盖（MySQL 里给生成列赋值本来就是错误，这里不静默
        # 保留用户值，而是以表达式结果为准）。
        values = self._apply_generated(values)

        # P6：AUTO_INCREMENT —— 省略/NULL 分配下一号；显式值推进计数器。
        # 必须在类型转换/NOT NULL 检查之前赋值（自增列通常声明 NOT NULL），
        # 且在拿行锁/写 WAL 之前完成（失败语句只会留下号空洞，不重号）。
        for i in self._auto_cols:
            v = values[i]
            if v is None:
                values[i] = self._allocate_auto_value(i)
            else:
                try:
                    iv = int(coerce(v, self.cols[i]['type']))
                except Exception:                 # 类型错误交给下面统一报
                    iv = None
                if iv is not None:
                    self._advance_auto_value(i, iv)

        # 类型转换 + 声明式类型约束 + NOT NULL 检查（修复 C）
        #
        # `coerce` 现在会按列声明校验范围/长度/日期/精度并抛带
        # errno/sqlstate 的 TypeCheckError（1264/1406/1292/1366…），
        # 不再静默入库；NOT NULL 违反抛 NotNullViolation（1048/23000）。
        # 全部发生在拿锁 / 写 WAL 之前，因此不会留下半条记录。
        row = []
        for i, c in enumerate(self.cols):
            v = coerce(values[i], c['type'])
            if v is None and c.get('notnull'):
                raise NotNullViolation(
                    f"列 '{c['name']}' 不允许为空")
            row.append(v)

        # 主键：**显式 NULL** → 1048（MySQL：PK 隐式 NOT NULL）；
        # 只有"列清单里没给主键"（`_OMITTED`）才沿用本引擎的既有能力
        # —— 自取下一个主键值（tests/test_insert_atomicity.py ⑤）。
        # 没有声明主键的表（pk_idx 回退到 0，仅作内部行键）保持旧行为。
        pk = row[self.pk_idx]
        if pk is None:
            pk_col = self.cols[self.pk_idx]
            if pk_col.get('primary') and self.pk_idx not in omitted:
                raise NotNullViolation(
                    f"列 '{pk_col['name']}' 不允许为空")
            pk = self._next_pk
            row[self.pk_idx] = pk

        # CHECK 约束：两条写入路径（SQL `INSERT` 与 `Table.insert` 直写）
        # 都汇到这里，因此这一处校验覆盖全部入口（SQL `UPDATE` 见
        # `_update_locked`）。放在拿锁 / 写 WAL **之前** —— 违规时不会留下
        # 任何半条记录。违规抛 `CheckConstraintError`（ValueError 子类），
        # SQL 层据此返回 `ERROR 3819 (HY000)`。
        self._validate_checks(row)

        # 唯一性（表级 UNIQUE KEY）：与 CHECK 同一位置 —— 拿锁 / 写 WAL
        # **之前**。冲突时抛 1062 语义的 UniqueViolationError，不会留下
        # 任何半条记录（也不会白占一个 WAL 序号）。
        self._validate_unique_insert(row)

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
        self._secondary_dirty = True
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

    def _free_space_of(self, pid):
        """页 pid 当前可复用字节数（不是堆页 / 读不到 → -1）"""
        p = self.bp.get_page(pid)
        if p is None or p.page_type != PAGE_TYPE_HEAP:
            return -1
        return p.free_space()

    def _register_space_page(self, pid):
        """
        把页登记为"可复用空闲页"（LIFO: 最近空出来的优先复用）。

        只有真的还有空间的页才登记。登记表会**持久化**（`<name>.free`），
        下一次启动仍然知道这些页可以填 —— 这正是"重启后 `.dat` 不白长"
        的唯一依据。
        """
        if pid < 1 or pid in self.freelist:
            return False
        if self._free_space_of(pid) <= 0:
            return False
        return self.freelist.add(pid)

    def _unregister_space_page(self, pid):
        """页被塞满（或要截断）时摘掉登记；空页也一并摘掉"""
        ok = self.freelist.discard(pid)
        if ok:
            # 页被填满是一次性事件（不是每行都发生），顺手落盘:
            # 否则崩溃重启后登记表里还留着"其实已经满了"的页，
            # 只浪费一次检查 —— 但落盘成本更低，且能让登记表更准确。
            self.freelist.save()
        return ok

    def _validate_free_pages(self):
        """
        校验登记表: 只保留"当前真的是堆页且 free_space() > 0"的页号。

        为什么每次都重新校验: 登记表是**旁挂提示**，`.dat` 的页号可以
        被 B+Tree 复用（截断后文件长回来）、也可以因为截断而越界。
        相信文件里写的页号会导致把行写进树页 / 写越界。
        返回被丢弃的页号个数。
        """
        drop = []
        for pid in list(self.freelist.pages):
            if self._free_space_of(pid) <= 0:
                drop.append(pid)
        for pid in drop:
            self.freelist.discard(pid)
        return len(drop)

    def free_page_stats(self):
        """空闲页统计（供 `VACUUM` 结果集 / 巡检使用）"""
        free_bytes = 0
        usable = 0
        for pid in list(self.freelist.pages):
            fs = self._free_space_of(pid)
            if fs <= 0:
                continue
            usable += 1
            free_bytes += fs
        return {
            'registered_pages': len(self.freelist.pages),
            'usable_pages': usable,
            'free_bytes': free_bytes,
            'corrupt_entries': self.freelist.corrupt_entries,
        }

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
            # 本页放不下了: 先把它登记为"还有空隙、可复用"（登记表持久化），
            # 下次 vacuum 之后/重启之后这些空隙还能填，而不是直接丢弃。
            self._register_space_page(last.page_id)
            target = self._find_heap_page_with_space(need)
            if target is None:
                target = self.bp.new_page(PAGE_TYPE_HEAP)
            sid = target.insert_row(rb)
            if sid is None:
                return None, None
            last = target
        if last.free_space() <= 4:
            # 刚刚被塞满: 摘掉登记，避免后续插入每次都白查一次
            self._unregister_space_page(last.page_id)
        self._last_heap_pid = last.page_id
        return last.page_id, sid

    def _find_heap_page_with_space(self, need: int):
        """
        在"有空闲空间的页"里找一个能容纳 need 字节的（没有则 None）。

        ★ 只查登记表（`self.freelist.pages`，**持久化**在 `<name>.free`），
        **不做全表页扫描**：全扫在缓冲区只有 500 帧、而 .dat 有几千页时
        会引发 LRU 颠簸（每插一行驱逐一个脏页），实测把 10 万行量级的
        插入拖成小时级。登记表里放不下的页在检查时被剔除（同时摘掉
        登记 = 它确实不再有用），因此稳态是 O(1)（每页被选中后一直
        用到放不下为止）。

        阶段 2 第 3 项的变化: 登记表不只记"vacuum 紧凑化过的页"，任何
        **插满后还剩空隙**的页都会在 `_heap_insert` 里登记（并立刻落盘），
        重启后这些页仍然可填 —— 这是"复用不因重启失效"的关键。
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

        # 唯一性（表级 UNIQUE KEY）：**先**做整体校验，理由有两层：
        # 1. 与 CHECK 一样"先对全部目标行整体校验，再逐行写"，否则第 N 行
        #    撞唯一值时前 N-1 行已经落盘（autocommit 下退不回来），语句
        #    报错却留下部分更新 —— 另一种"假成功"；
        # 2. 唯一冲突必须是 1062（带键名/值），不能被"不允许修改主键"这类
        #    通用 1105 抢先盖掉 —— 唯一列恰好是主键时最容易踩到。
        if self._unique_indexes:
            self._validate_unique_update(pks, set_pairs)

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
        if n:
            self._secondary_dirty = True
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
        if n:
            self._secondary_dirty = True
        return n

    # ==================== 读操作 ====================
    #
    # 读路径统一走 _read_version()：索引头 -> prev 链，取"对读视图
    # 可见的最新版本"。内存里的 mvcc.undo_log 只作为本进程内的补充
    # （页上没有的历史版本），重启后靠页上的链。
    # 扫描代价: 单主键等值查找是 O(链长) 次单槽读取（无 vacuum 时
    # 链会随更新次数增长）；全表扫描是 O(索引项数 × 链长)。

    # ==================== P5.1：非唯一二级索引（真实加速） ====================
    #
    # 只做**单列**二级索引（`CREATE INDEX idx ON t (col)`）：等值/范围
    # 走内存映射（key -> set(pk)），ORDER BY col 走按 key 排序的映射。
    # 映射来源是 `_live_rows()`（当前可见数据）；任何写操作只把映射标脏，
    # 下一次可安全使用索引的查询时重建 —— 因此不会出现"索引与表数据不一致
    # 却仍被使用"。有活跃显式事务/读视图时**安全回退**全表扫描：二级索引
    # 不承担 MVCC 可见性判断（正确性优先）。
    # 多列索引、OR、LIKE、表达式谓词等一律回退。

    _SECONDARY_INDEX_OPS = frozenset(('=', '<', '>', '<=', '>='))

    def _secondary_usable(self, read_view) -> bool:
        if not getattr(self, '_secondary_indexes', None):
            return False
        if getattr(self, '_read_views', None):
            return False
        return True

    def _secondary_specs_for_column(self, name):
        out = []
        for spec in (self._secondary_indexes or []):
            cols = [str(c) for c in (spec.get('columns') or [])]
            if name in cols:
                out.append(spec)
        return out

    def _secondary_spec_for_column(self, name):
        """以 `name` 为**首列**的二级索引（ORDER BY 前缀匹配用）"""
        for spec in (self._secondary_indexes or []):
            cols = [str(c) for c in (spec.get('columns') or [])]
            if cols and cols[0] == name:
                return spec
        return None

    def _rebuild_secondary_maps(self):
        """重建二级索引映射：每个长度 k 一份「前 k 列元组 -> pk 集合」"""
        maps = {}
        for spec in (self._secondary_indexes or []):
            cols = [str(c) for c in (spec.get('columns') or [])]
            if not cols:
                continue
            try:
                cis = [self._col_idx(c) for c in cols]
            except KeyError:
                continue
            n = len(cols)
            by_len = [None] + [{} for _ in range(n)]
            for pk, values in self._live_rows():
                vals = [values[ci] if ci < len(values) else None
                        for ci in cis]
                for k in range(1, n + 1):
                    by_len[k].setdefault(tuple(vals[:k]), set()).add(pk)
            maps[str(spec.get('name'))] = {
                'spec': spec, 'maps': by_len,
                'sorted': [None] * (n + 1)}
        self._secondary_maps = maps
        self._secondary_dirty = False
        self._index_stats['rebuilds'] += 1
        return maps

    def _secondary_entry(self, spec):
        if self._secondary_dirty or self._secondary_maps is None:
            self._rebuild_secondary_maps()
        return (self._secondary_maps or {}).get(str(spec.get('name')))

    @staticmethod
    def _secondary_key_order(key):
        """元组键的 NULL-last 排序键（逐列 `(is_none, value)`）"""
        return tuple((v is None, v) for v in key)

    def _secondary_sorted_keys(self, entry, prefix_len):
        keys = entry['sorted'][prefix_len]
        if keys is None:
            keys = sorted(entry['maps'][prefix_len].keys(),
                          key=self._secondary_key_order)
            entry['sorted'][prefix_len] = keys
        return keys

    def _secondary_conjuncts(self, where):
        """展开 AND 链里的单列比较条件（OR/其它形态返回空 = 不可用）"""
        out = []
        if not isinstance(where, dict):
            return out
        if 'and' in where:
            for c in where['and']:
                out.extend(self._secondary_conjuncts(c))
        elif 'column' in where:
            out.append(where)
        return out

    def _secondary_plan(self, where):
        """选择可用索引：返回 (spec, {列下标: [条件]}) 或 None

        对多列索引按最左前缀评分：连续等值列越多越优先；下一列的范围
        条件同样可用。非最左前缀（跳过首列）不参与评分 → 安全回退。
        """
        if not isinstance(where, dict) or 'or' in where:
            return None
        conj = self._secondary_conjuncts(where)
        if not conj:
            return None
        best = None
        for spec in (self._secondary_indexes or []):
            cols = [str(c) for c in (spec.get('columns') or [])]
            if not cols:
                continue
            by_idx = {}
            for cond in conj:
                if cond.get('op') not in self._SECONDARY_INDEX_OPS:
                    continue
                cname = cond.get('column')
                if cname not in cols:
                    continue
                by_idx.setdefault(cols.index(cname), []).append(cond)
            if not by_idx or 0 not in by_idx:
                continue            # 最左前缀不满足：该索引不可用
            prefix = 0
            for i in range(len(cols)):
                if any(c.get('op') == '=' for c in by_idx.get(i, ())):
                    prefix = i + 1
                else:
                    break
            next_range = any(c.get('op') != '='
                             for c in by_idx.get(prefix, ()))
            if prefix == 0 and not next_range:
                continue
            score = (prefix, len(cols))
            if best is None or score > best[0]:
                best = (score, spec, by_idx)
        if best is None:
            return None
        return best[1], best[2]

    def _secondary_candidate_pks(self, spec, by_idx):
        entry = self._secondary_entry(spec)
        if entry is None:
            return None
        cols = [str(c) for c in (spec.get('columns') or [])]
        n = len(cols)
        ctypes = []
        for c in cols:
            try:
                ctypes.append(self.cols[self._col_idx(c)].get('type'))
            except KeyError:
                return None

        def conv(v, i):
            try:
                return coerce(v, ctypes[i])
            except Exception:
                return v

        eq_vals = []
        for i in range(n):
            eqs = [c for c in by_idx.get(i, ()) if c.get('op') == '=']
            if not eqs:
                break
            eq_vals.append(conv(eqs[-1].get('value'), i))
        prefix = len(eq_vals)
        if prefix >= n:
            return set(entry['maps'][n].get(tuple(eq_vals), ()))
        range_conds = [c for c in by_idx.get(prefix, ())
                       if c.get('op') in ('<', '>', '<=', '>=')]
        if not range_conds:
            if prefix == 0:
                return None
            return set(entry['maps'][prefix].get(tuple(eq_vals), ()))
        lower = upper = None
        lower_inc = upper_inc = True
        for c in range_conds:
            v = conv(c.get('value'), prefix)
            op = c.get('op')
            if v is None:
                return set()
            try:
                if op == '>':
                    if lower is None or v > lower or \
                            (v == lower and not lower_inc):
                        lower, lower_inc = v, False
                elif op == '>=':
                    if lower is None or v > lower:
                        lower, lower_inc = v, True
                elif op == '<':
                    if upper is None or v < upper or \
                            (v == upper and not upper_inc):
                        upper, upper_inc = v, False
                elif op == '<=':
                    if upper is None or v < upper:
                        upper, upper_inc = v, True
            except TypeError:
                return None
        fixed = tuple(eq_vals)
        keys = [k for k in self._secondary_sorted_keys(entry, prefix + 1)
                if k[:prefix] == fixed and k[prefix] is not None]
        vals = [k[prefix] for k in keys]
        lo = 0
        hi = len(vals)
        if lower is not None:
            lo = (bisect.bisect_left(vals, lower) if lower_inc
                  else bisect.bisect_right(vals, lower))
        if upper is not None:
            hi = (bisect.bisect_right(vals, upper) if upper_inc
                  else bisect.bisect_left(vals, upper))
        out = set()
        full_map = entry['maps'][prefix + 1]
        for k in keys[lo:hi]:
            out.update(full_map.get(k, ()))
        return out

    def _normalize_order_keys(self, order_by):
        """把 `(col, desc)` / `[(col, desc), ...]` 归一成键列表"""
        if not order_by:
            return []
        if isinstance(order_by, (list, tuple)) and order_by \
                and isinstance(order_by[0], (list, tuple)):
            return [(str(c), bool(d)) for c, d in order_by]
        if isinstance(order_by, (list, tuple)) and len(order_by) >= 2:
            return [(str(order_by[0]), bool(order_by[1]))]
        return []

    def _index_ordered_pks(self, order_by, where, limit, read_view):
        """ORDER BY 与某个二级索引列前缀一致时的有序主键流；否则 None

        只支持**所有键同向**（全 ASC/全 DESC）且键序列是索引列前缀；
        混合方向/非前缀 → 安全回退（由调用方内存排序）。
        """
        ob = self._normalize_order_keys(order_by)
        if not ob or not self._secondary_usable(read_view):
            return None
        dirs = {d for _c, d in ob}
        if len(dirs) != 1:
            return None
        desc = dirs.pop()
        spec = None
        for cand in (self._secondary_indexes or []):
            cols = [str(c) for c in (cand.get('columns') or [])]
            if len(cols) >= len(ob) and \
                    [c for c, _d in ob] == cols[:len(ob)]:
                if spec is None or len(cols) > len(
                        spec.get('columns') or []):
                    spec = cand
        if spec is None:
            return None
        entry = self._secondary_entry(spec)
        if entry is None:
            return None
        k = len(ob)
        keys = list(self._secondary_sorted_keys(entry, k))
        if desc:
            keys.reverse()
        full_map = entry['maps'][k]
        out = []
        max_n = int(limit) if limit else None
        for key in keys:
            for pk in sorted(full_map.get(key, ())):
                v = self._read_version(pk, read_view)
                if v is None:
                    continue
                if where is not None and not self._match(v[0], where):
                    continue
                out.append(pk)
                if max_n is not None and len(out) >= max_n:
                    self._index_stats['ordered'] += 1
                    return out
        self._index_stats['ordered'] += 1
        return out

    def index_usage_stats(self) -> dict:
        """P5.1 观测：真实索引使用/回退/重建次数（供测试与基准量化）"""
        return dict(self._index_stats, secondary_indexes=len(
            getattr(self, '_secondary_indexes', []) or []))

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

        # P5.1：二级索引等值/范围（安全条件下；结果仍逐行用 _match 复核）
        if self._secondary_usable(read_view):
            plan = self._secondary_plan(where)
            if plan is not None:
                cand = self._secondary_candidate_pks(plan[0], plan[1])
                if cand is not None:
                    self._index_stats['lookups'] += 1
                    result = []
                    for pk in sorted(cand):
                        v = self._read_version(pk, read_view)
                        if v is not None and self._match(v[0], where):
                            result.append(pk)
                    return result
            self._index_stats['fallbacks'] += 1
        else:
            self._index_stats['fallbacks'] += 1

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
        has_agg = any(
            re.match(r'\w+\s*\(', c.strip())
            for c in columns if c != '*')
        ob = self._normalize_order_keys(order_by)
        index_ordered = False
        with self._lock:
            ordered_pks = None
            if ob and not has_agg:
                # P5.1：ORDER BY 与二级索引列前缀一致 → 按索引键序取行，
                # 可带 LIMIT 提前停止；有活跃事务/不支持形态时回退。
                ordered_pks = self._index_ordered_pks(
                    ob, where, limit, read_view)
            if ordered_pks is not None:
                index_ordered = True
                rows = []
                for pk in ordered_pks:
                    v = self._read_version(pk, read_view)
                    if v is not None:
                        rows.append(v[0])
            else:
                pks = self._find_pks(where, read_view)
                rows = []
                for pk in pks:
                    # _find_pks 已经按可见性挑选过，这里只需取回那一版的行值
                    v = self._read_version(pk, read_view)
                    if v is not None:
                        rows.append(v[0])

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

        if ob and not index_ordered:
            # 多键排序：从最后一个键往前做**稳定排序**（与统一路径的
            # NULL 语义一致：ASC 时 NULL 最后、DESC 时 NULL 最前）。
            for col, desc in reversed(ob):
                try:
                    i = out_cols.index(col)
                except ValueError:
                    continue
                out_rows.sort(
                    key=lambda r: (r[i] is None, r[i]),
                    reverse=bool(desc))

        if limit:
            out_rows = out_rows[:limit]

        return out_cols, out_rows

    def _aggregate(self, rows, columns):
        result = []
        for col in columns:
            col = col.strip()
            if re.match(r'\w+\s*\(\s*DISTINCT\b', col, re.IGNORECASE):
                # 存储层聚合只认 `FN(单列)` / `FN(*)`。DISTINCT 的去重
                # 语义在 SQL 执行器的聚合表达式求值器里（parser.
                # eval_agg_expr_over_rows，两条执行路径共用）。走到这里
                # 说明调用方绕过了执行器：显式报错，绝不静默忽略 DISTINCT
                # （那会把 COUNT(DISTINCT x) 算成 COUNT(x)）。
                raise ValueError(
                    f"存储层聚合不支持 DISTINCT（{col}）："
                    f"请通过 SQL 执行器路径执行，DISTINCT 的去重语义"
                    f"不能退化成普通聚合")
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
        self._secondary_dirty = True
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
            self._secondary_dirty = True
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
        #
        # ★ 但墓碑本身可能已经**不存在了**: 这条 DELETE 提交之后又跑过
        #   `vacuum`（页级回收），死版本的行数据已经被删掉（槽位清零、
        #   页可能已被搬空/截断）。此时再给这个位置插入索引项，就是
        #   "索引指向空槽位" —— 该主键永久读不到，完整性扫描器会报
        #   `('index', pk, loc, 'slot-empty')`。所以先确认槽位上还有行。
        page_now = self.bp.get_page(old_loc[0])
        if page_now is None or page_now.get_row(old_loc[1]) is None:
            return True                    # 墓碑已随 vacuum 消失：无需重建索引项
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

    def _has_live_txn(self) -> bool:
        """
        本表当前是否存在**真实**（非 autocommit 合成号）的活跃读视图。

        为什么需要它来决定"能不能搬页": 搬页会改变行的 (page, slot) 物理位置，
        而
          * 显式事务的**物理 undo 镜像**记的是"原页原槽位"，搬完就回滚不回去；
          * 活跃读视图可能还依赖页上的老版本位置。
        vacuum 的死版本判定已经用 `_vacuum_bounds()` 保护了前者，但"给活行
        换位置"这件事本身对**任何**未结束的真实事务都不安全，所以搬运与截断
        只在没有真实活跃事务时才做（autocommit 的合成号每个语句结束就释放，
        不算）。
        ★ 阶段 2.3 第①步补充: 判据不止"有没有活跃事务"，还要看
        "页上还有没有活跃事务的 undo 引用"（`_pending_undo_page_ids()`）
        —— 搬页/截断会改写或消灭 (page, slot)，而 undo 镜像记的正是
        原页原槽位。`_reclaim_pages()` 默认走"不搬页"的安全路径，
        受保护的页另行排除。
        """
        for tid in self._read_views:
            if not is_implicit_commit_tid(tid):
                return True
        for tid in self.mvcc.active_txns:
            if not is_implicit_commit_tid(tid):
                return True
        return False

    # ---------- 页级回收的"必须留"集合（阶段 2.3 第①步） ----------
    #
    # 为什么需要它（两处**实测**的数据丢失，都在本轮修掉）:
    #
    # ① `scan_pages` 判断"一个页能不能截断"时，只看**写在页上的**四条引用
    #    （可达树页、叶子兄弟链、prev 指针、索引项指向的槽位）。但索引项
    #    指向的槽位是**版本链头**，`_reachable_locs()` 判断"可达"时只沿
    #    prev **往前**走 —— 一个"槽位上只剩墓碑（xmax != 0）"的页因此两头
    #    不靠，被判成 `truncatable`：
    #      * 它是索引链头（老快照必须沿它读到被删的旧值）；
    #      * 它没有 `xmax == 0` 的活版本（`active_rows == 0`）；
    #      * 墓碑的 prev 是 NO_PREV（没人把它当 prev）。
    #    实测: 200 行**已提交**全删 + vacuum → 页数 4→2，老快照 200 行全部
    #    读不到（版本链头所在的页被截掉了）。
    #
    # ② 活跃事务的**物理 undo 镜像**记着"原页原槽位"（`UndoAction.page_id/
    #    slot_id`）。vacuum 期间若把这些页截掉、或把页上"谁也走不到"的行
    #    清掉（`_reclaim_pages` 的不可达版本清理），ROLLBACK 就写不回去。
    #    实测: `BEGIN; DELETE 100; vacuum(); ROLLBACK` 只恢复 **28/100**。
    #    注意 `BufferPool.truncate()` 会把 `page_id >= n_pages` 的帧从缓存
    #    里**丢弃**，所以被截断的页不是"缓存里还有"，而是真的没了。
    #
    # 两者的修法相同: 把这两类页显式声明为"逻辑上仍活着"，交给
    # `scan_pages(protected_pages=...)` 抬高截断上界，并让 `_reclaim_pages`
    # 的破坏性清理跳过它们。**保守方向**（少截几页）只损失空间收益，
    # 多截一页就是丢数据，所以这里是"宁可留着"。

    def _pending_undo_page_ids(self) -> set:
        """
        活跃事务物理 undo 镜像指向的页号集合（ROLLBACK 要写回这些页）。

        含义: 这些页上的槽位可能在事务提交/回滚前被 `_put_row_back` 覆写，
        因此
          * 不能截断（`BufferPool.truncate` 会丢帧，页就真的没了）；
          * 不能做"不可达行清理"或会**清空槽位数组**的紧凑化
            （`Page.compact` 在一行不剩时把 `n_slots` 归零，
             `Page.restore_row` 就再也挂不上数据）。
        """
        out = set()
        try:
            for (tid, _tbl), stack in list(self.mvcc.physical_undo.items()):
                if is_implicit_commit_tid(tid):
                    continue          # autocommit 合成号: 语句结束即释放
                for act in stack:
                    for pid in (act.page_id, act.new_page_id):
                        if pid:
                            out.add(pid)
        except Exception:
            pass
        return out

    def _index_chain_page_ids(self) -> set:
        """
        **索引版本链上**所有版本所在的页号集合。

        为什么这些页一个都不能截: 每个索引项都指向一条持久化的版本链
        （`_read_version` 沿 `prev_page/prev_slot` 往回走找"对读视图可见
        的最新版本"）。链上任何一个版本所在的页被截掉，那条链就
        断在空洞上 —— 老快照读到的是"行不存在"，而不是"被删的旧值"。
        墓碑（`xmax != 0`）恰恰**必须**留着，这正是①里的坑。

        顺着链走而不是只看索引项本身，是因为链中间的旧版本也可能
        独占某一页（多轮 UPDATE 之后尤其明显）。
        """
        out = set()
        try:
            for _k, pid, sid in self.bt.scan():
                cur = (pid, sid)
                seen = set()
                while cur is not None and cur not in seen:
                    seen.add(cur)
                    cpid, csid = cur
                    if cpid < 1:
                        break
                    out.add(cpid)
                    page = self.bp.get_page(cpid)
                    rb = page.get_row(csid) if page is not None else None
                    if rb is None or len(rb) < 21:
                        break
                    up, us = struct.unpack_from('<II', rb, 13)
                    cur = (up, us) if up >= 1 else None
        except Exception:
            pass
        return out

    def _reclaim_protected_pages(self, extra=None) -> set:
        """
        页级回收的"必须留"集合 = 索引版本链上的页 ∪ 活跃 undo 指向的页
        （∪ 调用方额外声明的页）。

        这是**截断上界的唯一来源**（除了 `scan_pages` 自己算的引用关系）。
        `extra` 供"只做某个局部操作"的调用方追加（例如搬页过程中已改写
        但尚未落盘的位置）。
        """
        out = set()
        try:
            out |= self._index_chain_page_ids()
        except Exception:
            pass
        try:
            out |= self._pending_undo_page_ids()
        except Exception:
            pass
        if extra:
            out |= set(extra)
        return out

    def vacuum_undo_protected_pages(self) -> list:
        """诊断/测试用: 当前"因活跃 undo 而必须留"的页号（升序）"""
        return sorted(self._pending_undo_page_ids())

    def _min_active_view_max_id(self):
        """
        所有活跃读视图 `max_id` 的最小值；**没有任何活跃读视图时返回
        `float('inf')`**（代表"这个上界不存在"，`xmax >= inf` 恒假，
        因此判定里的 `xmax >= self._view_floor → continue` 不会触发）。

        与 `_vacuum_bounds()` 的 `uncommitted_floor` 的区别（关键，踩过坑）:
          * `uncommitted_floor` 还含"最小活跃事务号"，负责挡住**显式事务
            自己刚写的墓碑**（否则 `BEGIN; DELETE; vacuum(); ROLLBACK`
            只恢复 28/100）；
          * 本方法只管"**存不存在**一个活跃读视图还看得见
            `max_id <= xmax` 的版本"。它必须对 **autocommit 的合成
            事务号**同样生效 —— 合成号在数值上远大于任何真实事务号，
            不能拿"最小活跃事务号"或 `next_txn_id` 去卡它
            （实测: 卡了就一个 autocommit 墓碑都回收不掉，
            `.dat` 一个字节都不缩）。

        为什么不用 0 当"不存在"的哨兵: 合成事务号可以取到 0 附近吗 ——
        不能（都 >= 0xF0000000），但 `xmax` 本身可能为 0 吗 ——
        不会（0 表示"未淘汰"，上面已经 `continue`）。即便如此，
        `0` 当哨兵会让 `xmax >= 0` 恒真、判定全部失效，是极易写错的
        陷阱，所以用 `inf` 表达"无上界"。
        """
        best = float('inf')
        try:
            for rv in list(self.mvcc.active_txns.values()):
                mid = getattr(rv, 'max_id', None)
                if isinstance(mid, int) and mid < best:
                    best = mid
        except Exception:
            pass
        for rv in list(self._read_views.values()):
            mid = getattr(rv, 'max_id', None)
            if isinstance(mid, int) and mid < best:
                best = mid
        return best

    def vacuum_horizon(self) -> int:
        """调用方可见的 horizon（= `_vacuum_bounds()[0]`）"""
        return self._vacuum_bounds()[0]

    def _vacuum_bounds(self):
        """
        vacuum 的两个上界 `(horizon, uncommitted_floor)`。

        `horizon`: 没有活跃事务时就是下一个待分配号（等价于"已提交的
            淘汰号全部可回收"，即阶段 2.1 的旧行为）；有活跃事务时取
            最小活跃号 —— 与 `mvcc.oldest_active_txn()` 完全一致，
            保证**显式传入的旧 horizon 语义不变**。

        `uncommitted_floor`: "最小活跃事务号"与"所有已登记读视图的 max_id"
            的下界，**不受调用方影响**，且**没有任何活跃事务时返回
            `float('inf')`**（代表"这个上界不存在"：`xmax >= inf` 恒假，
            所以判定里 `xmax >= uncommitted_floor → continue` 不会触发）。
            ★ 哨兵必须满足"`任何一个 uint32 事务号 >= 哨兵` 为假" ——
            `-1` 是**错的**（`xmax >= -1` 恒真，判定全部失效，实测
            autocommit 的墓碑一个都回收不掉）；`inf` 才对。
            判定里要求 `xmax` 严格小于它（见 `vacuum` 的 docstring）。
            三个来源一个都不能漏:
              1) `mvcc.oldest_active_txn()` —— 该事务可能还没提交它写的
                 DELETE/UPDATE，行头上的 `xmax` 就是它自己的号；把它删了
                 它的 ROLLBACK 就无处可回（物理 undo 要写回原页原槽位）。
                 ★ 这是**显式事务**的保护（实测:
                 `BEGIN; DELETE 100; vacuum(); ROLLBACK` 会只恢复 28/100），
                 上面那个"活跃读视图"上界挡不住它，因为**发起删除的那个
                 事务自己的读视图**同样看得见自己写的墓碑 —— 必须靠
                 "最小活跃事务号"这一条。
              2) `mvcc.active_txns[*].max_id` —— 每个活跃读视图的可见性
                 边界；`ReadView.is_visible` 里 `xmax >= max_id → 可见`。
              3) 本表 `_read_views` 里登记的快照（显式事务 UPDATE/DELETE
                 路径会注册进来）。它与 2) 常常是同一个对象，但调用方
                 直接传 read_view 时表里可能比 mvcc 多，所以两边都算。

        ★ 为什么没有活跃事务时不能用 `oldest_active_txn()`（踩过的坑）:
        它返回一个小整数（下一个待分配号），而 autocommit 的**合成
        事务号**（`0xFFFFFFFF` 往下）在数值上远大于它 —— 拿它当上界会让
        **所有 autocommit 的墓碑一个也回收不掉**（实测: 20000 行删
        18000 行后 vacuum 回收 0 个版本、`.dat` 一个字节都不缩）。
        """
        try:
            horizon = self.mvcc.oldest_active_txn()
        except Exception:
            horizon = 1
        has_active = bool(self.mvcc.active_txns) or bool(self._read_views)
        safe = float('inf')
        if has_active:
            safe = horizon
            try:
                for rv in list(self.mvcc.active_txns.values()):
                    mid = getattr(rv, 'max_id', None)
                    if isinstance(mid, int) and mid < safe:
                        safe = mid
            except Exception:
                pass
            for rv in list(self._read_views.values()):
                mid = getattr(rv, 'max_id', None)
                if isinstance(mid, int) and mid < safe:
                    safe = mid
        return horizon, safe

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

        ★★ 阶段 2 第 3 项（本批次）把"数据文件本身不会变小"这条**推翻**了:
        紧凑化之后再做**页级回收** —— 尾部那些"无活跃行、无 prev 引用、
        不是可达树页、不在树链上"的页被真正截掉，`.dat` 字节数下降
        （见 `trim_tail()` / `_reclaim_pages()`）；剩下的空闲页登记进
        **持久化**的 `<name>.free`（`FreePageRegistry`），重启后仍然复用。
        统计见 `vacuum_stats()`。

        ★★★ 快照安全（本批次修正的**两处真实数据丢失**）: 显式事务的
        DELETE/UPDATE 会把行头 `xmax` 写成**真实事务号**，而
        `mvcc.oldest_active_txn()` 取的是最小活跃号。若某活跃事务 T
        已经删了一批行、此刻又调用 vacuum，旧判定 `xmax <= horizon`
        会把这些行判成"可回收"（T 就是 horizon 本身），把它们物理删掉 ——
        T 随后 ROLLBACK 时，物理 undo 要还原的槽位已经不存在，回滚
        静默失败 = 数据丢失（实测 `BEGIN; DELETE 100; vacuum();
        ROLLBACK` 只恢复 **28/100**）。
        另一处是**老快照**：旧判定把 autocommit 的合成事务号整类放行
        （"已提交的删除一定可以回收"），但 `max_id <= xmax` 的老快照
        仍然看得见被删的行，回收 + 页级截断会让它读到"行不存在"
        （实测插 200 行 → 老快照 → autocommit 全删 → vacuum →
        老快照读到 **0/200**）。

        因此判定拆成**两个独立上界**（都由本表自己的方法给出，**都不能
        被调用方覆盖**）:
          * `horizon`（调用方可见/可传的语义，与阶段 2.1 一致）:
            非合成号 `xmax` 还必须满足 `xmax <= horizon`；
          * `uncommitted_floor`（`_vacuum_bounds()`）:
            `min(最小活跃事务号, 所有活跃读视图 max_id)`，
            **没有任何活跃事务时为 -1**（该条自动失效）。
            版本 V 必须满足 `xmax < uncommitted_floor`。它同时挡住:
              - 未提交事务自己写的淘汰号（`xmax == 活跃号`），
                其物理 undo 镜像要还原的槽位因此一定还在；
              - 老快照还看得见的墓碑（`xmax >= RV.max_id`）。
          * `_view_floor`（`_min_active_view_max_id()`）: 只管
            "活跃读视图还看得见吗"，**对合成号同样生效**
            （`uncommitted_floor` 那条因为 -1 哨兵而失效时，这一条仍然
            挡住合成号墓碑被老快照需要的情况）。

        返回回收的版本数。不会删除任何仍可能被读到的版本。
        """
        with self._lock:
            caller_horizon = horizon
            horizon, uncommitted_floor = self._vacuum_bounds()
            if caller_horizon is not None:
                horizon = caller_horizon
            self._recovery_remap = {}
            freed_bytes = 0
            # ★ 活跃读视图的可见性上界（inf = 没有活跃读视图，该上界不存在）。
            #   在收集版本**之前**取一次快照值: 判定与"哪些页被保护"
            #   必须用同一个界，否则会出现"版本没回收、页却被截断"。
            self._view_floor = self._min_active_view_max_id()

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
                    if xmax == 0:
                        continue
                    # ★ 回收判据（阶段 2.3 第①步收紧；两条必须同时成立）:
                    #
                    #   (1) `xmax >= uncommitted_floor` → 不回收。
                    #       `uncommitted_floor` = min(最小活跃事务号,
                    #       所有活跃读视图 max_id)，没有活跃事务时为 -1
                    #       （该条自动失效）。这一条保护两类版本:
                    #         a) **显式事务自己刚写的墓碑**（xmax == 它自己
                    #            的号 == 最小活跃号）—— 回收它，该事务
                    #            ROLLBACK 时物理 undo 要还原的槽位就没了。
                    #            实测: `BEGIN; DELETE 100; vacuum();
                    #            ROLLBACK` 只恢复 **28/100**。
                    #         b) **老快照还看得见的版本**（xmax >= 某个活跃
                    #            读视图的 max_id）。`ReadView.is_visible`
                    #            里 "xmax >= max_id → 可见"，回收 = 让老快照
                    #            从"读到旧值"变成"行不存在"。
                    #       ★ b) **同样适用于 autocommit 的合成事务号**。
                    #         曾经把合成号整类放行（"已提交的删除一定可以
                    #         回收"），那是错的: autocommit 的 DELETE 也是
                    #         已提交删除，但 `max_id <= xmax` 的老快照仍然
                    #         看得见被删的行。实测: 插 200 行 → 老快照 →
                    #         autocommit 全删 → vacuum → 老快照读到 0/200
                    #         （墓碑所在的页还被整页截掉了）。
                    #
                    #   (2) 非合成号还要过 `horizon`（调用方可传，语义与
                    #       阶段 2.1 完全一致）；合成号不过 horizon ——
                    #       它就是"已经提交"的代名词，而 horizon 是真实
                    #       事务号空间里的界，两者不可比（合成号恒大于
                    #       任何真实号，卡 horizon 会让 autocommit 的墓碑
                    #       一个也回收不掉）。
                    if xmax >= uncommitted_floor:
                        continue
                    if xmax >= self._view_floor:
                        continue
                    if is_implicit_commit_tid(xmax):
                        dead.add((pid, sid))
                        continue
                    if xmax > horizon:
                        continue
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
            self.last_vacuum_freed_bytes = freed_bytes

            # 6) ★ 页级回收（阶段 2 第 3 项）: 把"什么都没有了"的尾部页
            #    真正从 `.dat` 里截掉，并把剩余的空闲页登记持久化。
            stats = self._reclaim_pages(freed_bytes, removed)
            self._last_vacuum_pages = stats
            if freed_bytes or stats['trimmed_pages']:
                logger.info(
                    f"vacuum {self.name}: 回收 {removed} 个版本，"
                    f"页内释放 {freed_bytes} 字节，"
                    f"截断 {stats['trimmed_pages']} 页 "
                    f"({stats['bytes_before']} → {stats['bytes_after']} 字节)，"
                    f"空闲页 {stats['free_pages_registered']}")
            return removed

    # ==================== 页级回收（截断 + 空闲页持久化）====================

    def trim_tail(self, want_bytes: int = 0):
        """
        把 `.dat` 尾部**完全空置**的页截掉，文件真的变小。

        安全判据（`storage/pgscan.py::scan_pages` 一次扫清，四条引用全查）:
        一个页可以被截断 ⟺
          1) 它不是从根可达的 B+Tree 页（内部节点 or 叶子）；
          2) 不在叶子的兄弟链 `next` 上（否则 `scan()` 断链，静默丢行）；
          3) 不是**活跃行**所在的堆页（`xmax == 0` 的版本，或索引指向的墓碑）；
          4) 没有任何版本的 `prev_page` 指向它（版本链是持久化的，断链后
             老快照/回溯查询就读不到更早的版本）；
          5) ★ 阶段 2.3 第①步补充 —— 不在 `_reclaim_protected_pages()`
             里（**索引版本链上的页** ∪ **活跃事务 undo 镜像指向的页**）。
             前四条只看"写在页上的引用"，看不到这两种**逻辑上仍活着**
             的页，实测漏掉它们会丢数据（见 `_reclaim_protected_pages`
             的说明）。

        只有"截断点上界之后的**连续尾部**"会被截断 —— 不做页搬迁。
        代价是: 中间那些空页留在文件里（记进空闲页登记表供复用），
        换来的确定性是**没有任何 (page, slot) 引用会被改写**：
        索引项、`prev_page`、兄弟链、根指针全都不用动。
        页搬迁会让每条相关的引用都必须同步更新，那是另一个量级的
        风险面，本轮不做（见交付说明的"仍未回收的页"）。

        `want_bytes` 只用于统计（本次希望回收多少字节）；返回值是
        截掉的页数。
        """
        with self._lock:
            if self.bp.total_pages() <= 1:
                return 0
            born = self.bp.file_size()
            # 先落盘: 被截断的页必须是内容已经在别处的页
            self.bp.flush()
            live_btree = self.bt.live_pages()
            if not live_btree:
                # 根不可读（异常状态）时**不敢截断**
                return 0
            protected = self._reclaim_protected_pages()
            res = scan_pages(self.bp, self.bt, self.freelist.pages,
                             live_btree=live_btree,
                             protected_pages=protected)
            new_len = res.max_live_page() + 1
            cur = self.bp.total_pages()
            if new_len >= cur:
                return 0
            trimmed = self.bp.truncate(new_len)
            if not trimmed:
                return 0
            # 截断后: 越界的登记项丢掉（页号已经不在了）
            self.freelist.keep_within(new_len)
            if self._last_heap_pid is not None and \
                    self._last_heap_pid >= new_len:
                self._last_heap_pid = None
            self.freelist.save()
            self._last_trim = {
                'pages_before': cur, 'pages_after': new_len,
                'trimmed_pages': trimmed,
                'bytes_before': cur * 4096, 'bytes_after': new_len * 4096,
                'want_bytes': want_bytes, 'scan': res.summary(),
                'protected_pages': sorted(protected),
            }
            return trimmed

    def _reclaim_pages(self, freed_bytes: int, removed: int) -> dict:
        """
        vacuum 的收尾: **前向搬页** → 登记空闲页 → 截断尾部 → 登记表落盘。

        为什么需要"搬页"（本批次的核心）:
          只截尾部的话，一次 90% 删除几乎回收不到任何字节 —— 实测
          `dat=1257472 → 1257472`（截断 0 页），因为剩下 10% 的行
          **均匀散落在 187 个堆页上**，而尾部每一页都还有一两个活行，
          没有连续空置的尾部。要真的把文件缩小，必须把高页号的活行
          搬到低页号的空洞里，让尾部真正空出来。

        顺序（每一步单独失败都不丢数据）:
          1) 扫描"可达行"集合（索引项 + 沿 prev 链能走到的版本）；
          2) `_relocate_forward()`: 把高页号可达行搬到低页号空页，
             同步改写索引项与所有 prev 指针，再紧凑化源页；
          3) `trim_tail()`: 把尾部完全空置的页截掉（它内部先 fsync 页）；
          4) 登记剩余空闲页 → `freelist.save()`（重启后仍可复用）。
        """
        before = self.bp.file_size()
        before_pages = self.bp.total_pages()
        stats = {
            'reclaimed_versions': removed,
            'compacted_bytes': freed_bytes,
        }

        # ★ 本步的关键安全集合（见 `_reclaim_protected_pages` 的说明）:
        #   索引版本链上的页 + 活跃事务 undo 镜像指向的页。两者一律
        #   "不清理、不紧凑化到清空、不截断"。方向是**保守**：
        #   多留一页只损失空间，少留一页就是丢数据。
        protected = self._reclaim_protected_pages()
        reach = None

        if ENABLE_PAGE_RELOCATION:
            # ★★ 前向搬页（`_relocate_forward`）—— 默认关闭。
            #
            # 它是"非尾部空页"能被回收的唯一途径: 实测"20000 行删 90%"之后
            # 187 个堆页里**每页都还剩几个活行**（删除按行散布，不按页），
            # 尾部没有任何连续空置的页，纯截尾部回收 0 字节。把活行前向密排
            # 之后尾部才可能整段截掉，因此这里默认开启；它自带两道安全网:
            #   1) 计划期容量模型保守（软容量 + 物理容量取小），执行前还要过
            #      一次 `_prune_plan` 可行性检查；
            #   2) 执行后做**实测投影**（"搬完后的堆页上界"是否真的下降），
            #      没有收益就整份回滚，绝不出现"越回收越大"。
            #
            # ★ 阶段 2.3 第①步: 这一段（不可达版本清理 + 紧凑化 + 搬页）
            #   全部收在 `ENABLE_PAGE_RELOCATION` 之后。理由有两条，都是
            #   实测出来的数据丢失路径:
            #     a) 搬页会**改写 (page, slot)**，而活跃事务的物理 undo
            #        镜像记的正是原页原槽位 —— 搬过就回滚不回去；
            #        `_has_live_txn()` 只挡住"有活跃事务"这一半，另一半是
            #        "页上还有活跃事务的 undo 引用"；
            #     b) "不可达版本清理"用的是 `_reachable_locs()`（只沿 prev
            #        往前走的可达性），它会把**索引链头上的墓碑**也判成
            #        不可达 —— 而墓碑正是老快照要读的旧值。
            #   默认关闭时这两个动作都不执行，`compacted_bytes` 仍然照常
            #   统计"版本回收 + 槽位置零"释放的字节（这是确定性收益），
            #   但**不**做任何位置改写。
            #
            #   有真实活跃事务时（物理 undo 记的是原页原槽位）也一律跳过。
            reach = self._reachable_locs()
            #   全部堆页做一次"不可达版本清理 + 紧凑化"，`_heap_floor` 才算得准
            #   （否则 floor 停在"其实早该空掉"的页上，尾部一页都截不掉 ——
            #    本批次实测踩到过: heap_floor 停在 307 而不是 187）。
            #   ★ 被保护的页（索引链 / 活跃 undo）必须整个跳过: 清掉它们上面的
            #     "不可达"行会直接砸掉墓碑与 undo 目标。
            for pid in self._heap_pages():
                if pid in protected:
                    continue
                page = self.bp.get_page(pid)
                if page is None:
                    continue
                for sid, _rb in list(page.iter_rows()):
                    if (pid, sid) not in reach:
                        page.delete_row(sid)
                page.compact()
            self.bp.flush()

            if not self._has_live_txn():
                before_pages, moved, heap_floor, rollback = \
                    self._relocate_forward(reach)
            else:
                before_pages, moved, rollback = self.bp.total_pages(), 0, None
                heap_floor = self._heap_floor(self._heap_pages())
        else:
            # 默认路径（本步交付状态）: 只做"版本回收 + 槽位置零"带来的
            # 确定性收益，不清理、不搬页、不紧凑化到清空。`_heap_floor`
            # 仍要算（供 `pages_before` / 统计口径一致），它只读不写。
            moved, rollback = 0, None
            heap_floor = self._heap_floor(self._heap_pages())

        stats['relocated_rows'] = moved
        stats['pages_before'] = before_pages
        stats['heap_floor'] = heap_floor
        stats['protected_pages'] = sorted(protected)

        # ★ 索引页也要回收: 堆页搬完之后重建 B+Tree（密集打包），并让新树
        #   落在**被丢弃的旧索引页号**上（先把文件截到 heap_floor、
        #   再在 floor 之上的连续空页段里分配），这样新树不占新空间、
        #   尾部随即可以整段截掉。
        #
        #   阶段 2.3 第①步: 本步交付时 `ENABLE_BTREE_REBUILD=False`，
        #   实测它在"新树更小却落在更高页号"这条路径上会截掉新树自己
        #   的页 → 重启后整表读不到（dump 2000 行 → 0 行）。因此这里
        #   永远走 `skipped` 分支，不重建、不动根指针。
        if not ENABLE_BTREE_REBUILD or self._has_live_txn():
            stats['btree_rebuild'] = {'skipped': True,
                                      'enabled': ENABLE_BTREE_REBUILD}
            self.freelist.save()
        else:
            try:
                free_after = self._free_page_candidates(heap_floor)
                rb = self._rebuild_btree(floor_pages=heap_floor,
                                         free_pages=free_after)
            except Exception as e:
                rb = {'error': str(e)}
                logger.warn(f"重建索引失败 {self.name}: {e}")
            stats['btree_rebuild'] = rb

        # ★★ 收益门槛（**实测**，不是估算）: 搬页 + 重建有可能让文件反而
        #    变大。此时把搬页整体回滚，回到"一行没动"的状态 ——
        #    宁可不缩，绝不越回收越大。（`ENABLE_PAGE_RELOCATION=False`
        #    时 `moved == 0`，这一段不会触发。）
        if moved and rollback is not None and \
                self.bp.file_size() > before:
            self._rollback_relocate(rollback)
            stats['rollback'] = {
                'reason': 'file_grew',
                'bytes_before': before,
                'bytes_after_relocate': self.bp.file_size(),
            }
            stats['relocated_rows'] = 0
            stats['heap_floor'] = None
            self.bp.flush()

        trimmed = self.trim_tail(want_bytes=freed_bytes)
        after_pages = self.bp.total_pages()

        registered = 0
        reusable_bytes = 0
        for pid in range(1, after_pages):
            page = self.bp.get_page(pid)
            if page is None or page.page_type != PAGE_TYPE_HEAP:
                continue
            fs = page.free_space()
            if fs < MIN_REUSABLE_PAGE_BYTES:
                continue
            if self.freelist.add(pid):
                registered += 1
            reusable_bytes += fs
        # 登记表变更落盘（内容没变时不产生写盘）
        self.freelist.save()
        after_pages = self.bp.total_pages()
        after = self.bp.file_size()
        stats.update({
            'trimmed_pages': trimmed,
            'pages_after': after_pages,
            'bytes_before': before,
            'bytes_after': after,
            'bytes_freed': before - after,
            'free_pages_registered': len(self.freelist.pages),
            'free_pages_usable_bytes': reusable_bytes,
            'newly_registered_pages': registered,
        })
        return stats

    def _page_move_capacity(self, pid, out_sids, page_total=None):
        """
        一个堆页在"搬页"中的**可用空间**（字节）—— 只用当前可观察的
        数值算出来，不做乐观假设：

            可用 = free_space()
                 + Σ(搬出行字节 + 4)          ← 这些槽位会空出来
                 + 4 × n_slots（仅当本页全部搬空）

        为什么"全部搬空"才加 4×n_slots: `Page.compact()` 在一行不剩时会把
        `n_slots` 归零、行游标复位（页回到全新状态）；只搬走一部分时槽位
        数组**不缩**，那 4N 字节不能算可用。
        """
        page = self.bp.get_page(pid)
        if page is None or page.page_type != PAGE_TYPE_HEAP:
            return 0
        cap = page.free_space() + 4 * len(out_sids)
        out_bytes = 0
        for sid in out_sids:
            rb = page.get_row(sid)
            if rb is not None:
                out_bytes += len(rb)
        cap += out_bytes
        if page_total is not None and page_total and \
                out_bytes >= page_total:
            cap += 4 * page.n_slots
        return cap

    def _prune_plan(self, plan, src_moves, dst_rows):
        """
        计划可行性检查（**只读，不改页**），返回"还可以当目标页"的集合。

        为什么必须有这一步: 搬页的执行顺序是"先从源页删掉搬走的槽位、
        紧凑化，再往目标页 insert"。如果目标页其实装不下（容量模型与
        真实的槽位数组/页内空洞有偏差），`insert_row` 会返回 None ——
        那一行的源槽位**已经被删掉了**，索引却还指着它 → 该行静默消失，
        完整性扫描器会报 `('index', pk, loc, 'slot-empty')`。
        本批次第一次实现时就踩到了这个坑（实测 3000 行只剩少数几行可读）。

        判据（保守、逐页独立）: 目标页的搬入总量必须 <= 该页的
        `_page_move_capacity`。不满足的页被剔除，指向它的搬出计划一并
        作废（那些行留在原处）。保守到可能少搬几页，但绝不会出现
        "删了源、放不进目标"。
        """
        need = {}
        for pid, rows in dst_rows.items():
            need[pid] = sum(len(rb) + 4 for _sid, rb in rows)
        feasible = set()
        for pid, want in need.items():
            if want <= self._page_move_capacity(
                    pid, src_moves.get(pid, ()), self._page_total(pid)):
                feasible.add(pid)
        return feasible

    def _page_total(self, pid) -> int:
        """该堆页上**全部行**的字节总量（判断"能否被清成全新页"用）"""
        page = self.bp.get_page(pid)
        if page is None or page.page_type != PAGE_TYPE_HEAP:
            return 0
        return sum(len(rb) for _sid, rb in page.iter_rows())

    # ---------- 可达行集合（搬页的"必须保留"判据） ----------

    def _reachable_locs(self) -> set:
        """
        返回"必须保留"的 (page, slot) 集合 = 索引项 ∪ 沿 prev 链可达的版本。

        为什么不用"xmax == 0"当判据: 墓碑（xmax != 0）也可能必须留 ——
        索引指向的链头如果是墓碑（DELETE 后索引项保留），老快照要沿它
        往回走；被别的版本当 prev 引用的老版本同理。反过来，页上有些
        版本**谁也走不到**（多轮 UPDATE 之后被"跳"过去的中间版本），
        它们是真正的垃圾，搬页时可以直接丢。
        """
        reach = set()
        stack = []
        for _k, pid, sid in self.bt.scan():
            loc = (pid, sid)
            if loc not in reach:
                reach.add(loc)
                stack.append(loc)
        while stack:
            loc = stack.pop()
            page = self.bp.get_page(loc[0])
            rb = page.get_row(loc[1]) if page is not None else None
            if rb is None:
                continue
            pp, ps = struct.unpack_from('<II', rb, 13)
            if pp:
                nxt = (pp, ps)
                if nxt not in reach:
                    reach.add(nxt)
                    stack.append(nxt)
        return reach

    # ---------- 前向搬页 ----------

    def _relocate_forward(self, reach=None):
        """
        把可达行**前向密排**到页号最低的一批堆页里，使文件尾部真正空出来。

        为什么必须搬（实测）: 一次"插 5000 行 → 按 `i % 10 != 0` 删 90%"
        之后，45 个堆页里**每一页都还剩 11~12 个活行**（删除按行散布，
        不是按页），尾部没有任何连续空置的页 —— 纯截尾部回收 0 字节
        （实测 `dat=311296 → 311296`）。只有把活行往前搬，尾部才可能
        整段截掉。

        只搬**可达行**（`_reachable_locs`）:
          * 索引项指着的链头；
          * 沿 prev 链能走到的版本（含墓碑）。
        页上"谁也走不到"的中间版本是真垃圾，留在源页里随页一起截掉。

        正确性要点（每一条都对应一种数据丢失方式）:
          1) 先把**要从本页搬走**的行槽位置零、再 `compact()`，然后才把
             搬入行 `insert_row()` —— 顺序反了会出现"目标页已经满了、
             源行却还没搬出去"；
          2) 索引项改写: 搬动过的 (page,slot) 必须改指新位置，
             否则索引指向已删槽位 → 该行彻底读不到；
          3) prev 指针改写: 任何"指向搬动过位置"的 prev 必须改指新位置
             （在所有搬动完成后统一改写），否则版本链断在空洞上；
          4) 搬迁计划在**动手之前**一次算完（贪心装箱），保证每个目标页
             的容量是够的（估算偏乐观时下面的 `insert_row` 会返回 None，
             那一行就**不搬**、留在原处 —— 少搬不丢数据）。

        返回 (搬前总页数, 搬动的行数, heap_floor)；`heap_floor` =
        "搬完之后还有活行的最高堆页 + 1"，是本表堆页的**真实上界**，
        之下的索引页都可以安全丢弃（索引项与版本链已全部前向改写）。
        """
        n_pages = self.bp.total_pages()
        if n_pages <= 2:
            return n_pages, 0, 2, None
        self.bp.flush()
        if reach is None:
            reach = self._reachable_locs()
        if not reach:
            return n_pages, 0, 2, None

        heap_pages = []
        for pid in range(1, n_pages):
            page = self.bp.get_page(pid)
            if page is not None and page.page_type == PAGE_TYPE_HEAP:
                heap_pages.append(pid)
        if not heap_pages:
            return n_pages, 0, 2, None

        # ---- 1) 收集可达行（按 (page,slot) 升序 = 前向密排的自然顺序） ----
        #    ★ `reach` 之外的版本（页上谁也走不到的老版本 / 已经被上一个
        #      阶段清零的槽位）**不算行**，直接跳过 —— 否则计划里会混进
        #      不存在的行，执行阶段删了源槽位却 insert 不出东西，整份计划
        #      退化成空（本批次实测踩到: `plan=0` 而 `rows=20000`）。
        rows = []              # [(rb, old_loc)]
        costs = []             # 每页的 (页头 + 槽位 + 行数据) 成本
        for pid in heap_pages:
            page = self.bp.get_page(pid)
            live = [(sid, rb) for sid, rb in page.iter_rows()
                    if (pid, sid) in reach]
            if not live:
                continue
            for sid, rb in live:
                rows.append((rb, (pid, sid)))
            costs.append(PAGE_HEADER_SIZE + 4 * len(live)
                         + sum(len(rb) for _s, rb in live))
        if not rows:
            return n_pages, 0, 2, None

        # ---- 2) 贪心装箱: 理想最少页数（只作诊断，不参与正确性判断） ----
        target_pages = 1
        if costs:
            bins = []
            for cost in sorted(costs, reverse=True):
                for i, cap in enumerate(bins):
                    if cap + cost <= PAGE_SIZE:
                        bins[i] += cost
                        break
                else:
                    bins.append(cost)
            target_pages = 1 + len(bins)
        target_pages = max(1, min(target_pages, n_pages))
        self._target_pages = target_pages

        # ---- 3) 先把搬迁计划一次算完（**动手之前**），保证可执行 ----
        #    目标页只**往后**走（前向密排）: 一个源页的行全部处理完才允许
        #    进入下一页 —— 这样"计划槽位序号"与执行阶段 `insert_row` 的
        #    真实槽位号一一对应（执行是按 heap_pages 顺序、每页按序号插入）。
        #
        #    容量模型（保守、只用当前可观察的数值）:
        #        可用 = free_space()
        #             + Σ(已规划搬走的行字节 + 4)     ← 这些槽位会空出来
        #             + 4 × (本页全部搬空 ? 本页槽位数 : 0) ← compact() 复位
        #    `Page.compact()` 只在"一行不剩"时把 n_slots 归零、行游标复位；
        #    只搬走一部分时槽位数组不缩，那 4N 字节**不能算可用**
        #    （本批次实测踩到的坑: 把 4N 算进去 → 容量虚高 → 计划看似可行
        #     但执行时放不下，源槽位已被删 → 行静默消失）。
        #
        #    再给每页留出"可复用空闲"的余量: 目标页数按理想最小值放大
        #    `1/(1-RELOCATE_SLACK_RATIO)` 倍，并把总字节平均摊到这些页上，
        #    得到每页的**软容量**。这样每页都只装到软容量为止，
        #    剩下的空间就是真正能被后续 INSERT 复用的空闲空间。
        order = {p: i for i, p in enumerate(heap_pages)}
        per_pid = {p: [] for p in heap_pages}
        for rb, old_loc in rows:
            per_pid[old_loc[0]].append((old_loc[1], rb))
        page_total = {p: sum(len(rb) for _s, rb in v)
                      for p, v in per_pid.items()}
        out_bytes = {p: 0 for p in heap_pages}   # 已规划**搬走**的行字节
        out_cnt = {p: 0 for p in heap_pages}     # 已规划**搬走**的槽位数
        in_bytes = {p: 0 for p in heap_pages}    # 已规划**搬入**的行字节
        in_cnt = {p: 0 for p in heap_pages}      # 已规划**搬入**的槽位数

        total_live = sum(len(rb) + 4 for rb, _loc in rows)
        # 每页的**软容量**: 每页只装 `PAGE_SIZE * (1 - slack)` 的搬入量，
        # 剩下的空间就是搬完之后能被后续 INSERT 直接复用的空闲。
        # 计划期就用它当上限（保守），执行时一定放得下。
        soft_cap = int(PAGE_SIZE * (1.0 - RELOCATE_SLACK_RATIO))
        target_pages = max(1, (total_live + max(soft_cap, 1) - 1)
                           // max(soft_cap, 1))

        def avail_of(i):
            """第 i 个堆页当前可用容量（物理容量与软容量取小）；-1 = 不可用

            两个约束都要满足:
              * **物理**: `free_space() + 本页已规划搬走的行字节 + 4×槽位`
                —— 执行阶段是"先把所有源页的行挖空并 compact，再往目标页
                insert"，所以源页的原始活行不会挡住目标页；
              * **软容量**: 每页只装到 `soft_cap` 附近，剩下的空间才是
                "可复用空闲页"那部分收益。

            ★ `page_total` / `out_bytes` 等字典**必须用 `.get(pid)`**:
            它们只为"页上有可达行的页"建了键，而 `heap_pages` 里还有
            页号存在但一行都没有的空页。用下标取值会抛 KeyError，
            异常被 `Database.vacuum()` 的 try/except 吞掉 —— 表现为
            "vacuum 静默什么都不做、文件还变大"，极难定位
            （本批次实测踩到: `relocated_rows=0` 而 `heap_floor` 停在 307）。
            """
            pid = heap_pages[i]
            pg = self.bp.get_page(pid)
            if pg is None or pg.page_type != PAGE_TYPE_HEAP:
                return -1
            total = page_total.get(pid, 0)
            ob = out_bytes.get(pid, 0)
            oc = out_cnt.get(pid, 0)
            ib = in_bytes.get(pid, 0)
            ic = in_cnt.get(pid, 0)
            extra = 4 * pg.n_slots if (total and ob >= total) else 0
            raw = pg.free_space() + ob + 4 * oc + extra - ib - 4 * ic
            # 软容量: 只按"**搬入**的字节量"封顶（页上原本就有的活行不占用
            # 这一份额度，它们本来就在那里、也不会被搬走）。
            soft = soft_cap - ib - 4 * ic
            return min(raw, soft)

        plan = {}
        dst_i = 0
        stop = False
        # 目标页只**往后**走（前向密排），且只允许装到"当前还有活行的最高页"
        # 以内。搬页的收益最终由"实测投影"把关（见函数末尾的收益门槛）。
        for src_pid in heap_pages:
            if stop:
                break
            for sid, rb in per_pid[src_pid]:
                need = len(rb) + 4
                dst_pid = None
                i = dst_i
                while i < len(heap_pages):
                    if avail_of(i) >= need:
                        dst_i = i
                        dst_pid = heap_pages[i]
                        break
                    i += 1
                if dst_pid is None:
                    stop = True
                    break
                plan[(src_pid, sid)] = dst_pid
                out_bytes[src_pid] += need
                out_cnt[src_pid] += 1
                in_bytes[dst_pid] += need
                in_cnt[dst_pid] += 1
        # 填上每个目标页内的"计划序号"（执行时按该序号 insert_row）
        seq_of = {}
        for old_loc in sorted(plan, key=lambda o: (order[plan[o]],
                                                   order[o[0]], o[1])):
            dst_pid = plan[old_loc]
            plan[old_loc] = (dst_pid, seq_of.get(dst_pid, 0))
            seq_of[dst_pid] = seq_of.get(dst_pid, 0) + 1

        if rows:
            self._reloc_dbg = {
                'n_pages': n_pages, 'target_pages': target_pages,
                'rows': len(rows), 'heap_pages': len(heap_pages),
                'soft_cap': soft_cap,
            }
        moved = len(plan)
        if not moved:
            floor = self._heap_floor(heap_pages)
            self._reloc_dbg = {'n_pages': n_pages,
                               'target_pages': target_pages,
                               'plan': 0, 'moved': 0,
                               'rows': len(rows),
                               'avail_first': [avail_of(i)
                                               for i in range(min(5, len(heap_pages)))],
                               'heap_floor': floor}
            return n_pages, 0, floor, None

        # ---- 4) 一次性算出"每个目标页要装什么、每个源页要挖空什么" ----
        #    `plan` 只允许"往更低页号搬"，所以每页的角色是明确的:
        #    * 源页（在 plan 里出现）: 先 delete_row 挖空这些槽位再 compact；
        #    * 目标页: 挖空后按计划序号 insert_row。
        #    源页与目标页可能重合（既收又出），执行时**先出后进**。
        dst_rows = {}
        src_moves = {}
        for old_loc, (dst_pid, seq) in plan.items():
            sp = self.bp.get_page(old_loc[0])
            rb = sp.get_row(old_loc[1]) if sp is not None else None
            if rb is None:
                continue
            dst_rows.setdefault(dst_pid, []).append((seq, rb))
            src_moves.setdefault(old_loc[0], []).append(old_loc[1])
        plan = {o: plan[o] for o in plan if o in src_moves}
        feasible_dst = self._prune_plan(plan, src_moves, dst_rows)
        if not feasible_dst:
            return n_pages, 0, self._heap_floor(heap_pages), None
        plan = {o: n for o, n in plan.items() if n[0] in feasible_dst}
        dst_rows = {p: v for p, v in dst_rows.items() if p in feasible_dst}
        src_moves = {}
        for old_loc in plan:
            src_moves.setdefault(old_loc[0], []).append(old_loc[1])
        moved = len(plan)

        # ---- 5) 执行（两阶段，顺序即正确性）----
        #    阶段一: **先把所有源页要搬走的行挖空并 compact**。
        #           必须在任何 insert 之前做完 —— 否则一个"既收又出"的页
        #           会先被塞满搬入行，之后再删自己的搬出行，把刚搬进来的
        #           行一起删掉（本批次实测踩到的坑: 行数少了、且出现重复 pk）。
        #           ★ 挖空之前先把**行字节原样**留一份，供"越搬越大"时回滚。
        rollback = []
        for pid in sorted(src_moves):
            page = self.bp.get_page(pid)
            if page is None:
                continue
            for sid in src_moves[pid]:
                rb = page.get_row(sid)
                rollback.append(((pid, sid), None, rb))
                page.delete_row(sid)
            page.compact()
        #    阶段二: 按 (目标页, 计划序号) 顺序 insert_row。
        #           序号与 `insert_row` 返回的槽位号一一对应。
        executed = {}
        for pid in sorted(dst_rows):
            page = self.bp.get_page(pid)
            if page is None:
                continue
            for seq, rb in sorted(dst_rows[pid]):
                got = page.insert_row(rb)
                if got is None:
                    continue          # 兜底：放不下就不搬
                executed[(pid, seq)] = (pid, got)
        # ---- 6) 位置映射: 搬走的行 → 新位置；没搬的行 → 原地
        loc_map = {}
        for old_loc, new_loc in plan.items():
            real = executed.get(new_loc)
            if real is not None:
                loc_map[old_loc] = real
        if not loc_map:
            return n_pages, 0, self._heap_floor(heap_pages), None

        # 供"越搬越大"时的**回滚**使用（见 `_reclaim_pages`）:
        # (旧位置, 新位置, 行字节原样)
        rollback = []
        for old_loc, real in loc_map.items():
            sp = self.bp.get_page(old_loc[0])
            rb = sp.get_row(old_loc[1]) if sp is not None else None
            rollback.append((old_loc, real, rb))

        # ---- 5) 改写索引项（否则索引指向已删槽位） ----
        for k, pid, sid in list(self.bt.scan()):
            new_loc = loc_map.get((pid, sid))
            if new_loc is not None:
                self.bt.insert(k, new_loc[0], new_loc[1])

        # ---- 6) 改写 prev 指针（所有搬动已完成，引用关系稳定） ----
        for old_loc, (npid, nsid) in list(loc_map.items()):
            page = self.bp.get_page(npid)
            rb = page.get_row(nsid) if page is not None else None
            if rb is None:
                continue
            pp, ps = struct.unpack_from('<II', rb, 13)
            if not pp:
                continue
            new_prev = loc_map.get((pp, ps))
            if new_prev is None:
                continue
            b = bytearray(rb)
            struct.pack_into('<II', b, 13, new_prev[0], new_prev[1])
            b = bytes(b)
            if not page.update_row_inplace(nsid, b):
                page.restore_row(nsid, b)

        # ---- 7) 源页收尾紧凑化（搬空之后槽位数组与行游标一并复位） ----
        for pid in set(old[0] for old in loc_map):
            page = self.bp.get_page(pid)
            if page is not None:
                page.compact()
        self.bp.flush()
        # 搬完之后"还有活行的最高堆页 + 1": 之上所有页都可以安全丢弃
        # （索引项与版本链都已被前向改写，全部指向这个位置以下）。
        heap_floor = self._heap_floor(heap_pages)
        # ★★ 收益门槛（**实测投影，不是估算**）: 如果"搬完之后还有活行的
        #    最高堆页"不低于"搬之前"的最高活页，这次搬页等于白做
        #    （甚至因为每页留了可复用余量而需要更多页）。此时把搬页
        #    整体回滚，一行都不动 —— 宁可不缩，绝不越搬越大。
        live_before = 2
        for pid in heap_pages:
            page = self.bp.get_page(pid)
            if page is None:
                continue
            if any(True for _ in page.iter_rows()):
                live_before = max(live_before, pid)
        if heap_floor > live_before:
            self._rollback_relocate(rollback)
            self.bp.flush()
            self._reloc_dbg = {'n_pages': n_pages,
                               'target_pages': target_pages,
                               'planned': len(plan), 'moved': 0,
                               'rejected': 'no_gain(floor=%d>live=%d)'
                                           % (heap_floor, live_before),
                               'heap_floor': self._heap_floor(heap_pages)}
            return n_pages, 0, live_before + 1, None
        self._reloc_dbg = {'n_pages': n_pages, 'target_pages': target_pages,
                           'planned': len(plan), 'moved': moved,
                           'executed': len(loc_map),
                           'heap_floor': heap_floor}
        return n_pages, moved, heap_floor, rollback

    def _heap_pages(self) -> list:
        """当前文件里所有堆页的页号（升序）"""
        out = []
        for pid in range(1, self.bp.total_pages()):
            page = self.bp.get_page(pid)
            if page is not None and page.page_type == PAGE_TYPE_HEAP:
                out.append(pid)
        return out

    def _free_page_candidates(self, floor_pages: int) -> list:
        """
        列出 `floor_pages` 之上那些**可以安全复用**的页号。

        谁是安全的: `floor_pages` 之上、且不在"仍被引用的页"集合里的页。
        `scan_pages` 给出的 `live_pages` 含**四条引用**（可达树页、叶子
        兄弟链、活跃行所在堆页、被 prev 指向的堆页）。`floor_pages` 本身
        已经保证"之上的堆页没有活跃行"，所以余下不在 `live_pages` 里的页
        —— 主要是**即将被整棵丢弃的旧 B+Tree** —— 就是新树的落脚点。

        为什么必须用它们: 新树如果追加到文件末尾，"回收"反而让文件变大
        （本批次实测: 4000 行删 2/3 的场景 1257472 → 1306624 B）。
        """
        if floor_pages is None:
            return []
        try:
            res = scan_pages(self.bp, self.bt, self.freelist.pages,
                             protected_pages=self._reclaim_protected_pages())
        except Exception:
            return []
        out = []
        n_total = self.bp.total_pages()
        for pid in range(2, n_total):
            pg = self.bp.get_page(pid)
            if pg is None:
                continue
            if pg.page_type == PAGE_TYPE_HEAP:
                if pid >= floor_pages or any(True for _ in pg.iter_rows()):
                    continue          # 还有行的堆页 / floor 以下的堆页: 不能动
                out.append(pid)
                continue
            # 索引页: 旧树整棵都要丢弃（新树会密集打包），所以这些页号
            # 都可以当新树的家。堆页则只允许"无活行且不在 floor 以下"的。
            out.append(pid)
        return out

    def _rollback_relocate(self, rollback):
        """
        撤销一次前向搬页（把行写回**原页原槽位**），用于"越搬越大"的场合。

        顺序: 先把新位置的行删掉，再按**倒序**写回旧位置并紧凑化。
        倒序是为了让"同一页上多个槽位"的恢复不会互相覆盖（`restore_row`
        在槽位需要新空间时会移动 `next_row_ptr`）。索引项与 prev 指针
        不用管: 搬页时改过的索引项由调用方随后用 `_rebuild_btree` 重建，
        prev 指针只可能被改成"新位置"，而恢复后这些行的位置与搬页前
        完全一致 —— 调用方必须紧接着重建索引（`_reclaim_pages` 就是这么做的）。
        """
        pages = set()
        for old_loc, new_loc, _rb in rollback:
            pages.add(old_loc[0])
            if new_loc:
                pages.add(new_loc[0])
        # 1) 删掉搬家后的行
        for old_loc, new_loc, _rb in rollback:
            if new_loc is None:
                continue
            p = self.bp.get_page(new_loc[0])
            if p is not None:
                p.delete_row(new_loc[1])
        # 2) 倒序写回原位置
        for old_loc, _new_loc, rb in reversed(rollback):
            if rb is None:
                continue
            p = self.bp.get_page(old_loc[0])
            if p is None:
                continue
            if not p.restore_row(old_loc[1], rb):
                p.insert_row(rb)
        # 3) 紧凑化涉及的页
        for pid in pages:
            p = self.bp.get_page(pid)
            if p is not None:
                p.compact()
        self.bp.flush()

    def _heap_floor(self, heap_pages) -> int:
        """
        "还有活行的最高堆页 + 1"（没有活行时返回 2 = 页 0/1 之后）。

        这是本表堆页在**物理上**用到的上界: 之上的页要么是索引页、
        要么是空页，全都可以在重建索引时一并丢弃。
        """
        floor = 2
        for pid in heap_pages:
            page = self.bp.get_page(pid)
            if page is not None and any(True for _ in page.iter_rows()):
                floor = max(floor, pid + 1)
        return floor

        self.bp.flush()
        return n_pages, moved

    # ---------- B+Tree 原地重建（索引页级回收） ----------

    def _rebuild_btree(self, floor_pages: int = None,
                       free_pages: list = None) -> dict:
        """
        按**密集打包**重建 B+Tree，把稀疏的索引页真正回收掉。

        为什么索引也必须回收（实测）: 一次"插 20000 行 → 删 90%"之后
        `.dat` 307 页里只有 187 页是堆页，**119 页是索引页** ——
        20000 个主键分布在 60 片叶子上，删到只剩 2000 个键之后每片叶子
        只剩几十个条目（`LEAF_CAPACITY` = 337）。只回收堆页的话文件上限
        被索引页钉死在 307 页（1257472 B），实测只能截掉 1 页。

        做法: 把"索引项 → 行位置"按主键升序取出 → 新建一棵空树 →
        顺序插入。新树的叶子按 `LEAF_CAPACITY` 顺序填满，2000 个键只要
        ~6 片叶子（旧树 60 片）。旧索引页全部变成不可达孤儿页。

        ★ 新树必须**落在被丢弃的旧索引页号上**，不能追加到文件末尾
        （否则"回收"反而让文件变大 —— 本批次实测: 4000 行删 2/3 的场景
        1257472 → 1306624 B）。所以这里接收两样东西:
          * `floor_pages`: 把文件先截到这里（之下是堆页与元数据，安全）；
          * `free_pages`: 从 floor 之上挑出的**空页池**，`limit` 只在这些
            页号里分配。

        为什么还要"先把根指针置失效并 fsync": 截断之后盘上的旧根可能指向
        已经不存在的页。先写 -1（`_load_btree_root` 会把它当作"没有可用
        索引"）再建新树，崩溃后 `_rebuild_index()` 会扫堆页重建一份完整
        索引 —— 数据一行不少。**不置失效**而直接截断的话，盘上会留着
        指向垃圾页的根，比"没有索引"危险得多。

        返回统计 dict。任何一步失败都不影响堆数据（索引可从堆重建）。
        """
        entries = [(k, pid, sid) for k, pid, sid in self.bt.scan()]
        if not entries:
            return {'index_entries': 0}
        # ★ 只重建"槽位真实存在"的项: 页级回收刚把死版本的槽位置零过，
        #   索引项若指向空槽位，重建后那一行就永远读不到了。
        entries = [e for e in entries
                   if (self.bp.get_page(e[1]) is not None
                       and self.bp.get_page(e[1]).get_row(e[2]) is not None)]
        if not entries:
            return {'index_entries': 0}
        old_bt = self.bt
        old_live = self.bt.live_pages()
        old_root = self.bt.root_page_id
        old_file_size = self.bp.file_size()
        self.bp.flush()

        stats = {'index_entries': len(entries),
                 'old_index_pages': len(old_live), 'old_root': old_root}

        dropped = 0
        if floor_pages and floor_pages >= 2:
            if all(pid < floor_pages for _k, pid, _s in entries):
                dropped = self.bp.truncate(floor_pages)
                self._save_btree_root(-1, sync=True)
                self.bp.flush()
        stats['dropped_pages'] = dropped
        size_before_tree = self.bp.file_size()

        limit = None
        lo = 1
        if free_pages:
            pool = sorted(p for p in free_pages
                          if p >= 2 and (floor_pages is None
                                         or p >= floor_pages))
            if len(pool) >= 2:
                # 取**最长的一段连续空页**当新树的家（保证不追加到文件尾）
                runs, start, prev = [], None, None
                for p in pool:
                    if prev is None or p != prev + 1:
                        if start is not None:
                            runs.append((start, prev))
                        start = p
                    prev = p
                if start is not None:
                    runs.append((start, prev))
                a, b = max(runs, key=lambda r: r[1] - r[0])
                limit = b + 1
                lo = a
                stats['index_region'] = (a, b)
                stats['free_region_pages'] = b - a + 1

        new_bt = BTree(self.bp, root_page_id=None,
                       page_limit=(lo, limit) if limit else None)  # 新建空树
        for k, pid, sid in entries:
            new_bt.insert(k, pid, sid)
        self.bt = new_bt
        self._save_btree_root(new_bt.root_page_id, sync=True)
        self.bp.flush()
        new_live = new_bt.live_pages()
        grew = self.bp.file_size() > old_file_size
        # ★★ 收益门槛（**实测**）: 新树页数不少于旧树 ⇒ 重建是负收益，
        #    恢复旧树（它仍在盘上）。新树页数**更少**时接受它，并把
        #    "新树被追加到文件尾"多出来的长度截回去 —— 旧树的内容在
        #    `old_file_size` 之下，而新树的所有页都在它之上，因此这一步
        #    是安全的（本批次实测: 20000 行删 90% 的场景新树 119 → 12 页，
        #    若不截回文件长度就变成 1257472 → 1306624 B）。
        if len(new_live) >= len(old_live):
            self.bt = old_bt
            self._save_btree_root(old_root, sync=True)
            self.bp.truncate(max(1, old_file_size // PAGE_SIZE))
            self.bp.flush()
            stats.update({
                'new_index_pages': len(old_live),
                'new_root': old_root,
                'orphan_index_pages': 0,
                'rejected': 'no_gain(pages %d>=%d)'
                            % (len(new_live), len(old_live)),
            })
            return stats
        if grew:
            # 新树更小但被追加到尾部: 把尾部截回旧长度（新树页都在其上，
            # 截回后文件长度只覆盖旧树的位置；新树的页号仍然有效，
            # 因为它们是"被丢弃的旧树页"留下的空洞）。
            self.bp.truncate(max(1, old_file_size // PAGE_SIZE))
            self.bp.flush()
        self._save_btree_root(new_bt.root_page_id, sync=True)
        self.bp.flush()
        stats.update({
            'new_index_pages': len(new_live),
            'new_root': new_bt.root_page_id,
            'orphan_index_pages': len(old_live - new_live),
        })
        return stats

    # ==================== 完整性扫描器 ====================

    def _vacuum_stats(self) -> dict:
        """内部: 最近一次 vacuum 的页级统计"""
        return dict(self._last_vacuum_pages or {})

    def vacuum_stats(self) -> dict:
        """最近一次 vacuum 的页级统计（`Database.vacuum` / `VACUUM SQL` 用）"""
        return dict(self._last_vacuum_pages or {})

    # ==================== 完整性扫描器 ====================

    def integrity_scan(self, deep: bool = True) -> dict:
        """
        完整性扫描器: 遍历**索引**与**每一行的版本链**，报告所有
        (page, slot) 引用是否自洽。

        返回 dict（可直接断言/打印）:
          index_entries     索引项数
          chain_versions    沿链访问到的版本数
          dangling_prev     悬空的 prev_page（指向不存在的页/空槽）
          rows_on_free_pages 活跃版本落在**空闲页**上的行（不允许 > 0）
          live_rows         活跃（xmax == 0）版本数
          orphan_btree_pages 不可达的 B+Tree 页（浪费空间，非损坏）
          unreferenced_pages 无人引用、无活跃行的堆页（可截断候选）
          truncated_pages_ok 截断上界之上的页确实无人引用
          bad_magic_pages   魔数坏页
          leaked_slots      **活跃行**落在被截断页上的情况（恒为 0）

        deep=False 时只查索引 + 版本链，不做全页引用扫描（省一次全文件读）。
        """
        with self._lock:
            self.bp.flush()
            entries = list(self.bt.scan())
            seen_locs = set()
            dangling = []
            chain_count = 0
            live_rows = 0
            rows_on_free = []
            free_set = set(self.freelist.pages)
            n_pages = self.bp.total_pages()

            for k, pid, sid in entries:
                loc = (pid, sid)
                # 1) 索引项必须落在存在的页上
                if pid < 1 or pid >= n_pages:
                    dangling.append(('index', k, loc, 'page-missing'))
                    continue
                chain = list(self._chain_versions(loc))
                if not chain:
                    dangling.append(('index', k, loc, 'slot-empty'))
                    continue
                for vpid, vsid, rb, _vals, xmin, xmax, pp, ps in chain:
                    chain_count += 1
                    seen_locs.add((vpid, vsid))
                    if xmax == 0:
                        live_rows += 1
                    if xmax == 0 and (vpid, vsid) in free_set:
                        # 活跃行落在空闲页上: 登记表把它当"可复用"，
                        # 后续插入会把行**覆盖**掉 —— 这是最危险的一类
                        rows_on_free.append((k, vpid, vsid))
                    if pp == NO_PREV:
                        continue
                    tpage = self.bp.get_page(pp)
                    if tpage is None:
                        dangling.append(
                            ('prev', k, (vpid, vsid), (pp, ps), 'page-missing'))
                    elif tpage.get_row(ps) is None:
                        dangling.append(
                            ('prev', k, (vpid, vsid), (pp, ps), 'slot-empty'))

            out = {
                'table': self.name,
                'total_pages': n_pages,
                'index_entries': len(entries),
                'chain_versions': chain_count,
                'live_rows': live_rows,
                'dangling_prev': dangling,
                'rows_on_free_pages': rows_on_free,
                'free_pages': len(free_set),
            }
            if deep:
                res = scan_pages(self.bp, self.bt, self.freelist.pages,
                                 protected_pages=self._reclaim_protected_pages())
                out.update({
                    'orphan_btree_pages': sorted(res.orphan_btree),
                    'unreferenced_pages': sorted(res.truncatable),
                    'bad_magic_pages': sorted(res.bad_magic),
                    'max_live_page': res.max_live_page(),
                    'scan_dangling_prev': res.dangling_prev,
                    'truncatable_free_bytes': res.free_in_truncatable,
                })
            return out

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

    def wal_checkpoint_time_limit(self) -> float:
        """当前生效的**时间**阈值（秒）；<=0 表示关闭该维度"""
        v = self.wal_checkpoint_seconds
        if v is None:
            try:
                v = float(os.environ.get(
                    WAL_CHECKPOINT_SECONDS_ENV,
                    WAL_CHECKPOINT_SECONDS_DEFAULT))
            except (TypeError, ValueError):
                v = WAL_CHECKPOINT_SECONDS_DEFAULT
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return 0.0

    def checkpoint(self, force: bool = False, reason: str = 'manual') -> bool:
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
        `reason`（size/records/time/manual）只用于状态与日志，不参与语义。
        """
        with self._lock:
            if self._read_views and not force:
                return False
            before_bytes = self.wal.size_bytes()
            before_records = self.wal.record_count()
            self._retire_wal()
            # P6.2：checkpoint 是确定性落盘点：把自增预留收敛到**精确**
            # 下一号（重启不跳号），并保证已分配号在盘上。
            try:
                self._persist_auto_reservation(exact=True)
            except Exception as e:               # pragma: no cover - 防御
                logger.error(f"checkpoint 写自增预留失败 {self.name}: {e}")
            self._checkpoint_count += 1
            self._last_checkpoint_ts = time.time()
            self._last_checkpoint_reason = str(reason or 'manual')
            self._last_checkpoint_wal_bytes = before_bytes
            self._last_checkpoint_wal_records = before_records
            if before_records:
                # DEBUG 级：既有 crash 测试用 subprocess 捕获子进程 stdout
                # 并直接 `json.loads`，高频 checkpoint 日志若走 INFO 会污染
                # 该输出。常规状态观测走 `SHOW WAL STATUS`/`wal_status()`；
                # 需要日志时把 logger 级别调到 DEBUG 即可看到。
                logger.debug(
                    f"checkpoint {self.name}: reason="
                    f"{self._last_checkpoint_reason} "
                    f"retired_wal_bytes={before_bytes} "
                    f"retired_wal_records={before_records} "
                    f"total_checkpoints={self._checkpoint_count} "
                    f"(data pages fsynced, WAL truncated)")
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
        # 空闲页登记表随 checkpoint/close 一并落盘（内容没变时不写）:
        # 这是"重启后仍知道哪些页可用"的持久化点之一，另两处是
        # vacuum 收尾与"页被填满"时（_unregister_space_page）。
        try:
            self.freelist.save()
        except Exception as e:
            logger.warn(f"空闲页登记表落盘失败 {self.name}: {e}")
        self.wal.truncate(sync=sync_truncate)

    def _maybe_checkpoint(self) -> bool:
        """按阈值判断是否需要 checkpoint（只在语句成功收尾后调用）

        P5：三个维度 —— WAL 字节数 / 记录条数 / 距上次 checkpoint 的
        时间。任一达到即执行；大小/条数优先报对应原因。
        """
        limit_b, limit_r = self.wal_checkpoint_limits()
        limit_s = self.wal_checkpoint_time_limit()
        if limit_b <= 0 and limit_r <= 0 and limit_s <= 0:
            return False
        if self._read_views:          # 有未提交事务：推迟，绝不刷未提交数据
            return False
        records = self.wal.record_count()
        reason = None
        if (limit_b > 0 or limit_r > 0) and \
                self.wal.needs_checkpoint(limit_b, limit_r):
            reason = 'size' if limit_b > 0 and \
                self.wal.size_bytes() >= limit_b else 'records'
        elif limit_s > 0 and records > 0 and \
                (time.time() - self._last_checkpoint_ts) >= limit_s:
            reason = 'time'
        if reason is None:
            return False
        return self.checkpoint(reason=reason)

    def wal_status(self) -> dict:
        """P5：checkpoint / WAL 状态（`SHOW WAL STATUS` 的唯一数据源）

        所有取值都是**真实**的：WAL 字节/条数来自 WAL 计数量，checkpoint
        次数/最近原因来自本表实际执行记录，`.dat` 大小为磁盘真实字节数。
        """
        try:
            dat_bytes = self.bp.file_size()
        except Exception:                         # pragma: no cover - 防御
            dat_bytes = 0
        limit_b, limit_r = self.wal_checkpoint_limits()
        return {
            'table': self.name,
            'wal_bytes': self.wal.size_bytes(),
            'wal_records': self.wal.record_count(),
            'limit_bytes': limit_b,
            'limit_records': limit_r,
            'limit_seconds': self.wal_checkpoint_time_limit(),
            'checkpoints': self._checkpoint_count,
            'last_checkpoint_reason': self._last_checkpoint_reason,
            'last_checkpoint_age_seconds': max(
                0.0, time.time() - self._last_checkpoint_ts),
            'last_checkpoint_wal_bytes': self._last_checkpoint_wal_bytes,
            'last_checkpoint_wal_records': self._last_checkpoint_wal_records,
            'dat_bytes': dat_bytes,
            'free_pages': len(self.freelist.pages),
            'rows': self.row_count,
        }

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
        # P6.2：干净关闭把自增预留收敛到精确下一号（重启不跳号）
        try:
            self._persist_auto_reservation(exact=True)
        except Exception as e:                    # pragma: no cover - 防御
            logger.warn(f"关闭时写自增预留失败 {self.name}: {e}")
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