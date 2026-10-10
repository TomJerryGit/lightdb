"""
LightDB Studio REST API
"""
import json
import time
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, Header, Query
from pydantic import BaseModel, Field

from .auth import verify_token, require_permission
from lightdb_studio.db.connection import DBConnection
from lightdb_studio.config import ConnectionInfo


router = APIRouter(prefix="/api/v1", tags=["lightdb"])


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
# 简单实现：每个用户最多维持一个连接

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

@router.post("/login")
async def login(req: LoginRequest):
    from .server import app_state
    users = app_state.get('users', {})
    user = users.get(req.username)
    if not user or user['password'] != req.password:
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


@router.post("/connections/test")
async def test_connection(req: ConnectionModel,
                           payload: dict = Depends(verify_token)):
    try:
        info = ConnectionInfo(**req.dict())
        conn = DBConnection(info)
        conn.connect()
        version = conn.server_version
        conn.close()
        return {"ok": True, "version": version}
    except Exception as e:
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
    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
        tables = conn.get_tables()
        # 补上行数
        result = []
        for t in tables:
            try:
                _, r = conn.query(f"SELECT COUNT(*) FROM `{t}`")
                n = r[0][0] if r else 0
            except Exception:
                n = 0
            result.append({"name": t, "rows": n})
        return {"ok": True, "tables": result}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/databases/{db}/tables/{table}/columns")
async def list_columns(
        db: str, table: str,
        connection: Optional[str] = None,
        payload: dict = Depends(verify_token)):
    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
        cols = conn.get_columns(table)
        return {"ok": True, "columns": cols}
    except Exception as e:
        raise HTTPException(500, str(e))


# ==================== SQL 执行 ====================

@router.post("/query", response_model=QueryResponse)
async def execute_query(
        req: QueryRequest,
        payload: dict = Depends(require_permission('query'))):
    conn = _get_conn(payload['sub'], req.connection)
    if req.database:
        try:
            conn.switch_database(req.database)
        except Exception:
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
    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
    except Exception as e:
        raise HTTPException(500, str(e))

    sql = f"SELECT * FROM `{table}`"
    if order_by:
        sql += f" ORDER BY `{order_by}` {'DESC' if order_desc else 'ASC'}"
    sql += f" LIMIT {limit} OFFSET {offset}"

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
    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
        cols = list(data.keys())
        vals = list(data.values())
        cols_str = ', '.join(f"`{c}`" for c in cols)
        placeholders = ', '.join(['%s'] * len(cols))
        sql = f"INSERT INTO `{table}` ({cols_str}) VALUES ({placeholders})"
        n, err = conn.execute(sql, tuple(vals))
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "affected": n}
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

    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
        set_parts = ', '.join(f"`{k}` = %s" for k in set_data.keys())
        where_parts = ' AND '.join(f"`{k}` = %s" for k in where.keys())
        sql = f"UPDATE `{table}` SET {set_parts} WHERE {where_parts}"
        params = list(set_data.values()) + list(where.values())
        n, err = conn.execute(sql, tuple(params))
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "affected": n}
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

    conn = _get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
        where_parts = ' AND '.join(f"`{k}` = %s" for k in where.keys())
        sql = f"DELETE FROM `{table}` WHERE {where_parts}"
        n, err = conn.execute(sql, tuple(where.values()))
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "affected": n}
    except Exception as e:
        raise HTTPException(500, str(e))


# ==================== 查询历史 ====================

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