"""LightDB 可复跑性能基准包（阶段 2 第 5 项）。

本包是**仓库内唯一**按门禁方式复跑性能的入口；根目录的
`bench_heap_insert.py` / `bench_concurrency.py` 是历史脚本，
逻辑已被本包吸收（见 `benchmarks/heap_insert.py` / `benchmarks/concurrency.py`）。

快速开始::

    python -m benchmarks.run --suite quick --out benchmarks/results.json
    python -m benchmarks.run --suite quick --gate
    LIGHTDB_RUN_BENCHMARKS=1 python -m pytest tests/test_benchmarks.py -q

测量方法与目标依据见 `benchmarks/README.md`。
"""

__all__ = ['gate', 'cases', 'run', 'heap_insert', 'concurrency']
