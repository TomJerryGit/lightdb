"""性能门禁的 CI 入口（阶段 2 第 5 项）。

**默认全部跳过**：只有显式打开开关才会跑性能测量。

    LIGHTDB_RUN_BENCHMARKS=1 python -m pytest tests/test_benchmarks.py -q

为什么默认跳过：

* 性能与机器负载强相关（本机吞吐在测量窗口内单调下滑，同代码 20k 档
  90 分钟内 1,750 → 820 rows/s），跑进日常套件只会把"环境抖动"
  报成"实现回退"；
* 它要跑 ~11 s 的真实插入/崩溃恢复，日常套件不需要为它变慢。

打开开关后跑的是**与 `python -m benchmarks.run --suite quick` 逐字相同**
的小幅快速档（2,000 行），断言来自 `benchmarks/gate.py` 的目标表。
门禁阈值是"不回退"下限，不是性能承诺；依据见 `benchmarks/README.md`。

环境变量：

* `LIGHTDB_RUN_BENCHMARKS` —— 除 `''` / `0` / `false` 外都算打开
* `LIGHTDB_BENCH_DATA_DIR` —— 数据目录（默认 `benchmarks/data_bench`）
* `LIGHTDB_BENCH_MAX_WALL`  —— 整个 quick 档的墙上时间上限，默认 180 s
  （CI 上比"被测指标"更容易先撞到的是**总时长**，所以这里给一个
  宽松但存在的上界，避免挂死时没有反馈；本机实测 ~20 s）
* `LIGHTDB_BENCH_RETRIES`   —— 门禁未达标时"只重测未达标项"的次数，
  默认 1（与 `benchmarks.run --gate --retries` 同语义；0 = 不重试）
"""
import os
import time

import pytest

from benchmarks import gate as gate_mod
from benchmarks import heap_insert as H

_SWITCH = os.environ.get('LIGHTDB_RUN_BENCHMARKS', '')
ENABLED = _SWITCH.strip().lower() not in ('', '0', 'false', 'no', 'off')

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason='性能门禁默认跳过；设置 LIGHTDB_RUN_BENCHMARKS=1 打开'
           '（见 benchmarks/README.md 与 tests/test_benchmarks.py 文件头）')

_MAX_WALL = float(os.environ.get('LIGHTDB_BENCH_MAX_WALL', '180'))
_DATA_DIR = os.environ.get('LIGHTDB_BENCH_DATA_DIR') or H.DEFAULT_DATA_DIR
#: 门禁未达标时只重测未达标项的次数（与 `benchmarks.run --gate --retries`
#: 同一语义：干扰是单向的，只会让数字更差；真回退两次都差）
_RETRY_ATTEMPTS = int(os.environ.get('LIGHTDB_BENCH_RETRIES', '1'))

_RESULTS = {}
_WALL = {}
_RETRIES = {}


@pytest.fixture(scope='module', autouse=True)
def _quick_suite():
    """整个模块只跑一次 quick 档，后面每个用例只做断言（不重复测量）

    若首轮门禁未达标，**只重测未达标项**一次并取更有利的一次
    （与 `benchmarks.run --gate --retries 1` 同一语义：干扰是单向的，
    只会让数字更差；真回退两次都差）。重试次数与每次的原始值都记在
    `_RESULTS`/`_RETRIES` 里，断言失败时打得出来。
    """
    if not ENABLED:
        yield
        return
    from benchmarks import cases
    t0 = time.perf_counter()
    out = cases.run_quick_cases(data_dir=_DATA_DIR)
    _WALL['total'] = time.perf_counter() - t0
    results = {k: v for k, v in out.items() if not k.startswith('_')}
    _WALL['cases'] = out.get('_timings', {})

    g = gate_mod.evaluate(results, gate_mod.TARGETS)
    attempts = [results]
    while not g['passed'] and len(attempts) <= _RETRY_ATTEMPTS:
        redo = gate_mod.cases_for_targets(results, g['failed'])
        if not redo:
            break
        print(f"\n[bench] 门禁未达标 {g['failed']} -> 只重测 {redo} 一次")
        again = cases.run_quick_cases(data_dir=_DATA_DIR, only=redo)
        again.pop('_timings', None)
        attempts.append(again)
        results, picks = gate_mod.merge_best(attempts, gate_mod.TARGETS)
        for name, idx in picks.items():
            if idx:
                print(f'[bench] {name}: 采用第 {idx + 1} 次尝试')
        g = gate_mod.evaluate(results, gate_mod.TARGETS)

    _RESULTS.update(results)
    _RETRIES['attempts'] = len(attempts)
    _RETRIES['raw'] = attempts
    print(f"\n[bench] quick 档墙上时间 {_WALL['total']:.1f} s（上限 "
          f"{_MAX_WALL:.0f} s，尝试 {len(attempts)} 次）；逐项: "
          + ', '.join(f'{k}={v:.1f}s' for k, v in _WALL['cases'].items()))
    H.cleanup_dir(_DATA_DIR)
    yield
    H.cleanup_dir(_DATA_DIR)


def _gate():
    """用 `benchmarks/gate.py` 的**默认**目标表判定本次测量"""
    assert _RESULTS, 'quick 档没有产出任何测量值'
    return gate_mod.evaluate(_RESULTS, gate_mod.TARGETS)


def test_quick_suite_wall_time_is_bounded():
    """整套 quick 档必须在 `LIGHTDB_BENCH_MAX_WALL` 秒内跑完（默认 180 s）"""
    assert _WALL.get('total', 0) > 0, 'quick 档似乎没跑'
    assert _WALL['total'] <= _MAX_WALL, (
        f"quick 档耗时 {_WALL['total']:.1f}s 超过上限 {_MAX_WALL:.0f}s；"
        f'逐项: {_WALL.get("cases")}')


def test_every_gate_metric_present():
    """每个门禁目标都必须真的有测量值（跑漏了要报出来，而不是静默通过）"""
    g = _gate()
    missing = [c['target'] for c in g['checks'] if c['value'] is None]
    assert not missing, f'这些门禁目标没有测量值: {missing}'


def test_insert_rowwise_throughput_meets_gate():
    """逐行 INSERT 吞吐 >= 1,000 rows/s（Table API，2,000 行档）"""
    g = _gate()
    c = _find(g, 'insert_rowwise_table')
    assert c['passed'], _msg(c)


def test_batch_multi_value_speedup_meets_gate():
    """多值批量 INSERT >= 1.5x 逐行（同档成对：2,000 行档）"""
    g = _gate()
    c = _find(g, 'insert_batch_speedup')
    assert c['passed'], _msg(c)


def test_point_query_throughput_meets_gate():
    """进程内点查 >= 600 q/s（2,000 行表）"""
    g = _gate()
    c = _find(g, 'point_inproc')
    assert c['passed'], _msg(c)


def test_wal_stays_bounded():
    """写 N 行期间 WAL 峰值 <= 32 MB（checkpoint 走引擎默认阈值）"""
    g = _gate()
    c = _find(g, 'wal_bounded')
    assert c['passed'], _msg(c)


def test_crash_recovery_is_fast():
    """崩溃恢复 < 5 s（硬杀子进程 + 未提交事务）"""
    g = _gate()
    c = _find(g, 'crash_recovery')
    assert c['passed'], _msg(c)


def test_crash_recovery_loses_no_committed_row():
    """硬杀后**已提交**的行一行都不能少（数据丢失硬判据）"""
    r = _RESULTS.get('recovery_missing_rows')
    assert r is not None, 'recovery_missing_rows 没有测量值'
    assert r['metric_median'] == 0, (
        f"崩溃恢复丢失了 {r['metric_median']:.0f} 行已提交数据"
        f"（detail: {r.get('detail')}）")


def test_crash_recovery_leaks_no_uncommitted_row():
    """硬杀后**未提交**的行一行都不能泄漏成可见行（隔离性硬判据）"""
    r = _RESULTS.get('recovery_leaked_rows')
    assert r is not None, 'recovery_leaked_rows 没有测量值'
    assert r['metric_median'] == 0, (
        f"崩溃恢复泄漏了 {r['metric_median']:.0f} 行未提交数据"
        f"（detail: {r.get('detail')}）")


def test_whole_gate_passes():
    """汇总：`benchmarks.run --gate` 用的同一套判定必须整体通过"""
    g = _gate()
    assert g['passed'], (
        f"门禁 FAIL（{g['n_failed']}/{g['n_checks']} 项不达标）: "
        + '; '.join(c['reason'] for c in g['checks'] if not c['passed']))


def test_gate_self_check_detects_impossible_targets():
    """门禁本身的自检：阈值放到不可能达标时必须判 FAIL

    这条用例**不依赖机器性能**：它证明"门禁不是恒真的橡皮图章"。
    """
    g = gate_mod.evaluate(_RESULTS, gate_mod.override_targets(scale=1e6))
    assert not g['passed'], '把全部阈值放大 1e6 倍后门禁竟然还 PASS'
    g2 = gate_mod.evaluate(_RESULTS, gate_mod.override_targets(
        overrides={'insert_rowwise_table': 1e9}))
    assert not g2['passed'], '把逐行 INSERT 阈值设成 1e9 后门禁竟然还 PASS'


def test_retry_is_bounded_and_cannot_wash_out_a_real_regression(monkeypatch):
    """重试语义的自检（不依赖机器性能）：

    * 干扰是**单向**的 -> 一次差、一次好 => 按好的算（重试有意义）；
    * 真回退是**持续**的 -> 两次都差 => 仍然 FAIL（重试洗不白）；
    * 重试只针对未达标项，且次数固定（默认 1 次）。
    """
    from benchmarks import cases as _cases

    low = {'insert_rowwise_table_2000': {'metric_median': 10.0, 'min': 10.0,
                                         'max': 10.0, 'n_samples': 1,
                                         'samples': []}}
    high = {'insert_rowwise_table_2000': {'metric_median': 99_999.0,
                                          'min': 99_999.0, 'max': 99_999.0,
                                          'n_samples': 1, 'samples': []}}

    # 一次差一次好 -> 合并后按好的算，且只重测未达标项
    g1 = gate_mod.evaluate(low, gate_mod.TARGETS)
    assert not g1['passed']
    assert gate_mod.cases_for_targets(low, g1['failed']) == \
        ['insert_rowwise_table_2000'], '重试范围应该只有未达标项'
    merged, _picks = gate_mod.merge_best([low, high], gate_mod.TARGETS)
    assert gate_mod.evaluate(merged, gate_mod.TARGETS)['passed']

    # 两次都差 -> 仍然 FAIL
    merged2, _ = gate_mod.merge_best([low, low], gate_mod.TARGETS)
    assert not gate_mod.evaluate(merged2, gate_mod.TARGETS)['passed'], \
        '持续回退被重试洗白了'

    # 真跑一次"只重测未达标项"的路径，确认它是按 case 粒度重跑的
    calls = []

    def fake_run_case(name, data_dir=None):
        calls.append(name)
        return dict(high), {'case': name, 'wall_s': 0.0}

    monkeypatch.setattr(_cases, 'run_case', fake_run_case)
    again = _cases.run_cases(['insert_rowwise_table_2000'])
    assert calls == ['insert_rowwise_table_2000']
    assert again['insert_rowwise_table_2000']['metric_median'] == 99_999.0


# --------------------------------------------------------------- 小工具
def _find(gate: dict, target: str) -> dict:
    for c in gate['checks']:
        if c['target'] == target:
            return c
    raise AssertionError(f'门禁结果里没有 {target}: '
                         f'{[c["target"] for c in gate["checks"]]}')


def _msg(c: dict) -> str:
    return (f"门禁不达标 [{c['target']}] {c['reason']}\n"
            f'  依据: {c["basis"]}\n'
            f'  容忍: {c["tolerance"]}')
