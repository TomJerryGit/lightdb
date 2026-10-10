#!/bin/bash
# ============================================================
# LightDB v4.0 安装脚本 (Linux)
#
# 特点:
# - 安装 Cython 编译依赖
# - 编译 Cython 加速模块
# - 自动检测 CPU 核数配置 workers
# - 支持多进程模式
#
# 用法:
#   sudo ./install_v4.sh
#   ./install_v4.sh --prefix /opt/lightdb --workers 8
#   ./install_v4.sh --skip-cython     # 跳过 Cython 编译
# ============================================================

set -euo pipefail

# ---------- 默认参数 ----------
PREFIX="${PREFIX:-/opt/lightdb}"
VERSION="${LIGHTDB_VERSION:-4.0.0}"
PYTHON="${PYTHON:-python3}"
WORKERS="${WORKERS:-0}"
SKIP_CYTHON=0
SKIP_DEPS=0
REPO_URL="https://github.com/lightdb/lightdb.git"

DATA_DIR="${PREFIX}/data"
LOG_DIR="${PREFIX}/logs"
CONF_DIR="${PREFIX}/config"
BUILD_DIR="${PREFIX}/build"

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

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)      PREFIX="$2"; shift 2 ;;
        --version)     VERSION="$2"; shift 2 ;;
        --workers)     WORKERS="$2"; shift 2 ;;
        --python)      PYTHON="$2"; shift 2 ;;
        --skip-cython) SKIP_CYTHON=1; shift ;;
        --skip-deps)   SKIP_DEPS=1; shift ;;
        -h|--help)
            sed -n '3,18p' "$0"
            exit 0
            ;;
        *) err "未知参数: $1"; exit 1 ;;
    esac
done

# ---------- 环境检查 ----------
check_env() {
    step "1/9 检查环境"

    # 检查 Linux
    if [[ "$(uname -s)" != "Linux" ]]; then
        err "此脚本仅支持 Linux"
        exit 1
    fi
    log "系统: $(uname -a)"

    # Python
    if ! command -v "$PYTHON" &> /dev/null; then
        err "未找到 $PYTHON"
        exit 1
    fi

    PY_VER=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    log "Python: $PY_VER"

    if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)'; then
        err "需要 Python 3.8+"
        exit 1
    fi

    # CPU 核数
    CPU_COUNT=$(nproc 2>/dev/null || echo 4)
    log "CPU 核数: $CPU_COUNT"

    # 计算 workers
    if [[ "$WORKERS" -eq 0 ]]; then
        WORKERS=$((CPU_COUNT > 1 ? CPU_COUNT - 1 : 1))
    fi
    log "Worker 数: $WORKERS"

    # 内存
    if [[ -f /proc/meminfo ]]; then
        MEM_GB=$(awk '/MemTotal/ {printf "%.1f", $2/1024/1024}' /proc/meminfo)
        log "内存: ${MEM_GB} GB"

        if (( $(echo "$MEM_GB < 4" | bc -l) )); then
            warn "内存少于 4GB，多进程可能受限"
        fi
    fi

    log "✓ 环境检查通过"
}

# ---------- 安装系统依赖 ----------
install_system_deps() {
    step "2/9 安装系统依赖"

    if [[ "$SKIP_DEPS" -eq 1 ]]; then
        log "跳过系统依赖安装"
        return 0
    fi

    # 检测发行版
    if [[ -f /etc/debian_version ]]; then
        PKG_MGR="apt-get"
        log "检测到 Debian/Ubuntu"
        apt-get update -qq
        apt-get install -y -qq \
            build-essential \
            gcc \
            g++ \
            python3-dev \
            python3-venv \
            libc6-dev \
            git \
            curl \
            procps \
            netcat-openbsd

    elif [[ -f /etc/redhat-release ]]; then
        PKG_MGR="yum"
        log "检测到 RHEL/CentOS"
        yum install -y -q \
            gcc gcc-c++ make \
            python3-devel \
            git curl \
            procps-ng \
            nc

    elif [[ -f /etc/alpine-release ]]; then
        PKG_MGR="apk"
        log "检测到 Alpine"
        apk add --no-cache \
            build-base \
            gcc g++ \
            python3-dev \
            git curl \
            procps \
            netcat-openbsd

    else
        warn "未知发行版，请手动安装以下依赖:"
        echo "  build-essential gcc g++ python3-dev git curl"
    fi

    log "✓ 系统依赖就绪"
}

# ---------- 创建目录 ----------
create_dirs() {
    step "3/9 创建目录"

    for d in "$PREFIX" "$DATA_DIR" "$LOG_DIR" "$CONF_DIR" \
             "$PREFIX/backups" "$PREFIX/bin" "$BUILD_DIR"; do
        mkdir -p "$d"
    done

    log "✓ 目录已就绪"
}

# ---------- 创建虚拟环境 ----------
create_venv() {
    step "4/9 创建虚拟环境"

    VENV_DIR="$PREFIX/venv"
    if [[ -d "$VENV_DIR" ]]; then
        log "虚拟环境已存在: $VENV_DIR"
    else
        log "创建: $VENV_DIR"
        "$PYTHON" -m venv "$VENV_DIR"
    fi

    VENV_PY="$VENV_DIR/bin/python"
    VENV_PIP="$VENV_DIR/bin/pip"

    "$VENV_PY" -m pip install --upgrade pip -q

    log "✓ 虚拟环境就绪"
}

# ---------- 安装 Python 依赖 ----------
install_python_deps() {
    step "5/9 安装 Python 依赖"

    log "安装核心依赖..."
    "$VENV_PIP" install -q \
        "pymysql>=1.0.0" \
        "psutil>=5.9.0" \
        "numpy>=1.24.0"

    log "安装 Web 依赖..."
    "$VENV_PIP" install -q \
        "fastapi>=0.104.0" \
        "uvicorn[standard]>=0.24.0" \
        "python-multipart>=0.0.6" \
        "python-jose[cryptography]>=3.3.0" \
        "passlib[bcrypt]>=1.7.4" \
        "jinja2>=3.1.0" \
        "websockets>=12.0"

    log "✓ Python 依赖就绪"
}

# ---------- 编译 Cython ----------
build_cython() {
    step "6/9 编译 Cython 加速模块"

    if [[ "$SKIP_CYTHON" -eq 1 ]]; then
        warn "跳过 Cython 编译"
        return 0
    fi

    log "安装 Cython..."
    "$VENV_PIP" install -q "cython>=3.0.0"

    SRC_DIR="$PREFIX/src"
    if [[ ! -d "$SRC_DIR" ]]; then
        log "克隆源码到 $SRC_DIR"
        git clone -q "$REPO_URL" "$SRC_DIR"
    else
        cd "$SRC_DIR"
        git pull -q || warn "git pull 失败"
    fi

    CYTHON_DIR="$SRC_DIR/lightdb/cython"
    if [[ ! -d "$CYTHON_DIR" ]]; then
        warn "未找到 Cython 源目录: $CYTHON_DIR"
        return 0
    fi

    log "编译 Cython 模块..."
    cd "$CYTHON_DIR"

    if "$VENV_PY" setup.py build_ext --inplace 2>&1 | tee /tmp/cython_build.log; then
        log "✓ Cython 编译成功"
    else
        warn "Cython 编译失败，将使用纯 Python 版本"
        tail -20 /tmp/cython_build.log
    fi

    # 验证
    log "验证 Cython 模块..."
    "$VENV_PY" -c "
import sys
sys.path.insert(0, '$SRC_DIR')
try:
    from lightdb.cython import status
    st = status()
    for name, ok in st['available'].items():
        print(f'  {\"✅\" if ok else \"❌\"} {name}')
    sys.exit(0 if st['all_available'] else 1)
except Exception as e:
    print(f'  ⚠️ 状态检查失败: {e}')
    sys.exit(0)
" || warn "部分 Cython 模块未就绪"

    log "✓ Cython 模块处理完成"
}

# ---------- 安装 LightDB ----------
install_lightdb() {
    step "7/9 安装 LightDB"

    SRC_DIR="$PREFIX/src"
    if [[ -d "$SRC_DIR" ]]; then
        cd "$SRC_DIR"
        "$VENV_PIP" install -q -e . || warn "本地安装失败，尝试从 PyPI"
    else
        "$VENV_PIP" install -q "lightdb==$VERSION" || {
            err "安装失败"
            exit 1
        }
    fi

    log "✓ LightDB 安装完成"
}

# ---------- 生成配置 ----------
gen_config() {
    step "8/9 生成配置文件"

    CONF_FILE="$CONF_DIR/config.json"
    if [[ -f "$CONF_FILE" ]]; then
        log "配置文件已存在: $CONF_FILE"
        return 0
    fi

    cat > "$CONF_FILE" <<EOF
{
  "host": "0.0.0.0",
  "port": 8888,
  "max_connections": 500,
  "buffer_pool_size": 8192,
  "page_size": 4096,
  "data_dir": "$DATA_DIR",
  "backup_dir": "$PREFIX/backups",
  "workers": $WORKERS,
  "parallel_workers": $WORKERS,
  "slow_query_threshold": 0.5,
  "max_allowed_packet": 16777216,
  "log_level": "INFO",
  "ssl_enabled": false,
  "audit_level": "DDL",
  "semi_sync_enabled": false,
  "max_conn_per_ip": 100,
  "password_min_length": 6,
  "password_deny_weak": true
}
EOF

    log "已生成: $CONF_FILE"
    log "  Worker 数: $WORKERS"
    log "  缓冲池大小: 8192 页"
}

# ---------- 安装 CLI + 服务 ----------
install_cli_and_service() {
    step "9/9 安装 CLI 与服务"

    VENV_PY="$PREFIX/venv/bin/python"
    BIN_DIR="$PREFIX/bin"

    # ---------- CLI 包装 ----------
    for tool in server cli check bench tune dump admin diagnose index_advisor; do
        case "$tool" in
            server)  MODULE="lightdb.main" ;;
            cli)     MODULE="lightdb.tools.cli" ;;
            check)   MODULE="lightdb.tools.check" ;;
            bench)   MODULE="lightdb.benchmark.cli" ;;
            tune)    MODULE="lightdb.tools.tune" ;;
            dump)    MODULE="lightdb.tools.dump" ;;
            admin)   MODULE="lightdb.tools.admin" ;;
            diagnose) MODULE="lightdb.tools.diagnose" ;;
            index_advisor) MODULE="lightdb.tools.index_advisor" ;;
        esac

        cat > "$BIN_DIR/lightdb-$tool" <<EOF
#!/bin/bash
exec "$VENV_PY" -m $MODULE "\$@"
EOF
        chmod +x "$BIN_DIR/lightdb-$tool"
    done

    # 特殊: server 需要带 --config
    cat > "$BIN_DIR/lightdb-server" <<EOF
#!/bin/bash
exec "$VENV_PY" -m lightdb.main --config "$CONF_DIR/config.json" "\$@"
EOF
    chmod +x "$BIN_DIR/lightdb-server"

    log "已安装 CLI:"
    ls "$BIN_DIR"/lightdb-* | xargs -n1 basename | sed 's/^/  /'

    # ---------- systemd 服务 ----------
    if command -v systemctl &> /dev/null && [[ "$(id -u)" -eq 0 ]]; then
        SERVICE_FILE="/etc/systemd/system/lightdb.service"
        cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=LightDB v4.0 Multi-Process Database
After=network.target

[Service]
Type=simple
User=$(whoami)
Group=$(id -gn)
WorkingDirectory=$PREFIX
Environment="TZ=Asia/Shanghai"
Environment="PYTHONUNBUFFERED=1"
Environment="LIGHTDB_WORKERS=$WORKERS"
Environment="OMP_NUM_THREADS=4"
ExecStart=$BIN_DIR/lightdb-server
ExecReload=/bin/kill -HUP \$MAINPID
ExecStop=/bin/kill -TERM \$MAINPID
Restart=on-failure
RestartSec=5

LimitNOFILE=65536
LimitNPROC=65536
LimitMEMLOCK=infinity

StandardOutput=append:$LOG_DIR/server.log
StandardError=append:$LOG_DIR/server.err

[Install]
WantedBy=multi-user.target
EOF

        systemctl daemon-reload
        log "已生成 systemd 服务: $SERVICE_FILE"
        log "启用:"
        echo "  sudo systemctl enable lightdb"
        echo "  sudo systemctl start lightdb"
        echo "  sudo systemctl status lightdb"
    else
        warn "未以 root 运行或 systemctl 不可用，跳过服务注册"
        # 生成一个手动启动脚本
        cat > "$BIN_DIR/start.sh" <<EOF
#!/bin/bash
# 后台启动 LightDB
nohup "$BIN_DIR/lightdb-server" > "$LOG_DIR/server.log" 2>&1 &
echo \$! > "$PREFIX/lightdb.pid"
echo "LightDB 启动中，PID=\$!"
echo "日志: $LOG_DIR/server.log"
EOF
        chmod +x "$BIN_DIR/start.sh"

        cat > "$BIN_DIR/stop.sh" <<EOF
#!/bin/bash
# 停止 LightDB
if [[ -f "$PREFIX/lightdb.pid" ]]; then
    PID=\$(cat "$PREFIX/lightdb.pid")
    if kill -0 "\$PID" 2>/dev/null; then
        kill -TERM "\$PID"
        echo "已停止 PID=\$PID"
    fi
    rm -f "$PREFIX/lightdb.pid"
fi
EOF
        chmod +x "$BIN_DIR/stop.sh"

        log "已生成手动启停脚本:"
        echo "  $BIN_DIR/start.sh"
        echo "  $BIN_DIR/stop.sh"
    fi

    log "✓ CLI 与服务就绪"
}

# ---------- 打印总结 ----------
print_summary() {
    echo ""
    echo -e "${CYAN}============================================================${NC}"
    echo -e "${CYAN}  LightDB v$VERSION 安装完成！${NC}"
    echo -e "${CYAN}============================================================${NC}"
    echo ""
    echo "  安装目录: $PREFIX"
    echo "  数据目录: $DATA_DIR"
    echo "  配置目录: $CONF_DIR"
    echo "  日志目录: $LOG_DIR"
    echo "  Worker 数: $WORKERS"
    echo ""
    echo "  启动服务:"
    if [[ -f /etc/systemd/system/lightdb.service ]]; then
        echo "    sudo systemctl start lightdb"
    else
        echo "    $PREFIX/bin/start.sh"
    fi
    echo ""
    echo "  连接测试:"
    echo "    $PREFIX/bin/lightdb-cli -h 127.0.0.1 -P 8888 -u root"
    echo ""
    echo "  性能测试:"
    echo "    $PREFIX/bin/lightdb-bench run \\"
    echo "        --workload all --threads $WORKERS --duration 30"
    echo ""
    echo "  环境变量（可加入 ~/.bashrc）:"
    echo "    export PATH=\"$PREFIX/bin:\$PATH\""
    echo ""
    echo -e "${CYAN}============================================================${NC}"
}

# ---------- 主流程 ----------
main() {
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}  LightDB v$VERSION 安装程序 (Linux)${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo "  安装目录: $PREFIX"
    echo "  Worker 数: ${WORKERS:-自动}"
    echo "  版本: $VERSION"
    echo ""

    check_env
    install_system_deps
    create_dirs
    create_venv
    install_python_deps
    build_cython
    install_lightdb
    gen_config
    install_cli_and_service
    print_summary
}

main "$@"