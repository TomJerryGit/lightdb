"""LightDB 统一竞品/基线 benchmark harness（P5，2026-10）。

本模块是**独立入口**，与 `benchmarks.run`（阶段 2 第 5 项的门禁套件）并存、
互不修改、互不冲突：

* `benchmarks.run`  —— 既有回归门禁（WAL/崩溃恢复/OLAP 等），保持原样；
* `competitive_harness`（本模块）—— **同机基线 harness**，用于对外口径
  （"LightDB 在本机是什么水平"）以及**保守回归门槛**（防明显回退）。

设计要点
--------
1. **覆盖**：LightDB 进程内（`Executor.execute_sql`）与真实 MySQL 协议
   （`MySQLServer` + pymysql）两条路径，指标包括：
   逐行 INSERT、批量 INSERT（多值语句）、批量/逐行比值、点查、范围扫描、
   聚合、分页（深 OFFSET）、并发 1/4/8 客户端。
2. **可复现**：固定 seed 生成数据；每项先 warmup 再取多轮样本；报告
   **中位数（median）与最优（best）**；数据集的 SHA256 指纹写入 JSON。
   INSERT 指标复用 `benchmarks.heap_insert` 的确定性数据（`v = i % 1000`），
   查询/分页指标用 seed 生成的数据 —— 两者都完全可复现。
3. **JSON**：版本（harness/lightdb/python）、环境（OS/CPU/核数/git）、seed、
   每项 samples/median/best/worst、门禁阈值与判定依据，写入
   `_analysis_tmp/bench/`（可用 `--out` 覆盖）。
4. **无 MySQL 不假横比**：只做 **LightDB 基线**；`reference_db` 字段显式
   标注是否检测到 MySQL。检测不到时写"仅 LightDB 基线，不做跨库横比"；
   检测到但未实现统一口径的横比时也明确 `compared=False`，绝不编造数字。
5. **保守门槛**：`TARGETS` 每项带 `value`/`better`/`basis`（依据 = 本机实测
   标定 + 安全余量），只抓"明显回退"（数量级 / 数十个百分点），不追求
   ±10% 的判定；`--gate` 未达标退出码 1。
6. **自检开关**：`--scale` 缩小数据量与轮数（仅用于测试 harness 本身，
   门禁仍按绝对阈值判定）；`--target-scale` / `--target NAME=VALUE` 用于
   门禁自检（模拟必红/必绿），与 `benchmarks.run` 的同类参数一致。

用法
----
    python -m benchmarks.competitive_harness --suite quick
    python -m benchmarks.competitive_harness --suite quick --gate
    python -m benchmarks.competitive_harness --suite full --out _analysis_tmp/bench/full.json
    python -m benchmarks.competitive_harness --suite quick --gate --target-scale 100
    python -m benchmarks.competitive_harness --suite quick --scale 0.02   # 冒烟

退出码：0 = 跑完（`--gate` 时全部达标）；1 = 门禁未达标或测量失败；2 = 参数错误。

本机实测耗时（2026-10-22，8 逻辑核）：`quick` ≈ 29 s（15 项指标 + 11 项门禁），
`full` ≈ 2 min（5,000 行查询集 + 更多样本）。full 档的全表扫描类指标
（分页/范围/TCP 查询）阈值单独按 full 标定（见 TARGETS 的 `value_full`）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:                       # 允许直接运行本文件
    sys.path.insert(0, REPO_ROOT)

from benchmarks import concurrency as C            # noqa: E402
from benchmarks import heap_insert as H            # noqa: E402
from benchmarks.run import (                       # noqa: E402
    _safe_write, _utf8_streams, git_dirty, git_rev, machine_info)

SCHEMA = 'lightdb-competitive-harness/1'
HARNESS_VERSION = '1.0.0'
DEFAULT_SEED = 20261019
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, '_analysis_tmp', 'bench')
SUITES = ('quick', 'full')

#: 数据集 schema 与 `benchmarks.heap_insert.COLS` 一致，便于同机对照
COLS = H.COLS
BATCH = H.BATCH

#: 套件档位参数（--scale 会等比缩小 rows/查询次数/并发窗口）
#:
#: 查询数据集行数刻意小于 INSERT 行数：本引擎的 WHERE 范围/聚合/深分页都是
#: **全表扫描**（实测查询耗时随行数线性增长，见 TARGETS 标定依据），行数越大
#: 整套越慢；quick 档以 1,000 行为准，full 档 5,000 行。
SUITE_PARAMS = {
    'quick': {
        'rows': 1000,               # 查询类数据集行数
        'insert_rows': 2000,        # 逐行 INSERT 行数
        'batch_rows': 2000,         # 多值批量 INSERT 行数
        'speedup_rows': 1000,       # 批量/逐行比值测的成对行数
        'samples': 2,               # 有效样本数（不含 warmup 轮）
        'warmup_rounds': 1,         # INSERT 类：丢弃的第 1 轮
        'nq': {'point': 300, 'range': 12, 'agg': 10, 'page': 12},
        'warmup_queries': 3,        # 查询类 warmup 次数（每个指标）
        'tcp_nq': {'point': 120, 'range': 8, 'agg': 6, 'page': 8},
        'tcp_warmup': 3,
        'concurrency_seconds': 0.75,
        'concurrency_warmup': 0.15,
        'window': 100, 'page_size': 20,
    },
    'full': {
        'rows': 5000,
        'insert_rows': 5000,
        'batch_rows': 5000,
        'speedup_rows': 5000,
        'samples': 2,
        'warmup_rounds': 1,
        'nq': {'point': 1000, 'range': 15, 'agg': 12, 'page': 15},
        'warmup_queries': 5,
        'tcp_nq': {'point': 300, 'range': 12, 'agg': 8, 'page': 12},
        'tcp_warmup': 5,
        'concurrency_seconds': 2.0,
        'concurrency_warmup': 0.4,
        'window': 100, 'page_size': 20,
    },
}

#: 保守回归门槛（2026-10-22 本机实测标定，见每项 basis）。
#: 全部是"绝对下限"式的**不回退**门禁，不是性能承诺/SLA。
#: 取实测中位数的 ~20%–30% 作为下限：只抓数量级/数十个百分点级别的回退，
#: 不追求 ±10% 判定（本机吞吐跨窗口可漂移 ±40%+）。
#:   * better='higher'：value 为下限（先乘 --target-scale）；
#:   * better='lower' ：value 为上限。
TARGETS = {
    'insert_rowwise_rows_s': {
        'value': 1200.0, 'better': 'higher', 'unit': 'rows/s',
        'basis': '2026-10-22 quick 实测中位 4,491 rows/s（best 4,576；'
                 'benchmarks 历史区间 1,374–6,373）；取 1,200 ≈ 实测 27%，'
                 '只抓数量级回退',
        'tolerance': '中位数；容忍 ~70% 抖动',
    },
    'insert_batch_speedup': {
        'value': 1.2, 'better': 'higher', 'unit': 'x',
        'basis': '2026-10-22 同窗交错成对实测 2.10x（best 2.26；历史 1.0–1.9x）；'
                 '取 1.2 与既有 benchmarks 门禁一致，只抓"批量不再更快"',
        'tolerance': '3 对交错取比值中位数；单对抖动可达 ±40%',
    },
    'point_inproc_qps': {
        'value': 400.0, 'better': 'higher', 'unit': 'q/s',
        'basis': '2026-10-22 quick 实测中位 1,296 q/s（best 1,409；'
                 'benchmarks 历史 588–2,150）；取 400 ≈ 实测 31%',
        'tolerance': '中位数；容忍 ~70% 抖动',
    },
    'range_scan_rows_s': {
        'value': 3000.0, 'better': 'higher', 'unit': 'rows/s',
        'basis': '2026-10-22 quick 实测中位 12,644 rows/s（best 12,669）。'
                 '该指标按全表扫描基数计：实测 WHERE 范围查询耗时随行数线性'
                 '（200 行 12ms / 2,000 行 120ms），无区间快路径；取 3,000 ≈ '
                 '实测 24%',
        'tolerance': '中位数；容忍 ~75% 抖动',
    },
    'aggregate_rows_s': {
        'value': 4000.0, 'better': 'higher', 'unit': 'rows/s',
        'basis': '2026-10-22 quick 实测中位 15,527 rows/s（best 16,303）；'
                 '取 4,000 ≈ 实测 26%',
        'tolerance': '中位数；容忍 ~75% 抖动',
    },
    'pagination_qps': {
        'value': 4.0, 'better': 'higher', 'unit': 'q/s',
        'value_full': 1.0,
        'basis': '2026-10-22 quick（1,000 行表）实测中位 15.6 q/s、full（5,000 '
                 '行表）4.25 q/s。深 OFFSET 每次全表扫描+排序，行数越大越慢，'
                 '故分档设下限：quick 4（≈实测 26%）、full 1（≈实测 24%）',
        'tolerance': '中位数；容忍 ~75% 抖动',
    },
    'tcp_point_qps': {
        'value': 200.0, 'better': 'higher', 'unit': 'q/s',
        'basis': '2026-10-22 quick 实测中位 805 q/s、full 761 q/s（历史 '
                 '516–1,033）；取 200 ≈ 实测 25%',
        'tolerance': '中位数；容忍 ~75% 抖动',
    },
    'tcp_range_qps': {
        'value': 4.0, 'better': 'higher', 'unit': 'q/s',
        'value_full': 0.8,
        'basis': '2026-10-22 quick（1,000 行表）实测中位 14.1 q/s、full'
                 '（5,000 行表，全表扫描 5 倍耗时）3.10 q/s；分档下限 '
                 'quick 4 / full 0.8（均 ≈实测 25–28%）',
        'tolerance': '中位数；容忍 ~70% 抖动',
    },
    'tcp_agg_qps': {
        'value': 4.0, 'better': 'higher', 'unit': 'q/s',
        'value_full': 1.0,
        'basis': '2026-10-22 quick 实测中位 17.1 q/s、full（5,000 行表）3.93 '
                 'q/s；分档下限 quick 4 / full 1（均 ≈实测 23–25%）',
        'tolerance': '中位数；容忍 ~75% 抖动',
    },
    'tcp_page_qps': {
        'value': 5.0, 'better': 'higher', 'unit': 'q/s',
        'value_full': 1.0,
        'basis': '2026-10-22 quick 实测中位 21.8 q/s、full（5,000 行表）4.02 '
                 'q/s；分档下限 quick 5 / full 1（均 ≈实测 23–25%）',
        'tolerance': '中位数；容忍 ~75% 抖动',
    },
    'concurrency_errors': {
        'value': 0.0, 'better': 'lower', 'unit': 'errors',
        'basis': '并发负载期间任何异常都算失败（硬判据，无容忍）；'
                 '2026-10-22 实测 1/4/8 客户端均为 0',
        'tolerance': '硬判据：必须 0',
    },
}

#: case 元信息：谁测、单位、口径、对应门禁目标（None = 只报告）
CASE_META = {
    'insert_rowwise': {
        'metric': '逐行 INSERT 吞吐（Table API，autocommit，进程内）',
        'unit': 'rows/s', 'better': 'higher', 'gate': 'insert_rowwise_rows_s',
    },
    'insert_batch': {
        'metric': '多值批量 INSERT 吞吐（500 行/语句，完整 SQL 路径）',
        'unit': 'rows/s', 'better': 'higher', 'gate': None,
        'note': '仅报告绝对值；批量是否值得看 insert_batch_speedup 的同窗比值',
    },
    'insert_batch_speedup': {
        'metric': '多值批量 vs 等价逐行语句的吞吐比（同窗交错成对）',
        'unit': 'x', 'better': 'higher', 'gate': 'insert_batch_speedup',
    },
    'point_inproc': {
        'metric': '点查吞吐（进程内 Executor.execute_sql，WHERE id=?）',
        'unit': 'q/s', 'better': 'higher', 'gate': 'point_inproc_qps',
    },
    'range_scan': {
        'metric': '范围扫描吞吐（进程内，WHERE id BETWEEN lo AND hi；按全表扫描'
                  '基数 rows/s 计，本引擎该查询无区间快路径）',
        'unit': 'rows/s', 'better': 'higher', 'gate': 'range_scan_rows_s',
    },
    'aggregate': {
        'metric': '聚合吞吐（进程内，SELECT COUNT(*), SUM(v) 全表）',
        'unit': 'rows/s', 'better': 'higher', 'gate': 'aggregate_rows_s',
    },
    'pagination': {
        'metric': '分页吞吐（进程内，ORDER BY id LIMIT 20 OFFSET n，深分页）',
        'unit': 'q/s', 'better': 'higher', 'gate': 'pagination_qps',
    },
    'tcp_point': {
        'metric': '点查吞吐（真实 MySQL 协议 + pymysql）',
        'unit': 'q/s', 'better': 'higher', 'gate': 'tcp_point_qps',
    },
    'tcp_range': {
        'metric': '范围扫描吞吐（真实 MySQL 协议）',
        'unit': 'q/s', 'better': 'higher', 'gate': 'tcp_range_qps',
    },
    'tcp_agg': {
        'metric': '聚合查询吞吐（真实 MySQL 协议）',
        'unit': 'q/s', 'better': 'higher', 'gate': 'tcp_agg_qps',
    },
    'tcp_page': {
        'metric': '分页查询吞吐（真实 MySQL 协议，深 OFFSET）',
        'unit': 'q/s', 'better': 'higher', 'gate': 'tcp_page_qps',
    },
    'concurrency_point_1': {
        'metric': '进程内并发点查总吞吐（1 客户端，固定窗口）',
        'unit': 'q/s', 'better': 'higher', 'gate': None,
        'note': '仅报告：GIL + 表级锁下并发不扩展是已量化事实，不设计数门禁',
    },
    'concurrency_point_4': {
        'metric': '进程内并发点查总吞吐（4 客户端，固定窗口）',
        'unit': 'q/s', 'better': 'higher', 'gate': None,
        'note': '仅报告：已知不扩展，只在 concurrency_errors 上设硬判据',
    },
    'concurrency_point_8': {
        'metric': '进程内并发点查总吞吐（8 客户端，固定窗口）',
        'unit': 'q/s', 'better': 'higher', 'gate': None,
        'note': '仅报告：已知不扩展，只在 concurrency_errors 上设硬判据',
    },
    'concurrency_errors': {
        'metric': '并发点查窗口内错误总数（必须 0）',
        'unit': 'errors', 'better': 'lower', 'gate': 'concurrency_errors',
    },
}


# ==================== 数据与查询计划（可复现性核心） ====================

def generate_rows(seed: int, n: int) -> list:
    """用固定 seed 生成 n 行 `[id, v]` 数据（id 从 1..n，v ∈ [1,1000]）。

    同一 (seed, n) 永远得到同一份数据；换 seed 得到不同 v 序列。
    这是 harness "可复现" 的数据来源（唯一随机源）。
    """
    rng = random.Random(int(seed))
    return [[i, rng.randrange(1, 1001)] for i in range(1, int(n) + 1)]


def dataset_signature(rows) -> str:
    """数据集指纹（SHA256），写入 JSON 便于事后核对"两次跑的是同一份数据" """
    h = hashlib.sha256()
    for r in rows:
        h.update(repr(tuple(r)).encode('utf-8'))
        h.update(b'\n')
    return h.hexdigest()


def query_plan(seed: int, nrows: int, counts: dict, window: int = 100,
               page_size: int = 20) -> dict:
    """生成确定性的查询参数（seed 是唯一随机源），供进程内/TCP 两条路径共用。

    `counts` = {'point': N, 'range': N, 'agg': N, 'page': N}，各指标查询次数
    可以不同（全表扫描类的指标用小 N 控制整套耗时）。
    """
    rng = random.Random(int(seed) * 31 + 7)
    nrows = max(1, int(nrows))
    window = max(1, min(int(window), nrows))
    page_size = max(1, min(int(page_size), nrows))
    counts = {k: max(1, int(counts.get(k, counts.get('point', 1))))
              for k in ('point', 'range', 'agg', 'page')}
    return {
        'nrows': nrows,
        'counts': counts,
        'window': window,
        'page_size': page_size,
        'point_ids': [rng.randint(1, nrows) for _ in range(counts['point'])],
        'range_lo': [rng.randint(1, max(1, nrows - window + 1))
                     for _ in range(counts['range'])],
        'page_off': [rng.randint(0, max(0, nrows - page_size))
                     for _ in range(counts['page'])],
    }


def _build_sql(plan: dict) -> dict:
    """把查询计划编译成 SQL 文本列表（在计时循环外构建，避免把字符串格式化算进测量）"""
    w, ps = plan['window'], plan['page_size']
    return {
        'point': [f'SELECT v FROM t WHERE id = {i}' for i in plan['point_ids']],
        'range': [f'SELECT id, v FROM t WHERE id BETWEEN {lo} AND {lo + w - 1}'
                  for lo in plan['range_lo']],
        'agg': ['SELECT COUNT(*), SUM(v) FROM t' for _ in range(plan['counts']['agg'])],
        'page': [f'SELECT id, v FROM t ORDER BY id LIMIT {ps} OFFSET {off}'
                 for off in plan['page_off']],
    }


# ==================== 通用小工具 ====================

def _summarize(samples, better='higher', warmup=0, **extra) -> dict:
    """样本 -> median/best/worst/min/max；warmup>0 时丢弃最前面的样本。"""
    kept = samples[warmup:] if warmup and len(samples) > warmup else samples[:]
    vals = sorted(float(s['value']) for s in kept)
    if not vals:                                    # pragma: no cover
        vals = [0.0]
    import statistics
    median = statistics.median(vals)
    out = {
        'samples': list(samples),
        'n_samples': len(vals),
        'warmup_dropped': max(0, len(samples) - len(kept)),
        'median': median,
        'best': vals[-1] if better == 'higher' else vals[0],
        'worst': vals[0] if better == 'higher' else vals[-1],
        'min': vals[0], 'max': vals[-1],
        'spread_ratio': ((vals[-1] - vals[0]) / median) if median else None,
        'better': better,
    }
    out.update(extra)
    return out


def _root_password_for(data_dir: str) -> str:
    """取本数据目录的 root 口令（P0-A 安全默认值之后新库会生成随机口令）。

    * 新数据目录：`Database()` 初始化时生成随机 root 口令并写入
      `<data_dir>/root.password`（见 `lightdb/db/database.py`），这里读回；
    * 旧数据目录 / pytest conftest 设了 `LIGHTDB_ALLOW_EMPTY_PASSWORD=1`
      的降级环境：没有口令文件，回退空口令。
    口令只用于本机 pymysql 连接，**绝不写入 JSON / stdout**。
    """
    try:
        from lightdb.db.database import ROOT_PASSWORD_FILE
        path = os.path.join(data_dir, ROOT_PASSWORD_FILE)
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except Exception:                                # pragma: no cover
        pass
    return os.environ.get('LIGHTDB_ROOT_PASSWORD', '')


def _resolve_params(suite: str, scale: float) -> dict:
    """按 --scale 缩小档位参数（仅自检/单测用；门禁仍按绝对阈值判定）。"""
    base = dict(SUITE_PARAMS[suite])
    scale = float(scale)
    if scale <= 0:
        raise ValueError('--scale 必须 > 0')
    if scale == 1.0:
        return base
    scaled = dict(base)
    for key in ('rows', 'insert_rows', 'batch_rows', 'speedup_rows'):
        scaled[key] = max(20, int(base[key] * scale))
    for key in ('nq', 'tcp_nq'):
        scaled[key] = {m: max(1 if key == 'tcp_nq' else 5, int(v * scale))
                       for m, v in base[key].items()}
    for key in ('warmup_queries', 'tcp_warmup'):
        scaled[key] = max(1, int(base[key] * scale))
    scaled['samples'] = 1
    scaled['warmup_rounds'] = 1
    scaled['concurrency_seconds'] = max(0.1, base['concurrency_seconds'] * scale)
    scaled['concurrency_warmup'] = max(0.02,
                                       base['concurrency_warmup'] * scale)
    scaled['scale'] = scale
    return scaled


# ==================== 测量：INSERT（复用 benchmarks.heap_insert） ====================

def _measure_insert(params: dict, data_dir: str) -> dict:
    """逐行 INSERT 与多值批量 INSERT（各跑 warmup_rounds + samples 轮）"""
    n_row = params['insert_rows']
    n_batch = params['batch_rows']
    rounds = params['warmup_rounds'] + params['samples']

    rowwise = []
    dd_row = os.path.join(data_dir, 'insert_rowwise')
    for _ in range(rounds):
        r = H.rowwise_round(n_row, 'table', dd_row)
        rowwise.append({'value': r['rows_s'], 'secs': r['secs'],
                        'n': n_row, 'rows': n_row})

    batch = []
    dd_batch = os.path.join(data_dir, 'insert_batch')
    for _ in range(rounds):
        r = H.batch_round(n_batch, BATCH, dd_batch)
        batch.append({'value': r['rows_s'], 'secs': r['secs'],
                      'n': n_batch, 'rows': n_batch})

    sig = dataset_signature([[i, i % 1000] for i in range(1, n_row + 1)])
    cases = {}
    cases['insert_rowwise'] = _summarize(
        rowwise, better='higher', warmup=params['warmup_rounds'],
        metric=CASE_META['insert_rowwise']['metric'], unit='rows/s',
        n=n_row, rows=n_row, gate_target='insert_rowwise_rows_s',
        data_signature=sig, note='数据为 benchmarks.heap_insert 确定性序列 v=i%%1000')
    cases['insert_batch'] = _summarize(
        batch, better='higher', warmup=params['warmup_rounds'],
        metric=CASE_META['insert_batch']['metric'], unit='rows/s',
        n=n_batch, rows=n_batch, gate_target=None,
        data_signature=sig, note='500 行/语句；绝对值仅报告')
    return cases


def _measure_batch_speedup(params: dict, data_dir: str) -> dict:
    """同窗交错成对的"多值批量 / 逐行语句"吞吐比（唯一站得住的批量判据）"""
    n = params['speedup_rows']
    dd = os.path.join(data_dir, 'insert_speedup')
    r = H.batch_vs_individual_round(n=n, batch=BATCH, data_dir=dd)
    samples = []
    for p in r['pairs']:
        samples.append({'value': p['ratio'], 'secs': None,
                        'n': n, 'batch': p['batch_rows_s'],
                        'individual': p['individual_rows_s']})
    return {'insert_batch_speedup': _summarize(
        samples, better='higher', warmup=0,
        metric=CASE_META['insert_batch_speedup']['metric'], unit='x',
        n=n, rows=n, gate_target='insert_batch_speedup',
        data_signature=dataset_signature([[i, i % 1000] for i in range(1, n + 1)]),
        detail={'batch_rows_s_median': r['batch_rows_s'],
                'individual_rows_s_median': r['individual_rows_s']},
        note='3 对交错取比值中位数；两侧都在全新数据目录上跑')}


# ==================== 测量：进程内查询（点查/范围/聚合/分页） ====================

def _select_dataset(rows, data_dir: str):
    """建表并灌入 seed 数据，返回 (db, executor)。调用方负责 shutdown。"""
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    dd = H.fresh_dir(data_dir)
    db = Database(dd)
    tbl = db.create_table('t', COLS)
    for r in rows:
        tbl.insert(r)
    return db, Executor(db)


def _execute_one(ex, sql: str):
    r = ex.execute_sql(sql)
    if isinstance(r, dict) and r.get('type') == 'error':
        raise RuntimeError(f'查询失败: {sql!r}: {r}')
    return r


def _measure_inproc_queries(params: dict, rows, data_dir: str) -> dict:
    """一轮建表 + warmup，然后每个指标各取 params['samples'] 轮样本。

    SELECT-only 无状态变化，所以**复用同一份已 warmup 的数据**；
    易变指标（INSERT）才每轮重建。

    口径：
      * point / page  -> q/s（查询数/秒）
      * range / agg   -> rows/s。本引擎对 WHERE 范围查询与聚合都是**全表扫描**
        （实测查询耗时随行数线性增长），所以 range 的 rows/s 按"每次查询
        扫描的行数 = 表行数"计，agg 同理（COUNT/SUM 扫全表）。
    """
    plan = query_plan(params['seed'], params['rows'], params['nq'],
                      window=params['window'], page_size=params['page_size'])
    sqls = _build_sql(plan)
    nrows = params['rows']
    per_metric = {
        'point': {'rows_per_q': 1, 'unit': 'q/s'},
        'range': {'rows_per_q': nrows, 'unit': 'rows/s'},
        'agg': {'rows_per_q': nrows, 'unit': 'rows/s'},
        'page': {'rows_per_q': plan['page_size'], 'unit': 'q/s'},
    }
    db, ex = _select_dataset(rows, os.path.join(data_dir, 'queries'))
    out = {}
    try:
        # warmup：四个指标各跑 params['warmup_queries'] 次
        for m in ('point', 'range', 'agg', 'page'):
            lst = sqls[m]
            for i in range(params['warmup_queries']):
                _execute_one(ex, lst[i % len(lst)])
        # 有效样本
        timings = {m: [] for m in per_metric}
        for _ in range(params['samples']):
            for m, info in per_metric.items():
                lst = sqls[m]
                t0 = time.perf_counter()
                for sql in lst:
                    _execute_one(ex, sql)
                secs = time.perf_counter() - t0
                n = len(lst)
                total_rows = n * info['rows_per_q']
                value = (n / secs) if info['unit'] == 'q/s' else (total_rows / secs)
                timings[m].append({'value': value, 'secs': secs,
                                   'n': n, 'rows': total_rows})
    finally:
        db.shutdown()
    sig = dataset_signature(rows)
    names = {'point': 'point_inproc', 'range': 'range_scan',
             'agg': 'aggregate', 'page': 'pagination'}
    for m, info in per_metric.items():
        meta = CASE_META[names[m]]
        out[names[m]] = _summarize(
            timings[m], better='higher', warmup=0,
            metric=meta['metric'], unit=info['unit'],
            n=plan['counts'][m], rows=info['rows_per_q'],
            gate_target=meta['gate'], data_signature=sig,
            note=f'表行数={nrows}；每次查询扫描 {info["rows_per_q"]} 行')
    return out


# ==================== 测量：TCP（真实 MySQL 协议） ====================

def _measure_tcp_queries(params: dict, rows, data_dir: str) -> dict:
    """起一个进程内 MySQLServer（127.0.0.1 + 动态空闲端口），pymysql 实测四类查询。"""
    import pymysql

    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    nrows = len(rows)
    plan = query_plan(params['seed'], nrows, params['tcp_nq'],
                      window=params['window'], page_size=params['page_size'])
    sqls = _build_sql(plan)
    per_metric = {
        'point': {'rows_per_q': 1, 'name': 'tcp_point'},
        'range': {'rows_per_q': nrows, 'name': 'tcp_range'},
        'agg': {'rows_per_q': nrows, 'name': 'tcp_agg'},
        'page': {'rows_per_q': plan['page_size'], 'name': 'tcp_page'},
    }
    dd = os.path.join(data_dir, 'tcp')
    H.fresh_dir(dd)
    seed_db = Database(dd)
    tbl = seed_db.create_table('t', COLS)
    for r in rows:
        tbl.insert(r)
    seed_db.shutdown()
    password = _root_password_for(dd)

    # 端口 0 = 由操作系统分配空闲端口（避免"先探测再 bind"的竞态）
    cfg = Config()
    cfg.host, cfg.port = '127.0.0.1', 0
    db = Database(dd)
    ex = Executor(db)
    srv = MySQLServer(cfg, db, ex)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    time.sleep(0.5)
    conn = pymysql.connect(host='127.0.0.1', port=port, user='root',
                           password=password, autocommit=True,
                           connect_timeout=5)
    cur = conn.cursor()
    timings = {m: [] for m in per_metric}
    out = {}
    try:
        def q(sql):
            cur.execute(sql)
            cur.fetchall()

        for m in ('point', 'range', 'agg', 'page'):
            lst = sqls[m]
            for i in range(params['tcp_warmup']):
                q(lst[i % len(lst)])
        for _ in range(params['samples']):
            for m, info in per_metric.items():
                lst = sqls[m]
                t0 = time.perf_counter()
                for sql in lst:
                    q(sql)
                secs = time.perf_counter() - t0
                timings[m].append({'value': len(lst) / secs, 'secs': secs,
                                   'n': len(lst),
                                   'rows': len(lst) * info['rows_per_q']})
        sig = dataset_signature(rows)
        for m, info in per_metric.items():
            name = info['name']
            meta = CASE_META[name]
            out[name] = _summarize(
                timings[m], better='higher', warmup=0,
                metric=meta['metric'], unit='q/s',
                n=plan['counts'][m], rows=info['rows_per_q'],
                gate_target=meta['gate'], data_signature=sig,
                port=port, note=f'127.0.0.1:{port}；表行数={nrows}')
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        try:
            db.shutdown()
        except Exception:
            pass
    return out


# ==================== 测量：并发 1/4/8 客户端 ====================

def _measure_concurrency(params: dict, data_dir: str) -> dict:
    out = {}
    errors = []
    for clients in (1, 4, 8):
        dd = os.path.join(data_dir, f'conc_{clients}')
        r = C.concurrency_round(kind='point', clients=clients,
                                seconds=params['concurrency_seconds'],
                                nrows=params['rows'],
                                warmup=params['concurrency_warmup'],
                                data_dir=dd)
        errors.extend(r.get('errors') or [])
        name = f'concurrency_point_{clients}'
        meta = CASE_META[name]
        out[name] = _summarize(
            [{'value': r['q_s'], 'secs': r['wall_s'], 'n': r['nqueries'],
              'rows': None}],
            better='higher', warmup=0,
            metric=meta['metric'], unit='q/s',
            n=params['rows'], rows=None, gate_target=None,
            clients=clients, p50_ms=r.get('p50_ms'),
            p95_ms=r.get('p95_ms'), scaling_ratio=r.get('scaling_ratio'),
            note='仅报告；GIL + 表级锁下并发不扩展是已量化事实')
    out['concurrency_errors'] = {
        'metric': CASE_META['concurrency_errors']['metric'],
        'unit': 'errors', 'better': 'lower',
        'samples': [{'value': float(len(errors)), 'secs': None,
                     'n': 3, 'rows': None}],
        'n_samples': 1, 'warmup_dropped': 0,
        'median': float(len(errors)), 'best': float(len(errors)),
        'worst': float(len(errors)), 'min': float(len(errors)),
        'max': float(len(errors)), 'spread_ratio': None,
        'n': 3, 'rows': None, 'gate_target': 'concurrency_errors',
        'errors': errors[:5],
        'note': '1/4/8 客户端窗口内异常总数；硬判据必须 0',
    }
    return out


# ==================== 门禁 ====================

def resolve_targets(target_scale: float = 1.0, overrides=None,
                    suite: str = 'quick') -> dict:
    """复制一份 TARGETS 并解析为**当前档位**的有效阈值。

    * `suite='full'` 且 spec 带 `value_full` 时用 full 标定值（行数更大、
      全表扫描类指标更慢，见各 spec 的 basis）；
    * 应用 `--target` 覆盖与 `--target-scale`（自检用）。
    """
    import copy
    targets = copy.deepcopy(TARGETS)
    for name, spec in targets.items():
        if suite == 'full' and 'value_full' in spec:
            spec['value'] = spec['value_full']
    for name, val in (overrides or {}).items():
        if name not in targets:
            raise KeyError(f'未知门禁目标 {name!r}；可选: {sorted(targets)}')
        targets[name]['value'] = float(val)
    scale = float(target_scale)
    if scale != 1.0:
        for spec in targets.values():
            spec['value'] = float(spec['value']) * scale
    return targets


def evaluate_gate(measurements: dict, targets: dict = None) -> dict:
    """按 case->目标映射判定门禁。

    `measurements` = {case_name: headline_value}（即各 case 的 median）。
    返回 {passed, failed, checks:[{case,target,value,threshold,better,ok,basis}]}。
    """
    targets = targets or TARGETS
    checks, failed = [], []
    for case_name, meta in CASE_META.items():
        tname = meta.get('gate')
        if not tname:
            continue
        spec = targets[tname]
        value = measurements.get(case_name)
        ok = False
        if value is not None:
            if spec['better'] == 'higher':
                ok = float(value) >= float(spec['value'])
            else:
                ok = float(value) <= float(spec['value'])
        checks.append({
            'case': case_name, 'target': tname,
            'value': (None if value is None else float(value)),
            'threshold': float(spec['value']),
            'better': spec['better'], 'unit': spec.get('unit'),
            'ok': bool(ok), 'basis': spec.get('basis'),
            'tolerance': spec.get('tolerance'),
        })
        if not ok:
            failed.append(tname)
    return {'passed': not failed, 'failed': failed,
            'n_checks': len(checks), 'n_passed': len(checks) - len(failed),
            'checks': checks}


# ==================== 参考库检测（无 MySQL 时明确"仅基线"） ====================

def detect_reference_db(host: str = None, port: int = None) -> dict:
    """探测可比对的 MySQL。**检测不到就明确"仅 LightDB 基线"，绝不假横比。**"""
    host = host or os.environ.get('LIGHTDB_COMPETITIVE_MYSQL_HOST') or None
    if not host:
        return {
            'kind': 'mysql', 'available': False, 'compared': False,
            'note': '仅 LightDB 基线：未提供 MySQL 目标（--mysql-host / '
                    'LIGHTDB_COMPETITIVE_MYSQL_HOST），不做跨库横比',
        }
    try:
        port = int(port or os.environ.get('LIGHTDB_COMPETITIVE_MYSQL_PORT', 3306))
        import pymysql
        conn = pymysql.connect(host=host, port=port,
                               user=os.environ.get(
                                   'LIGHTDB_COMPETITIVE_MYSQL_USER', 'root'),
                               password=os.environ.get(
                                   'LIGHTDB_COMPETITIVE_MYSQL_PASSWORD', ''),
                               connect_timeout=1.5)
        try:
            version = conn.get_server_info()
        finally:
            conn.close()
        return {
            'kind': 'mysql', 'available': True, 'compared': False,
            'host': host, 'port': port, 'server_version': version,
            'note': '检测到 MySQL，但本 harness 未启用跨库横比（需要统一数据/'
                    '查询口径后另行实现）；本报告仅 LightDB 基线数字',
        }
    except Exception as e:                          # noqa: BLE001
        return {
            'kind': 'mysql', 'available': False, 'compared': False,
            'host': host, 'port': port,
            'note': f'仅 LightDB 基线：MySQL 连接失败（{type(e).__name__}），'
                    '不做跨库横比',
        }


# ==================== 汇总 ====================

def _lightdb_version() -> str:
    try:
        import lightdb
        return str(getattr(lightdb, '__version__', 'unknown'))
    except Exception:                                # pragma: no cover
        return 'unknown'


def build_payload(suite: str, params: dict, cases: dict, gate_result,
                  reference_db: dict, timings: dict, targets: dict) -> dict:
    seed = int(params['seed'])
    rows = generate_rows(seed, params['rows'])
    payload = {
        'schema': SCHEMA,
        'harness_version': HARNESS_VERSION,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'generated_ts': time.time(),
        'suite': suite,
        'seed': seed,
        'scale': params.get('scale', 1.0),
        'lightdb_version': _lightdb_version(),
        'python_version': sys.version.split()[0],
        'git_rev': git_rev(),
        'git_dirty': git_dirty(),
        'environment': machine_info(),
        'method': {
            'baseline_only': not reference_db.get('compared', False),
            'warmup': 'INSERT 类：丢弃第 1 轮；查询类：每指标先跑 warmup_queries 次',
            'samples': params['samples'],
            'statistic': '每项报告 median（主判据）与 best（最优）、worst、min、max',
            'seed_source': '查询/分页数据由固定 seed 生成；INSERT 指标用 '
                           'benchmarks.heap_insert 的 v=i%%1000 确定性数据',
            'dataset_signature_sha256': dataset_signature(rows),
            'fresh_state': 'INSERT/并发类每轮删库重建；查询类建一次表后复用（只读）',
            'timing': 'time.perf_counter() 只包住被测语句循环',
            'env_isolation': 'LIGHTDB_AUDIT_LEVEL=NONE、LIGHTDB_LOG_LEVEL=ERROR',
            'load_warning': '绝对吞吐受机器负载影响；跨版本对照必须在同一时间窗'
                            '内交错成对测量，顺序式 before/after 不可信',
            'scale_note': '--scale<1 仅用于 harness 自检/单测，会缩小数据量与轮数；'
                          '门禁仍按绝对阈值判定',
        },
        'reference_db': reference_db,
        'targets': targets,
        'params': {k: v for k, v in params.items() if k != 'seed'},
        'cases': cases,
        'gate': gate_result,
        'total_wall_s': timings.get('total'),
        'case_wall_s': {k: v for k, v in timings.items() if k != 'total'},
    }
    return payload


def run_harness(suite: str = 'quick', seed: int = DEFAULT_SEED,
                scale: float = 1.0, gate: bool = False,
                target_scale: float = 1.0, target_overrides: dict = None,
                data_dir: str = None, out: str = None,
                keep_data: bool = False, mysql_host: str = None,
                mysql_port: int = None) -> tuple:
    """跑完整 harness，返回 (payload, exit_code)。

    这是 tests 与 CLI 共用的单一入口（避免测试复制测量逻辑）。
    """
    if suite not in SUITES:
        raise ValueError(f'未知 suite {suite!r}（可选 {SUITES}）')
    params = _resolve_params(suite, scale)
    params['seed'] = int(seed)
    data_dir = data_dir or os.path.join(DEFAULT_OUT_DIR, 'data')
    H.prepare_env('default')
    rows = generate_rows(seed, params['rows'])
    cases, timings = {}, {}
    t_all = time.perf_counter()
    try:
        t0 = time.perf_counter()
        cases.update(_measure_insert(params, data_dir))
        timings['insert'] = time.perf_counter() - t0
        t0 = time.perf_counter()
        cases.update(_measure_batch_speedup(params, data_dir))
        timings['insert_batch_speedup'] = time.perf_counter() - t0
        t0 = time.perf_counter()
        cases.update(_measure_inproc_queries(params, rows, data_dir))
        timings['queries_inproc'] = time.perf_counter() - t0
        t0 = time.perf_counter()
        cases.update(_measure_tcp_queries(params, rows, data_dir))
        timings['queries_tcp'] = time.perf_counter() - t0
        t0 = time.perf_counter()
        cases.update(_measure_concurrency(params, data_dir))
        timings['concurrency'] = time.perf_counter() - t0
    finally:
        timings['total'] = time.perf_counter() - t_all
        if not keep_data:
            shutil.rmtree(data_dir, ignore_errors=True)

    reference_db = detect_reference_db(mysql_host, mysql_port)
    measurements = {name: c.get('median') for name, c in cases.items()}
    targets = resolve_targets(target_scale, target_overrides, suite=suite)
    gate_result = evaluate_gate(measurements, targets) if gate else None
    payload = build_payload(suite, params, cases, gate_result, reference_db,
                            timings, targets)
    if out:
        out_dir = os.path.dirname(os.path.abspath(out))
        os.makedirs(out_dir, exist_ok=True)
        tmp = out + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, out)
        payload['_out'] = os.path.abspath(out)
    rc = 0
    if gate and not gate_result['passed']:
        rc = 1
    return payload, rc


def format_summary(payload: dict) -> str:
    """人读摘要（全部 ASCII 标记，避免 GBK 控制台编码问题）"""
    lines = []
    lines.append('LightDB 统一 benchmark harness · suite=%s · seed=%s · %s'
                 % (payload['suite'], payload['seed'],
                    payload['generated_at']))
    env = payload['environment']
    lines.append('%s · %s · %s 逻辑核 · git %s%s'
                 % (payload.get('python_version'), env.get('platform'),
                    env.get('cpu_count'), (payload.get('git_rev') or '?')[:8],
                    '（工作树有未提交改动）' if payload.get('git_dirty') else ''))
    ref = payload['reference_db']
    lines.append('参考库: ' + ref['note'])
    lines.append('-' * 78)
    for name, c in payload['cases'].items():
        med = c.get('median')
        unit = c.get('unit', '')
        if med is None:
            lines.append('  %-24s N/A' % name)
            continue
        lines.append('  %-24s %12.3f %-7s (median, n=%d, best=%.3f)'
                     % (name, med, unit, c.get('n_samples', 0),
                        c.get('best') if c.get('best') is not None else med))
    lines.append('-' * 78)
    g = payload.get('gate')
    if g:
        verdict = 'PASS' if g['passed'] else 'FAIL'
        lines.append('门禁结果: %s （%d/%d 项达标）'
                     % (verdict, g['n_passed'], g['n_checks']))
        for chk in g['checks']:
            lines.append('  [%s] %-26s %10.3f %-6s %s %10.3f'
                         % ('PASS' if chk['ok'] else 'FAIL', chk['case'],
                            chk['value'] if chk['value'] is not None else -1.0,
                            chk['better'], chk['unit'] or '',
                            chk['threshold']))
    lines.append('总墙钟: %.2f s' % (payload.get('total_wall_s') or 0.0))
    if payload.get('_out'):
        lines.append('JSON: %s' % payload['_out'])
    return '\n'.join(lines)


# ==================== CLI ====================

def _parse_target_overrides(items) -> dict:
    overrides = {}
    for item in items or []:
        if '=' not in item:
            raise ValueError(f'--target 需要 NAME=VALUE 形式，收到 {item!r}')
        k, v = item.split('=', 1)
        overrides[k.strip()] = float(v)
    return overrides


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog='python -m benchmarks.competitive_harness',
        description='LightDB 统一竞品/基线 harness（P5）：in-process + TCP 基线、'
                    'JSON 输出、保守回归门禁')
    ap.add_argument('--suite', default='quick', choices=SUITES)
    ap.add_argument('--seed', type=int, default=DEFAULT_SEED,
                    help='数据随机种子（固定 seed 保证可复现，默认 %(default)s）')
    ap.add_argument('--scale', type=float, default=1.0,
                    help='缩小数据量/轮数（仅自检/单测用，默认 1.0）')
    ap.add_argument('--out', default=None,
                    help='JSON 输出路径（默认 _analysis_tmp/bench/harness_<suite>.json）')
    ap.add_argument('--data-dir', default=None,
                    help='临时数据目录（默认 _analysis_tmp/bench/data，跑完删除）')
    ap.add_argument('--gate', action='store_true',
                    help='按 TARGETS 判定；未达标退出码 1')
    ap.add_argument('--target-scale', type=float, default=1.0,
                    help='自检用：全部阈值乘该系数（100 = 必红，0.001 = 必绿）')
    ap.add_argument('--target', action='append', default=[], metavar='NAME=VALUE',
                    help='自检用：逐项覆盖阈值，可重复')
    ap.add_argument('--json-only', action='store_true', help='只输出 JSON')
    ap.add_argument('--keep-data', action='store_true', help='保留临时数据目录')
    ap.add_argument('--mysql-host', default=None,
                    help='可选：探测该 MySQL 主机以标注参考库（不启用横比）')
    ap.add_argument('--mysql-port', type=int, default=None)
    args = ap.parse_args(argv)
    _utf8_streams()

    out = args.out
    if out is None:
        out = os.path.join(DEFAULT_OUT_DIR, 'harness_%s.json' % args.suite)
    overrides = {}
    try:
        overrides = _parse_target_overrides(args.target)
        payload, rc = run_harness(
            suite=args.suite, seed=args.seed, scale=args.scale,
            gate=args.gate, target_scale=args.target_scale,
            target_overrides=overrides, data_dir=args.data_dir, out=out,
            keep_data=args.keep_data, mysql_host=args.mysql_host,
            mysql_port=args.mysql_port)
    except KeyError as e:
        _safe_write(f'参数错误: {e}', sys.stderr)
        return 2
    except ValueError as e:
        _safe_write(f'参数错误: {e}', sys.stderr)
        return 2
    except Exception as e:                          # noqa: BLE001
        _safe_write(f'测量失败: {type(e).__name__}: {e}', sys.stderr)
        return 1

    if args.json_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_summary(payload))
    return rc


if __name__ == '__main__':
    sys.exit(main())
