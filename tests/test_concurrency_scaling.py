"""P3+：并发扩展性测量（`benchmarks/scaling.py`）的测试。

覆盖：
* 扩展性矩阵字段完整：1/2/4/8/16 客户端结构、吞吐/延迟百分位 p50-p99、
  错误数、扩展效率（相对 1 客户端）；
* 读 / 写小事务 / 混合三种负载都能跑通、错误为 0、JSON 可序列化；
* 固定数据集一次 setup 的优化路径可用（`setup_cache`）；
* 瓶颈证据：线程栈采样（blocked_fraction / engine 热点）与 cProfile Top；
* harness 集成：`run_harness(scaling=True)` 附加 `scaling` 段，且
  quick 门禁结果不受影响（scaling 不进 gate）。

运行：python -m pytest tests/test_concurrency_scaling.py -q
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks import scaling as S  # noqa: E402


def _quiet(monkeypatch):
    monkeypatch.setenv('LIGHTDB_AUDIT_LEVEL', 'NONE')
    monkeypatch.setenv('LIGHTDB_LOG_LEVEL', 'ERROR')
    try:
        from lightdb.logger import logger
        logger.set_level('ERROR')
    except Exception:                              # pragma: no cover
        pass


# ==================== 百分位工具 ====================

def test_percentile_helpers():
    vals = list(range(1, 101))
    assert S._pctl(vals, 0.50) == pytest.approx(50.5)
    assert S._pctl(vals, 0.95) == pytest.approx(95.05, abs=0.1)
    assert S._pctl(vals, 0.99) == pytest.approx(99.01, abs=0.1)
    assert S._pctl([], 0.5) == 0.0
    assert S._pctl([7], 0.99) == 7.0
    summary = S._latency_summary([1.0, 2.0, 3.0, 4.0, 5.0])
    assert summary['n'] == 5
    assert summary['p50_ms'] <= summary['p95_ms'] <= summary['p99_ms']
    assert summary['p99_ms'] == pytest.approx(4.96, abs=0.01)


# ==================== 扩展性矩阵 ====================

def test_scaling_matrix_read(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    report = S.scaling_matrix(clients=[1, 2], kinds=['read'],
                              seconds=0.15, nrows=200, warmup=0.05,
                              data_dir=str(tmp_path / 'data'))
    assert report['schema'] == S.SCHEMA
    assert report['total_windows'] == 2
    assert report['params']['clients'] == [1, 2]
    assert report['total_wall_s'] >= 0
    rows = report['results']
    assert [r['clients'] for r in rows] == [1, 2]
    for r in rows:
        assert r['kind'] == 'read'
        assert r['throughput_qps'] > 0
        assert r['per_client_qps'] > 0
        assert r['n_ops'] > 0
        assert r['errors'] == 0
        lat = r['latency']
        assert 0 <= lat['p50_ms'] <= lat['p95_ms'] <= lat['p99_ms']
        assert lat['n'] == r['n_ops']
    # 扩展效率：1 客户端基线 100%，N 客户端有明确百分比
    assert rows[0]['efficiency_pct'] == 100.0
    assert rows[0]['speedup_vs_1'] == 1.0
    assert rows[1]['speedup_vs_1'] is not None
    assert 0 < rows[1]['efficiency_pct'] <= 100 * 2
    summary = report['summary']['read']
    assert summary['baseline_qps_1client'] > 0
    assert summary['best_clients'] in (1, 2)
    json.dumps(report, ensure_ascii=False)          # 必须可 JSON 序列化


def test_scaling_matrix_write_and_mixed(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    report = S.scaling_matrix(
        clients=[1, 2], kinds=['write', 'mixed'], seconds=0.12, nrows=120,
        warmup=0.04, data_dir=str(tmp_path / 'data'))
    assert report['total_windows'] == 4
    kinds = [r['kind'] for r in report['results']]
    assert kinds.count('write') == 2 and kinds.count('mixed') == 2
    for r in report['results']:
        assert r['errors'] == 0, r
        assert r['throughput_qps'] > 0
    assert set(report['summary']) == {'write', 'mixed'}
    for kind in ('write', 'mixed'):
        assert report['summary'][kind]['baseline_qps_1client'] > 0


def test_scaling_matrix_uncached_setup_path(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    report = S.scaling_matrix(clients=[1], kinds=['read'], seconds=0.1,
                              nrows=100, warmup=0.03,
                              data_dir=str(tmp_path / 'data'),
                              setup_cache=False)
    assert report['setup_cache'] is False
    assert report['total_windows'] == 1
    assert report['results'][0]['throughput_qps'] > 0


# ==================== 瓶颈证据 ====================

def test_sampling_profile_evidence(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    prof = S.sampling_profile(nrows=120, clients=2, seconds=0.5,
                              kind='mixed', interval=0.005,
                              data_dir=str(tmp_path / 'prof'))
    assert prof['kind'] == 'sampling'
    assert prof['worker_samples'] > 0
    assert 0.0 <= prof['blocked_fraction'] <= 1.0
    assert prof['top_leaf'], '采样必须能给出叶帧 Top'
    assert prof['engine_leaf_top'], '混合负载应采到 lightdb 引擎帧'
    frames = [x['frame'] for x in prof['engine_leaf_top']]
    assert any(f.startswith('lightdb/') for f in frames)
    assert prof['errors'] == []
    json.dumps(prof, ensure_ascii=False)


def test_cprofile_evidence(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    prof = S.cprofile_profile(nrows=100, ops=40, kind='mixed',
                              data_dir=str(tmp_path / 'prof2'))
    assert prof['kind'] == 'cprofile'
    assert prof['top'], 'cProfile 必须给出热点 Top'
    assert any('cum_pct' in e for e in prof['top'])
    assert isinstance(prof['raw_top'], str) and prof['raw_top']
    json.dumps(prof, ensure_ascii=False)


def test_lock_contention_profile_evidence(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    prof = S.lock_contention_profile(nrows=120, clients=2, seconds=0.25,
                                     kind='read',
                                     data_dir=str(tmp_path / 'lockprof'))
    assert prof['kind'] == 'lock-contention'
    assert prof['locks'], '应至少包装到 1 张表锁'
    lock = prof['locks'][0]
    assert lock['acquires'] > 0
    assert lock['wait_total_ms'] >= 0
    assert 0 <= lock['wait_frac_of_thread_time'] <= 100
    assert 0 <= lock['hold_frac_of_thread_time'] <= 100
    assert prof['window']['errors'] == 0
    json.dumps(prof, ensure_ascii=False)


# ==================== 与 harness 集成 ====================

def test_harness_scaling_integration_keeps_gate(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    from benchmarks import competitive_harness as CH

    payload, rc = CH.run_harness(
        suite='quick', scale=0.01, gate=True, target_scale=0.001,
        data_dir=str(tmp_path / 'harness_data'),
        scaling=True,
        scaling_options={
            'clients': [1, 2], 'kinds': ['read', 'mixed'],
            'seconds': 0.1, 'warmup': 0.05, 'nrows': 50,
            'profile': False, 'compare_cache': False,
            'out': str(tmp_path / 'scaling.json'),
        })
    assert rc == 0
    assert payload['gate']['passed'] is True
    assert payload['gate']['n_checks'] == 11
    assert payload['scaling'] is not None
    matrix = payload['scaling']['matrix']
    assert matrix['total_windows'] == 4          # 2 kinds × 2 clients
    assert (tmp_path / 'scaling.json').is_file()
    on_disk = json.loads((tmp_path / 'scaling.json').read_text(encoding='utf-8'))
    assert on_disk['kind'] == 'suite'
    # 不带 scaling 时字段为 None（向后兼容）
    payload2, rc2 = CH.run_harness(
        suite='quick', scale=0.01, gate=False,
        data_dir=str(tmp_path / 'harness_data2'))
    assert rc2 == 0 and payload2['scaling'] is None
