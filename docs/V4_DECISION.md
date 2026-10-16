# LightDB v4.0 技术决策文档

> ⚠️ **状态说明（2026-10 阶段 0 收尾补充；本文档**已被有意截断**，本文件
> 不补写缺失内容）**：正文"目标"一节（`12,000 QPS` / `80,000 QPS` /
> `1M rows/s`）以及"现状"里的 `6500 / 8200 QPS` **均无仓库内基准产物支撑，
> 属设计目标/估计值**；`lightdb/olap/**`（向量化）、`lightdb/columnar/**`
> （列存）、`lightdb/parallel/**` 在 `Executor` / `plan_executor` 里**零引用**
> （未接入 SQL 执行路径），`lightdb/cython/*.pyx` 编译产物为 **0 个**
> （**未编译验证**）。本机可复现实测见 [review/01_engine.md](review/01_engine.md)：
> 逐行 INSERT **1,501 rows/s**、批量 INSERT **1,356 rows/s**、点查 **888 q/s**
> 进程内 / **1,138 q/s** TCP（与 12,000 QPS 相差 **13.5 倍**）、并发 1→8
> 客户端 **−36%**、WAL **≈200 B/行**、10 轮 UPDATE 后 `.dat` **669 KB→16 MB**。
> 本文末尾（第 44 行）写明"原文档在此处截断，后续章节缺失"——**该截断是
> 有意为之的既存状态，本文件不补写历史内容**。

> ⚠️ **数据可信度声明（2026-10 审计）**：本文档中的 QPS、加速比、压缩比等数字**并非由本仓库内的工具产出** —— `lightdb/benchmark/` 下没有任何基准运行产物，`lightdb/benchmark/baseline.py:11` 指向的 `~/.lightdb_studio/benchmarks` 目录也不存在（`~/.lightdb_studio/` 下只有空的 `reports/`、`soak/`）。其中 12000 QPS / 80000 QPS 目标只出现在 `tests/regression_v4/test_performance_v4.py:13-18` 的 `PERF_TARGETS` 字典里，**从未被任何测试断言**。因此本文数字应视为**设计目标或估计值**，而非实测结果。正文表格保持原貌以便对照，完整审计结论见 [AUDIT_REPORT.md](AUDIT_REPORT.md)。

**决策日期**：2025-02-15
**决策范围**：v4.0 性能优化技术路线
**决策者**：LightDB 架构组

---

## 一、背景

### 现状

> ⚠️ **状态（阶段 2 第 6 项）**：本节与下节的全部 QPS / rows/s 数字（含
> `6500 / 8200 QPS`、`12,000 / 80,000 QPS`、`1M rows/s`）**均非实测**；
> `OLAP 列式扫描` 属**实验性、未接入 SQL 路径、不计入性能声明**。对照见
> [benchmarks/README.md](../benchmarks/README.md)。

- **v3.0 性能**：MySQL 5.7 的 80%
- **单核瓶颈**：6500 QPS（MySQL 8000）
- **多核瓶颈**：8 核仅 8200 QPS（GIL 限制）
- **OLAP**：不支持列式扫描

### 目标

> ⚠️ **状态（阶段 2 第 6 项）**：以下为**目标值**，不是实测可达能力；
> `OLAP：1M rows/s` 所在链路**实验性、未接入 SQL 路径、不计入性能声明**。
> 对照见 [benchmarks/README.md](../benchmarks/README.md)。

- **v4.0 性能**：MySQL 5.7 的 **120%**
- **单线程**：12,000 QPS
- **多线程**：80,000 QPS（8 核）
- **OLAP**：1M rows/s

---

## 二、诊断结论

### 2.1 性能热点分布

| 类别 | 耗时占比 | 说明 |
|------|:--------:|------|
| B+Tree | 25% | 纯 Python 指针操作 |
| 行序列化 | 20% | JSON 转 Python |
| 页操作 | 15% | 字节读写 |
| 执行器 | 10% | 火山模型 |
| 网络 | 8% | MySQL 协议 |
| 其他 | 22% | 分散 |

### 2.2 GIL 影响量化

> 注：原文档在此处截断，后续章节缺失。
