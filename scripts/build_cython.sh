#!/bin/bash
# ============================================================
# LightDB Cython 编译脚本
#
# 功能:
# - 检测 Cython / C 编译器
# - 编译 6 个 Cython 模块
# - 验证编译产物
# - 对比 Python vs Cython 性能
#
# 用法:
#   ./build_cython.sh                    # 默认编译
#   ./build_cython.sh --clean            # 清理
#   ./build_cython.sh --benchmark        # 编译 + 性能对比
#   ./build_cython.sh --debug            # Debug 模式
#   ./build_cython.sh --no-native        # 不启用 -march=native
# ============================================================

set -euo pipefail

# ---------- 默认参数 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CYTHON_DIR="$PROJECT_ROOT/lightdb/cython"

CLEAN=0
BENCHMARK=0
DEBUG=0
NATIVE=1
THREADS="${THREADS:-$(nproc 2>/dev/null || echo 4)}"
PYTHON="${PYTHON:-python3}"

# ---------- 颜色 ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step() { echo -e "\n${BLUE}━━━ $* ━━━${NC}"; }
info() { echo -e "${CYAN}  →${NC} $*"; }

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --clean)      CLEAN=1; shift ;;
        --benchmark)  BENCHMARK=1; shift ;;
        --debug)      DEBUG=1; shift ;;
        --no-native)  NATIVE=0; shift ;;
        --python)     PYTHON="$2"; shift 2 ;;
        --threads)    THREADS="$2"; shift 2 ;;
        -h|--help)
            sed -n '3,17p' "$0"
            exit 0
            ;;
        *) err "未知参数: $1"; exit 1 ;;
    esac
done

# ---------- 环境检查 ----------
check_env() {
    step "1/6 环境检查"

    # Python
    if ! command -v "$PYTHON" &> /dev/null; then
        err "未找到 Python: $PYTHON"
        exit 1
    fi

    PY_VER=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')
    log "Python: $PY_VER"

    # Cython
    if ! "$PYTHON" -c "import Cython" &> /dev/null; then
        warn "Cython 未安装，正在安装..."
        "$PYTHON" -m pip install "cython>=3.0.0" -q
    fi

    CY_VER=$("$PYTHON" -c "import Cython; print(Cython.__version__)")
    log "Cython: $CY_VER"

    # C 编译器
    local CC=""
    for c in gcc clang cc; do
        if command -v "$c" &> /dev/null; then
            CC="$c"
            break
        fi
    done

    if [[ -z "$CC" ]]; then
        err "未找到 C 编译器"
        echo "  Debian/Ubuntu: apt install build-essential"
        echo "  RHEL/CentOS:   yum install gcc gcc-c++ make"
        echo "  macOS:         xcode-select --install"
        exit 1
    fi

    log "C 编译器: $CC ($($CC --version | head -1))"

    # numpy（部分模块需要）
    if ! "$PYTHON" -c "import numpy" &> /dev/null; then
        warn "numpy 未安装，正在安装..."
        "$PYTHON" -m pip install numpy -q
    fi
    log "numpy: $("$PYTHON" -c "import numpy; print(numpy.__version__)")"

    # CPU 特性
    if [[ -f /proc/cpuinfo ]]; then
        local flags
        flags=$(grep -m1 '^flags' /proc/cpuinfo | cut -d: -f2)
        local features=""
        for f in avx2 avx fma sse4_2; do
            if echo "$flags" | grep -qw "$f"; then
                features="$features $f"
            fi
        done
        log "CPU 特性:${features:- 基础}"
    fi

    log "✓ 环境检查通过"
}

# ---------- 清理 ----------
clean_build() {
    step "2/6 清理旧构建"

    cd "$CYTHON_DIR"

    # 清理编译产物
    for ext in c cpp so pyd html; do
        find . -maxdepth 1 -name "*.${ext}" -delete 2>/dev/null || true
    done

    # 清理 build 目录
    rm -rf build/ __pycache__/ 2>/dev/null || true

    log "✓ 清理完成"
}

# ---------- 修改编译参数 ----------
patch_setup() {
    step "3/6 配置编译参数"

    cd "$CYTHON_DIR"

    # 检查 setup.py
    if [[ ! -f setup.py ]]; then
        err "未找到 setup.py: $CYTHON_DIR/setup.py"
        exit 1
    fi

    # 环境变量方式控制
    export CFLAGS="${CFLAGS:-}"

    if [[ "$DEBUG" -eq 1 ]]; then
        export CFLAGS="$CFLAGS -O0 -g"
        log "编译模式: Debug（-O0 -g）"
    else
        export CFLAGS="$CFLAGS -O3"
        log "编译模式: Release（-O3）"
    fi

    if [[ "$NATIVE" -eq 1 ]]; then
        export CFLAGS="$CFLAGS -march=native"
        log "启用: -march=native"
    fi

    export NPY_NUM_BUILD_JOBS="$THREADS"
    log "并行编译: $THREADS 线程"

    log "✓ 参数配置完成"
}

# ---------- 编译 ----------
compile_cython() {
    step "4/6 编译 Cython 模块"

    cd "$CYTHON_DIR"

    log "开始编译..."
    local start_time
    start_time=$(date +%s)

    # 用 build_ext --inplace
    if ! "$PYTHON" setup.py build_ext --inplace \
        --parallel "$THREADS" 2>&1 | tee /tmp/cython_build.log; then

        err "编译失败"
        echo ""
        echo "错误日志（最后 30 行）:"
        tail -30 /tmp/cython_build.log
        exit 1
    fi

    local end_time
    end_time=$(date +%s)
    local elapsed=$((end_time - start_time))

    log "编译耗时: ${elapsed}s"

    # 显示编译产物
    log "编译产物:"
    find . -maxdepth 1 -name "*.so" -o -name "*.pyd" 2>/dev/null | \
        while read -r f; do
            local size
            size=$(du -h "$f" | cut -f1)
            echo "  $(basename "$f")  ($size)"
        done

    log "✓ 编译完成"
}

# ---------- 验证 ----------
verify_modules() {
    step "5/6 验证 Cython 模块"

    cd "$PROJECT_ROOT"

    "$PYTHON" <<'PYEOF'
import sys
sys.path.insert(0, '.')

try:
    from lightdb.cython import status
except ImportError as e:
    print(f"❌ 无法导入 lightdb.cython: {e}")
    sys.exit(1)

st = status()
avail = st['available']

print(f"\n  模块可用性:")
for name, ok in avail.items():
    icon = '✅' if ok else '❌'
    print(f"    {icon} {name}")

if st['errors']:
    print(f"\n  错误详情:")
    for name, err in st['errors'].items():
        print(f"    ⚠️ {name}: {err[:100]}")

total = len(avail)
ok_count = sum(1 for v in avail.values() if v)

print(f"\n  总计: {ok_count}/{total}")

if ok_count == 0:
    print("\n❌ 所有模块都不可用")
    sys.exit(1)
elif ok_count < total:
    print(f"\n⚠️ 部分模块缺失 ({ok_count}/{total})")
    sys.exit(0)
else:
    print("\n✅ 所有模块就绪")
    sys.exit(0)
PYEOF
}

# ---------- 性能对比 ----------
run_benchmark() {
    step "6/6 性能对比"

    cd "$PROJECT_ROOT"

    "$PYTHON" <<'PYEOF'
import sys
import time
sys.path.insert(0, '.')

def bench_python_btree(n=100000):
    """Python 版本 B+Tree 基准"""
    import bisect
    keys = list(range(n))
    values = {k: k * 10 for k in keys}
    sorted_keys = sorted(keys)

    t0 = time.perf_counter()
    for k in keys:
        idx = bisect.bisect_left(sorted_keys, k)
        if idx < len(sorted_keys) and sorted_keys[idx] == k:
            _ = values[sorted_keys[idx]]
    t1 = time.perf_counter()
    return t1 - t0

def bench_cython_btree(n=100000):
    """Cython 版本 B+Tree 基准"""
    try:
        from lightdb.cython import btree_pyx
    except ImportError:
        return None

    if btree_pyx is None:
        return None

    try:
        bt = btree_pyx.RawBTreeIndex(n + 10)
        t0 = time.perf_counter()
        for i in range(n):
            bt.insert(i, 1, i)
        for i in range(n):
            bt.search(i)
        t1 = time.perf_counter()
        return t1 - t0
    except Exception as e:
        print(f"  ⚠️ Cython B+Tree 失败: {e}")
        return None

def bench_python_codec(n=100000):
    """Python 版本序列化基准"""
    import struct
    fmt = struct.Struct('<q')
    values = list(range(n))

    t0 = time.perf_counter()
    b = b''.join(fmt.pack(v) for v in values)
    _ = [fmt.unpack_from(b, i*8)[0] for i in range(n)]
    t1 = time.perf_counter()
    return t1 - t0

def bench_cython_codec(n=100000):
    """Cython 版本序列化基准"""
    try:
        from lightdb.cython import row_codec
    except ImportError:
        return None

    if row_codec is None:
        return None

    try:
        values = list(range(n))
        t0 = time.perf_counter()
        b = row_codec.encode_int64_list(values)
        _ = row_codec.decode_int64_list(b)
        t1 = time.perf_counter()
        return t1 - t0
    except Exception as e:
        print(f"  ⚠️ Cython 序列化失败: {e}")
        return None

print("\n" + "=" * 60)
print("  Python vs Cython 性能对比")
print("=" * 60)

N = 100000

# B+Tree
print(f"\n  [B+Tree] {N} 次操作")
py_time = bench_python_btree(N)
cy_time = bench_cython_btree(N)

print(f"    Python: {py_time*1000:>10.2f} ms")
if cy_time is not None:
    speedup = py_time / cy_time if cy_time > 0 else 0
    print(f"    Cython: {cy_time*1000:>10.2f} ms")
    print(f"    加速:   {speedup:>10.2f}x")
else:
    print(f"    Cython: N/A")

# 序列化
print(f"\n  [序列化] {N} 个整数")
py_time = bench_python_codec(N)
cy_time = bench_cython_codec(N)

print(f"    Python: {py_time*1000:>10.2f} ms")
if cy_time is not None:
    speedup = py_time / cy_time if cy_time > 0 else 0
    print(f"    Cython: {cy_time*1000:>10.2f} ms")
    print(f"    加速:   {speedup:>10.2f}x")
else:
    print(f"    Cython: N/A")

print("\n" + "=" * 60)
PYEOF

    log "✓ 性能对比完成"
}

# ---------- 打印总结 ----------
print_summary() {
    local elapsed="$1"

    echo ""
    echo -e "${CYAN}============================================================${NC}"
    echo -e "${CYAN}  Cython 构建完成${NC}"
    echo -e "${CYAN}============================================================${NC}"
    echo ""
    echo "  编译目录: $CYTHON_DIR"
    echo "  编译模式: $([[ "$DEBUG" -eq 1 ]] && echo "Debug" || echo "Release")"
    echo "  编译耗时: ${elapsed}s"
    echo "  并行线程: $THREADS"
    echo ""
    echo "  加载 Cython 模块:"
    echo "    from lightdb.cython import status"
    echo "    print(status())"
    echo ""
    echo "  使用方式（自动回退）:"
    echo "    from lightdb.cython import btree_pyx"
    echo "    if btree_pyx:"
    echo "        bt = btree_pyx.RawBTreeIndex()"
    echo ""
    echo -e "${CYAN}============================================================${NC}"
}

# ---------- 主流程 ----------
main() {
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}  LightDB Cython 构建脚本${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo "  项目根: $PROJECT_ROOT"
    echo "  Cython: $CYTHON_DIR"
    echo ""

    if [[ "$CLEAN" -eq 1 ]]; then
        clean_build
        log "✅ 清理完成"
        exit 0
    fi

    check_env
    clean_build
    patch_setup

    local start_time
    start_time=$(date +%s)

    compile_cython
    verify_modules

    local end_time
    end_time=$(date +%s)
    local elapsed=$((end_time - start_time))

    if [[ "$BENCHMARK" -eq 1 ]]; then
        run_benchmark
    fi

    print_summary "$elapsed"
}

main "$@"