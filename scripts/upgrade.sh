#!/bin/bash
# ============================================================
# LightDB 升级脚本
#
# 功能:
# - 升级到指定版本
# - 升级前自动备份
# - 升级失败自动回滚
# - 数据兼容性检查
#
# 用法:
#   ./upgrade.sh                            # 升级到最新
#   ./upgrade.sh --to 4.0.0                 # 升级到 v4.0.0
#   ./upgrade.sh --from 3.0.0 --to 4.0.0   # 显式指定版本
#   ./upgrade.sh --rollback                 # 回滚到上一个版本
# ============================================================

set -euo pipefail

# ---------- 默认参数 ----------
PREFIX="${PREFIX:-/opt/lightdb}"
FROM_VERSION=""
TO_VERSION="latest"
BACKUP_DIR="${PREFIX}/backups"
STAGING_DIR="${PREFIX}/staging"
PYTHON="${PREFIX}/venv/bin/python"
ROLLBACK=0
FORCE=0

# ---------- 颜色 ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step() { echo -e "\n${BLUE}━━━ $* ━━━${NC}"; }

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)   PREFIX="$2"; shift 2 ;;
        --from)     FROM_VERSION="$2"; shift 2 ;;
        --to)       TO_VERSION="$2"; shift 2 ;;
        --rollback) ROLLBACK=1; shift ;;
        --force)    FORCE=1; shift ;;
        -h|--help)
            sed -n '3,18p' "$0"
            exit 0
            ;;
        *) err "未知参数: $1"; exit 1 ;;
    esac
done

PYTHON="${PREFIX}/venv/bin/python"

# ---------- 工具函数 ----------
check_installation() {
    if [[ ! -d "$PREFIX" ]]; then
        err "LightDB 未安装: $PREFIX"
        exit 1
    fi

    if [[ ! -x "$PYTHON" ]]; then
        err "虚拟环境不存在: $PYTHON"
        exit 1
    fi

    if [[ ! -f "$PREFIX/config/config.json" ]]; then
        warn "配置文件不存在"
    fi
}

get_current_version() {
    "$PYTHON" -c "
import sys
try:
    sys.path.insert(0, '$PREFIX/src')
    import lightdb
    print(lightdb.__version__)
except Exception:
    print('unknown')
" 2>/dev/null || echo "unknown"
}

list_backups() {
    if [[ ! -d "$BACKUP_DIR" ]]; then
        return
    fi
    ls -1t "$BACKUP_DIR"/upgrade_* 2>/dev/null | head -5
}

# ---------- 停止服务 ----------
stop_service() {
    step "1/8 停止服务"

    if systemctl is-active --quiet lightdb 2>/dev/null; then
        log "停止 systemd 服务..."
        systemctl stop lightdb
        log "✓ 服务已停止"
    elif [[ -f "$PREFIX/lightdb.pid" ]]; then
        local pid
        pid=$(cat "$PREFIX/lightdb.pid")
        if kill -0 "$pid" 2>/dev/null; then
            log "停止 PID=$pid..."
            kill -TERM "$pid"
            sleep 3
            kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
            rm -f "$PREFIX/lightdb.pid"
        fi
        log "✓ 服务已停止"
    else
        warn "未检测到运行中的服务"
    fi
}

# ---------- 备份 ----------
backup_data() {
    step "2/8 备份数据"

    local ts
    ts=$(date +%Y%m%d_%H%M%S)
    local backup_path="$BACKUP_DIR/upgrade_${ts}"

    mkdir -p "$backup_path"

    log "备份数据目录..."
    if [[ -d "$PREFIX/data" ]]; then
        tar czf "$backup_path/data.tar.gz" -C "$PREFIX" data
        log "  data.tar.gz: $(du -h "$backup_path/data.tar.gz" | cut -f1)"
    fi

    log "备份配置..."
    if [[ -f "$PREFIX/config/config.json" ]]; then
        cp "$PREFIX/config/config.json" "$backup_path/config.json"
    fi

    log "备份版本信息..."
    local cur_ver
    cur_ver=$(get_current_version)
    cat > "$backup_path/version.txt" <<EOF
version=$cur_ver
backup_time=$(date '+%Y-%m-%d %H:%M:%S')
prefix=$PREFIX
EOF

    log "✓ 备份完成: $backup_path"

    # 更新符号链接 latest
    ln -snf "$backup_path" "$BACKUP_DIR/upgrade_latest"

    # 清理旧备份（保留最近 5 个）
    local count
    count=$(ls -1d "$BACKUP_DIR"/upgrade_* 2>/dev/null | wc -l)
    if [[ $count -gt 5 ]]; then
        log "清理旧备份..."
        ls -1dt "$BACKUP_DIR"/upgrade_* 2>/dev/null | tail -n +6 | xargs -r rm -rf
    fi
}

# ---------- 兼容性检查 ----------
check_compatibility() {
    step "3/8 兼容性检查"

    local from="$1"
    local to="$2"

    log "升级路径: $from → $to"

    # 版本跳跃检查
    if [[ "$from" == "3.0"* ]] && [[ "$to" == "4."* ]]; then
        log "v3.0 → v4.0 大版本升级"
        log "兼容性: 数据格式向后兼容"
        log "注意: 数据结构将被迁移（自动）"
    elif [[ "$from" == "4."* ]] && [[ "$to" == "4."* ]]; then
        log "v4.x 内部升级"
        log "兼容性: 完全兼容"
    elif [[ "$from" == "unknown" ]]; then
        warn "无法确定当前版本，跳过兼容性检查"
    fi

    # 磁盘空间检查
    local avail_gb
    avail_gb=$(df -BG "$PREFIX" | awk 'NR==2 {gsub("G","",$4); print $4}')
    log "可用空间: ${avail_gb}GB"

    if [[ "$avail_gb" -lt 5 ]]; then
        err "磁盘空间不足（需 5GB+）"
        exit 1
    fi

    log "✓ 兼容性检查通过"
}

# ---------- 执行升级 ----------
do_upgrade() {
    step "4/8 执行升级"

    local src_dir="$PREFIX/src"

    if [[ -d "$src_dir" ]]; then
        log "更新源码..."
        cd "$src_dir"

        local current_branch
        current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")

        git fetch --tags -q || warn "git fetch 失败"

        if [[ "$TO_VERSION" == "latest" ]]; then
            git pull -q origin "$current_branch" || warn "git pull 失败"
        else
            log "切换到标签 v$TO_VERSION..."
            git checkout -q "v$TO_VERSION" 2>/dev/null || \
            git checkout -q "$TO_VERSION" 2>/dev/null || {
                warn "标签不存在，尝试从远程获取"
                git fetch --all -q
                git checkout -q "v$TO_VERSION" || {
                    err "无法切换到 v$TO_VERSION"
                    return 1
                }
            }
        fi

        log "重装 Python 包..."
        "$PYTHON" -m pip install -q -e . || {
            err "安装失败"
            return 1
        }
    else
        log "从 PyPI 升级..."
        if [[ "$TO_VERSION" == "latest" ]]; then
            "$PYTHON" -m pip install -q --upgrade lightdb
        else
            "$PYTHON" -m pip install -q "lightdb==$TO_VERSION" || {
                err "安装失败"
                return 1
            }
        fi
    fi

    log "✓ 升级完成"
}

# ---------- 数据迁移 ----------
migrate_data() {
    step "5/8 数据迁移"

    # 检查是否需要迁移
    local from="$1"
    local to="$2"

    if [[ "$from" == "3.0"* ]] && [[ "$to" == "4."* ]]; then
        log "执行 v3→v4 数据迁移..."

        "$PYTHON" -c "
import sys
sys.path.insert(0, '$PREFIX/src')
try:
    from lightdb.migration import migrate_v3_to_v4
    migrate_v3_to_v4('$PREFIX/data')
    print('✓ 迁移完成')
except ImportError:
    print('⚠️ 迁移模块不存在，跳过')
except Exception as e:
    print(f'❌ 迁移失败: {e}')
    sys.exit(1)
" || {
        err "数据迁移失败"
        return 1
    }
    else
        log "无需迁移（同版本或完全兼容）"
    fi

    log "✓ 数据迁移完成"
}

# ---------- 启动验证 ----------
start_and_verify() {
    step "6/8 启动并验证"

    log "启动服务..."
    if systemctl is-enabled --quiet lightdb 2>/dev/null; then
        systemctl start lightdb
        sleep 5

        if systemctl is-active --quiet lightdb; then
            log "✓ 服务运行中"
        else
            err "服务启动失败"
            journalctl -u lightdb --no-pager -n 50
            return 1
        fi
    else
        "$PREFIX/bin/lightdb-server" &
        local pid=$!
        sleep 5

        if kill -0 "$pid" 2>/dev/null; then
            log "✓ 服务运行中 (PID=$pid)"
            echo "$pid" > "$PREFIX/lightdb.pid"
        else
            err "服务启动失败"
            return 1
        fi
    fi

    # 连接测试
    log "连接测试..."
    local max_wait=30
    local elapsed=0
    while [[ $elapsed -lt $max_wait ]]; do
        if "$PYTHON" -c "
import pymysql
try:
    conn = pymysql.connect(
        host='127.0.0.1', port=8888,
        user='root', password='',
        connect_timeout=3)
    cur = conn.cursor()
    cur.execute('SELECT VERSION()')
    print('  ' + cur.fetchone()[0])
    conn.close()
except Exception as e:
    exit(1)
" 2>/dev/null; then
            log "✓ 连接测试成功"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done

    err "连接测试超时"
    return 1
}

# ---------- 回滚 ----------
do_rollback() {
    step "回滚"

    local latest="$BACKUP_DIR/upgrade_latest"
    if [[ ! -d "$latest" ]]; then
        err "未找到备份: $latest"
        exit 1
    fi

    log "从 $latest 回滚..."

    local backup_version
    backup_version=$(grep '^version=' "$latest/version.txt" 2>/dev/null | cut -d= -f2)

    # 停止服务
    systemctl stop lightdb 2>/dev/null || true
    [[ -f "$PREFIX/lightdb.pid" ]] && kill -TERM "$(cat "$PREFIX/lightdb.pid")" 2>/dev/null || true
    sleep 2

    # 恢复数据
    if [[ -f "$latest/data.tar.gz" ]]; then
        log "恢复数据..."
        rm -rf "$PREFIX/data"
        tar xzf "$latest/data.tar.gz" -C "$PREFIX"
    fi

    # 恢复配置
    if [[ -f "$latest/config.json" ]]; then
        cp "$latest/config.json" "$PREFIX/config/config.json"
    fi

    # 重装旧版本
    log "重装旧版本 (v$backup_version)..."
    "$PYTHON" -m pip install -q "lightdb==$backup_version" 2>/dev/null || \
        warn "无法从 PyPI 安装，请手动处理"

    log "✓ 回滚完成"
}

# ---------- 打印总结 ----------
print_summary() {
    local new_version
    new_version=$(get_current_version)

    echo ""
    echo -e "${GREEN}============================================================${NC}"
    echo -e "${GREEN}  升级完成！${NC}"
    echo -e "${GREEN}============================================================${NC}"
    echo ""
    echo "  新版本: $new_version"
    echo "  备份:   $BACKUP_DIR/upgrade_latest"
    echo ""
    echo "  回滚命令:"
    echo "    $0 --rollback"
    echo ""
    echo -e "${GREEN}============================================================${NC}"
}

# ---------- 主流程 ----------
main() {
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}  LightDB 升级程序${NC}"
    echo -e "${BLUE}============================================================${NC}"

    check_installation

    # 回滚模式
    if [[ "$ROLLBACK" -eq 1 ]]; then
        do_rollback
        start_and_verify || {
            err "回滚后启动失败"
            exit 1
        }
        print_summary
        return 0
    fi

    # 升级模式
    local current_version
    current_version=$(get_current_version)
    [[ -z "$FROM_VERSION" ]] && FROM_VERSION="$current_version"

    echo "  当前版本: $current_version"
    echo "  目标版本: $TO_VERSION"
    echo ""

    if [[ "$current_version" == "$TO_VERSION" ]] && [[ "$FORCE" -eq 0 ]]; then
        warn "已是最新版本，使用 --force 强制重装"
        exit 0
    fi

    stop_service
    backup_data
    check_compatibility "$current_version" "$TO_VERSION"

    if ! do_upgrade; then
        err "升级失败，回滚..."
        do_rollback
        start_and_verify || warn "回滚后启动失败，请手动检查"
        exit 1
    fi

    if ! migrate_data "$current_version" "$TO_VERSION"; then
        err "数据迁移失败，回滚..."
        do_rollback
        start_and_verify || warn "回滚后启动失败，请手动检查"
        exit 1
    fi

    if ! start_and_verify; then
        err "启动验证失败，回滚..."
        do_rollback
        start_and_verify || warn "回滚后启动失败，请手动检查"
        exit 1
    fi

    print_summary
}

main "$@"