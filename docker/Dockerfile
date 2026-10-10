# ============================================================
# LightDB 通用服务镜像 (数据库服务端 + Studio Web)
# 构建上下文 = 仓库根目录，构建命令示例:
#   docker build -f docker/Dockerfile -t lightdb/server:4.0.0 .
#   docker compose -f docker/docker-compose.yml up -d
#
# 说明:
# - 镜像内包含 lightdb/ (数据库服务端)、web/ (Studio Web)、lightdb_studio/ (GUI 源码)。
# - 入口为 docker/entrypoint.sh，支持: server(默认) / web / cli / check / bench / bash
#   见 docker/entrypoint.sh 的 main() 分发。
# - server 模式执行: python -m lightdb.main --mode single ...
# - 不安装 PySide6: 容器内以无头方式运行，桌面 GUI 不适用，
#   因此只装 requirements.txt(核心) + requirements_web.txt(Web)。
# ============================================================
FROM python:3.11-slim

LABEL maintainer="LightDB Team"
LABEL description="LightDB 数据库服务端 + Studio Web"
LABEL version="4.0.0"

WORKDIR /app

# ---------- 系统依赖 ----------
# netcat-openbsd: entrypoint.sh 的 wait_for_host() 依赖 nc 探测依赖服务
# tini: entrypoint 作为 PID 1 的信号转发与僵尸回收
# curl: 便于容器内健康检查与调试
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    netcat-openbsd \
    curl \
    tini \
    && rm -rf /var/lib/apt/lists/*

# ---------- Python 依赖 ----------
# requirements.txt 只含核心依赖(pymysql/psutil/numpy)，不含 PySide6；
# Web 依赖另装 requirements_web.txt。
COPY requirements.txt requirements_web.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements_web.txt

# ---------- 应用代码（下列路径均在仓库根目录真实存在）----------
COPY lightdb/ ./lightdb/
COPY lightdb_studio/ ./lightdb_studio/
COPY web/ ./web/
COPY docker/entrypoint.sh ./entrypoint.sh

# ---------- 非 root 用户 ----------
RUN chmod +x /app/entrypoint.sh && \
    mkdir -p /app/data /app/logs /app/backups && \
    useradd -m -u 1000 lightdb && \
    chown -R lightdb:lightdb /app
USER lightdb

# ---------- 暴露端口 ----------
# 8888 = 数据库服务端(MySQL 协议)，8080 = Studio Web
EXPOSE 8888 8080

# ---------- 健康检查 ----------
# 默认以 server 模式启动，因此探测 8888；
# 以 web 模式运行时请覆盖: --health-cmd 或 compose 中的 healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import socket; s=socket.create_connection(('127.0.0.1',8888),3); s.close()" || exit 1

# ---------- 入口 ----------
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["server"]
