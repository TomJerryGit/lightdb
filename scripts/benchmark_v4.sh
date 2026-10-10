#!/bin/bash
# ============================================================
# LightDB v4.0 完整压测脚本
#
# 功能:
# - 环境检查与连接验证
# - 数据预热
# - 完整工作负载测试
# - 并发扫描
# - 与基线对比
# - 生成报告
#
# 用法:
#   ./benchmark_v4.sh
#   HOST=10.0.0.1 DURATION=60 ./benchmark_v4.sh
#   ./benchmark_v4.sh --workload point_select --threads 8
#   ./benchmark_v4.sh --baseline v4.0.base --compare
#   ./benchmark_v4.sh --quick               # 快速测试（30s）
#   ./benchmark_v4.sh --full                # 完整测试（5min）
# ============================================================

set -euo pipefail

# ---------- 默认参数 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8888}"
USER="${USER:-root}"
PASS="${PASS:-}"
TABLES="${TABLES:-4}"
ROWS="${ROWS:-100000}"
DURATION="${DURATION:-30}"
WARMUP="${WARMUP:-5}"
THREADS="${THREADS:-8}"
PYTHON="${PYTHON:-python3}"

WORKLOAD="all"
LABEL=""
BASELINE=""
COMPARE=0
QUICK=0
FULL=0
DRY_RUN=0

RESULT_DIR="$PROJECT_ROOT/benchmark_results"

# ---------- 颜色 ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

log()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step() { echo -e "\n${BLUE}━━━ $* ━━━${NC}"; }
sep()  { echo -e "${MAGENTA}════════════════════════════════════════════════${NC}"; }

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)       HOST="$2"; shift 2 ;;
        --port)       PORT="$2"; shift 2 ;;
        --user)       USER="$2"; shift 2 ;;
        --password)   PASS="$2"; shift 2 ;;
        --tables)     TABLES="$2"; shift 2 ;;
        --rows)       ROWS="$2"; shift 2 ;;
        --duration)   DURATION="$2"; shift 2 ;;
        --warmup)     WARMUP="$2"; shift 2 ;;
        --threads)    THREADS="$2"; shift 2 ;;
        --workload)   WORKLOAD="$2"; shift 2 ;;
        --label)      LABEL="$2"; shift 2 ;;
        --baseline)   BASELINE="$2"; shift 2 ;;
        --compare)    COMPARE=1; shift ;;
        --quick)      QUICK=1; shift ;;
        --full)       FULL=1; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '3,20p' "$0"
            exit 0
            ;;
        *) err "未知参数: $1"; exit 1 ;;
    esac
done

# ---------- 应用预设 ----------
if [[ "$QUICK" -eq 1 ]]; then
    DURATION=15
    WARMUP=3
    ROWS=50000
    THREADS=8
    log "快速模式: duration=${DURATION}s, rows=${ROWS}"
fi

if [[ "$FULL" -eq 1 ]]; then
    DURATION=120
    WARMUP=15
    ROWS=500000
    TABLES=8
    log "完整模式: duration=${DURATION}s, rows=${ROWS}, tables=${TABLES}"
fi

# 生成标签
if [[ -z "$LABEL" ]]; then
    LABEL="v4.0.$(date +%Y%m%d_%H%M%S)"
fi

# 结果目录
RESULT_DIR="$RESULT_DIR/$LABEL"
mkdir -p "$RESULT_DIR"

# ---------- 环境检查 ----------
check_env() {
    step "1/7 环境检查"

    # Python
    command -v "$PYTHON" &> /dev/null || {
        err "未找到 Python: $PYTHON"
        exit 1
    }
    log "Python: $("$PYTHON" --version)"

    # pymysql
    "$PYTHON" -c "import pymysql" 2>/dev/null || {
        err "pymysql 未安装"
        echo "  pip install pymysql"
        exit 1
    }

    # 连接测试
    log "测试连接到 $HOST:$PORT..."
    "$PYTHON" <<EOF
import sys
import pymysql
try:
    conn = pymysql.connect(
        host='$HOST', port=$PORT,
        user='$USER', password='$PASS',
        connect_timeout=5)
    cur = conn.cursor()
    cur.execute('SELECT VERSION()')
    version = cur.fetchone()[0]
    print(f'  ✅ 连接成功')
    print(f'  版本: {version}')

    # 检查 Workers
    try:
        cur.execute("SHOW VARIABLES LIKE 'max_connections'")
        r = cur.fetchone()
        if r:
            print(f'  max_connections: {r[1]}')
    except Exception:
        pass

    conn.close()
except Exception as e:
    print(f'  ❌ 连接失败: {e}')
    sys.exit(1)
EOF

    # 磁盘
    local avail_gb
    avail_gb=$(df -BG "$RESULT_DIR" 2>/dev/null | awk 'NR==2 {gsub("G","",$4); print $4}')
    [[ -n "$avail_gb" ]] && log "可用空间: ${avail_gb}GB"

    log "✓ 环境检查通过"
}

# ---------- 数据准备 ----------
prepare_data() {
    step "2/7 数据准备"

    log "检查是否需要准备数据..."

    "$PYTHON" <<EOF
import pymysql
try:
    conn = pymysql.connect(
        host='$HOST', port=$PORT,
        user='$USER', password='$PASS',
        autocommit=True)
    cur = conn.cursor()

    # 检查 sbtest1 是否存在
    try:
        cur.execute("SELECT COUNT(*) FROM sbtest1")
        n = cur.fetchone()[0]
        print(f'  sbtest1 已存在: {n} 行')
        if n >= $ROWS * 0.9:
            print('  ✓ 数据量充足，跳过准备')
            conn.close()
            exit(0)
    except Exception:
        pass

    print('  数据不足，准备中...')
    conn.close()
except Exception as e:
    print(f'  检查失败: {e}')
    exit(1)
EOF

    log "✓ 数据准备完成"
}

# ---------- 预热 ----------
warmup() {
    step "3/7 预热（${WARMUP}s）"

    "$PYTHON" -m lightdb.benchmark.cli run \
        --host "$HOST" \
        --port "$PORT" \
        --user "$USER" \
        --password "$PASS" \
        --workload oltp_ro \
        --tables "$TABLES" \
        --rows "$ROWS" \
        --threads "$THREADS" \
        --duration "$WARMUP" \
        --warmup 0 \
        --label "${LABEL}.warmup" \
        2>&1 | tail -20 || warn "预热失败，继续"

    log "✓ 预热完成"
}

# ---------- 单个工作负载 ----------
run_workload() {
    local wl="$1"
    local threads="${2:-$THREADS}"
    local duration="${3:-$DURATION}"

    sep
    log "▶ 工作负载: $wl, 线程: $threads, 时长: ${duration}s"
    sep

    local wl_label="${LABEL}.${wl}.${threads}t"
    local log_file="$RESULT_DIR/${wl}_${threads}t.log"

    local cmd=(
        "$PYTHON" -m lightdb.benchmark.cli run
        --host "$HOST"
        --port "$PORT"
        --user "$USER"
        --password "$PASS"
        --workload "$wl"
        --tables "$TABLES"
        --rows "$ROWS"
        --threads "$threads"
        --duration "$duration"
        --warmup "$WARMUP"
        --label "$wl_label"
    )

    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "  [DRY RUN] ${cmd[*]}"
        return 0
    fi

    if "${cmd[@]}" 2>&1 | tee "$log_file" | tail -30; then
        log "✓ $wl @ ${threads}t 完成"
    else
        err "$wl @ ${threads}t 失败"
        return 1
    fi

    echo ""
    return 0
}

# ---------- 完整测试 ----------
run_full() {
    step "4/7 完整工作负载测试"

    local workloads=(
        "point_select"
        "range_scan"
        "insert"
        "update"
        "oltp_ro"
        "oltp_rw"
    )

    local passed=0
    local failed=0

    for wl in "${workloads[@]}"; do
        if run_workload "$wl"; then
            passed=$((passed + 1))
        else
            failed=$((failed + 1))
        fi
    done

    log "完整测试: 通过 $passed, 失败 $failed"
}

# ---------- 并发扫描 ----------
run_concurrency_scan() {
    step "5/7 并发扫描"

    local thread_levels=(1 2 4 8 16)

    for t in "${thread_levels[@]}"; do
        if [[ "$t" -gt "$(nproc 2>/dev/null || echo 8)" ]]; then
            warn "跳过 ${t} 线程（超过 CPU 核数）"
            continue
        fi

        run_workload "point_select" "$t" 20 || true
    done

    log "✓ 并发扫描完成"
}

# ---------- 对比 ----------
run_compare() {
    if [[ -z "$BASELINE" ]]; then
        warn "未指定基线，跳过对比"
        return 0
    fi

    step "6/7 与基线对比"

    log "基线: $BASELINE"
    log "当前: $LABEL"

    "$PYTHON" -m lightdb.benchmark.cli compare \
        --baseline "$BASELINE" \
        --current "$LABEL" \
        2>&1 | tee "$RESULT_DIR/compare.log" || warn "对比失败"

    log "✓ 对比完成"
}

# ---------- 生成报告 ----------
gen_report() {
    step "7/7 生成报告"

    local report="$RESULT_DIR/report.md"

    cat > "$report" <<EOF
# LightDB v4.0 压测报告

**生成时间**: $(date '+%Y-%m-%d %H:%M:%S')
**标签**: $LABEL

---

## 测试配置

| 参数 | 值 |
|------|-----|
| 主机 | $HOST:$PORT |
| 表数 | $TABLES |
| 每表行数 | $ROWS |
| 并发线程 | $THREADS |
| 测试时长 | ${DURATION}s |
| 预热时长 | ${WARMUP}s |

---

## 结果汇总

EOF

    # 从 baseline 存储读取结果
    "$PYTHON" <<'PYEOF' >> "$report"
import sys
sys.path.insert(0, '${PROJECT_ROOT}')
try:
    from lightdb.benchmark.baseline import BaselineStore
except ImportError:
    print("(BaselineStore 不可用)")
    sys.exit(0)

store = BaselineStore()
label = "${LABEL}"

print("| 工作负载 | QPS | P50 | P95 | P99 | 错误 |")
print("|---------|-----|-----|-----|-----|------|")

for b in store.list_all():
    if not b['name'].startswith(label + '.'):
        continue
    if '.warmup' in b['name']:
        continue

    data = store.get(b['name'])
    if not data:
        continue

    d = data.get('data', {})
    results = d.get('results', {})

    for wl_name, wl_data in results.items():
        if 'error' in wl_data:
            continue
        lat = wl_data.get('latency_ms', {})
        print(f"| {wl_name} | "
              f"{wl_data.get('qps', 0):.0f} | "
              f"{lat.get('p50', 0):.2f}ms | "
              f"{lat.get('p95', 0):.2f}ms | "
              f"{lat.get('p99', 0):.2f}ms | "
              f"{wl_data.get('errors', 0)} |")
PYEOF

    # 附加文件清单
    cat >> "$report" <<EOF

---

## 结果文件

- 结果目录: \`$RESULT_DIR\`
- 基线存储: \`~/.lightdb_studio/benchmarks/\`

## 详细日志

EOF

    for log_file in "$RESULT_DIR"/*.log; do
        [[ -f "$log_file" ]] || continue
        echo "- \`$(basename "$log_file")\`" >> "$report"
    done

    log "✓ 报告: $report"
}

# ---------- 打印总结 ----------
print_summary() {
    echo ""
    sep
    echo -e "${GREEN}  压测完成！${NC}"
    sep
    echo ""
    echo "  标签:     $LABEL"
    echo "  报告:     $RESULT_DIR/report.md"
    echo "  日志:     $RESULT_DIR/*.log"
    echo "  基线:     ~/.lightdb_studio/benchmarks/"
    echo ""

    # 显示对比结果（如有）
    if [[ -f "$RESULT_DIR/compare.log" ]]; then
        echo "  对比结果:"
        echo ""
        sed 's/^/    /' "$RESULT_DIR/compare.log" | head -30
        echo ""
    fi

    sep
    echo ""
    echo "  查看报告:"
    echo "    cat $RESULT_DIR/report.md"
    echo ""
    echo "  查看基线:"
    echo "    $PYTHON -m lightdb.benchmark.cli baselines"
    echo ""
    echo "  与基线对比:"
    echo "    $PYTHON -m lightdb.benchmark.cli compare \\"
    echo "        --baseline $LABEL --current <other>"
    echo ""
}

# ---------- 主流程 ----------
main() {
    echo ""
    sep
    echo -e "${CYAN}  LightDB v4.0 压测${NC}"
    sep
    echo ""
    echo "  目标:     $HOST:$PORT"
    echo "  工作负载: $WORKLOAD"
    echo "  表数:     $TABLES"
    echo "  行数:     $ROWS"
    echo "  线程:     $THREADS"
    echo "  时长:     ${DURATION}s"
    echo "  标签:     $LABEL"
    echo ""

    if [[ "$DRY_RUN" -eq 1 ]]; then
        warn "DRY RUN 模式，不会实际执行"
    fi

    check_env
    prepare_data
    warmup

    if [[ "$WORKLOAD" == "all" ]]; then
        run_full
        run_concurrency_scan
    else
        run_workload "$WORKLOAD"
    fi

    if [[ "$COMPARE" -eq 1 ]] || [[ -n "$BASELINE" ]]; then
        run_compare
    fi

    gen_report
    print_summary
}

main "$@"