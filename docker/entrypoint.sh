#!/bin/bash
# ============================================================
# LightDB 容器入口脚本 (v3 通用版)
#
# 用法:
#   docker run lightdb/server:3.0.0 server
#   docker run lightdb/server:3.0.0 web
#   docker run lightdb/server:3.0.0 cli
#   docker run lightdb/server:3.0.0 migrate
#   docker run lightdb/server:3.0.0 check
# ============================================================

set -euo pipefail

# ---------- 颜色 ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[ENTRYPOINT]${NC} $*"; }
warn() { echo -e "${YELLOW}[ENTRYPOINT]${NC} $*"; }
err()  { echo -e "${RED}[ENTRYPOINT]${NC} $*" >&2; }

# ---------- 环境变量默认值 ----------
export TZ="${TZ:-Asia/Shanghai}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export LIGHTDB_LOG_LEVEL="${LIGHTDB_LOG_LEVEL:-INFO}"
export LIGHTDB_DATA_DIR="${LIGHTDB_DATA_DIR:-/app/data}"
export LIGHTDB_CONFIG="${LIGHTDB_CONFIG:-/app/config.json}"
export LIGHTDB_HOST="${LIGHTDB_HOST:-0.0.0.0}"
export LIGHTDB_PORT="${LIGHTDB_PORT:-8888}"

# ---------- 准备目录 ----------
prepare_dirs() {
    mkdir -p "$LIGHTDB_DATA_DIR" \
             /app/logs \
             /app/backups \
             "${HOME:-/home/lightdb}/.lightdb_studio" 2>/dev/null || true
}

# ---------- 等待依赖服务 ----------
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

# ---------- 打印启动横幅 ----------
print_banner() {
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}  LightDB v3.0.0 - 单进程数据库${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo "  模式:         $1"
    echo "  数据目录:     $LIGHTDB_DATA_DIR"
    echo "  配置文件:     $LIGHTDB_CONFIG"
    echo "  监听地址:     $LIGHTDB_HOST:$LIGHTDB_PORT"
    echo "  日志级别:     $LIGHTDB_LOG_LEVEL"
    echo "  时区:         $TZ"
    echo -e "${BLUE}============================================================${NC}"
    echo ""
}

# ---------- 命令: server ----------
cmd_server() {
    print_banner "server"

    log "启动 LightDB 服务器..."
    exec python -m lightdb.main \
        --mode single \
        --host "$LIGHTDB_HOST" \
        --port "$LIGHTDB_PORT" \
        --config "$LIGHTDB_CONFIG"
}

# ---------- 命令: web ----------
cmd_web() {
    print_banner "web"

    local target_host="${LIGHTDB_HOST:-lightdb}"
    if [ "$target_host" = "0.0.0.0" ]; then
        target_host="lightdb"
    fi

    # 等待 LightDB 就绪
    wait_for_host "$target_host" "$LIGHTDB_PORT" "LightDB" 60 || true

    log "启动 Studio Web..."
    exec python -m web.start \
        --host 0.0.0.0 \
        --port "${WEB_PORT:-8080}"
}

# ---------- 命令: cli ----------
cmd_cli() {
    log "启动 LightDB CLI..."
    shift || true
    exec python -m lightdb.tools.cli \
        -h "${LIGHTDB_HOST:-127.0.0.1}" \
        -P "${LIGHTDB_PORT:-8888}" \
        "$@"
}

# ---------- 命令: migrate ----------
cmd_migrate() {
    log "启动数据库迁移工具..."
    wait_for_host "${LIGHTDB_HOST:-lightdb}" "$LIGHTDB_PORT" "LightDB" 60 || true

    exec python -m lightdb.tools.migrate "$@"
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

# ---------- 命令: bench ----------
cmd_bench() {
    log "启动压测客户端..."
    wait_for_host "${LIGHTDB_HOST:-lightdb}" "$LIGHTDB_PORT" "LightDB" 60 || true

    exec python -m lightdb.benchmark.cli "$@"
}

# ---------- 命令: bash ----------
cmd_bash() {
    log "进入交互式 Shell..."
    exec /bin/bash
}

# ---------- 主分发 ----------
main() {
    prepare_dirs

    local cmd="${1:-server}"
    shift || true

    case "$cmd" in
        server)   cmd_server   "$@" ;;
        web)      cmd_web      "$@" ;;
        cli)      cmd_cli      "$@" ;;
        migrate)  cmd_migrate  "$@" ;;
        check)    cmd_check    "$@" ;;
        bench)    cmd_bench    "$@" ;;
        bash|sh)  cmd_bash     "$@" ;;
        *)
            # 未知命令：当作 python 命令执行
            warn "未知命令 '$cmd'，直接执行..."
            exec "$cmd" "$@"
            ;;
    esac
}

main "$@"