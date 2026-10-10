# LightDB Studio Web - 生产镜像
FROM python:3.11-slim

LABEL maintainer="LightDB Team"
LABEL description="LightDB Studio Web - 数据库管理平台"

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt requirements_web.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements_web.txt

# 应用代码
COPY lightdb_studio/ ./lightdb_studio/
COPY web/ ./web/
COPY main.py ./

# 非 root 用户
RUN useradd -m -u 1000 lightdb && \
    chown -R lightdb:lightdb /app
USER lightdb

# 暴露端口
EXPOSE 8080 8888

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; \
        urllib.request.urlopen('http://localhost:8080/api/v1/health')" \
    || exit 1

# 默认启动 Web 服务
CMD ["python", "-m", "web.start", "--host", "0.0.0.0", "--port", "8080"]