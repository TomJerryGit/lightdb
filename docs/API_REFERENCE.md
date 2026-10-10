# LightDB Studio Web - REST API 参考

**Base URL**：`http://<host>:8080/api/v1`
**认证**：JWT Bearer Token
**文档**：`/docs`（Swagger）

---

## 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/login` | 登录，返回 token |
| GET | `/me` | 当前用户 |

**登录请求 / 响应**

    POST /api/v1/login
    {"username": "admin", "password": "admin123"}

    {"ok": true, "token": "eyJ...",
     "user": {"username": "admin", "role": "admin"}}

Token 有效期 7 天，后续请求带 `Authorization: Bearer <token>`。

---

## 健康检查

| 方法 | 路径 | 认证 |
|------|------|:----:|
| GET | `/health` | 无 |

    {"ok": true, "version": "1.0.0", "ts": 1735660800.123}

---

## 连接管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/connections` | 连接列表 |
| POST | `/connections/test` | 测试连接 |

---

## 元数据

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/databases` | 数据库列表 |
| GET | `/databases/{db}/tables` | 表列表（含行数） |
| GET | `/databases/{db}/tables/{t}/columns` | 列信息 |

**Query 参数**：`connection=<连接名>`（可选）

---

## SQL 查询

**`POST /query`**

    {
      "sql": "SELECT * FROM users LIMIT 10",
      "connection": "prod-db",
      "database": "default",
      "limit": 1000,
      "timeout": 30
    }

响应：

    {
      "ok": true,
      "columns": ["id", "name"],
      "rows": [[1, "Alice"]],
      "row_count": 1,
      "affected": 0,
      "elapsed_ms": 3.14,
      "error": null
    }

写操作返回 `affected`（行数），`columns`/`rows` 为空。

**`GET /history?limit=50`** — 查询历史

---

## 表操作

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/databases/{db}/tables/{t}/data` | 读取（`limit` / `offset` / `order_by`） |
| POST | `/databases/{db}/tables/{t}/insert` | 插入（body 为列名→值） |
| PUT | `/databases/{db}/tables/{t}/update` | 更新（body: `set_data` + `where`） |
| DELETE | `/databases/{db}/tables/{t}/delete` | 删除（body: `where`） |

**约束**：`update` / `delete` 的 `where` 不能为空，否则返回 `400`。

---

## 报表

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/reports` | 报表列表 |
| POST | `/reports/{name}/run` | 运行报表 |

---

## 监控

| 方法 | 路径 | 认证 | 格式 |
|------|------|:----:|------|
| GET | `/metrics` | 无 | Prometheus 文本 |
| GET | `/api/v1/metrics/json` | 无 | JSON |

**注意**：`/metrics` 默认未在 `web/server.py` 中注册，需手动 `app.include_router(metrics_router)`。

---

## 权限矩阵

| 操作 | viewer | analyst | developer | admin |
|------|:------:|:-------:|:---------:|:-----:|
| 读（SELECT / SHOW） | ✅ | ✅ | ✅ | ✅ |
| 写（INSERT / UPDATE / DELETE） | ❌ | ❌ | ✅ | ✅ |
| DDL | ❌ | ❌ | ❌ | ✅ |

---

## 状态码

| 码 | 含义 |
|----|------|
| 200 | 成功 |
| 400 | 参数错误（如 `where` 为空） |
| 401 | 未认证 |
| 403 | 无权限 |
| 404 | 资源不存在 |
| 500 | 服务器错误 |

---

## 客户端示例

**Python**

    import requests
    BASE = "http://localhost:8080/api/v1"

    token = requests.post(f"{BASE}/login",
        json={"username": "admin", "password": "admin123"}).json()["token"]

    r = requests.post(f"{BASE}/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"sql": "SELECT 1", "limit": 10})
    print(r.json())

**curl**

    TOKEN=$(curl -s -X POST $BASE/login \
        -H 'Content-Type: application/json' \
        -d '{"username":"admin","password":"admin123"}' \
        | jq -r .token)

    curl -X POST $BASE/query \
        -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' \
        -d '{"sql":"SELECT 1","limit":10}'

---

## ⚠️ 安全提示（生产环境必须处理）

| 问题 | 位置 | 修复 |
|------|------|------|
| JWT SECRET 硬编码 | `web/auth.py` | 改为环境变量 |
| 默认弱密码 admin123 | `web/server.py` | 强制修改 |
| CORS `allow_origins=['*']` | `web/server.py` | 改白名单 |
| `/metrics` 未注册 | `web/server.py` | `include_router` |
| 会话连接池无锁 | `web/api.py` | 加线程锁 |