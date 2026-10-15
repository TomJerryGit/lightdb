==== BEGIN FILE: docs/ARCHITECTURE_v4.md ====

# LightDB v4.0 架构设计文档

> ⚠️ **数据可信度声明（2026-10 审计）**：本文档中的 QPS、加速比、压缩比、rows/s 等数字**并非由本仓库内的工具产出** —— `lightdb/benchmark/` 下没有任何基准运行产物，`lightdb/benchmark/baseline.py:11` 指向的 `~/.lightdb_studio/benchmarks` 目录也不存在（`~/.lightdb_studio/` 下只有空的 `reports/`、`soak/`）。其中 12000 QPS / 80000 QPS 目标只出现在 `tests/regression_v4/test_performance_v4.py:13-18` 的 `PERF_TARGETS` 字典里，**从未被任何测试断言**。因此本文数字应视为**设计目标或估计值**，而非实测结果。正文表格保持原貌以便对照，完整审计结论见 [AUDIT_REPORT.md](AUDIT_REPORT.md)。

> ⚠️ **状态说明（2026-10 阶段 0 收尾补充）**：`lightdb/olap/**`（向量化引擎）、
> `lightdb/columnar/**`（列存 `ColumnStore`）、`lightdb/parallel/**`（并行执行）
> 在 SQL 执行路径（`Executor` / `plan_executor`）里**零引用** —— 也就是说
> 「未接入 SQL 执行路径」；`lightdb/cython/*.pyx` 的编译产物（`.pyd`/`.so`/`.c`）
> 在仓库里 **0 个**，`status()['any_accelerated'] == False`，即「**未编译验证**」。
> 阶段 0 修好的向量化聚合/过滤**错值**（`int32` 溢出、批间条件失效）同样
> **仍未接线**。因此全文的 OLAP / 列存 / 并行 / Cython 数字（含下文 7.3 与
> 各对比表）都是**设计目标，不是实测**。本机可复现实测见
> [review/01_engine.md](review/01_engine.md)：逐行 INSERT **1,501 rows/s**、
> 批量 INSERT **1,356 rows/s**、点查 **888 q/s** 进程内 / **1,138 q/s** TCP、
> 并发 1→8 客户端 1,163→**749 q/s（−36%）**、WAL **≈200 B/行**、
> 10 轮 UPDATE 后 `.dat` **669 KB→16 MB**。
>
> 🔧 **多进程实现现状（2026-10 接线后）**：多进程模式已可用，但实际架构与本文描述的「多进程 + 共享内存突破 GIL」不同 —— **存储引擎只由 Master 进程持有并串行执行 SQL**，Worker 仅负责连接与协议处理（原因与实测见 [AUDIT_REPORT.md §15](AUDIT_REPORT.md)）。因此它带来的是连接层多核化与崩溃隔离，并非查询执行的 CPU 加速；本文的 8 核 QPS 数字与此无关。

**版本**：4.0.0
**更新日期**：2025-08-01
**读者对象**：架构师、核心开发者、技术决策者

---

## 目录

1. 设计目标
2. 整体架构
3. 突破 GIL：多进程
4. 单核突破：Cython
5. 存储引擎
6. 并发控制
7. OLAP 引擎
8. 并行执行
9. 复制
10. 与 v3.0 的差异

---

## 1. 设计目标

### 1.1 v4.0 目标

| 指标           | v3.0 现状    | v4.0 目标      | 达成      |
|----------------|:------------:|:--------------:|:---------:|
| 单线程点查     | 6,500 QPS    | 12,000 QPS     | 是 +85%   |
| 8 核并发       | 8,200 QPS    | 92,000 QPS     | 是 +1022% |
| OLAP 扫描      | 不支持       | 3.5M rows/s    | 是 新增   |
| 大表聚合       | 不支持       | 85M rows/s     | 是 新增   |
| MySQL 5.7 兼容 | 80%          | 95%            | 是        |
| 综合性能       | MySQL 的 80% | MySQL 的 125%  | 是        |

### 1.2 约束

- 必须保留 Python 生态：兼容现有 Python 模块
- 不能重写核心：只做增量优化
- 部署方式不变：用户侧只改版本号
- 数据格式向后兼容：v3 数据可直接升级

---

## 2. 整体架构

### 2.1 分层视图

    +-----------------------------------------------------+
    |                   Client Layer                      |
    |  mysql client / PyMySQL / JDBC / Studio Web / CLI   |
    +-------------------------+---------------------------+
                              | MySQL Wire Protocol v10
    +-------------------------v---------------------------+
    |                   Network Layer                     |
    |  SO_REUSEPORT Listener  +  MasterListener (Win)     |
    +-------------------------+---------------------------+
                              |
            +-----------------+-----------------+
            |                 |                 |
            v                 v                 v
       +--------+        +--------+        +--------+
       |Worker 0|        |Worker 1|  ...   |Worker N|
       +---+----+        +---+----+        +---+----+
           |                 |                 |
           +-----------------+-----------------+
                             |
    +------------------------v---------------------------+
    |                   SQL Layer                        |
    |  Parser -> Optimizer (RBO+CBO) -> Plan -> Executor |
    +------------------------+---------------------------+
                             |
    +------------------------v---------------------------+
    |                 Storage Layer                      |
    |  Buffer Pool | B+Tree | WAL | MVCC | Lock Manager  |
    +------------------------+---------------------------+
                             |
    +------------------------v---------------------------+
    |                Columnar Layer (OLAP)               |
    |  ColumnStore | Encoders | Vectorized Engine        |
    +------------------------+---------------------------+
                             |
    +------------------------v---------------------------+
    |             Shared Infrastructure                  |
    |  Shared Memory | Cluster Coordinator | Stats Hub   |
    +----------------------------------------------------+

### 2.2 进程模型

Master 进程（1 个）：

- 初始化共享内存（事务协调器 / 统计中心 / 缓冲池）
- 监控 Worker 状态
- 自动重启崩溃的 Worker
- 接收信号，优雅关闭

Worker 进程（N 个，N = CPU 核数）：

- 独立监听同一端口（SO_REUSEPORT）
- 独立处理客户端连接
- 通过共享内存协调事务与锁

Network 层：

- Linux/macOS：SO_REUSEPORT，内核负载均衡
- Windows：Master 接受连接后通过 Queue 分发给 Worker

### 2.3 关键设计决策

| 决策     | 方案                    | 理由                            |
|----------|-------------------------|---------------------------------|
| 突破 GIL | 多进程 + 共享内存       | Python 无法避免 GIL             |
| 单核优化 | Cython 加速热点         | 3-12x 加速，保留 Python 接口    |
| 列存     | 独立 ColumnStore        | 不影响现有行存表                |
| 兼容性   | MySQL Wire Protocol     | 复用现有客户端生态              |
| 部署     | Docker + K8s            | 云原生标准                      |

---

## 3. 突破 GIL：多进程

### 3.1 GIL 影响量化

在 8 核机器上测试 CPU 密集任务：

| 并行度 | 多线程加速 | 多进程加速 |
|:------:|:----------:|:----------:|
| 1      | 1.00x      | 1.00x      |
| 2      | 1.02x      | 1.94x      |
| 4      | 1.05x      | 3.82x      |
| 8      | 1.08x      | 7.61x      |

结论：多线程加速比永远接近 1（GIL 限制），多进程接近线性扩展。

### 3.2 SO_REUSEPORT

原理：多个进程 bind 同一 host:port，内核按 hash 把新连接分给某个进程。

每个 Worker 独立执行：

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', 8888))
    sock.listen(128)

优势：

- 内核级负载均衡，无用户态开销
- Worker 崩溃不影响其他 Worker
- 无 Master 单点

限制：

- 仅 Linux >= 3.9 / macOS
- 需要所有 Worker 使用相同启动参数

### 3.3 共享内存协调

问题：多进程下，事务 ID、锁状态、统计需要跨进程共享。

方案：基于 mmap + 文件锁（fcntl.flock）

    class SharedCounter:
        def __init__(self, shm, offset):
            self.shm = shm
            self.offset = offset

        def increment(self, delta=1):
            with self.shm.xlock.acquire():
                pos = self.shm.HEADER_SIZE + self.offset
                val = struct.unpack_from('<q', self.shm.mm, pos)[0]
                new_val = val + delta
                struct.pack_into('<q', self.shm.mm, pos, new_val)
                return new_val

组件：

| 组件                     | 用途         | 大小      |
|--------------------------|--------------|-----------|
| SharedTxnCoordinator     | 事务 ID 分配 | 64 B      |
| LockCoordinator          | 行锁状态     | 动态      |
| SharedBufferPool         | 页缓存共享   | 可配置    |
| StatsHub                 | 统计聚合     | 32 B × N  |

### 3.4 事务协调器设计

关键点：区分 create=True（Master）和 create=False（Worker）

Master 启动时：

    self.txn_coord = SharedTxnCoordinator('txn_coord', create=True)
    self.txn_coord.counter_next_id.set(1)   # 初始化为 1

Worker 打开（不重置）：

    txn_coord = SharedTxnCoordinator('txn_coord', create=False)
    # counter_next_id 保持 Master 设置的值

早期版本的 Bug：Worker 打开时总是 create=True，导致计数器被重置，事务 ID 冲突。

### 3.5 优雅关闭

    def shutdown(self):
        # 1. 停止 Worker 池（发送 SIGTERM，等待退出）
        self.workers.stop(timeout=10)

        # 2. 关闭共享内存
        self.mgr.close_all()

        # 3. 清理资源
        for comp in (self.txn_coord, self.stats_hub, self.buffer_pool):
            comp.close()

---

## 4. 单核突破：Cython

### 4.1 热点分布（v3.0 实测）

| 类别       | 耗时占比 | 优化方向       |
|------------|:--------:|----------------|
| B+Tree     | 25%      | Cython         |
| 行序列化   | 20%      | Cython + 二进制|
| 页操作     | 15%      | Cython         |
| 执行器     | 10%      | 批处理         |
| 网络       | 8%       | -              |
| 其他       | 22%      | 分散           |

### 4.2 Cython 模块

| 模块              | 功能             | 加速比  |
|-------------------|------------------|:-------:|
| btree_pyx         | B+Tree 二分查找  | 12.5x   |
| row_codec         | 二进制行编解码   | 3-4x    |
| page_pyx          | 页读写           | 5x      |
| predicate_pyx     | 谓词求值         | 2.6x    |
| buffer_pool_pyx   | LRU 缓存         | 2x      |
| hash_table_pyx    | 哈希表           | 3x      |

### 4.3 编译指令

cython/setup.py 关键配置：

    COMPILER_DIRECTIVES = {
        'language_level': 3,
        'boundscheck': False,      # 关闭边界检查
        'wraparound': False,       # 关闭负索引
        'cdivision': True,         # C 除法
        'initializedcheck': False,
        'nonecheck': False,
    }

    EXTRA_COMPILE_ARGS = [
        '-O3',
        '-march=native',
        '-flto',
        '-fno-semantic-interposition',
    ]

### 4.4 自动回退

cython/__init__.py 里做检测：

    try:
        from . import btree_pyx
        AVAILABLE['btree'] = True
    except ImportError:
        AVAILABLE['btree'] = False
        btree_pyx = None

使用处：

    if btree_pyx is not None:
        result = btree_pyx.leaf_search(data, key)
    else:
        result = _python_fallback_search(data, key)

保证：Cython 编译失败不影响功能，只影响性能。

---

## 5. 存储引擎

### 5.1 页结构

    +---------------------------+
    | Magic (4B)                | 0x4C444250 "LDBP"
    | Page ID (4B)              |
    | LSN (8B)                  |
    | Page Type (1B)            | 0=meta 1=heap 2=btree_leaf 3=btree_int
    | N Slots (2B)              |
    | Next Row Ptr (2B)         |
    | Reserved (3B)             |
    +---------------------------+
    | Slot Array (向下增长)     |
    +---------------------------+
    | Free Space                |
    +---------------------------+
    | Row Data (向上增长)       |
    +---------------------------+
                        4096 B

### 5.2 B+Tree

特性：

- 键：4 字节整数
- 值：8 字节（page_id + slot_id）
- 唯一键，重复覆盖
- 支持内部节点分裂 + 根退化

容量：

- 叶子节点：(4096 - 24 - 6) / 12 - 1 = 338 条
- 内部节点：(4096 - 24 - 6) / 8 - 1 = 507 条

高度示例：

| 行数     | 高度 |
|:--------:|:----:|
| < 338    | 1    |
| < 171k   | 2    |
| < 87M    | 3    |
| < 44G    | 4    |

### 5.3 WAL

格式：JSON 行（每行一条记录）

    {"lsn": 1, "txn_id": 42, "type": "INSERT", "data": {...}}
    {"lsn": 2, "txn_id": 42, "type": "UPDATE", "data": {...}}
    {"lsn": 3, "txn_id": 42, "type": "COMMIT", "data": {}}

恢复流程：

1. 扫描所有 WAL 记录
2. 按 txn_id 分组
3. 找每个事务的 COMMIT / ABORT
4. 只重放有 COMMIT 的事务

Checkpoint：

    wal.checkpoint()             # 记录安全 LSN
    wal.compact_before(lsn)      # 删除 LSN 之前的记录

### 5.4 MVCC

行头：

    xmin (4B) | xmax (4B) | flags (1B) | data_len (4B) | data

可见性规则：

- xmin == self.txn_id  — 自己创建的可见
- xmin in active_txns  — 其他活跃事务不可见
- xmin in aborted_txns — 已回滚的事务不可见
- xmax == 0            — 未删除，可见
- xmax in active_txns  — 删除未提交，仍可见

Undo Log：

- 每个 (table, pk) 维护版本链
- 通过 find_visible_version 回溯

GC：

- oldest_active_txn 之前的版本可清理

### 5.5 行格式

JSON（v3）：

    json.dumps(values).encode()      # 每行约 200 字节

二进制（v4）：

    [null_bitmap (1B/8cols)] [len1 data1] [len2 data2] ...

性能对比（10000 行）：

| 格式    | 编码   | 解码   | 大小    |
|---------|:------:|:------:|:-------:|
| JSON    | 15ms   | 18ms   | 350KB   |
| Binary  | 5ms    | 4ms    | 120KB   |
| 加速    | 3x     | 4.5x   | 2.9x    |

---

## 6. 并发控制

### 6.1 锁粒度

| 级别 | 用途                              |
|------|-----------------------------------|
| 表级 | LOCK TABLES                       |
| 行级 | INSERT/UPDATE/DELETE 默认         |

### 6.2 锁类型

| 类型             | 说明                    |
|------------------|-------------------------|
| S（共享）        | 读锁                    |
| X（排他）        | 写锁                    |
| IS（意向共享）   | 表级，表示下面有 S 锁   |
| IX（意向排他）   | 表级，表示下面有 X 锁   |

兼容矩阵：

|      | S  | X  | IS | IX |
|------|----|----|----|----|
| S    | 是 | 否 | 是 | 否 |
| X    | 否 | 否 | 否 | 否 |
| IS   | 是 | 否 | 是 | 是 |
| IX   | 否 | 否 | 是 | 是 |

### 6.3 死锁检测

三色 DFS：

    def _detect_cycle(self, start):
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {}

        def dfs(node):
            color[node] = GRAY
            for nxt in self.wait_graph.get(node, ()):
                c = color.get(nxt, WHITE)
                if c == GRAY:
                    return True        # 找到环
                if c == WHITE and dfs(nxt):
                    return True
            color[node] = BLACK
            return False

        return dfs(start)

策略：发现死锁后，当前事务作为受害者，抛出 DeadlockError，上层回滚。

### 6.4 条件变量优化

问题：所有事务共用一个 Condition，会造成惊群。

方案：每个 key 一个 Condition：

    def _get_key_cond(self, key):
        cond = self.key_conds.get(key)
        if cond is None:
            cond = threading.Condition(self.lock)
            self.key_conds[key] = cond
        return cond

    def release_all(self, txn_id):
        released_keys = ...   # 收集释放的 key
        for key in released_keys:
            cond = self.key_conds.get(key)
            if cond:
                cond.notify_all()   # 只通知等待该 key 的事务

效果：锁等待场景下，CPU 占用降低 60%+。

---

## 7. OLAP 引擎

### 7.1 列式存储

数据结构：

    ColumnStore
    |- ColumnChunk[]  (每列多个 chunk)
    |   |- values          # Python list
    |   |- encoded_data    # 编码后的 bytes
    |   |- stats           # min/max/null_count/distinct
    |- meta

Chunk 大小：4096 行

### 7.2 5 种编码

| 编码    | 触发条件               | 效果           |
|---------|------------------------|----------------|
| Plain   | 默认                   | 无压缩         |
| Dict    | distinct < 30%         | 低基数字符串   |
| RLE     | 重复率 > 50%           | 大量重复       |
| Delta   | 单调递增且 delta 小    | 时间序列       |
| BitPack | max < 65535            | 小整数         |

### 7.3 向量化执行

> ⚠️ **状态说明（2026-10 补充，原文保留未删）**：本节的
> `VectorizedScan -> VectorizedFilter -> VectorizedAggregate` 流水线与
> 表 7.2 的 **41x / 3.5M rows/s** **未接入 SQL 执行路径** ——
> `lightdb/olap/**`、`lightdb/columnar/**`、`lightdb/parallel/**` 在
> `Executor` / `plan_executor` 里**零引用**，因此这些数字在查询路径上
> **没有落点**，属设计目标。已修但仍未接线的错值：向量化 `SUM` 因
> `np.array` 默认 `int32` 溢出（阶段 0 修好算法）、`VectorizedFilter`
> 批间条件传递失效（实测 50,000 行 vs 应 16,317）、列存 `GROUP BY`
> 返回 0 行。实测替代数字见 [review/01_engine.md](review/01_engine.md)：
> 逐行 INSERT **1,501 rows/s**、批量 INSERT **1,356 rows/s**、聚合/范围扫描
> **11k / 4.8k rows/s**、点查 **888 q/s**（进程内）/ **1,138 q/s**（TCP）、
> 并发 1→8 客户端 **−36%**。

流程：

    VectorizedScan -> VectorizedFilter -> VectorizedAggregate

关键点：

- 每次处理 4096 行（VECTOR_SIZE）
- 使用 NumPy 数组而非 Python list
- 过滤用 mask 而非逐行判断

性能（100 万行聚合）：

| 实现        | 耗时   | 吞吐        |
|-------------|:------:|:-----------:|
| Python 逐行 | 12s    | 83K rows/s  |
| NumPy 向量化| 0.29s  | 3.5M rows/s |
| 加速        | 41x    |             |

### 7.4 列裁剪

优化器只扫描 SELECT / WHERE / GROUP BY / ORDER BY 中出现的列：

    needed_cols = set()
    if columns == ['*']:
        needed_cols.update(store.col_names)
    else:
        for c in columns:
            needed_cols.add(c.split('.')[-1])
    if where:
        needed_cols.add(where['column'].split('.')[-1])
    ...
    scan = VectorizedScan(store, list(needed_cols))

---

## 8. 并行执行

### 8.1 自适应并行度

    def decide(self, total_rows, is_expensive=False, has_lock=False):
        if total_rows < 10000:      # 小数据串行
            return 1
        if has_lock:                # 持锁串行
            return 2
        workers = min(self.max_workers, total_rows // 50000)
        if is_expensive:
            workers *= 2
        if load > 0.8:              # 系统已忙
            workers //= 2
        return max(1, workers)

### 8.2 分片

四种策略：

- Range：按行号范围（默认）
- Hash：按值的 hash
- Chunk：按固定块大小
- Partition：按已有分区

### 8.3 工作窃取

    class WorkStealingScheduler:
        def _worker_loop(self, worker_id):
            my_queue = self.queues[worker_id]
            while self.running:
                # 1. 自己的队列（LIFO）
                task = my_queue.pop_front()
                if task:
                    self._execute(task)
                    continue

                # 2. 窃取其他 Worker 的（FIFO）
                task = self._try_steal(worker_id)
                if task:
                    self._execute(task)
                    continue

                time.sleep(0.001)

优势：负载均衡，避免长任务拖累整个查询。

### 8.4 结果合并

| 聚合类型 | 合并策略                        |
|----------|---------------------------------|
| COUNT    | sum(count_i)                    |
| SUM      | sum(sum_i)                      |
| MIN      | min(min_i)                      |
| MAX      | max(max_i)                      |
| AVG      | sum(sum_i) / sum(count_i)       |
| STDDEV   | 需保存 sum, sum_sq, count       |

---

## 9. 复制

### 9.1 Binlog

格式：JSON 行（每行一个事件）

    {"pos": 1, "txn_id": 42, "type": "INSERT", "table": "users", "data": {"row": [1, "Alice"]}}
    {"pos": 2, "txn_id": 42, "type": "COMMIT", "table": null, "data": {}}

文件轮转：64MB 一个文件

关键点：

- 全局递增 pos（不因轮转而重置）
- read_from(start_pos) 遍历所有文件

### 9.2 从库复制

两个线程：

- IO 线程：从主库拉取 binlog，写入 relay log
- SQL 线程：从 relay log 读，应用到本地

命令：

    SELECT * FROM __BINLOG_FETCH__ WHERE pos >= N LIMIT 500

这是主库实现的一个虚拟表。

### 9.3 半同步复制

流程：

1. 主库 commit 时调用 semi_sync_master.wait_for_ack(pos)
2. 从库收到 binlog 后发送 __SEMI_SYNC_ACK__ slave_id pos
3. 主库收到 N 个 ACK，提交成功
4. 超时，降级为异步

关键：主库必须实现 ACK 命令的接收。

---

## 10. 与 v3.0 的差异

### 10.1 架构对比

| 维度       | v3.0            | v4.0                     |
|------------|-----------------|--------------------------|
| 进程模型   | 单进程          | 多进程                   |
| 线程安全   | GIL 保护        | 共享内存 + 文件锁        |
| 行格式     | JSON            | 二进制 + Cython          |
| 单核性能   | 6,500 QPS       | 12,000 QPS               |
| 多核性能   | 8,200 QPS       | 92,000 QPS               |
| OLAP       | 无              | 列存 + 向量化            |
| 并行查询   | 无              | 分片 + 工作窃取          |

### 10.2 兼容性

| 层面          | 兼容性                    |
|---------------|---------------------------|
| Wire Protocol | 完全兼容                  |
| SQL 语法      | 向后兼容                  |
| 数据格式      | 自动迁移                  |
| 配置文件      | 新增字段，旧字段保留      |
| Docker 部署   | 需使用 v4 镜像            |

### 10.3 权衡

得到：

- 10-1000% 性能提升
- OLAP 能力
- 与 MySQL 5.7 的 95% 兼容

代价：

- 运维复杂度提升（多进程 + 共享内存）
- 调试难度提升（跨进程问题）
- Cython 编译需要 C 工具链

---

## 附录 A：性能基准

与 MySQL 5.7 对比：

| 场景         | LightDB v4   | MySQL 5.7    | 达成    |
|--------------|:------------:|:------------:|:-------:|
| 单线程点查   | 12,000       | 8,000        | 150%    |
| 8 核点查     | 92,000       | 28,000       | 328%    |
| 并发 TPS     | 22,000       | 5,500        | 400%    |
| 批量插入     | 150K/s       | 80K/s        | 188%    |
| 复杂 JOIN    | 35K/s        | 28K/s        | 125%    |
| OLAP 扫描    | 3.5M rows/s  | 200K rows/s  | 1750%   |
| 大表聚合     | 85M rows/s   | 500K rows/s  | 17000%  |

与历史版本对比：

| 版本 | 综合得分 |
|------|:--------:|
| v1.0 | 3.5 / 10 |
| v2.0 | 5.0 / 10 |
| v3.0 | 7.0 / 10 |
| v4.0 | 8.8 / 10 |

---

## 附录 B：技术决策

### 为什么选择多进程而不是子解释器？

子解释器（PEP 684）：Python 3.12+ 支持，但目前：

- 生态支持差（第三方库不兼容）
- 状态隔离不彻底
- 性能收益不稳定

多进程：

- 生态成熟（所有库都支持）
- 完全隔离
- 性能线性扩展

结论：v4.0 选多进程，未来可评估子解释器。

### 为什么选 Cython 而不是 Rust？

Rust 扩展：

- 性能最优
- 但需要 PyO3/maturin 工具链
- 团队需要学 Rust

Cython：

- 类似 Python 语法
- 已有 C 编译器即可
- 渐进式改造

结论：v4.0 选 Cython，未来热点可考虑 Rust。

---

文档反馈：architecture@lightdb.io

==== END FILE ====