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
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict
from typing import Dict, List, Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

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
    # task-60：SSH/SSL 高级字段（Web 只展示状态，绝不回显口令/私钥内容）
    ssh_options: Dict = Field(default_factory=dict)
    ssl_options: Dict = Field(default_factory=dict)


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

def _public_connection(c) -> dict:
    """task-60：连接列表只读展示 SSH/SSL 状态；口令/私钥内容绝不回显。"""
    ssh = dict(getattr(c, 'ssh_options', None) or {})
    ssl_opts = dict(getattr(c, 'ssl_options', None) or {})
    ssh_enabled = bool(ssh.get('enabled'))
    ssl_enabled = bool(getattr(c, 'ssl', False))
    return {
        "name": c.name, "host": c.host, "port": c.port, "user": c.user,
        "database": c.database,
        "ssh": {
            "enabled": ssh_enabled,
            "status": "enabled" if ssh_enabled else "disabled",
            "host": ssh.get('host') or None,
            "port": int(ssh.get('port') or 22) if ssh_enabled else None,
            "user": ssh.get('user') or None,
            "credential_set": bool(ssh.get('password')),
            "has_key": bool(ssh.get('key_path')),
        },
        "ssl": {
            "enabled": ssl_enabled,
            "status": "enabled" if ssl_enabled else "disabled",
            "has_ca": bool(ssl_opts.get('ca')),
            "has_client_cert": bool(ssl_opts.get('cert')),
            "verify_mode": ssl_opts.get('verify_mode') or None,
        },
    }


@router.get("/connections")
async def list_connections(payload: dict = Depends(verify_token)):
    from .server import app_state
    conns = app_state.get('connections', [])
    return [_public_connection(c) for c in conns]


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
        "connection": _public_connection(info),
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
        total = None
        try:
            _tc, trows = conn.query(f"SELECT COUNT(*) FROM `{table}`")
            if trows:
                total = int(trows[0][0])
        except Exception:
            total = None
        cols, rows = conn.query(sql)
        return {
            "ok": True,
            "columns": cols,
            "rows": [_serialize_row(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "order_by": order_by,
            "order_desc": bool(order_desc),
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


# ============================================================================
# P2 Web/GUI 能力对齐：共享 lightdb_studio.core 的 format / 报表 / 质量能力
# ============================================================================

#: GUI 能力 → Web 能力矩阵。测试断言 key/状态合法；后续能力变化必须同步这里。
WEB_GUI_PARITY: Dict[str, dict] = {
    'query_execution': {
        'gui': 'SQL 编辑器执行查询', 'web': 'POST /api/v1/query',
        'status': 'full', 'shared_core': 'lightdb_studio.db.connection',
        'notes': '参数/标识符校验与权限沿用现有 Web 查询链路',
    },
    'connection_management': {
        'gui': '连接管理 / 对象树', 'web': 'GET/POST /api/v1/connections, '
        'GET /databases*, GET /tables*, POST /connections/test',
        'status': 'partial', 'shared_core': 'lightdb_studio.config.ConnectionInfo',
        'notes': 'Web 可增改/测试连接与浏览对象；列表只读展示 SSH/SSL '
                 '状态（不回显口令）；编辑连接对话框是 GUI 专属交互',
    },
    'object_browser': {
        'gui': '对象浏览器（库/表/列）', 'web': 'GET /databases, /tables, /columns',
        'status': 'full', 'shared_core': 'lightdb_studio.db.connection',
        'notes': '表行数统计、分页/排序数据浏览为 Web 补充能力',
    },
    'table_designer': {
        'gui': '表设计器（列编辑/AUTO_INCREMENT/索引）',
        'web': 'GET/POST /api/v1/databases/{db}/tables/{table}/design',
        'status': 'partial', 'shared_core': 'lightdb_studio.db.connection',
        'notes': '预览/保存列增删改、AUTO_INCREMENT、索引增删；主键/唯一变更'
                 '仍显式拒绝（引擎 ALTER 变体限制）',
    },
    'result_paging': {
        'gui': '数据网格分页/排序',
        'web': 'GET /databases/{db}/tables/{table}/data?limit&offset&order_by',
        'status': 'full', 'shared_core': 'lightdb_studio.db.connection',
        'notes': '返回 total/limit/offset/order_by；前端网格分页排序',
    },
    'explain': {
        'gui': 'EXPLAIN 执行计划',
        'web': 'POST /api/v1/explain',
        'status': 'full', 'shared_core': 'lightdb_studio.db.connection',
        'notes': '仅 SELECT/WITH；错误显式 400',
    },
    'sql_format': {
        'gui': '编辑器「格式化 SQL」', 'web': 'POST /api/v1/sql/format',
        'status': 'full', 'shared_core': 'lightdb_studio.core.sql_formatter',
        'notes': '与 GUI 同一 SQLFormatter；空 SQL 显式 400',
    },
    'chart': {
        'gui': '数据图表（可配置/PNG/SVG）', 'web': None,
        'status': 'not_applicable',
        'notes': '图表依赖 Qt 渲染，Web 端以 API 数据为主；无对应端点',
    },
    'reports': {
        'gui': '报表设计器（模板/参数/预览/导出）',
        'web': 'GET/POST/DELETE /reports/templates, '
        'POST /reports/templates/{name}/preview, '
        'POST /reports/templates/{name}/export',
        'status': 'full', 'shared_core': 'lightdb_studio.core.reports',
        'notes': '参数化 preview/export 复用 ReportEngine；html/csv/xlsx 真实生成',
    },
    'schema_compare': {
        'gui': 'Schema 对比 / ER 图',
        'web': 'GET /api/v1/schema/compare?source_db=&target_db=',
        'status': 'partial', 'shared_core': 'lightdb_studio.db.connection',
        'notes': '只读结构对比（缺失表/列差异/类型）；Web 不做自动同步写入',
    },
    'snapshot_backup': {
        'gui': '快照/备份/恢复',
        'web': 'POST /api/v1/backup',
        'status': 'partial', 'shared_core': 'lightdb_studio.db.connection',
        'notes': '真实生成逻辑 SQL 备份（SHOW CREATE + INSERT）；不含恢复/'
                 '二进制快照（桌面端文件系统能力）',
    },
    'data_quality': {
        'gui': '数据质量检查', 'web': 'POST /api/v1/quality/check',
        'status': 'partial',
        'shared_core': 'lightdb_studio.core.data_quality',
        'notes': '复用 DataQualityChecker；Web 当前按单表触发并返回 issues 摘要',
    },
    'import_export': {
        'gui': 'CSV 导入/导出（进度/取消）',
        'web': 'GET .../export?fmt=csv|sql; POST .../import; '
               'POST /api/v1/query/export',
        'status': 'full', 'shared_core': 'lightdb_studio.db.connection',
        'notes': '表 CSV/SQL 导出、CSV 导入（显式行错误）、查询结果 CSV/SQL '
                 '导出；大文件流式进度/取消未做（P2）',
    },
    'plugins': {
        'gui': '插件启用/禁用/菜单注入', 'web': None,
        'status': 'not_applicable',
        'notes': '插件作用于本地 GUI 进程，Web 进程不加载插件',
    },
    'i18n': {
        'gui': '中英文热切换', 'web': None,
        'status': 'not_applicable',
        'notes': 'Web 是 API，错误/数据为程序消费；Web UI 文案暂未 i18n',
    },
}


def _err(status_code: int, message: str, code: str) -> HTTPException:
    """新端点统一的 JSON 错误：``{"detail": {"detail": ..., "code": ...}}``。"""
    return HTTPException(status_code, detail={'detail': str(message),
                                              'code': str(code)})


def _report_engine():
    """报表引擎：目录可注入（create_app(reports_dir=...) / env），默认为用户目录。"""
    from lightdb_studio.core.reports import ReportEngine
    from .server import app_state
    base = app_state.get('reports_dir') or os.environ.get(
        'LIGHTDB_WEB_REPORTS_DIR')
    return ReportEngine(base_dir=str(base)) if base else ReportEngine()


def _template_to_dict(t) -> dict:
    return {
        'name': t.name, 'description': t.description, 'sql': t.sql,
        'connection': t.connection, 'database': t.database,
        'chart': dict(t.chart or {}), 'params': dict(t.params or {}),
        'preview_limit': int(t.preview_limit or 100),
        'schedule': t.schedule, 'last_run': t.last_run,
        'last_status': t.last_status,
    }


class SqlFormatRequest(BaseModel):
    sql: str
    minify: bool = False


class ReportTemplateModel(BaseModel):
    name: str
    description: str = ''
    sql: str
    connection: str = ''
    database: str = ''
    params: Dict = Field(default_factory=dict)
    chart: Dict = Field(default_factory=dict)
    preview_limit: int = Field(100, ge=1, le=10000)
    schedule: str = ''


class ReportPreviewRequest(BaseModel):
    params: Optional[Union[Dict, List]] = None
    limit: Optional[int] = Field(None, ge=1, le=10000)
    connection: Optional[str] = None


class QualityRequest(BaseModel):
    table: Optional[str] = None
    connection: Optional[str] = None


@router.post("/sql/format")
async def format_sql(req: SqlFormatRequest,
                     payload: dict = Depends(verify_token)):
    """SQL 格式化（与 GUI 共用 core.sql_formatter）；空 SQL 显式 400。"""
    sql = (req.sql or '').strip()
    if not sql:
        raise _err(400, 'SQL 不能为空', 'empty_sql')
    try:
        from lightdb_studio.core.sql_formatter import SQLFormatter
        fmt = SQLFormatter()
        formatted = fmt.minify(sql) if req.minify else fmt.format(sql)
    except Exception as exc:                          # noqa: BLE001
        raise _err(400, f'SQL 格式化失败: {exc}', 'format_failed')
    return {'ok': True, 'sql': formatted,
            'length_in': len(sql), 'length_out': len(formatted)}


# ---------------- 报表模板 CRUD ----------------

@router.get('/reports/templates')
async def list_report_templates(payload: dict = Depends(verify_token)):
    try:
        eng = _report_engine()
    except Exception as exc:                          # noqa: BLE001
        raise _err(500, f'报表引擎初始化失败: {exc}', 'reports_unavailable')
    return {'ok': True,
            'templates': [_template_to_dict(t) for t in eng.templates]}


@router.post('/reports/templates')
async def save_report_template(
        req: ReportTemplateModel,
        payload: dict = Depends(require_permission('write'))):
    from lightdb_studio.core.reports import (
        ReportTemplate, ReportValidationError)
    try:
        tmpl = ReportTemplate(
            name=req.name, description=req.description, sql=req.sql,
            connection=req.connection, database=req.database,
            params=dict(req.params or {}), chart=dict(req.chart or {}),
            preview_limit=req.preview_limit, schedule=req.schedule)
        tmpl.validate()
    except ReportValidationError as exc:
        raise _err(400, str(exc), 'invalid_template')
    except Exception as exc:                          # noqa: BLE001
        raise _err(400, f'模板字段非法: {exc}', 'invalid_template')
    try:
        eng = _report_engine()
        saved = eng.upsert(tmpl)
    except Exception as exc:                          # noqa: BLE001
        raise _err(500, f'模板保存失败: {exc}', 'reports_unavailable')
    return {'ok': True, 'template': _template_to_dict(saved)}


@router.delete('/reports/templates/{name}')
async def delete_report_template(
        name: str,
        payload: dict = Depends(require_permission('write'))):
    try:
        eng = _report_engine()
    except Exception as exc:                          # noqa: BLE001
        raise _err(500, f'报表引擎初始化失败: {exc}', 'reports_unavailable')
    if not eng.remove_by_name(name):
        raise _err(404, f"报表模板不存在: {name}", 'template_not_found')
    return {'ok': True, 'deleted': name}


def _load_template_or_404(eng, name):
    tmpl = eng.get(name)
    if tmpl is None:
        raise _err(404, f"报表模板不存在: {name}", 'template_not_found')
    return tmpl


def _bound_params(tmpl, req_params):
    """请求未给 params 时用模板保存的默认参数（可能是空 dict）。"""
    return dict(req_params) if isinstance(req_params, dict) else (
        list(req_params) if isinstance(req_params, list) else dict(tmpl.params or {}))


def _run_report_with_params(eng, tmpl, req, token=None):
    from lightdb_studio.core.reports import ReportParameterError
    params = _bound_params(tmpl, getattr(req, 'params', None))
    conn_name = (getattr(req, 'connection', None) or tmpl.connection) or None
    try:
        conn = _get_conn(token, conn_name)
    except HTTPException:
        raise
    except Exception as exc:                          # noqa: BLE001
        raise _err(400, f'连接不可用: {exc}', 'connection_failed')
    try:
        if tmpl.database:
            conn.switch_database(tmpl.database)
    except Exception:
        pass
    try:
        limit = getattr(req, 'limit', None)
        if not limit:
            limit = int(getattr(tmpl, 'preview_limit', 0) or 0) or None
        result = eng.preview_query(conn, tmpl.sql, params, limit)
    except ReportParameterError as exc:
        raise _err(400, str(exc), 'invalid_params')
    except Exception as exc:                          # noqa: BLE001
        raise _err(400, f'报表执行失败: {exc}', 'query_failed')
    return result


@router.post('/reports/templates/{name}/preview')
async def preview_report_template(
        name: str,
        req: Optional[ReportPreviewRequest] = None,
        payload: dict = Depends(verify_token)):
    req = req or ReportPreviewRequest()
    try:
        eng = _report_engine()
    except Exception as exc:                          # noqa: BLE001
        raise _err(500, f'报表引擎初始化失败: {exc}', 'reports_unavailable')
    tmpl = _load_template_or_404(eng, name)
    result = _run_report_with_params(eng, tmpl, req, token=payload['sub'])
    return {
        'ok': True,
        'name': tmpl.name,
        'columns': result['columns'],
        'rows': [_serialize_row(r) for r in result['rows']],
        'total': result['total'], 'returned': result['returned'],
        'limit': result['limit'], 'params': result['params'],
        'style': result['style'], 'param_names': result['param_names'],
    }


_EXPORT_TYPES = {
    'csv': ('text/csv; charset=utf-8', 'csv'),
    'html': ('text/html; charset=utf-8', 'html'),
    'xlsx': ('application/vnd.openxmlformats-officedocument.'
             'spreadsheetml.sheet', 'xlsx'),
}


@router.post('/reports/templates/{name}/export')
async def export_report_template(
        name: str,
        format: str = Query('csv'),
        req: Optional[ReportPreviewRequest] = None,
        payload: dict = Depends(verify_token)):
    fmt = (format or '').strip().lower()
    if fmt not in _EXPORT_TYPES:
        raise _err(400, f'不支持的格式: {format!r}（可选 html/csv/xlsx）',
                   'invalid_format')
    req = req or ReportPreviewRequest()
    try:
        eng = _report_engine()
    except Exception as exc:                          # noqa: BLE001
        raise _err(500, f'报表引擎初始化失败: {exc}', 'reports_unavailable')
    tmpl = _load_template_or_404(eng, name)
    # 导出全量（不受 preview limit 截断）
    from lightdb_studio.core.reports import ReportParameterError
    params = _bound_params(tmpl, req.params)
    conn_name = (req.connection or tmpl.connection) or None
    try:
        conn = _get_conn(payload['sub'], conn_name)
        if tmpl.database:
            conn.switch_database(tmpl.database)
        cols, rows = eng.run_query(conn, tmpl.sql, params)
    except ReportParameterError as exc:
        raise _err(400, str(exc), 'invalid_params')
    except HTTPException:
        raise
    except Exception as exc:                          # noqa: BLE001
        raise _err(400, f'报表导出失败: {exc}', 'query_failed')

    tmpdir = tempfile.mkdtemp(prefix='lightdb_web_report_')
    ext = _EXPORT_TYPES[fmt][1]
    filepath = os.path.join(tmpdir, f'{tmpl.name}.{ext}')
    try:
        eng.export_data(cols, rows, filepath, fmt=fmt, title=tmpl.name)
    except Exception as exc:                          # noqa: BLE001
        raise _err(400, f'导出文件生成失败: {exc}', 'export_failed')
    if not os.path.exists(filepath) or os.path.getsize(filepath) <= 0:
        raise _err(400, '导出文件为空', 'export_failed')
    media_type = _EXPORT_TYPES[fmt][0]

    def _cleanup():
        try:
            os.remove(filepath)
        except OSError:
            pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    return FileResponse(filepath, media_type=media_type,
                        filename=f'{tmpl.name}.{ext}',
                        background=BackgroundTask(_cleanup))


# ---------------- 数据质量 ----------------

@router.post('/quality/check')
async def quality_check(req: QualityRequest = None,
                        payload: dict = Depends(verify_token)):
    req = req or QualityRequest()
    table = (req.table or '').strip()
    if not table:
        raise _err(400, 'table 不能为空（请指定要检查的表）', 'table_required')
    table = _ident(table, '表名')
    try:
        from lightdb_studio.core.data_quality import DataQualityChecker
    except Exception as exc:                          # noqa: BLE001
        raise _err(503, f'数据质量模块不可用: {exc}', 'quality_unavailable')
    try:
        conn = _get_conn(payload['sub'], req.connection or None)
    except HTTPException:
        raise
    except Exception as exc:                          # noqa: BLE001
        raise _err(400, f'连接不可用: {exc}', 'connection_failed')
    try:
        issues = DataQualityChecker(conn).check_table(table)
    except Exception as exc:                          # noqa: BLE001
        raise _err(400, f'数据质量检查失败: {exc}', 'quality_failed')
    issue_dicts = [asdict(i) if hasattr(i, '__dataclass_fields__') else dict(i)
                   for i in issues]
    summary = {'total': len(issue_dicts), 'error': 0, 'warning': 0, 'info': 0}
    for item in issue_dicts:
        level = str(item.get('level') or 'info').lower()
        if level not in summary:
            level = 'info'
        summary[level] += 1
    return {'ok': True, 'table': table, 'issues': issue_dicts,
            'summary': summary}

