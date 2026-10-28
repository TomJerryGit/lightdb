"""P5 统一 benchmark harness（`benchmarks/competitive_harness.py`）的测试。

覆盖（任务要求）：
1. 小样本可用：`--scale` 缩档后 harness 能完整跑通并产出 JSON；
2. JSON 字段完整：版本/环境/seed/每项 median+best+samples/reference_db/门禁；
3. 可复现：固定 seed 的数据集与查询计划逐字节一致，换 seed 则不同；
4. 阈值模拟：合成测量值判定 PASS/FAIL；CLI `--gate --target-scale 1e9`
   必然未达标 -> 退出码 1（且 JSON 里 gate.passed=False）；
5. **环境变量隔离（task-78）**：`prepare_env`/case/harness 退出（含异常）
   必须精确恢复 os.environ（原本不存在的键删除、原值写回），避免污染同进程
   后续测试（根因：全量字母序下 test_production_metrics 审计不落盘）。

运行：
    python -m pytest tests/test_benchmark_harness.py -q
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from benchmarks import cases as B                  # noqa: E402
from benchmarks import competitive_harness as CH  # noqa: E402
from benchmarks import heap_insert as H           # noqa: E402

REQUIRED_CASES = {
    'insert_rowwise', 'insert_batch', 'insert_batch_speedup',
    'point_inproc', 'range_scan', 'aggregate', 'pagination',
    'tcp_point', 'tcp_range', 'tcp_agg', 'tcp_page',
    'concurrency_point_1', 'concurrency_point_4', 'concurrency_point_8',
    'concurrency_errors',
}
JSON_TOP_LEVEL = {
    'schema', 'harness_version', 'generated_at', 'suite', 'seed',
    'lightdb_version', 'python_version', 'git_rev', 'environment',
    'method', 'reference_db', 'targets', 'cases', 'gate', 'scaling',
    'total_wall_s',
}
TINY_ARGS = ['--suite', 'quick', '--scale', '0.02', '--json-only']


# ==================== 可复现性 ====================

def test_dataset_generator_is_seed_reproducible():
    a = CH.generate_rows(12345, 64)
    b = CH.generate_rows(12345, 64)
    c = CH.generate_rows(12346, 64)
    assert a == b
    assert CH.dataset_signature(a) == CH.dataset_signature(b)
    assert CH.dataset_signature(a) != CH.dataset_signature(c)
    assert [r[0] for r in a] == list(range(1, 65))
    assert all(1 <= r[1] <= 1000 for r in a)


def test_query_plan_is_seed_reproducible_and_well_formed():
    counts = {'point': 50, 'range': 30, 'agg': 10, 'page': 20}
    p1 = CH.query_plan(7, 200, counts)
    p2 = CH.query_plan(7, 200, counts)
    p3 = CH.query_plan(8, 200, counts)
    assert p1 == p2
    assert p1['point_ids'] != p3['point_ids'] or p1['page_off'] != p3['page_off']
    assert p1['counts'] == counts
    assert len(p1['point_ids']) == 50
    assert len(p1['range_lo']) == 30
    assert len(p1['page_off']) == 20
    assert all(1 <= i <= 200 for i in p1['point_ids'])
    assert all(1 <= lo <= 200 - p1['window'] + 1 for lo in p1['range_lo'])
    assert all(0 <= o <= 200 - p1['page_size'] for o in p1['page_off'])
    # 生成的 SQL 数量与计划一致（agg 用 counts['agg'] 条固定聚合 SQL）
    sqls = CH._build_sql(p1)
    assert set(sqls) == {'point', 'range', 'agg', 'page'}
    assert len(sqls['point']) == 50
    assert len(sqls['range']) == 30
    assert len(sqls['agg']) == 10
    assert len(sqls['page']) == 20


# ==================== 门禁定义与判定 ====================

def test_targets_are_documented_and_referenced():
    # 每个门禁目标都必须有标定依据、方向、单位
    for name, spec in CH.TARGETS.items():
        assert spec['better'] in ('higher', 'lower'), name
        assert spec['basis'], name
        assert '2026-10' in spec['basis'], name      # 标定日期可追溯
        assert 'tolerance' in spec, name
        assert isinstance(spec['value'], (int, float)), name
    # case 上引用的门禁目标必须存在；门禁目标也应被至少一个 case 引用
    referenced = {m['gate'] for m in CH.CASE_META.values() if m.get('gate')}
    assert referenced == set(CH.TARGETS), (referenced ^ set(CH.TARGETS))


def test_case_meta_covers_required_metrics():
    assert REQUIRED_CASES <= set(CH.CASE_META)
    for name in REQUIRED_CASES:
        assert CH.CASE_META[name].get('metric'), name
        assert CH.CASE_META[name].get('unit'), name


def test_gate_evaluate_pass_and_fail():
    good = {}
    for case_name, meta in CH.CASE_META.items():
        tname = meta.get('gate')
        if not tname:
            continue
        spec = CH.TARGETS[tname]
        # 造一个必定达标（或失败）的合成值
        if spec['better'] == 'higher':
            good[case_name] = spec['value'] * 10 + 1
        else:
            good[case_name] = spec['value'] * 0.5
    res = CH.evaluate_gate(good)
    assert res['passed'] is True
    assert res['failed'] == []
    assert res['n_checks'] == len([m for m in CH.CASE_META.values() if m.get('gate')])

    bad = dict(good)
    target_case = next(c for c, m in CH.CASE_META.items()
                       if m.get('gate') == 'point_inproc_qps')
    bad[target_case] = CH.TARGETS['point_inproc_qps']['value'] / 100.0
    res2 = CH.evaluate_gate(bad)
    assert res2['passed'] is False
    assert 'point_inproc_qps' in res2['failed']
    failed_check = [c for c in res2['checks'] if not c['ok']]
    assert failed_check and failed_check[0]['case'] == 'point_inproc'


def test_target_scale_and_override_self_check_knobs():
    strict = CH.resolve_targets(target_scale=1e9)
    assert strict['point_inproc_qps']['value'] == \
        pytest.approx(CH.TARGETS['point_inproc_qps']['value'] * 1e9)
    loose = CH.resolve_targets(target_scale=0.001)
    assert loose['point_inproc_qps']['value'] < 1.0
    over = CH.resolve_targets(overrides={'tcp_agg_qps': 123})
    assert over['tcp_agg_qps']['value'] == pytest.approx(123.0)
    with pytest.raises(KeyError):
        CH.resolve_targets(overrides={'no_such_target': 1})


def test_suite_specific_thresholds_for_scan_metrics():
    """全表扫描类指标在 full 档（5,000 行）天然比 quick 档（1,000 行）慢，
    阈值必须分档标定，不能拿 quick 的下限去卡 full。"""
    quick = CH.resolve_targets(suite='quick')
    full = CH.resolve_targets(suite='full')
    for name in ('pagination_qps', 'tcp_range_qps', 'tcp_agg_qps', 'tcp_page_qps'):
        assert CH.TARGETS[name]['value_full'] < CH.TARGETS[name]['value'], name
        assert full[name]['value'] < quick[name]['value'], name
        assert full[name]['value'] > 0, name
    # 与数据集行数无关的指标两档一致
    for name in ('insert_rowwise_rows_s', 'insert_batch_speedup',
                 'point_inproc_qps', 'concurrency_errors'):
        assert full[name]['value'] == quick[name]['value'], name


# ==================== CLI：小样本 + JSON 字段 ====================

def _run_cli(tmp_path, extra):
    out = tmp_path / 'harness.json'
    data = tmp_path / 'data'
    args = list(TINY_ARGS) + ['--out', str(out), '--data-dir', str(data)] + extra
    rc = CH.main(args)
    payload = None
    if out.exists():
        payload = json.loads(out.read_text(encoding='utf-8'))
    return rc, payload, data


def test_cli_tiny_run_produces_complete_json(tmp_path):
    rc, payload, data = _run_cli(tmp_path, [])
    assert rc == 0
    assert payload is not None
    assert JSON_TOP_LEVEL <= set(payload)
    assert payload['schema'] == CH.SCHEMA
    assert payload['harness_version'] == CH.HARNESS_VERSION
    assert payload['lightdb_version']
    assert payload['suite'] == 'quick'
    assert payload['seed'] == CH.DEFAULT_SEED
    assert isinstance(payload['environment'], dict)
    assert 'cpu_count' in payload['environment']
    # 数据集可复现：JSON 里记录的指纹 == 用 seed 重新生成的数据指纹
    expected_sig = CH.dataset_signature(
        CH.generate_rows(CH.DEFAULT_SEED, payload['params']['rows']))
    assert payload['method']['dataset_signature_sha256'] == expected_sig
    # reference_db：没有 MySQL 时必须明确"仅基线"，且绝不声明 compared=True
    ref = payload['reference_db']
    assert ref['compared'] is False
    if not ref['available']:
        assert '仅 LightDB 基线' in ref['note']
        assert payload['method']['baseline_only'] is True
    # 覆盖指标齐全
    assert REQUIRED_CASES <= set(payload['cases'])
    for name in REQUIRED_CASES:
        case = payload['cases'][name]
        for key in ('metric', 'unit', 'samples', 'n_samples',
                    'median', 'best', 'worst', 'better'):
            assert key in case, (name, key)
        assert case['samples'], name
        assert case['median'] is not None, name
        assert case['best'] is not None, name
        if case['better'] == 'higher':
            assert case['best'] >= case['median']
        else:
            assert case['best'] <= case['median']
    # 临时数据目录默认清理
    assert not data.exists()
    g = payload['gate']
    assert g is None                      # 未加 --gate 时不应伪造门禁结果


def test_cli_gate_fail_simulation_exit_1(tmp_path):
    rc, payload, _ = _run_cli(
        tmp_path, ['--gate', '--target-scale', '1e9', '--gate-retries', '0'])
    # 阈值 ×1e9 后所有非零指标都不可能达标：退出码必须为 1
    assert rc == 1
    assert payload is not None
    assert payload['gate'] is not None
    assert payload['gate']['passed'] is False
    assert payload['gate']['failed'], '门禁失败时必须列出未达标项'
    assert payload['gate']['n_checks'] == len(payload['gate']['checks'])
    assert payload['gate']['retries_used'] == 0
    # JSON 仍完整写出（失败也要可分析）
    assert REQUIRED_CASES <= set(payload['cases'])


def test_merge_best_cases_and_gate_retry_attempts(tmp_path):
    # 合并语义：逐项取更有利一次（higher 取大、lower 取小），记录 attempt 历史
    best = {'a': {'median': 10.0, 'better': 'higher'},
            'b': {'median': 5.0, 'better': 'lower'}}
    new = {'a': {'median': 12.0, 'better': 'higher'},
           'b': {'median': 6.0, 'better': 'lower'}}
    merged = CH._merge_best_cases(best, new)
    assert merged['a']['median'] == 12.0
    assert merged['a']['attempt_values'] == [10.0, 12.0]
    assert merged['a']['attempt_source'] == 'attempt_2'
    assert merged['b']['median'] == 5.0            # lower better：5 更优
    assert merged['b']['attempt_source'] == 'attempt_1'

    # 真实门禁失败 + 默认重试 1 次：attempts 应记录 2 次，仍失败 -> exit 1
    rc, payload, _ = _run_cli(tmp_path, ['--gate', '--target-scale', '1e9'])
    assert rc == 1
    g = payload['gate']
    assert g['retries_used'] == 1
    assert len(g['attempts']) == 2
    assert g['attempts'][0]['attempt'] == 1
    assert g['attempts'][1]['attempt'] == 2
    assert 'point_inproc_qps' in g['failed']


def test_cli_rejects_unknown_target_and_bad_suite(tmp_path):
    with pytest.raises(SystemExit):
        CH.main(['--suite', 'nope'])
    assert CH.main(['--gate', '--target', 'no_such_target=1',
                    '--scale', '0.01', '--out', str(tmp_path / 'x.json'),
                    '--data-dir', str(tmp_path / 'd'), '--json-only']) == 2


# ==================== task-78：benchmark 环境变量泄漏回归 ====================

def _restore_whole_env(snapshot: dict) -> None:
    os.environ.clear()
    os.environ.update(snapshot)


def test_prepare_env_restores_missing_and_existing_keys(tmp_path):
    """prepare→teardown 后 env 与之前完全一致；原本不存在的键必须删除。"""
    before = dict(os.environ)
    try:
        # 场景 1：四个键原本都不存在 -> 退出后必须仍不存在
        for key in H.BENCH_ENV_KEYS:
            os.environ.pop(key, None)
        snap_absent = dict(os.environ)
        with H.prepare_env('default'):
            assert os.environ['LIGHTDB_AUDIT_LEVEL'] == 'NONE'
            assert os.environ['LIGHTDB_LOG_LEVEL'] == 'ERROR'
            assert 'LIGHTDB_WAL_CHECKPOINT_BYTES' not in os.environ
            assert 'LIGHTDB_WAL_CHECKPOINT_RECORDS' not in os.environ
        assert dict(os.environ) == snap_absent
        for key in H.BENCH_ENV_KEYS:
            assert key not in os.environ, key

        # 场景 2：原有自定义值 + cfg='off' 改写 -> 退出后原值写回
        os.environ['LIGHTDB_AUDIT_LEVEL'] = 'DML'
        os.environ['LIGHTDB_LOG_LEVEL'] = 'DEBUG'
        os.environ['LIGHTDB_WAL_CHECKPOINT_BYTES'] = '123456'
        os.environ['LIGHTDB_WAL_CHECKPOINT_RECORDS'] = '9'
        snap_existing = dict(os.environ)
        with H.prepare_env('off', audit='ALL'):
            assert os.environ['LIGHTDB_AUDIT_LEVEL'] == 'ALL'
            assert os.environ['LIGHTDB_WAL_CHECKPOINT_BYTES'] == str(10 ** 15)
            assert os.environ['LIGHTDB_WAL_CHECKPOINT_RECORDS'] == '0'
        assert dict(os.environ) == snap_existing
        assert os.environ['LIGHTDB_AUDIT_LEVEL'] == 'DML'
        assert os.environ['LIGHTDB_WAL_CHECKPOINT_BYTES'] == '123456'
    finally:
        _restore_whole_env(before)


def test_prepare_env_restores_on_exception(tmp_path):
    """异常路径也必须恢复 env（内部 raise 不允许把变量留在进程里）。"""
    before = dict(os.environ)
    try:
        os.environ.pop('LIGHTDB_AUDIT_LEVEL', None)
        os.environ.pop('LIGHTDB_LOG_LEVEL', None)
        snap = dict(os.environ)
        with pytest.raises(RuntimeError, match='boom'):
            with H.prepare_env('default'):
                assert os.environ['LIGHTDB_AUDIT_LEVEL'] == 'NONE'
                raise RuntimeError('boom')
        assert dict(os.environ) == snap
        assert 'LIGHTDB_AUDIT_LEVEL' not in os.environ
        assert 'LIGHTDB_LOG_LEVEL' not in os.environ
    finally:
        _restore_whole_env(before)


def test_prepare_env_restores_global_logger_level(tmp_path):
    """prepare_env 顺带恢复全局 logger 级别（不只恢复环境变量）。"""
    from lightdb.logger import logger

    before = dict(os.environ)
    level_before = logger.level
    try:
        logger.set_level('INFO')
        level_mid = logger.level
        with H.prepare_env('default'):
            assert logger.level != level_mid          # 被压到 ERROR
        assert logger.level == level_mid              # 退出恢复
    finally:
        logger.set_level(level_before)
        _restore_whole_env(before)


def test_case_function_does_not_leak_env(tmp_path):
    """gate 使用的 case 函数（cases.py 装饰器）跑完不得泄漏 env。"""
    before = dict(os.environ)
    try:
        os.environ.pop('LIGHTDB_AUDIT_LEVEL', None)
        os.environ.pop('LIGHTDB_LOG_LEVEL', None)
        snap = dict(os.environ)
        res = B._case_point_inproc(nrows=120, rounds=1, nq=10,
                                   data_dir=str(tmp_path / 'case_data'))
        assert res['metric_median'] > 0
        assert dict(os.environ) == snap
        assert 'LIGHTDB_AUDIT_LEVEL' not in os.environ
    finally:
        _restore_whole_env(before)


def test_run_harness_does_not_leak_env(tmp_path):
    """进程内跑完整 harness 后 env 与跑之前完全一致（根因回归）。"""
    before = dict(os.environ)
    try:
        os.environ.pop('LIGHTDB_AUDIT_LEVEL', None)
        os.environ.pop('LIGHTDB_LOG_LEVEL', None)
        os.environ.pop('LIGHTDB_WAL_CHECKPOINT_BYTES', None)
        os.environ.pop('LIGHTDB_WAL_CHECKPOINT_RECORDS', None)
        snap = dict(os.environ)
        payload, rc = CH.run_harness(
            suite='quick', scale=0.01,
            data_dir=str(tmp_path / 'harness_env_data'))
        assert rc == 0
        assert payload['suite'] == 'quick'
        assert dict(os.environ) == snap
        for key in ('LIGHTDB_AUDIT_LEVEL', 'LIGHTDB_LOG_LEVEL',
                    'LIGHTDB_WAL_CHECKPOINT_BYTES',
                    'LIGHTDB_WAL_CHECKPOINT_RECORDS'):
            assert key not in os.environ, key
    finally:
        _restore_whole_env(before)
