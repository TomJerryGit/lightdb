"""
LightDB Studio REST API

安全要点（P0-D 修复）：
* 所有拼进反引号标识符的用户输入（数据库名 / 表名 / 列名 / order_by）
  必须先过 :func:`_ident` 白名单校验（``^[A-Za-z_][A-Za-z0-9_$]*$``），
  非法返回 400 —— 此前这些位置是 f-string 直接拼接，构成「已认证 SQL
  注入」（普通用户也能借表名/排序列名越权执行任意 SQL）；
* 值仍然全部用 ``%s`` 参数化，绝不拼接值；
* 新增 ``POST /api/v1/connections``（仅 admin）：把连接写进
  ``app_state['connections']``，使 Web 端能真正配置连接后执行查询。
"""
import asyncio
import hmac
import logging
import os
import re
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import require_permission, verify_token
from lightdb_studio.db.connection import DBConnection
from lightdb_studio.config import ConnectionInfo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["lightdb"])


# ==================== 标识符安全 ====================
# MySQL 标识符只允许 ASCII 字母/数字/下划线/美元符，且不能以数字开头。
# 用户输入拼进反引号前必须过这里：否则 ``table = "t` ; DROP ..."`` 直接
# 变成任意 SQL（反引号可被提前闭合）。
_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*$')
_IDENT_MAX_LEN = 64            # MySQL 标识符上限


def _ident(name, what: str = '标识符') -> str:
    """校验并返回可安全拼接的标识符；非法输入抛 HTTP 400。"""
    if (not isinstance(name, str) or len(name) > _IDENT_MAX_LEN
            or not _IDENT_RE.match(name)):
        raise HTTPException(400, f"非法{what}: {name!r}")
    return name


def _model_dict(model):
    """兼容 pydantic v1/v2 的模型转 dict。"""
    if hasattr(model, 'model_dump'):
        return model.model_dump()
    return model.dict()


def _drop_sessions_for_connection(name: str):
    """连接配置变更后，丢弃该连接名下的会话缓存（旧配置不再复用）。"""
    dead = []
    for key in list(_session_conns):
        if key.endswith(':' + name):
            dead.append(_session_conns.pop(key))
    for conn in dead:
        try:
            conn.close()
        except Exception:                          # noqa: BLE001
            pass


# ==================== 请求模型 ====================

class QueryRequest(BaseModel):
    sql: str
    connection: Optional[str] = None
    database: Optional[str] = None
    limit: int = Field(1000, ge=1, le=100000)
    timeout: int = Field(30, ge=1, le=300)


class QueryResponse(BaseModel):
    ok: bool
    columns: List[str] = []
    rows: List[List] = []
    row_count: int = 0
    affected: int = 0
    elapsed_ms: float = 0
    error: Optional[str] = None


class ConnectionModel(BaseModel):
    name: str
    host: str = "127.0.0.1"
    port: int = 8888
    user: str = "root"
    password: str = ""
    database: str = ""
    charset: str = "utf8mb4"
    ssl: bool = False


class LoginRequest(BaseModel):
    username: str
    password: str


class CommentRequest(BaseModel):
    target_id: str
    content: str
    mentions: List[str] = []


# ==================== 会话连接池 ====================
# 简单实现：每个用户最多维持一个连接（测试用 create_app 会清空）

_session_conns = {}   # token -> DBConnection


def _get_conn(token: str, name: str = None):
    key = f"{token}:{name or 'default'}"
    if key in _session_conns:
        conn = _session_conns[key]
        if conn.is_connected():
            return conn

    # 从服务器配置中查找
    from .server import app_state
    cfg = None
    for c in app_state.get('connections', []):
        if not name or c.name == name:
            cfg = c
            break

    if cfg is None:
        raise HTTPException(404, f"连接 '{name}' 不存在")

    conn = DBConnection(cfg)
    conn.connect()
    _session_conns[key] = conn
    return conn


# ==================== 健康检查 ====================

@router.get("/health")
async def health():
    return {
        "ok": True,
        "version": "1.0.0",
        "ts": time.time(),
    }


# ==================== 认证 ====================

# 登录失败固定延迟（缓解用户名枚举/在线暴力破解；常量时间比较见下）
_LOGIN_FAILURE_DELAY = 0.3
_UNKNOWN_USER_PASSWORD = 'unknown-user-placeholder'


def _password_matches(stored, provided) -> bool:
    """常量时间比较口令（``hmac.compare_digest``，UTF-8 bytes）。"""
    try:
        return hmac.compare_digest(str(stored).encode('utf-8'),
                                   str(provided).encode('utf-8'))
    except (TypeError, ValueError, UnicodeError):
        return False


@router.post("/login")
async def login(req: LoginRequest):
    from .server import app_state
    users = app_state.get('users', {})
    user = users.get(req.username)

    stored = user.get('password') if isinstance(user, dict) else None
    if not isinstance(stored, str):
        stored = _UNKNOWN_USER_PASSWORD

    matched = _password_matches(stored, req.password)
    if user is None or not matched:
        # 失败分支同样做满常量时间比较并固定延迟，不区分「用户不存在」
        # 与「口令错误」，避免用户名枚举。
        await asyncio.sleep(_LOGIN_FAILURE_DELAY)
        raise HTTPException(401, "用户名或密码错误")

    from .auth import create_token
    token = create_token(req.username, user.get('role', 'user'))
    return {
        "ok": True,
        "token": token,
        "user": {
            "username": req.username,
            "role": user.get('role', 'user'),
        }
    }


@router.get("/me")
async def me(payload: dict = Depends(verify_token)):
    return {
        "username": payload['sub'],
        "role": payload['role'],
    }


# ==================== 连接管理 ====================

@router.get("/connections")
async def list_connections(payload: dict = Depends(verify_token)):
    from .server import app_state
    conns = app_state.get('connections', [])
    return [{
        "name": c.name,
        "host": c.host,
        "port": c.port,
        "user": c.user,
        "database": c.database,
    } for c in conns]


@router.post("/connections")
async def create_connection(
        req: ConnectionModel,
        payload: dict = Depends(require_permission('admin'))):
    """新增/覆盖一条连接（仅 admin）。

    此前 Web 版**没有**任何写入 `app_state['connections']` 的端点，
    导致 `/api/v1/query` 恒 404、浏览器用户根本查不了库。现在：
      * 连接写入内存后立即可被 `/api/v1/query?connection=<name>` 使用；
      * 若配置了 ``LIGHTDB_WEB_CONNECTIONS_FILE`` / ``--connections-file``，
        同时持久化为 JSON 数组（0600，含口令），重启不丢；
      * 响应不回显口令。
    """
    from .server import (app_state, resolve_connections_file,
                         save_connections_file)

    name = (req.name or '').strip()
    if not name:
        raise HTTPException(400, "连接名不能为空")
    if len(name) > 128:
        raise HTTPException(400, "连接名过长（上限 128 字符）")

    try:
        info = ConnectionInfo(**_model_dict(req))
    except TypeError as e:
        raise HTTPException(400, f"连接配置字段非法: {e}")

    info.name = name
    conns = app_state.setdefault('connections', [])
    replaced = False
    for i, c in enumerate(conns):
        if c.name == name:
            conns[i] = info
            replaced = True
            break
    if not replaced:
        conns.append(info)
    _drop_sessions_for_connection(name)

    persisted_path = resolve_connections_file()
    persisted = False
    warning = None
    if persisted_path:
        persisted = save_connections_file(persisted_path, conns)
        if not persisted:
            warning = (f"连接 '{name}' 已在内存生效，但持久化到 "
                       f"{persisted_path} 失败（重启后会丢失）")

    return {
        "ok": True,
        "replaced": replaced,
        "persisted": persisted,
        "warning": warning,
        "connection": {
            "name": info.name,
            "host": info.host,
            "port": info.port,
            "user": info.user,
            "database": info.database,
        },
    }


def _allowed_hosts() -> List[str]:
    raw = (os.environ.get('LIGHTDB_WEB_ALLOWED_HOSTS') or '').strip()
    return [h.strip().lower() for h in raw.split(',') if h.strip()]


def _check_connect_host(host: str, payload: dict):
    """``/connections/test`` 的目标主机白名单 + 审计日志。

    未设置 ``LIGHTDB_WEB_ALLOWED_HOSTS`` 时保持行为（允许）但记审计日志；
    设置后只允许名单内的主机（防 SSRF 探测内网/云元数据地址）。
    """
    host_norm = (host or '').strip().lower()
    allowed = _allowed_hosts()
    if allowed:
        if host_norm not in allowed:
            logger.warning(
                '拒绝 /connections/test 到未授权主机 %r（用户=%r，白名单=%s）',
                host, payload.get('sub'), allowed)
            raise HTTPException(
                403, f"主机 {host!r} 不在 LIGHTDB_WEB_ALLOWED_HOSTS 白名单内")
        logger.info('/connections/test 白名单主机 %r（用户=%r）',
                    host, payload.get('sub'))
    else:
        logger.warning(
            '审计: /connections/test 连接目标 %r（用户=%r）；未设置 '
            'LIGHTDB_WEB_ALLOWED_HOSTS，默认允许', host, payload.get('sub'))


@router.post("/connections/test")
async def test_connection(req: ConnectionModel,
                          payload: dict = Depends(
                              require_permission('admin'))):
    """测试连接：仅 admin（此前任何已登录用户都能拿它打任意主机端口）。"""
    _check_connect_host(req.host, payload)
    try:
        info = ConnectionInfo(**_model_dict(req))
        conn = DBConnection(info)
        conn.connect()
        version = conn.server_version
        conn.close()
        return {"ok": True, "version": version}
    except Exception as e:                        # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ==================== 元数据 ====================

@router.get("/databases")
async def list_databases(
        connection: Optional[str] = None,
        token: str = Header(None, alias="X-Connection-Token"),
        payload: dict = Depends(verify_token)):
    conn = _get_conn(payload['sub'], connection)
    try:
        return {"ok": True, "databases": conn.get_databases()}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/databases/{db}/tables")
async def list_tables(
        db: str,
        connection: Optional[str] = None,
        payload: dict = Depends(verify_token)):
    db = _ident(db, '数据库名')
    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
        tables = conn.get_tables()
        # 补上行数
        result = []
        for t in tables:
            try:
                safe_t = _ident(t, '表名')
                _, r = conn.query(f"SELECT COUNT(*) FROM `{safe_t}`")
                n = r[0][0] if r else 0
            except Exception:                     # noqa: BLE001
                n = 0
            result.append({"name": t, "rows": n})
        return {"ok": True, "tables": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/databases/{db}/tables/{table}/columns")
async def list_columns(
        db: str, table: str,
        connection: Optional[str] = None,
        payload: dict = Depends(verify_token)):
    db = _ident(db, '数据库名')
    table = _ident(table, '表名')
    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
        cols = conn.get_columns(table)
        return {"ok": True, "columns": cols}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ==================== SQL 执行 ====================

@router.post("/query", response_model=QueryResponse)
async def execute_query(
        req: QueryRequest,
        payload: dict = Depends(require_permission('query'))):
    # 客户端传入的 schema 名同样要过标识符校验（会进入连接会话切换）
    if req.database:
        req.database = _ident(req.database, '数据库名')

    conn = _get_conn(payload['sub'], req.connection)
    if req.database:
        try:
            conn.switch_database(req.database)
        except Exception:                         # noqa: BLE001
            pass

    t0 = time.time()
    try:
        up = req.sql.strip().upper()

        if up.startswith(('SELECT', 'SHOW', 'DESC', 'EXPLAIN', 'WITH')):
            cols, rows = conn.query(req.sql)
            elapsed = (time.time() - t0) * 1000

            # 截断
            if len(rows) > req.limit:
                rows = rows[:req.limit]

            # 转换
            safe_rows = [_serialize_row(r) for r in rows]

            return QueryResponse(
                ok=True,
                columns=cols,
                rows=safe_rows,
                row_count=len(safe_rows),
                elapsed_ms=elapsed,
            )
        else:
            # 写操作：需要额外权限
            if not _has_perm(payload, 'write'):
                raise HTTPException(403, "无写权限")

            rows_affected, err = conn.execute(req.sql)
            elapsed = (time.time() - t0) * 1000
            if err:
                return QueryResponse(ok=False, error=err,
                                     elapsed_ms=elapsed)
            return QueryResponse(
                ok=True, affected=rows_affected,
                elapsed_ms=elapsed,
            )
    except HTTPException:
        raise
    except Exception as e:
        return QueryResponse(
            ok=False, error=str(e),
            elapsed_ms=(time.time() - t0) * 1000)


def _serialize_row(row):
    """处理不可序列化的值"""
    result = []
    for v in row:
        if v is None or isinstance(v, (int, float, str, bool)):
            result.append(v)
        elif isinstance(v, bytes):
            result.append(f"<binary {len(v)}B>")
        else:
            result.append(str(v))
    return result


def _has_perm(payload, perm):
    role = payload.get('role', 'user')
    perms = {
        'admin': {'query', 'write', 'ddl', 'admin'},
        'developer': {'query', 'write'},
        'analyst': {'query'},
        'viewer': {'query'},
    }
    return perm in perms.get(role, set())


# ==================== 表操作 ====================

@router.get("/databases/{db}/tables/{table}/data")
async def get_table_data(
        db: str, table: str,
        limit: int = Query(100, ge=1, le=10000),
        offset: int = Query(0, ge=0),
        order_by: Optional[str] = None,
        order_desc: bool = False,
        connection: Optional[str] = None,
        payload: dict = Depends(verify_token)):
    # 标识符校验必须在任何 I/O 之前：非法输入一律 400（而不是 500/404）
    db = _ident(db, '数据库名')
    table = _ident(table, '表名')
    if order_by:
        order_by = _ident(order_by, 'order_by 列名')
    direction = 'DESC' if order_desc else 'ASC'   # 枚举，不接受用户任意串

    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
    except Exception as e:
        raise HTTPException(500, str(e))

    sql = f"SELECT * FROM `{table}`"
    if order_by:
        sql += f" ORDER BY `{order_by}` {direction}"
    sql += f" LIMIT {limit} OFFSET {offset}"   # limit/offset 已被 Query 校验为 int

    try:
        cols, rows = conn.query(sql)
        return {
            "ok": True,
            "columns": cols,
            "rows": [_serialize_row(r) for r in rows],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/databases/{db}/tables/{table}/insert")
async def insert_row(
        db: str, table: str,
        data: dict,
        connection: Optional[str] = None,
        payload: dict = Depends(require_permission('write'))):
    db = _ident(db, '数据库名')
    table = _ident(table, '表名')
    if not data:
        raise HTTPException(400, "插入数据不能为空")
    cols = [_ident(c, '列名') for c in data.keys()]
    vals = list(data.values())

    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
        cols_str = ', '.join(f"`{c}`" for c in cols)
        placeholders = ', '.join(['%s'] * len(cols))
        sql = f"INSERT INTO `{table}` ({cols_str}) VALUES ({placeholders})"
        n, err = conn.execute(sql, tuple(vals))
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "affected": n}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.put("/databases/{db}/tables/{table}/update")
async def update_rows(
        db: str, table: str,
        set_data: dict,
        where: dict,
        connection: Optional[str] = None,
        payload: dict = Depends(require_permission('write'))):
    if not where:
        raise HTTPException(400, "必须提供 WHERE 条件")
    if not set_data:
        raise HTTPException(400, "SET 数据不能为空")

    db = _ident(db, '数据库名')
    table = _ident(table, '表名')
    set_cols = [_ident(k, '列名') for k in set_data.keys()]
    where_cols = [_ident(k, 'WHERE 列名') for k in where.keys()]

    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
        set_parts = ', '.join(f"`{k}` = %s" for k in set_cols)
        where_parts = ' AND '.join(f"`{k}` = %s" for k in where_cols)
        sql = f"UPDATE `{table}` SET {set_parts} WHERE {where_parts}"
        params = list(set_data.values()) + list(where.values())
        n, err = conn.execute(sql, tuple(params))
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "affected": n}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/databases/{db}/tables/{table}/delete")
async def delete_rows(
        db: str, table: str,
        where: dict,
        connection: Optional[str] = None,
        payload: dict = Depends(require_permission('write'))):
    if not where:
        raise HTTPException(400, "必须提供 WHERE 条件")

    db = _ident(db, '数据库名')
    table = _ident(table, '表名')
    where_cols = [_ident(k, 'WHERE 列名') for k in where.keys()]

    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
        where_parts = ' AND '.join(f"`{k}` = %s" for k in where_cols)
        sql = f"DELETE FROM `{table}` WHERE {where_parts}"
        n, err = conn.execute(sql, tuple(where.values()))
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "affected": n}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ==================== 查询历史 ====================
# 说明（P2，遗留）：QueryHistory 是桌面端共享的本地历史文件，条目里没有
# owner 字段，无法按 payload['sub'] 过滤。此处保持现有行为（仅登录用户
# 可读，未登录 401），按用户隔离历史需要先给 lightdb_studio 的历史条目
# 增加 owner 字段，列为 P2。

@router.get("/history")
async def get_history(
        limit: int = Query(50, ge=1, le=500),
        payload: dict = Depends(verify_token)):
    from lightdb_studio.utils.history import QueryHistory
    h = QueryHistory()
    return {"ok": True, "entries": h.recent(limit)}


# ==================== 报表 ====================

@router.get("/reports")
async def list_reports(payload: dict = Depends(verify_token)):
    from lightdb_studio.core.reports import ReportEngine
    eng = ReportEngine()
    return {
        "ok": True,
        "reports": [{
            "name": t.name,
            "description": t.description,
            "schedule": t.schedule,
            "last_run": t.last_run,
        } for t in eng.templates]
    }


@router.post("/reports/{name}/run")
async def run_report(name: str,
                     connection: Optional[str] = None,
                     payload: dict = Depends(verify_token)):
    from lightdb_studio.core.reports import ReportEngine
    eng = ReportEngine()
    tmpl = eng.get(name)
    if not tmpl:
        raise HTTPException(404, "报表不存在")

    conn = _get_conn(payload['sub'], connection or tmpl.connection)
    try:
        if tmpl.database:
            conn.switch_database(tmpl.database)
        cols, rows = conn.query(tmpl.sql)
        return {
            "ok": True,
            "columns": cols,
            "rows": [_serialize_row(r) for r in rows],
        }
    except Exception as e:
        raise HTTPException(500, str(e))
