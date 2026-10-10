#!/bin/bash
# ============================================================
# LightDB v3.0 安装脚本 (Linux / macOS)
#
# 用法:
#   curl -sSL https://get.lightdb.io/install.sh | bash
#   ./install.sh                          # 默认安装
#   ./install.sh --prefix /opt/lightdb    # 指定目录
#   ./install.sh --version 4.0.0          # 指定版本
#   ./install.sh --mode venv              # venv 模式（推荐）
# ============================================================

set -euo pipefail

# ---------- 默认参数 ----------
PREFIX="${PREFIX:-/opt/lightdb}"
VERSION="${LIGHTDB_VERSION:-3.0.0}"
PYTHON="${PYTHON:-python3}"
MODE="pip"                 # pip / venv / docker
REPO_URL="https://github.com/lightdb/lightdb.git"
DATA_DIR="${PREFIX}/data"
LOG_DIR="${PREFIX}/logs"
CONF_DIR="${PREFIX}/config"

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
        --version)  VERSION="$2"; shift 2 ;;
        --mode)     MODE="$2"; shift 2 ;;
        --python)   PYTHON="$2"; shift 2 ;;
        -h|--help)
            sed -n '3,15p' "$0"
            exit 0
            ;;
        *) err "未知参数: $1"; exit 1 ;;
    esac
done

# ---------- 环境检查 ----------
check_env() {
    step "1/7 检查环境"

    # 操作系统
    OS="$(uname -s)"
    case "$OS" in
        Linux)  log "操作系统: Linux" ;;
        Darwin) log "操作系统: macOS" ;;
        *)      err "不支持的操作系统: $OS"; exit 1 ;;
    esac

    # Python
    if ! command -v "$PYTHON" &> /dev/null; then
        err "未找到 $PYTHON，请先安装 Python 3.8+"
        exit 1
    fi

    PY_VER=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    log "Python: $PY_VER"

    if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)'; then
        err "Python 版本过低，需要 3.8+"
        exit 1
    fi

    # pip
    if ! "$PYTHON" -m pip --version &> /dev/null; then
        err "pip 未安装"
        exit 1
    fi
    log "pip: $("$PYTHON" -m pip --version | awk '{print $2}')"

    # 磁盘空间（至少 2GB）
    if command -v df &> /dev/null; then
        AVAIL_KB=$(df -k "$(dirname "$PREFIX")" 2>/dev/null | awk 'NR==2 {print $4}')
        if [[ -n "$AVAIL_KB" && "$AVAIL_KB" -lt 2097152 ]]; then
            warn "可用空间少于 2GB (${AVAIL_KB}KB)"
        fi
    fi

    log "✓ 环境检查通过"
}

# ---------- 创建目录 ----------
create_dirs() {
    step "2/7 创建目录"

    for d in "$PREFIX" "$DATA_DIR" "$LOG_DIR" "$CONF_DIR" \
             "$PREFIX/backups" "$PREFIX/bin"; do
        if [[ ! -d "$d" ]]; then
            mkdir -p "$d"
            log "创建 $d"
        fi
    done

    log "✓ 目录已就绪"
}

# ---------- 安装依赖 ----------
install_deps() {
    step "3/7 安装依赖"

    log "升级 pip..."
    "$PYTHON" -m pip install --upgrade pip -q

    log "安装核心依赖..."
    "$PYTHON" -m pip install -q \
        "pymysql>=1.0.0" \
        "psutil>=5.9.0"

    log "✓ 依赖安装完成"
}

# ---------- 安装 LightDB ----------
install_lightdb() {
    step "4/7 安装 LightDB $VERSION"

    case "$MODE" in
        pip)
            log "从 PyPI 安装 lightdb==$VERSION..."
            if ! "$PYTHON" -m pip install -q "lightdb==$VERSION"; then
                warn "PyPI 安装失败，尝试从源码安装"
                install_from_source
            fi
            ;;
        venv)
            log "创建虚拟环境: $PREFIX/venv"
            "$PYTHON" -m venv "$PREFIX/venv"
            source "$PREFIX/venv/bin/activate"
            "$PYTHON" -m pip install --upgrade pip -q
            "$PYTHON" -m pip install -q \
                "pymysql>=1.0.0" \
                "psutil>=5.9.0"
            install_from_source
            ;;
        docker)
            log "Docker 模式：请使用 docker compose 部署"
            return 0
            ;;
    esac

    log "✓ LightDB 安装完成"
}

install_from_source() {
    local SRC_DIR="$PREFIX/src"
    if [[ -d "$SRC_DIR" ]]; then
        log "源码目录已存在，拉取更新..."
        cd "$SRC_DIR"
        git pull -q || warn "git pull 失败"
    else
        log "克隆源码..."
        git clone -q "$REPO_URL" "$SRC_DIR"
        cd "$SRC_DIR"
    fi
    "$PYTHON" -m pip install -q -e .
}

# ---------- 生成配置 ----------
gen_config() {
    step "5/7 生成配置文件"

    local CONF_FILE="$CONF_DIR/config.json"
    if [[ -f "$CONF_FILE" ]]; then
        log "配置文件已存在，跳过"
        return 0
    fi

    cat > "$CONF_FILE" <<EOF
{
  "host": "0.0.0.0",
  "port": 8888,
  "max_connections": 100,
  "buffer_pool_size": 1000,
  "page_size": 4096,
  "data_dir": "$DATA_DIR",
  "backup_dir": "$PREFIX/backups",
  "workers": 1,
  "parallel_workers": 2,
  "slow_query_threshold": 0.5,
  "max_allowed_packet": 16777216,
  "log_level": "INFO"
}
EOF

    log "已生成: $CONF_FILE"
    log "✓ 配置就绪"
}

# ---------- 安装命令行工具 ----------
install_cli() {
    step "6/7 安装命令行工具"

    local BIN_DIR="$PREFIX/bin"
    local VENV_PY="$PYTHON"
    if [[ "$MODE" == "venv" ]]; then
        VENV_PY="$PREFIX/venv/bin/python"
    fi

    # lightdb-server
    cat > "$BIN_DIR/lightdb-server" <<EOF
#!/bin/bash
exec "$VENV_PY" -m lightdb.main --config "$CONF_DIR/config.json" "\$@"
EOF
    chmod +x "$BIN_DIR/lightdb-server"

    # lightdb-cli
    cat > "$BIN_DIR/lightdb-cli" <<EOF
#!/bin/bash
exec "$VENV_PY" -m lightdb.tools.cli "\$@"
EOF
    chmod +x "$BIN_DIR/lightdb-cli"

    # lightdb-check
    cat > "$BIN_DIR/lightdb-check" <<EOF
#!/bin/bash
exec "$VENV_PY" -m lightdb.tools.check "\$@"
EOF
    chmod +x "$BIN_DIR/lightdb-check"

    # lightdb-bench
    cat > "$BIN_DIR/lightdb-bench" <<EOF
#!/bin/bash
exec "$VENV_PY" -m lightdb.benchmark.cli "\$@"
EOF
    chmod +x "$BIN_DIR/lightdb-bench"

    # lightdb-tune
    cat > "$BIN_DIR/lightdb-tune" <<EOF
#!/bin/bash
exec "$VENV_PY" -m lightdb.tools.tune "\$@"
EOF
    chmod +x "$BIN_DIR/lightdb-tune"

    log "已安装 CLI 工具:"
    for f in "$BIN_DIR"/lightdb-*; do
        [[ -x "$f" ]] && echo "  $(basename "$f")"
    done

    # 加入 PATH 提示
    if ! echo "$PATH" | grep -q "$BIN_DIR"; then
        warn "请将 $BIN_DIR 加入 PATH:"
        echo "  export PATH=\"$BIN_DIR:\$PATH\""
    fi

    log "✓ CLI 就绪"
}

# ---------- 生成服务文件 ----------
gen_service() {
    step "7/7 生成服务文件"

    if [[ "$OS" == "Linux" ]] && command -v systemctl &> /dev/null; then
        local SERVICE_FILE="/etc/systemd/system/lightdb.service"
        if [[ -w /etc/systemd/system ]] || [[ "$(id -u)" -eq 0 ]]; then
            cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=LightDB Database Server
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$PREFIX
ExecStart=$PREFIX/bin/lightdb-server
ExecStop=/bin/kill -TERM \$MAINPID
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
LimitNPROC=65536
StandardOutput=append:$LOG_DIR/server.log
StandardError=append:$LOG_DIR/server.err

[Install]
WantedBy=multi-user.target
EOF
            log "已生成 systemd 服务: $SERVICE_FILE"
            log "启用服务:"
            echo "  sudo systemctl daemon-reload"
            echo "  sudo systemctl enable lightdb"
            echo "  sudo systemctl start lightdb"
        else
            warn "无权限写入 systemd，请手动创建服务文件"
        fi
    elif [[ "$OS" == "Darwin" ]]; then
        local PLIST="$HOME/Library/LaunchAgents/io.lightdb.server.plist"
        cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.lightdb.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PREFIX/bin/lightdb-server</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PREFIX</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/server.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/server.err</string>
</dict>
</plist>
EOF
        log "已生成 launchd 服务: $PLIST"
        log "加载服务: launchctl load $PLIST"
    fi

    log "✓ 服务文件就绪"
}

# ---------- 打印总结 ----------
print_summary() {
    echo ""
    echo -e "${GREEN}============================================================${NC}"
    echo -e "${GREEN}  LightDB 安装完成！${NC}"
    echo -e "${GREEN}============================================================${NC}"
    echo ""
    echo "  安装目录: $PREFIX"
    echo "  数据目录: $DATA_DIR"
    echo "  配置目录: $CONF_DIR"
    echo "  日志目录: $LOG_DIR"
    echo "  可执行:   $PREFIX/bin/"
    echo ""
    echo "  启动服务:"
    echo "    $PREFIX/bin/lightdb-server &"
    echo ""
    echo "  连接测试:"
    echo "    $PREFIX/bin/lightdb-cli -h 127.0.0.1 -P 8888 -u root"
    echo ""
    echo "  性能测试:"
    echo "    $PREFIX/bin/lightdb-bench run --workload all"
    echo ""
    echo -e "${GREEN}============================================================${NC}"
}

# ---------- 主流程 ----------
main() {
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}  LightDB v$VERSION 安装程序${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo "  安装目录: $PREFIX"
    echo "  安装模式: $MODE"
    echo ""

    check_env
    create_dirs
    install_deps
    install_lightdb
    gen_config
    install_cli
    gen_service
    print_summary
}

main "$@"