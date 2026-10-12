#!/bin/bash
# =============================================================================
# LightDB Cython 编译脚本
#
# 真实状态（改动后，务必按此理解）
# --------------------------------
# * 本仓库 `lightdb/cython/*.pyx` 是 6 个**从未编译过**的 Cython 源码，
#   仓库里没有任何 .pyd/.so/.c 产物，也没有任何实测加速比。
# * 本机（Windows + D:\lightdb_project）**没有安装 Cython**，也**没有 C
#   编译器**（gcc / cl / clang 均未找到），所以本脚本在这里**不可能**
#   编译成功 —— 它会明确报告原因并**非零退出**，而不是假装成功。
# * 运行时**不受影响**：`lightdb/cython/fallback/` 提供纯 Python 等价实现，
#   `from lightdb.cython import btree_pyx` 一定拿到可调用对象（非 None）。
#   回退保证的是可用性/正确性，**不是性能**。
# * `lightdb/cython/*.pyx` 里的三处编译错误（缺 cimport、cpdef 指针参数）
#   已按 Cython 语义静态修好，但**未在本机验证编译**。
#   验证条件：Cython >= 3.0 + C 编译器（Windows: MSVC cl.exe；Linux: gcc）。
#
# 功能:
# - 只检测环境与源码（--check / --doctor）
# - 具备条件时编译 6 个 Cython 模块
# - 验证导入结果并如实报告 source = compiled 还是 python-fallback
# - 可选性能对比（--benchmark）
#
# 用法:
#   ./build_cython.sh --check          # 只体检，不编译（退出码即结论）
#   ./build_cython.sh                  # 编译（缺条件则非零退出）
#   ./build_cython.sh --clean          # 清理编译产物
#   ./build_cython.sh --benchmark      # 编译 + 性能对比
#   ./build_cython.sh --debug          # Debug 模式（-O0 -g）
#   ./build_cython.sh --no-native      # 不启用 -march=native（默认已不启用）
#   ./build_cython.sh --avx2           # 允许 MSVC 加 /arch:AVX2（默认不加）
# =============================================================================

set -euo pipefail

# ---------- 默认参数 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CYTHON_DIR="$PROJECT_ROOT/lightdb/cython"

CLEAN=0
BENCHMARK=0
DEBUG=0
CHECK_ONLY=0
NATIVE=0            # 默认**不**启用 -march=native（不再硬编码激进架构参数）
AVX2=0              # 默认**不**启用 /arch:AVX2（仅 MSVC 且显式要求时生效）
THREADS="${THREADS:-$( (nproc 2>/dev/null) || echo 4)}"
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
        --check|--doctor) CHECK_ONLY=1; shift ;;
        --native)     NATIVE=1; shift ;;
        --no-native)  NATIVE=0; shift ;;
        --avx2)       AVX2=1; shift ;;
        --python)     PYTHON="$2"; shift 2 ;;
        --threads)    THREADS="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,45p' "$0"
            exit 0
            ;;
        *) err "未知参数: $1"; exit 1 ;;
    esac
done

# ---------- 记录缺失项，统一在末尾决定退出码 ----------
MISSING=()
HAVE_CYTHON=0
COMPILER=""

# ---------- 环境检查 ----------
check_env() {
    step "1/5 环境检查（如实报告，不尝试 pip install）"

    if ! command -v "$PYTHON" &> /dev/null; then
        err "未找到 Python: $PYTHON"
        echo "  用 --python <路径> 指定解释器"
        exit 1
    fi
    local py_ver
    py_ver=$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
    log "Python: $py_ver"

    # Cython —— 只检测，不自动安装（本机 PyPI 不可达，装也装不上）
    if "$PYTHON" -c "import Cython" &> /dev/null; then
        local cy_ver
        cy_ver=$("$PYTHON" -c "import Cython; print(Cython.__version__)")
        log "Cython: $cy_ver"
        HAVE_CYTHON=1
    else
        HAVE_CYTHON=0
        MISSING+=("Cython（当前解释器 $PYTHON 里 import Cython 失败）")
        warn "Cython: ❌ 未安装 —— 本脚本不会自动安装（本机 PyPI 不可达）"
        info  "手动安装命令: $PYTHON -m pip install 'cython>=3.0.0'"
    fi

    # C 编译器
    for c in cl gcc clang cc; do
        if command -v "$c" &> /dev/null; then
            COMPILER="$c"
            break
        fi
    done
    if [[ -n "$COMPILER" ]]; then
        log "C 编译器: $COMPILER"
    else
        MISSING+=("C 编译器（cl / gcc / clang / cc 均未找到）")
        warn "C 编译器: ❌ 未找到"
        info "Windows: 安装 MSVC Build Tools（提供 cl.exe）"
        info "Debian/Ubuntu: apt install build-essential"
        info "macOS: xcode-select --install"
    fi

    # 源码清点（.pyx 是否齐全）
    step "2/5 源码清点"
    local n_pyx=0 n_artifacts=0
    n_pyx=$(find "$CYTHON_DIR" -maxdepth 1 -name '*.pyx' 2>/dev/null | wc -l | tr -d ' ')
    n_artifacts=$(find "$CYTHON_DIR" -maxdepth 1 \( -name '*.pyd' -o -name '*.so' \) 2>/dev/null | wc -l | tr -d ' ')
    info ".pyx 源码: $n_pyx 个"
    info "已有编译产物 (.pyd/.so): $n_artifacts 个"
    if [[ "$n_artifacts" -eq 0 ]]; then
        info "（0 个产物 = 本仓库的 Cython 层从未编译过；运行时会走纯 Python 回退）"
    fi

    if [[ ${#MISSING[@]} -gt 0 ]]; then
        return 1
    fi
    log "✓ 环境检查通过：具备编译条件"
    return 0
}

# ---------- 体检结论 ----------
report_check() {
    step "体检结论"
    if [[ ${#MISSING[@]} -eq 0 ]]; then
        log "✅ 具备编译条件（Cython $( "$PYTHON" -c 'import Cython; print(Cython.__version__)' ) + $COMPILER）"
        info "下一步: $0            # 真正编译"
        return 0
    fi
    err "❌ 不具备编译条件，缺少 ${#MISSING[@]} 项:"
    local i=1
    for m in "${MISSING[@]}"; do
        echo "    $i) $m"
        i=$((i + 1))
    done
    echo ""
    warn "后果: lightdb/cython/*.pyx 保持未编译，不会产生任何 .pyd/.so。"
    warn "运行时不受影响: lightdb/cython/fallback/ 提供纯 Python 等价实现，"
    warn "                但它**没有**加速效果 —— 请不要宣称已加速。"
    return 1
}

# ---------- 清理 ----------
clean_build() {
    step "3/5 清理旧构建"
    cd "$CYTHON_DIR"
    for ext in c cpp so pyd html; do
        # 注意: 只删编译产物，不删任何 .pyx / .py
        find . -maxdepth 1 -name "*.${ext}" -delete 2>/dev/null || true
    done
    rm -rf build/ 2>/dev/null || true
    # __pycache__ 只清 Cython 加载器产生的缓存
    rm -rf __pycache__/ 2>/dev/null || true
    log "✓ 清理完成（.pyx 与 .py 未被触碰）"
}

# ---------- 编译参数（透传给 setup.py，避免硬编码架构）----------
export_build_env() {
    step "4/5 配置编译参数"

    if [[ "$DEBUG" -eq 1 ]]; then
        export CFLAGS="${CFLAGS:-} -O0 -g"
        log "编译模式: Debug（-O0 -g）"
    else
        export CFLAGS="${CFLAGS:-} -O3"
        log "编译模式: Release（-O3）"
    fi

    if [[ "$NATIVE" -eq 1 ]]; then
        export LIGHTDB_CYTHON_NATIVE=1
        log "启用: -march=native（显式要求）"
    else
        export LIGHTDB_CYTHON_NATIVE=0
        info "未启用 -march=native（默认保守；--native 可开启）"
    fi

    if [[ "$AVX2" -eq 1 ]]; then
        export LIGHTDB_CYTHON_AVX2=1
        info "请求 /arch:AVX2（setup.py 仅在确认为 MSVC 时才真正加上）"
    else
        export LIGHTDB_CYTHON_AVX2=0
        info "未请求 /arch:AVX2（默认；--avx2 可开启）"
    fi

    export LIGHTDB_CYTHON_THREADS="$THREADS"
    export NPY_NUM_BUILD_JOBS="$THREADS"
    log "并行编译: $THREADS 线程"
}

# ---------- 编译 ----------
compile_cython() {
    step "5/5 编译 Cython 模块"
    cd "$CYTHON_DIR"

    # 先跑一次自检，让 setup.py 打印体检表
    if ! "$PYTHON" setup.py --check; then
        err "setup.py 自检未通过：缺少编译条件"
        return 1
    fi

    log "开始编译..."
    local start_time end_time
    start_time=$(date +%s)

    local log_file="${TMPDIR:-/tmp}/lightdb_cython_build.log"
    if ! "$PYTHON" setup.py build_ext --inplace \
            --parallel "$THREADS" 2>&1 | tee "$log_file"; then
        err "编译失败（日志: $log_file）"
        echo ""
        echo "错误日志（最后 30 行）:"
        tail -30 "$log_file"
        return 1
    fi

    end_time=$(date +%s)
    log "编译耗时: $((end_time - start_time))s"

    # 产物必须真实存在，否则视为失败
    log "编译产物:"
    local found=0
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        found=$((found + 1))
        echo "  $(basename "$f")  ($(du -h "$f" | cut -f1))"
    done < <(find . -maxdepth 1 \( -name '*.so' -o -name '*.pyd' \) 2>/dev/null)
    if [[ "$found" -eq 0 ]]; then
        err "编译命令返回 0，但没有找到任何 .pyd/.so —— 视为失败"
        return 1
    fi
    log "✓ 编译完成，共 $found 个产物"
}

# ---------- 验证 ----------
verify_modules() {
    step "验证加载结果（如实区分 compiled / python-fallback）"
    cd "$PROJECT_ROOT"

    # 这里要读子进程退出码：必须临时关掉 set -e，否则非零会直接终止脚本
    set +e
    "$PYTHON" - <<'PYEOF'
import sys
sys.path.insert(0, '.')
from lightdb.cython import status

st = status()
print("\n  模块来源:")
for name, src in st['available'].items():
    icon = {'compiled': '✅', 'python-fallback': '🐍'}.get(src, '❌')
    print(f"    {icon} {name:<12} {src}")

if st['fallback']:
    print(f"\n  🐍 纯 Python 回退: {', '.join(st['fallback'])}")
    print("     （可调用，但**没有**加速）")
if st['errors']:
    print("\n  编译模块缺失原因:")
    for name, err in st['errors'].items():
        print(f"    {name}: {err[:110]}")
if st['fallback_errors']:
    print("\n  回退模块也失败（不应出现）:")
    for name, err in st['fallback_errors'].items():
        print(f"    {name}: {err[:110]}")

compiled = len(st['compiled'])
total = len(st['available'])
print(f"\n  编译版: {compiled}/{total}")
print(f"  加速已启用: {'是' if st['any_accelerated'] else '否'}")

if not st['all_available']:
    print("\n❌ 有模块既无编译版也无回退版")
    sys.exit(1)
if compiled == 0:
    print("\n⚠️ 没有任何编译产物：本次构建**未产生加速**，全部走纯 Python 回退。")
    sys.exit(2)          # 与"缺条件"区分：这是编译没产出
print(f"\n✅ {compiled} 个模块为编译版本")
sys.exit(0)
PYEOF

    local rc=$?
    set -e
    case "$rc" in
        0) log "✓ 编译版模块已就绪" ; return 0 ;;
        2) err "没有任何模块以编译版加载：请检查上面的编译输出" ; return 1 ;;
        *) err "模块加载验证失败 (rc=$rc)" ; return 1 ;;
    esac
}

# ---------- 性能对比 ----------
run_benchmark() {
    step "性能对比（仅当编译版可用时才有意义）"
    cd "$PROJECT_ROOT"

    "$PYTHON" - <<'PYEOF'
import sys, time
sys.path.insert(0, '.')
from lightdb.cython import status

st = status()
if not st['any_accelerated']:
    print("\n  ⚠️ 没有任何编译版模块，性能对比**无意义**，已跳过。")
    print("     纯 Python 回退不比既有 Python 实现快；不要据此生成加速比。")
    sys.exit(0)

from lightdb.cython import btree_pyx, row_codec

N = 100000
print(f"\n  编译版模块: {', '.join(st['compiled'])}")

# B+Tree
idx = btree_pyx.RawBTreeIndex(capacity=N + 10)
t0 = time.perf_counter()
for i in range(N):
    idx.insert(i, i, i)
for i in range(N):
    idx.search(i)
t1 = time.perf_counter()
print(f"  [B+Tree] 编译版 {N} 插+查: {(t1-t0)*1000:.1f} ms")

# 序列化
vals = list(range(N))
t0 = time.perf_counter()
b = row_codec.encode_int64_list(vals)
_ = row_codec.decode_int64_list(b)
t1 = time.perf_counter()
print(f"  [编解码] 编译版 {N} 个整数: {(t1-t0)*1000:.1f} ms")
print("  （对比基线需另行用纯 Python 路径测量，本脚本不虚构数字）")
PYEOF
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
    echo "  编译模式: $( [[ "$DEBUG" -eq 1 ]] && echo 'Debug' || echo 'Release' )"
    echo "  编译耗时: ${elapsed}s"
    echo "  并行线程: $THREADS"
    echo "  /arch:AVX2: $( [[ "$AVX2" -eq 1 ]] && echo '请求（仅 MSVC 生效）' || echo '未请求' )"
    echo ""
    echo "  查看状态:"
    echo "    $PYTHON -c \"from lightdb.cython import status; print(status())\""
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
    echo "  说明: 本脚本不自动 pip install；缺条件时明确报错并非零退出。"
    echo ""

    if [[ "$CLEAN" -eq 1 ]]; then
        clean_build
        log "✅ 清理完成"
        exit 0
    fi

    # 环境检查：失败且是 --check，就直接给出结论退出
    local env_ok=1
    check_env || env_ok=0

    if [[ "$CHECK_ONLY" -eq 1 ]]; then
        report_check || exit 1
        exit 0
    fi

    if [[ "$env_ok" -eq 0 ]]; then
        echo ""
        err "============================================================"
        err "  构建中止：缺少编译 Cython 所需的条件，未生成任何产物。"
        err "============================================================"
        local i=1
        for m in "${MISSING[@]}"; do
            echo "    $i) $m" >&2
            i=$((i + 1))
        done
        echo "" >&2
        warn "运行时不受影响: lightdb.cython 会自动使用纯 Python 回退"
        warn "（可调用，但没有加速效果）。请勿据此宣称已加速。"
        exit 1        # ← 关键：明确非零退出，不再假装成功
    fi

    clean_build
    export_build_env

    local start_time end_time
    start_time=$(date +%s)

    compile_cython
    verify_modules

    end_time=$(date +%s)
    local elapsed=$((end_time - start_time))

    if [[ "$BENCHMARK" -eq 1 ]]; then
        run_benchmark
    fi

    print_summary "$elapsed"
}

main "$@"
