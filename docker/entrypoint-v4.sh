#!/bin/bash
# ============================================================
# LightDB v4.0 容器入口脚本 (多进程 + Cython)
#
# 用法:
#   docker run lightdb/server:4.0.0 server          # 多进程 server
#   docker run lightdb/server:4.0.0 server-single   # 单进程 server
#   docker run lightdb/server:4.0.0 web
#   docker run lightdb/server:4.0.0 cli
#   docker run lightdb/server:4.0.0 check
#   docker run lightdb/server:4.0.0 bench
#   docker run lightdb/server:4.0.0 bash
# ============================================================

set -euo pipefail

# ---------- 颜色 ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log()   { echo -e "${GREEN}[ENTRYPOINT-v4]${NC} $*"; }
warn()  { echo -e "${YELLOW}[ENTRYPOINT-v4]${NC} $*"; }
err()   { echo -e "${RED}[ENTRYPOINT-v4]${NC} $*" >&2; }
info()  { echo -e "${CYAN}[ENTRYPOINT-v4]${NC} $*"; }

# ---------- 环境变量默认值 ----------
export TZ="${TZ:-Asia/Shanghai}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export LIGHTDB_LOG_LEVEL="${LIGHTDB_LOG_LEVEL:-INFO}"
export LIGHTDB_DATA_DIR="${LIGHTDB_DATA_DIR:-/app/data}"
export LIGHTDB_CONFIG="${LIGHTDB_CONFIG:-/app/config.json}"
export LIGHTDB_HOST="${LIGHTDB_HOST:-0.0.0.0}"
export LIGHTDB_PORT="${LIGHTDB_PORT:-8888}"
export LIGHTDB_WORKERS="${LIGHTDB_WORKERS:-0}"

# Cython / OpenMP 相关
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"

# 共享内存目录
export LIGHTDB_SHM_DIR="${LIGHTDB_SHM_DIR:-/dev/shm}"

# ---------- 准备目录 ----------
prepare_dirs() {
    mkdir -p "$LIGHTDB_DATA_DIR" \
             /app/logs \
             /app/backups \
             "$LIGHTDB_SHM_DIR" \
             "${HOME:-/home/lightdb}/.lightdb_studio" 2>/dev/null || true

    # 清理残留的共享内存文件（避免上次崩溃残留）
    if [ -d "$LIGHTDB_SHM_DIR" ]; then
        find "$LIGHTDB_SHM_DIR" -maxdepth 1 -name 'lightdb_*' -type f \
            -mmin +60 -delete 2>/dev/null || true
    fi
}

# ---------- 计算 Worker 数 ----------
calc_workers() {
    if [ "$LIGHTDB_WORKERS" = "0" ]; then
        local cpu_count
        cpu_count=$(nproc 2>/dev/null || echo 4)
        # 保留 1 核给系统
        local workers=$((cpu_count > 1 ? cpu_count - 1 : 1))
        echo "$workers"
    else
        echo "$LIGHTDB_WORKERS"
    fi
}

# ---------- 检查 Cython 模块状态 ----------
check_cython() {
    info "检查 Cython 加速模块..."
    python -c "
import sys
try:
    from lightdb.cython import status
    st = status()
    avail = st['available']
    all_ok = all(avail.values())
    for name, ok in avail.items():
        icon = '✅' if ok else '❌'
        print(f'  {icon} {name}')
    if not all_ok:
        errs = st.get('errors', {})
        for name, e in errs.items():
            print(f'  ⚠️ {name}: {e}')
    sys.exit(0 if all_ok else 1)
except Exception as e:
    print(f'  ⚠️ Cython 状态检查失败: {e}')
    sys.exit(0)
" 2>&1 || warn "Cython 模块部分缺失，将回退 Python 版本"
}

# ---------- 检查共享内存 ----------
check_shm() {
    local shm_size
    if [ -d "$LIGHTDB_SHM_DIR" ]; then
        shm_size=$(df -h "$LIGHTDB_SHM_DIR" 2>/dev/null | awk 'NR==2 {print $2}')
        info "共享内存目录: $LIGHTDB_SHM_DIR ($shm_size)"
        if [ -z "$shm_size" ]; then
            warn "共享内存不可用，多进程性能将受影响"
        fi
    fi
}

# ---------- 等待依赖 ----------
wait_for_host() {
    local host="$1"
    local port="$2"
    local name="${3:-$host}"
    local max_wait="${4:-60}"

    log "等待 $name ($host:$port) 就绪..."

    local elapsed=0
    while [ $elapsed -lt $max_wait ]; do
        if nc -z "$host" "$port" 2>/dev/null; then
            log "✓ $name 就绪（${elapsed}s）"
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done

    warn "⚠ $name 等待超时（${max_wait}s）"
    return 1
}

# ---------- 启动横幅 ----------
print_banner() {
    local mode="$1"
    local workers="$2"
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}  LightDB v4.0.0 - 多进程 + Cython 性能版${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo "  模式:         $mode"
    echo "  数据目录:     $LIGHTDB_DATA_DIR"
    echo "  配置文件:     $LIGHTDB_CONFIG"
    echo "  监听地址:     $LIGHTDB_HOST:$LIGHTDB_PORT"
    echo "  Worker 数:    $workers"
    echo "  日志级别:     $LIGHTDB_LOG_LEVEL"
    echo "  共享内存:     $LIGHTDB_SHM_DIR"
    echo "  CPU 核数:     $(nproc 2>/dev/null || echo '?')"
    echo "  OMP 线程数:   $OMP_NUM_THREADS"
    echo "  时区:         $TZ"
    echo -e "${BLUE}============================================================${NC}"
    echo ""
}

# ---------- 命令: server (多进程) ----------
cmd_server() {
    local workers
    workers=$(calc_workers)

    print_banner "server (multi-process)" "$workers"
    check_cython
    check_shm

    log "启动 LightDB 多进程服务器..."

    # 用 exec 替换当前进程，让 tini 能正确转发信号
    exec python -m lightdb.main \
        --mode multi \
        --workers "$workers" \
        --host "$LIGHTDB_HOST" \
        --port "$LIGHTDB_PORT" \
        --config "$LIGHTDB_CONFIG"
}

# ---------- 命令: server-single (单进程) ----------
cmd_server_single() {
    print_banner "server (single-process)" 1
    check_cython

    log "启动 LightDB 单进程服务器..."

    exec python -m lightdb.main \
        --mode single \
        --host "$LIGHTDB_HOST" \
        --port "$LIGHTDB_PORT" \
        --config "$LIGHTDB_CONFIG"
}

# ---------- 命令: web ----------
cmd_web() {
    print_banner "studio-web" 0

    local target_host="${LIGHTDB_HOST:-lightdb}"
    if [ "$target_host" = "0.0.0.0" ]; then
        target_host="lightdb"
    fi

    wait_for_host "$target_host" "$LIGHTDB_PORT" "LightDB" 60 || true

    log "启动 Studio Web..."
    exec python -m web.start \
        --host 0.0.0.0 \
        --port "${WEB_PORT:-8080}"
}

# ---------- 命令: cli ----------
cmd_cli() {
    log "启动 LightDB CLI..."
    exec python -m lightdb.tools.cli \
        -h "${LIGHTDB_HOST:-127.0.0.1}" \
        -P "${LIGHTDB_PORT:-8888}" \
        "$@"
}

# ---------- 命令: check ----------
cmd_check() {
    log "运行健康检查..."
    wait_for_host "${LIGHTDB_HOST:-lightdb}" "$LIGHTDB_PORT" "LightDB" 60 || true

    exec python -m lightdb.tools.check \
        -h "${LIGHTDB_HOST:-lightdb}" \
        -P "$LIGHTDB_PORT" \
        "$@"
}

# ---------- 命令: tune ----------
cmd_tune() {
    log "运行参数调优建议..."
    wait_for_host "${LIGHTDB_HOST:-lightdb}" "$LIGHTDB_PORT" "LightDB" 60 || true

    exec python -m lightdb.tools.tune \
        -h "${LIGHTDB_HOST:-lightdb}" \
        -P "$LIGHTDB_PORT" \
        "$@"
}

# ---------- 命令: diagnose ----------
cmd_diagnose() {
    log "运行性能诊断..."
    exec python -m lightdb.tools.diagnose "$@"
}

# ---------- 命令: bench ----------
cmd_bench() {
    log "启动压测客户端..."
    wait_for_host "${LIGHTDB_HOST:-lightdb}" "$LIGHTDB_PORT" "LightDB" 60 || true

    exec python -m lightdb.benchmark.cli \
        --host "${LIGHTDB_HOST:-lightdb}" \
        --port "$LIGHTDB_PORT" \
        "$@"
}

# ---------- 命令: bash ----------
cmd_bash() {
    log "进入交互式 Shell..."
    info "Cython 状态:"
    check_cython || true
    exec /bin/bash
}

# ---------- 信号处理 ----------
handle_signal() {
    local sig="$1"
    warn "收到信号: $sig，正在优雅退出..."
    # Master 会自己处理子进程
    exit 0
}

trap 'handle_signal SIGTERM' SIGTERM
trap 'handle_signal SIGINT'  SIGINT

# ---------- 主分发 ----------
main() {
    prepare_dirs

    local cmd="${1:-server}"
    shift || true

    case "$cmd" in
        server)         cmd_server        "$@" ;;
        server-single)  cmd_server_single "$@" ;;
        web)            cmd_web           "$@" ;;
        cli)            cmd_cli           "$@" ;;
        check)          cmd_check         "$@" ;;
        tune)           cmd_tune          "$@" ;;
        diagnose)       cmd_diagnose      "$@" ;;
        bench)          cmd_bench         "$@" ;;
        bash|sh)        cmd_bash          "$@" ;;
        *)
            warn "未知命令 '$cmd'，直接执行..."
            exec "$cmd" "$@"
            ;;
    esac
}

main "$@"