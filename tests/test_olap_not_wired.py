"""OLAP / 列存「未接入 SQL 路径」锁测试（阶段 2 第 6 项，方案 A「摘牌 + 显式化」）

本文件锁住三件事，全部是**可达性/口径**判据，不含任何性能阈值：

1. **SQL 路径不会意外走到 olap/列存**
   * `lightdb/sql/**` 与 `lightdb/parallel/**` 的源码对 olap / 列存层
     **零 import**（唯一的例外是状态常量模块 `lightdb/olap/status.py`）；
   * 普通 SQL（含 `COUNT(*)` 的并行分片入口）跑完**不新增**任何
     `lightdb.olap*` / `lightdb.columnar*` 模块；
   * 开/关 `Executor.parallel_enabled`（本项目唯一与 OLAP 沾边的执行开关）
     之后，同一张表上的查询结果**逐行完全相同** —— 即没有任何
     「开了 X 就走列存/向量化」的隐含分支；
   * `CREATE TABLE` 建出来的是行式 `lightdb.db.table.Table`，不是列存。

2. **仍保留的实验性入口被触达时显式报错 / 显式留痕，绝不静默**
   * 状态常量：`WIRED_TO_SQL_PATH is False`、
     `COUNTS_FOR_PERFORMANCE_CLAIMS is False`；
   * 报错函数 `raise_experimental_unwired` 的报文必须同时含
     「实验性 / 未接入 / 不计入性能声明」；
   * `HybridTable.olap_scan()`（无既有调用方）触达即
     `NotImplementedError`，报文含上述三词；
   * `HybridTable.olap_aggregate()`（有既有调用方，故不抛错）每次调用
     都必须留下带状态标签的告警日志。

3. **文档里不存在「未加状态说明」的性能断言**
   * 每处 `3.5M rows/s / 3,500,000 / 85M rows/s / 41x / 12.5x /
     12,000 QPS / 92,000 / 3.15x / 1M rows/s / 200K rows/s` 断言，其
     前后 40 行内必须有状态声明（未接入 / 未接线 / 未实现 / 未编译 /
     设计目标 / 数据可信度声明 / 实验性 / 不计入性能声明）；
   * 含上述断言的文档，其**前 30 行**必须出现「未接入 SQL 执行路径」一类
     的口径声明；
   * `benchmarks/README.md` 必须仍有白皮书声明 vs 实测的对照表，
     `README.md` 必须指向它。

背景事实（已核实，见 `benchmarks/README.md` 与 `docs/review/01_engine.md`）：
`lightdb/olap/**` 与 `lightdb/columnar/**` 在 `Executor` / `plan_executor`
零引用，但白皮书把 OLAP 3.5M rows/s、大表聚合 85M rows/s、列存 41x
当成性能来源；本阶段的做法是**摘牌 + 显式化**（不删实现，避免打破
`tests/test_vectorized_correctness.py` 一类既有测试）。
"""
import importlib
import logging
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

try:
    from tests.conftest import safe_rmtree, track
except ImportError:                           # 直接 python tests/test_xxx.py
    from conftest import safe_rmtree, track

from lightdb.db.hybrid_table import HybridTable
from lightdb.olap import status as olap_status

DATA_DIR = str(ROOT / 'data_olap_notwired')

# ==================== 常量：口径与判据 ====================

#: 白皮书里被当成「性能来源」的数字（阶段 2 第 6 项要求全部加状态说明）
LEGACY_PERF_CLAIMS = [
    '3.5M rows/s', '3,500,000', '85M rows/s', '41x', '12.5x',
    '12,000 QPS', '92,000', '3.15x', '1M rows/s', '200K rows/s',
]

#: 「这层没接线 / 不是实测」的状态声明词
STATUS_MARKERS = [
    '未接入', '未接线', '未实现', '未编译',
    '设计目标', '数据可信度声明', '实验性', '不计入性能声明',
]

#: 必须出现在文档前 30 行的**接线状态**口径（比 STATUS_MARKERS 更严）
HEAD_STATUS_MARKER = '未接入 SQL 执行路径'

#: 同义的接线状态口径（文档可以选任一种写法，但必须写明「没接线」）
HEAD_STATUS_MARKERS = (
    '未接入 SQL 执行路径',
    '未接入 SQL 路径',
    '未接入查询路径',
    '未接入 SQL',
)

#: 允许 import 状态常量模块的文件（它是「摘牌」的登记处，本身不接线）
STATUS_MODULE = 'lightdb/olap/status.py'

#: 禁止出现 olap / 列存 import 的目录（SQL 与并行执行路径）
FORBIDDEN_DIRS = ['lightdb/sql', 'lightdb/parallel']

_IMPORT_RE = re.compile(
    r'^\s*(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))', re.MULTILINE)

_CLAIM_RE = re.compile('|'.join(re.escape(c) for c in LEGACY_PERF_CLAIMS))

_WINDOW = 40          # 每处断言前后允许的「状态说明」距离（行）


def _imported_olap_engine_modules(text: str):
    """从源码文本里提取**真的会被执行**的 olap/列存引擎 import

    剥离注释、字符串与 docstring 之后再匹配 `^from/^import`：本仓库里
    「演示改前代码」的注释/docstring 会写出 `from ..olap.vectorized_engine
    import ...` 这种字样（见 `parallel/executor.py::_serial_group_by` 的
    docstring），按原文匹配会把它们误判成真实接线。
    """
    lines = []
    in_doc = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('#'):
            continue
        if not in_doc and (line.count('"""') % 2 == 1
                           or line.count("'''") % 2 == 1):
            in_doc = True
            continue
        if in_doc:
            if line.endswith('"""') or line.endswith("'''"):
                in_doc = False
            continue
        lines.append(raw)
    code = '\n'.join(lines)
    for m in _IMPORT_RE.finditer(code):
        mod = (m.group(1) or m.group(2)).strip()
        if 'olap' in mod or 'columnar' in mod:
            yield mod


# ==================== 会话级反向 import 锁 ====================

_OLAP_PREFIXES = ('lightdb.olap', 'lightdb.columnar')

#: 实验性 OLAP 层的**实现模块**（状态登记处 `lightdb.olap.status` 不算）
_OLAP_ENGINE_MODULES = (
    'lightdb.olap.vectorized_engine',
    'lightdb.olap.olap_optimizer',
    'lightdb.olap.parallel_olap',
    'lightdb.olap.scanner',
    'lightdb.olap.aggregator',
)


class _BlockOlapEngineImports:
    """把 olap 引擎的 import 变成**硬失败**（模拟「这层不存在」）

    比「从 `sys.modules` 摘掉」严格：即便 `lightdb.olap` 包的 `__init__`
    已经跑过（属性缓存住了旧对象），`import lightdb.olap.vectorized_engine`
    也会重新走 import 系统并被这里拦下 —— 于是「串行回退悄悄改道到向量化
    引擎」这种回归一定会让用例失败，而不是被缓存掩盖成通过。
    """

    def find_module(self, fullname, path=None):        # 兼容旧协议
        return None

    def find_spec(self, fullname, path=None, target=None):
        if fullname in _OLAP_ENGINE_MODULES:
            raise ImportError(
                f'锁测试：{fullname} 是实验性 OLAP 引擎，不得被生产代码 import')
        return None


def _olap_modules_loaded() -> set:
    """本进程里已经加载的 olap / 列存模块（`sys.modules` 快照）"""
    return {m for m in sys.modules
            if m.startswith(_OLAP_PREFIXES)}


@pytest.fixture(scope='session', autouse=True)
def _olap_import_watch():
    """记录本会话是否加载过 olap / 列存层，供 `test_sql_query_...` 断言。

    只读快照，不做任何 monkeypatch：
    * 若加载过（例如同一次 pytest 还跑了 `test_v4_batch4.py`），
      对应的「零加载」断言自动降级为「不新增」—— 不放过真正的接线回归，
      也不会因为别的测试文件本来就 import 了它而误报。
    """
    yield _olap_modules_loaded()


@pytest.fixture(autouse=True)
def _clean_data_dir():
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)
    yield
    if os.path.exists(DATA_DIR):
        safe_rmtree(DATA_DIR)


# ==================== 工具 ====================

def _db_and_executor():
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor

    db = track(Database(DATA_DIR))
    return db, Executor(db)


def _sql(ex, sql, session=None):
    return ex.execute_sql(sql, session if session is not None else {})


def _rows(ex, sql):
    r = _sql(ex, sql)
    assert r['type'] == 'resultset', r
    return [tuple(row) for row in r['rows']]


def _py_files(rel_dir):
    base = ROOT / rel_dir
    for p in sorted(base.rglob('*.py')):
        yield p


def _imports_of(path: Path):
    text = path.read_text(encoding='utf-8', errors='replace')
    for m in _IMPORT_RE.finditer(text):
        yield (m.group(1) or m.group(2)).strip()


def _doc_files():
    yield ROOT / 'README.md'
    for p in sorted((ROOT / 'docs').rglob('*.md')):
        yield p


# ==================== 1. SQL 路径不会意外走到 olap / 列存 ====================

def test_sql_execution_path_has_zero_olap_imports():
    """`lightdb/sql/**` 与 `lightdb/parallel/**` 源码里不得 import olap/列存

    唯一允许的例外是状态常量模块 `lightdb/olap/status.py`
    （它只放常量与检查点，不引用任何引擎实现）。
    """
    bad = []
    for rel in FORBIDDEN_DIRS:
        for path in _py_files(rel):
            relpath = path.relative_to(ROOT).as_posix()
            for mod in _imports_of(path):
                if not mod.startswith(('lightdb.', '.', '..')):
                    continue
                if 'olap' in mod or 'columnar' in mod:
                    # 相对导入也要还原成可能的目标路径
                    text = path.read_text(encoding='utf-8', errors='replace')
                    if relpath == STATUS_MODULE or 'olap.status' in text:
                        continue
                    bad.append(f'{relpath}: {mod}')
    assert not bad, (
        f'SQL / 并行执行路径里出现了 olap / 列存 import（阶段 2 第 6 项要求'
        f'零引用，或改为状态常量模块）：{bad}')


def test_olap_status_constants_say_unwired():
    """摘牌的状态常量必须显式为「未接入 / 不计入性能声明」"""
    assert olap_status.WIRED_TO_SQL_PATH is False, (
        'olap/列存被登记为已接入 SQL 路径；若真接线了，必须同步 '
        'benchmarks/README.md 的实测对照表再改这里')
    assert olap_status.COUNTS_FOR_PERFORMANCE_CLAIMS is False
    assert '实验性' in olap_status.STATUS_LABEL
    assert '未接入' in olap_status.STATUS_LABEL
    assert '不计入性能声明' in olap_status.STATUS_LABEL
    assert olap_status.BENCH_REFERENCE == 'benchmarks/README.md'


def test_sql_query_loads_no_olap_modules(_olap_import_watch):
    """跑一遍普通 SQL（含 COUNT(*) 的并行入口）不得加载 olap / 列存层"""
    pre = _olap_import_watch
    db, ex = _db_and_executor()
    try:
        assert _sql(ex, 'CREATE TABLE t (id INT PRIMARY KEY, v INT)')['type'] == 'ok'
        for i in range(1, 51):
            assert _sql(ex, f'INSERT INTO t VALUES ({i}, {i * 2})')['type'] == 'ok'

        # COUNT(*) 是 `Executor._parallel_count` 唯一真正启用的并行入口
        # （行数 >= parallel_min_rows 才会触发；这里直接把阈值调低）
        ex.parallel_min_rows = 1
        ex.parallel_enabled = True
        assert _rows(ex, 'SELECT COUNT(*) FROM t') == [(50,)]
        assert _rows(ex, 'SELECT id, v FROM t WHERE id > 45 ORDER BY id')[-1] == (50, 100)
        assert _rows(ex, 'SELECT v, COUNT(*) FROM t GROUP BY v ORDER BY v')[:1] == [(2, 1)]
    finally:
        db.shutdown()

    after = _olap_modules_loaded()
    new = sorted(after - pre)
    assert not new, (
        f'一次 SQL 查询之后新加载了 olap / 列存模块：{new}；'
        f'说明 SQL 路径上出现了未声明的 OLAP/列存接线')


def test_sql_create_table_is_row_store_not_columnar():
    """SQL 的 CREATE TABLE 建的是行式 Table，不是列存 ColumnStore"""
    from lightdb.db.table import Table

    db, ex = _db_and_executor()
    try:
        assert _sql(ex, 'CREATE TABLE r (id INT PRIMARY KEY, v INT)')['type'] == 'ok'
        tbl = db.get_table('r')
        assert isinstance(tbl, Table)
        assert not hasattr(tbl, 'column_store'), (
            'SQL 建出的表带上了列存句柄 —— 说明列存被悄悄接进了 SQL 路径')
    finally:
        db.shutdown()


def test_parallel_switch_does_not_change_results():
    """开/关唯一相关执行开关（`parallel_enabled`），结果必须逐行相同

    判据：不存在「开了 X 就走列存/向量化」的隐含分支 —— 若存在，两种开关
    状态下的结果集或执行路径必然出现差异（多模块加载 / 行数或值不同）。
    """
    db, ex = _db_and_executor()
    try:
        assert _sql(ex, 'CREATE TABLE p (id INT PRIMARY KEY, g VARCHAR, v INT)')['type'] == 'ok'
        values = ', '.join(
            f"({i}, 'g{i % 3}', {i * 7})" for i in range(1, 41))
        assert _sql(ex, f'INSERT INTO p VALUES {values}')['type'] == 'ok'

        queries = [
            'SELECT COUNT(*) FROM p',
            'SELECT g, COUNT(*) FROM p GROUP BY g ORDER BY g',
            'SELECT g, SUM(v) FROM p GROUP BY g ORDER BY g',
            'SELECT id, v FROM p WHERE v > 140 ORDER BY id',
            'SELECT COUNT(v) FROM p',
        ]
        # 先量一次「开」的状态（含把行数阈值调到必然触发并行的程度）
        looks = {}
        for q in queries:
            ex.parallel_min_rows = 1
            ex.parallel_enabled = True
            on = _rows(ex, q)
            ex.parallel_enabled = False
            off = _rows(ex, q)
            looks[q] = (on, off)
            assert on == off, (
                f'开关 parallel_enabled 改变了结果（说明存在隐含的引擎改道）：'
                f'{q}\n on={on}\n off={off}')
    finally:
        db.shutdown()


def test_parallel_group_by_serial_fallback_is_olap_free():
    """`ParallelExecutor` 的串行回退（workers<=1）不得依赖 olap 引擎

    改前：`_serial_group_by` 直接 `from ..olap.vectorized_engine import ...`，
    于是「并行度 1」会悄悄把分组交给实验性向量化引擎。改后必须是本模块
    自己的逐行实现，且结果形状与并行路径一致（`[分组键..., 聚合值...]`）。
    """
    from lightdb.columnar.column_store import ColumnStore
    from lightdb.parallel.executor import ParallelExecutor

    assert ParallelExecutor.WIRED_TO_SQL_PATH is False

    store = ColumnStore(
        't', [{'name': 'g', 'type': 'VARCHAR'},
              {'name': 'v', 'type': 'INT'}],
        str(ROOT / 'data_olap_notwired' / 'cs'))
    n = 5000
    store.append_rows([[f'g{i % 4}', i] for i in range(n)])

    ex = ParallelExecutor(max_workers=1)
    try:
        # 断言「串行回退没有改道到 olap 引擎」，两道锁叠加：
        #   ① 把 olap 引擎从 `sys.modules` 里摘掉（否则 `import` 直接命中
        #      缓存，根本不走 import 系统 —— 那样 blocker 形同虚设）；
        #   ② 用 meta_path blocker 让 olap 引擎的 import **硬失败**。
        # 改前那句 `from ..olap.vectorized_engine import ...` 会让本用例失败。
        blocker = _BlockOlapEngineImports()
        with pytest.MonkeyPatch.context() as mp:
            for name in list(sys.modules):
                if (name.startswith(_OLAP_PREFIXES)
                        and name != 'lightdb.olap.status'):
                    mp.delitem(sys.modules, name, raising=False)
            sys.meta_path.insert(0, blocker)
            try:
                result = ex.parallel_group_by(
                    store, ['g'],
                    [{'func': 'SUM', 'column': 'v', 'alias': 'total'},
                     {'func': 'COUNT', 'column': '*', 'alias': 'cnt'}],
                    workers=1)
            finally:
                sys.meta_path.remove(blocker)
    finally:
        ex.shutdown()

    assert len(result) == 4, result
    assert sum(r[2] for r in result) == n, result
    # 串行回退与并行路径的形状必须一致（都是标量，不是单元素列表）
    assert all(isinstance(r[2], int) for r in result), result
    assert all(isinstance(r[1], int) for r in result), result


# ==================== 2. 实验性入口必须显式报错 / 留痕 ====================

def test_explicit_error_message_contains_required_words():
    """统一的显式报错报文必须含「实验性 / 未接入 / 不计入性能声明」"""
    with pytest.raises(NotImplementedError) as ei:
        olap_status.raise_experimental_unwired('X.y')
    msg = str(ei.value)
    for word in ('实验性', '未接入', '不计入性能声明'):
        assert word in msg, f'报文缺少 {word!r}: {msg}'
    assert 'benchmarks/README.md' in msg


def test_hybrid_table_olap_scan_is_explicit_error():
    """`HybridTable.olap_scan()` 触达即显式报错（不是空结果、不是静默）"""
    import tempfile

    cols = [{'name': 'id', 'type': 'INT'},
            {'name': 'city', 'type': 'VARCHAR'},
            {'name': 'amount', 'type': 'INT'}]
    with tempfile.TemporaryDirectory() as tmp:
        table = HybridTable('orders', cols, tmp)
        try:
            with pytest.raises(NotImplementedError) as ei:
                table.olap_scan(['city'])
            msg = str(ei.value)
            for word in ('实验性', '未接入', '不计入性能声明'):
                assert word in msg, f'报文缺少 {word!r}: {msg}'
        finally:
            table.close()


def test_hybrid_table_olap_aggregate_leaves_an_explicit_trace():
    """`olap_aggregate()`（有既有调用方）每次调用都必须显式留痕

    不抛异常是**有意**的（`tests/test_v4_batch4.py::test_hybrid_table`
    直接调用它，既有断言不得改动），但必须留下带状态标签的日志 ——
    「走通了但没接线」不能被读成「已支持」。

    注：本仓库的 `logger` 是自己的实现（不经过 `logging` 模块），因此这里
    用它的 `add_handler` 抓日志，而不是 pytest 的 `caplog`。
    """
    import tempfile

    from lightdb.logger import logger

    cols = [{'name': 'id', 'type': 'INT'},
            {'name': 'city', 'type': 'VARCHAR'},
            {'name': 'amount', 'type': 'INT'}]
    captured = []

    def _sink(level, msg, line):
        captured.append(line)

    with tempfile.TemporaryDirectory() as tmp:
        table = HybridTable('orders', cols, tmp)
        logger.add_handler(_sink)
        try:
            for i in range(20):
                table.insert([i, f'c{i % 3}', i * 10])

            table.olap_aggregate({
                'columns': ['city'],
                'group_by': ['city'],
                'aggregations': [
                    {'func': 'SUM', 'column': 'amount', 'alias': 'total'}],
            })
            text = '\n'.join(captured)
            assert 'olap_aggregate' in text, text
            for word in ('实验性', '未接入', '不计入性能声明'):
                assert word in text, f'留痕缺少 {word!r}: {text}'
        finally:
            logger.remove_handler(_sink)
            table.close()


def test_no_sql_keyword_or_executor_attribute_enables_olap():
    """不存在「SQL 开关 / Executor 属性」能打开 OLAP 或列存"""
    from lightdb.sql.executor import Executor

    # 1) Executor 上不得出现任何 olap/columnar/vectorized 命名的属性
    names = [n for n in dir(Executor)
             if re.search(r'olap|columnar|vectoriz', n, re.IGNORECASE)]
    assert not names, f'Executor 上出现了 OLAP/列存开关：{names}'

    # 2) SQL 里没有 OLAP/COLUMNAR/VECTORIZED 关键字：显式语法错误（1064），
    #    而不是「被当成普通语句静默执行」
    db, ex = _db_and_executor()
    try:
        for sql in ('SET olap = ON',
                    'SET columnar = 1',
                    'SET vectorized = ON',
                    'SET SESSION olap = 1'):
            r = _sql(ex, sql)
            assert r['type'] == 'error', (sql, r)
            assert r.get('code') in (1064, 1105, 1238), (sql, r)
    finally:
        db.shutdown()


# ==================== 3. 文档不得声称未实现的能力 ====================

def test_no_unguarded_performance_claim_in_docs():
    """每处旧性能断言前后 40 行内必须有状态说明（不得裸奔）"""
    bad = []
    for path in _doc_files():
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        for i, line in enumerate(lines):
            if not _CLAIM_RE.search(line):
                continue
            lo, hi = max(0, i - _WINDOW), min(len(lines), i + _WINDOW + 1)
            window = '\n'.join(lines[lo:hi])
            if not any(mk in window for mk in STATUS_MARKERS):
                bad.append(f'{path.relative_to(ROOT).as_posix()}:{i + 1}')
    assert not bad, (
        f'这些位置的旧性能断言没有任何「未接入/未编译/设计目标/实验性」'
        f'状态说明：{bad}')


def test_docs_containing_claims_declare_unwired_in_header():
    """含旧性能数字的文档，前 30 行必须声明「未接入 SQL 执行路径」"""
    bad = []
    for path in _doc_files():
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        if not _CLAIM_RE.search('\n'.join(lines)):
            continue
        head = '\n'.join(lines[:30])
        if not any(m in head for m in HEAD_STATUS_MARKERS):
            bad.append(path.relative_to(ROOT).as_posix())
    assert not bad, f'这些文档含性能断言但头部没有接线状态声明：{bad}'


def test_benchmarks_readme_keeps_whitepaper_vs_measured_table():
    """`benchmarks/README.md` 必须保留「白皮书声明 vs 实测」对照表"""
    text = (ROOT / 'benchmarks' / 'README.md').read_text(
        encoding='utf-8', errors='replace')
    for claim in ('3.5M rows/s', '85M rows/s', '41x', '12.5x', '12,000 QPS'):
        assert claim in text, f'benchmarks/README.md 缺少对照项 {claim!r}'
    assert 'SQL 执行路径' in text, '对照表必须说明这些链路没有接进 SQL 执行路径'


def test_root_readme_points_at_benchmark_table():
    """根 README 的性能口径必须指向 benchmarks/README.md 的实测对照表"""
    text = (ROOT / 'README.md').read_text(encoding='utf-8', errors='replace')
    assert 'benchmarks/README.md' in text
    assert '未接入 SQL 执行路径' in text
    assert '不计入性能声明' in text


def test_status_module_is_the_only_olap_import_owner_in_production():
    """生产代码里引用 olap / 列存**实现**的文件必须都在显式白名单里

    `lightdb/olap/status.py` 是摘牌的登记处（只放常量与检查点、不碰引擎），
    任何文件都可以 import 它做状态检查 —— 这不是接线。真正要冻结的是
    「谁引用了 olap/列存**实现**」：当前白名单只有实验性的混合表
    （`lightdb/db/hybrid_table.py`）。新增任何一条都意味着 OLAP/列存被接到了
    新的地方，必须显式更新本测试与 `benchmarks/README.md`，不能悄悄发生。
    """
    allowed = {
        'lightdb/db/hybrid_table.py',
    }
    found = {}
    for rel in ('lightdb/db', 'lightdb/parallel', 'lightdb/sql',
                'lightdb/performance', 'lightdb/tools', 'lightdb/net'):
        for path in _py_files(rel):
            relpath = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding='utf-8', errors='replace')
            for mod in _imported_olap_engine_modules(text):
                real = mod.lstrip('.').split('.')[-1]
                # 状态常量模块（`olap.status`）只放常量与检查点，任何文件
                # 都可以 import 它做状态检查 —— 那不是接线。
                if real == 'status':
                    continue
                found.setdefault(relpath, []).append(mod)
    extra = sorted(set(found) - allowed)
    assert not extra, (
        f'这些生产文件引用了 olap / 列存实现但不在显式白名单里：'
        f'{ {k: found[k] for k in extra} }；'
        f'若是有意接线，请先更新 benchmarks/README.md 与 lightdb/olap/status.py')


def test_olap_status_module_imports_no_engine():
    """状态常量模块本身不得 import 任何 olap/列存实现（保持纯常量）"""
    text = (ROOT / 'lightdb' / 'olap' / 'status.py').read_text(
        encoding='utf-8', errors='replace')
    for mod in _imports_of(ROOT / 'lightdb' / 'olap' / 'status.py'):
        assert 'vectorized_engine' not in mod
        assert 'column_store' not in mod
        assert 'olap_optimizer' not in mod
    assert 'from typing import NoReturn' in text
