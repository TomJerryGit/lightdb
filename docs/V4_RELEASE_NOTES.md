# LightDB v4.0.0 发布说明

> ⚠️ **状态说明（2026-10 阶段 0 收尾补充，正文一字未删）**：本文的
> **12,000 / 92,000 QPS、3.5M / 85M rows/s、12.5x（Cython B+Tree）、
> 3.5M rows/s（向量化扫描）、85M rows/s（向量化聚合）** 属**设计目标**：
> `lightdb/olap/**`、`lightdb/columnar/**`、`lightdb/parallel/**` 在
> `Executor` / `plan_executor` 里**零引用**（未接入 SQL 执行路径）；
> `lightdb/cython/*.pyx` 的编译产物为 **0 个**（`any_accelerated=False`，
> **从未编译验证**，本机无 Cython/C 编译器）。可复现实测见
> [review/01_engine.md](review/01_engine.md)：逐行 INSERT **1,501 rows/s**、
> 批量 INSERT **1,356 rows/s**、点查 **888 q/s** 进程内 / **1,138 q/s** TCP、
> 并发 1→8 客户端 **−36%**、WAL **≈200 B/行**、10 轮 UPDATE 后 `.dat`
> **669 KB→16 MB**。本文正文保持原貌以便对照，**不补写**未验证内容。
>
> 🔻 **阶段 2 第 6 项（方案 A「摘牌 + 显式化」）补充**：OLAP / 列存层现在明确
> 登记为「**实验性、未接入 SQL 路径、不计入性能声明**」
> （`lightdb/olap/status.py`，`WIRED_TO_SQL_PATH = False`）；生产代码里唯一
> 一条会悄悄走到向量化引擎的分派（`lightdb/parallel/executor.py` 的
> `_serial_group_by`）已删除，保留的实验性入口触达时显式报错或显式留痕。
> 本文所有性能数字一律以 [benchmarks/README.md](../benchmarks/README.md)
> 的实测对照表为准，不得单独引用。

> ⚠️ **数据可信度声明（2026-10 审计）**：本文档中的 QPS、加速比、压缩比等数字**并非由本仓库内的工具产出** —— `lightdb/benchmark/` 下没有任何基准运行产物，`lightdb/benchmark/baseline.py:11` 指向的 `~/.lightdb_studio/benchmarks` 目录也不存在（`~/.lightdb_studio/` 下只有空的 `reports/`、`soak/`）。其中 12000 QPS / 80000 QPS 目标只出现在 `tests/regression_v4/test_performance_v4.py:13-18` 的 `PERF_TARGETS` 字典里，**从未被任何测试断言**。因此本文数字应视为**设计目标或估计值**，而非实测结果。正文表格保持原貌以便对照，完整审计结论见 [AUDIT_REPORT.md](AUDIT_REPORT.md)。

**发布日期**：2025-08-01
**版本代号**：Performance Breakthrough
**对标目标**：MySQL 5.7（综合达成 125%）

---

## 🎉 里程碑

LightDB 从 v3.0 的"生产可用"到 v4.0 的"**性能超越**"：

> ⚠️ **状态（阶段 2 第 6 项）**：下表 `OLAP 扫描 3.5M rows/s` / `大表聚合
> 85M rows/s` 属**实验性、未接入 SQL 路径、不计入性能声明**；`12,000 /
> 92,000 QPS` 为设计目标。对照见
> [benchmarks/README.md](../benchmarks/README.md)。

| 指标 | v3.0 | v4.0 | 提升 |
|------|:----:|:----:|:----:|
| **单线程点查** | 6,500 QPS | **12,000 QPS** | **+85%** |
| **8 核并发** | 8,200 QPS | **92,000 QPS** | **+1022%** |
| **OLAP 扫描** | 不支持 | **3.5M rows/s** | 新增 |
| **大表聚合** | 不支持 | **85M rows/s** | 新增 |
| **综合** | 80% MySQL | **125% MySQL** | **+56%** |

---

## ✨ v4.0 六大突破

### 1. 突破 GIL：多进程架构

- **SO_REUSEPORT** 内核级负载均衡
- **Worker 池** 自动重启
- **共享内存** 跨进程页缓存
- **集中式** 事务/锁协调器

### 2. Cython 关键路径

> ⚠️ **状态（阶段 2 第 6 项）**：下表加速比**未编译验证**
> （`lightdb/cython/*.pyx` 编译产物 0 个、`any_accelerated=False`），
> **不计入性能声明**；对照见 [benchmarks/README.md](../benchmarks/README.md)。

| 模块 | Python | Cython | 加速 |
|------|:------:|:------:|:----:|
| B+Tree 查询 | 100ms | 8ms | **12.5x** |
| 行编码 | 18.3ms | 6.2ms | **2.95x** |
| 行解码 | 12.6ms | 3.1ms | **4.06x** |
| 谓词过滤 | 15.3ms | 5.8ms | **2.64x** |

### 3. 列式存储 + OLAP

> ⚠️ **状态（阶段 2 第 6 项）**：本节的 `压缩比 3-60x`、`向量化扫描
> 3.5M rows/s`、`向量化聚合 85M rows/s` 均属**实验性、未接入 SQL 路径、
> 不计入性能声明**；对照见 [benchmarks/README.md](../benchmarks/README.md)。

- **5 种编码**：Plain / Dict / RLE / Delta / BitPack
- **压缩比**：3-60x
- **向量化扫描**：3.5M rows/s
- **向量化聚合**：85M rows/s

### 4. 并行查询

- **分片**：范围 / 块 / 哈希 / 分区
- **工作窃取** 负载均衡
- **自适应** 并行度决策
- **资源组** 隔离

### 5. HTAP 混合表

- 行存（OLTP）+ 列存（OLAP）
- 双写自动同步
- 阈值/时间触发

### 6. 兼容性保证

- MySQL 5.7 兼容度 **95%**
- 从 v3.0 数据无缝升级
- Cython 模块可选（自动回退）

---

## 📊 性能基准

### 与 MySQL 5.7 对比

> ⚠️ **状态（阶段 2 第 6 项）**：下表 `OLAP 扫描 / 大表聚合` 两行属
> **实验性、未接入 SQL 路径、不计入性能声明**；其余 QPS / TPS / 吞吐为
> 设计目标，不是本机实测。对照见
> [benchmarks/README.md](../benchmarks/README.md)。

| 场景 | LightDB v4 | MySQL 5.7 | 达成 |
|------|:----------:|:---------:|:----:|
| 单线程点查 | 12,000 | 8,000 | **150%** ✅ |
| 8 核点查 | 92,000 | 28,000 | **328%** ✅ |
| 并发 TPS | 22,000 | 5,500 | **400%** ✅ |
| 批量插入 | 150K/s | 80K/s | **188%** ✅ |
| 复杂 JOIN | 35K/s | 28K/s | **125%** ✅ |
| **OLAP 扫描** | **3.5M rows/s** | 200K/s | **1750%** ✅ |
| **大表聚合** | **85M rows/s** | 500K/s | **17000%** ✅ |

### 与自身历史对比

> ⚠️ **状态（阶段 2 第 6 项）**：下表 `OLAP` 列的 ✅ 不代表已接入 SQL 路径 ——
> 该能力**实验性、未接入 SQL 路径、不计入性能声明**；对照见
> [benchmarks/README.md](../benchmarks/README.md)。

| 版本 | 综合得分 | OLAP |
|------|:--------:|:----:|
| v1.0 | 3.5/10 | - |
| v2.0 | 5.0/10 | - |
| v3.0 | 7.0/10 | - |
| **v4.0** | **8.8/10** | ✅ |

---

## 🚀 新特性

### 多进程模式

```bash
# 启动
lightdb-server --mode multi --workers 8