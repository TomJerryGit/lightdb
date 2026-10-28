# -*- coding: utf-8 -*-
"""task-60：Web GUI 对标 P0/P1 端点（表设计器 / 导入导出 / EXPLAIN / 备份 /
结构对比 / 结果分页排序辅助）。所有写操作真实调用 DBConnection，错误显式；
标识符走 ``web.api._ident`` 白名单，值参数化，绝不拼接用户值。
"""
import csv
import io
import os
import re
import tempfile
import threading
import time
import uuid
from typing import Dict, List, Optional, Union

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile)
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from . import api
from .auth import require_permission, verify_token

router = APIRouter(prefix="/api/v1", tags=["web-parity"])

# task-69：multipart CSV 上传大小上限（可用 env 覆盖；测试可 monkeypatch）
MAX_IMPORT_BYTES = int(os.environ.get(
    'LIGHTDB_WEB_IMPORT_MAX_BYTES', str(20 * 1024 * 1024)))

_TYPE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
_LEN_RE = re.compile(r'^\d+(,\d+)?$')


# ==================== 请求模型 ====================

class ColumnSpec(BaseModel):
    name: str
    type: str = 'INT'
    length: Union[str, int] = ''
    notnull: bool = False
    primary: bool = False
    unique: bool = False
    auto_increment: bool = False
    default: Optional[str] = None
    # 仅新增列：'' / '末尾' / 'FIRST' / 'AFTER col'
    position: str = ''


class IndexSpec(BaseModel):
    name: str
    columns: List[str]
    unique: bool = False


class DesignRequest(BaseModel):
    columns: List[ColumnSpec]
    indexes: Optional[List[IndexSpec]] = None
    dry_run: bool = True
    auto_increment_start: Optional[int] = None


class CsvImportRequest(BaseModel):
    csv: str
    has_header: bool = True


class QueryExportRequest(BaseModel):
    sql: str
    connection: Optional[str] = None
    database: Optional[str] = None
    format: str = 'csv'
    limit: int = Field(10000, ge=1, le=100000)


class ExplainRequest(BaseModel):
    sql: str
    connection: Optional[str] = None
    database: Optional[str] = None


class BackupRequest(BaseModel):
    connection: Optional[str] = None
    database: Optional[str] = None
    tables: Optional[List[str]] = None
    limit_per_table: int = Field(1000, ge=1, le=100000)


# ==================== 通用辅助 ====================

def _err(status: int, message: str, code: str, **extra):
    detail = {'detail': str(message), 'code': str(code)}
    detail.update(extra)
    return HTTPException(status, detail=detail)


def _conn_for(db: str, connection, payload):
    conn = api._get_conn(payload['sub'], connection)
    try:
        conn.switch_database(db)
    except HTTPException:
        raise
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'切换数据库失败: {exc}', 'switch_database_failed')
    return conn


def _type_sql(spec: ColumnSpec):
    ctype = str(spec.type or '').strip().upper()
    if not _TYPE_RE.match(ctype):
        raise _err(400, f'非法列类型: {spec.type!r}', 'invalid_column_type')
    length = str(spec.length or '').strip()
    if length:
        if not _LEN_RE.match(length):
            raise _err(400, f'非法列长度: {length!r}', 'invalid_column_length')
        ctype += f'({length})'
    return ctype


def _default_sql(value):
    if value is None:
        return ''
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    if text == '':
        return "''"
    upper = text.upper()
    if upper in ('NULL', 'CURRENT_TIMESTAMP', 'CURRENT_TIMESTAMP()'):
        return text
    if re.match(r'^-?\d+(\.\d+)?$', text):
        return text
    return "'" + text.replace("'", "''") + "'"


def _position_clause(position: str):
    value = str(position or '').strip()
    if not value or value in ('末尾', '—', 'TAIL'):
        return ''
    if value.upper() == 'FIRST':
        return 'FIRST'
    if value.upper().startswith('AFTER'):
        anchor = value[5:].strip().strip('`')
        if not anchor:
            raise _err(400, '位置 AFTER 缺少锚点列', 'invalid_position')
        return f'AFTER `{api._ident(anchor, "位置锚点列名")}`'
    return f'AFTER `{api._ident(value, "位置锚点列名")}`'


def _column_add_sql(table, spec: ColumnSpec):
    name = api._ident(spec.name, '列名')
    parts = [f"ALTER TABLE `{table}` ADD COLUMN `{name}` {_type_sql(spec)}"]
    parts.append('NOT NULL' if (spec.notnull or spec.auto_increment)
                 else 'NULL')
    if spec.default is not None:
        parts.append(f'DEFAULT {_default_sql(spec.default)}')
    if spec.unique:
        parts.append('UNIQUE')
    if spec.auto_increment:
        parts.append('AUTO_INCREMENT')
    clause = _position_clause(spec.position)
    if clause:
        parts.append(clause)
    return ' '.join(parts) + ';'


def _column_modify_sql(table, spec: ColumnSpec):
    name = api._ident(spec.name, '列名')
    parts = [f"ALTER TABLE `{table}` MODIFY COLUMN `{name}` {_type_sql(spec)}",
             'NOT NULL' if spec.notnull else 'NULL']
    if spec.default is not None:
        parts.append(f'DEFAULT {_default_sql(spec.default)}')
    if spec.auto_increment:
        parts.append('AUTO_INCREMENT')
    return ' '.join(parts) + ';'


def _normalize_db_column(c: dict) -> dict:
    full_type = str(c.get('type') or '')
    base, length = full_type, ''
    if '(' in full_type:
        base, _, rest = full_type.partition('(')
        length = rest.rstrip(')')
    key = str(c.get('key') or '').upper()
    extra = str(c.get('extra') or '').lower()
    return {
        'name': str(c.get('name') or ''),
        'type': base.strip().upper(),
        'length': length.strip(),
        'notnull': str(c.get('null', 'YES')).upper() == 'NO',
        'primary': key == 'PRI',
        'unique': key == 'UNI',
        'auto_increment': 'auto_increment' in extra,
        'default': '' if c.get('default') is None else str(c.get('default')),
        'position': '',
    }


def _spec_norm(spec: ColumnSpec) -> dict:
    return {
        'name': api._ident(spec.name, '列名'),
        'type': str(spec.type or '').strip().upper(),
        'length': str(spec.length or '').strip(),
        'notnull': bool(spec.notnull),
        'primary': bool(spec.primary),
        'unique': bool(spec.unique),
        'auto_increment': bool(spec.auto_increment),
        'default': None if spec.default is None else str(spec.default),
        'position': str(spec.position or ''),
    }


def _read_indexes(conn, table) -> List[dict]:
    try:
        cols, rows = conn.query(f"SHOW INDEX FROM `{table}`")
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'读取索引失败: {exc}', 'index_read_failed')
    index = {str(c).lower(): i for i, c in enumerate(cols or [])}
    out = {}
    for row in rows or []:
        def val(name, default=''):
            i = index.get(name.lower())
            return row[i] if i is not None and i < len(row) else default
        name = str(val('key_name'))
        if not name:
            continue
        item = out.setdefault(name, {
            'name': name, 'unique': int(val('non_unique', 1)) == 0,
            'columns': [],
        })
        item['columns'].append((int(val('seq_in_index', 1)),
                                str(val('column_name'))))
    for item in out.values():
        item['columns'] = [c for _n, c in sorted(item['columns'])]
    return sorted(out.values(), key=lambda x: x['name'])


def _read_design(conn, table):
    try:
        raw = conn.get_columns(table) or []
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'读取列失败: {exc}', 'design_read_failed')
    if not raw:
        raise _err(404, f'表不存在或没有列: {table}', 'table_not_found')
    columns = [_normalize_db_column(c) for c in raw]
    create_sql = ''
    try:
        _c, rows = conn.query(f"SHOW CREATE TABLE `{table}`")
        if rows:
            create_sql = str(rows[0][1])
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'SHOW CREATE TABLE 失败: {exc}', 'show_create_failed')
    auto_increment = None
    try:
        cols, rows = conn.query('SHOW TABLE STATUS')
        idx = {str(c).lower(): i for i, c in enumerate(cols or [])}.get(
            'auto_increment')
        if idx is not None:
            for row in rows or []:
                if str(row[0]) == table:
                    value = row[idx]
                    auto_increment = None if value is None else int(value)
                    break
    except Exception:
        auto_increment = None
    return {
        'ok': True,
        'table': table,
        'columns': columns,
        'indexes': _read_indexes(conn, table),
        'create_sql': create_sql,
        'auto_increment': auto_increment,
    }


def _build_alter(conn, table, req: DesignRequest):
    current = _read_design(conn, table)
    original = {c['name']: c for c in current['columns']}
    desired = [_spec_norm(s) for s in req.columns]
    if not desired:
        raise _err(400, '至少保留一列', 'empty_columns')
    names = [c['name'] for c in desired]
    if len(names) != len(set(names)):
        raise _err(400, '列名重复', 'duplicate_column')
    desired_map = {c['name']: c for c in desired}
    added = [c for c in desired if c['name'] not in original]
    removed = [n for n in original if n not in desired_map]
    final_names = names
    statements = []
    for name in removed:
        statements.append(
            f'ALTER TABLE `{table}` DROP COLUMN `{api._ident(name, "列名")}`;')
    for spec in desired:
        name = spec['name']
        if name in original:
            old = original[name]
            if spec['primary'] != old['primary']:
                raise _err(400, 'Web 不支持通过 ALTER 修改主键；请用唯一键',
                           'unsupported_primary_change')
            if spec['unique'] != old['unique']:
                raise _err(400, 'Web 不支持通过 ALTER 修改唯一约束',
                           'unsupported_unique_change')
            changed = any(spec[k] != old[k] for k in (
                'type', 'length', 'notnull', 'auto_increment'))
            if ('' if spec['default'] is None else spec['default']) != \
                    old['default']:
                changed = True
            if changed:
                cs = ColumnSpec(**spec)
                statements.append(_column_modify_sql(table, cs))
    for spec in added:
        name = spec['name']
        if spec['primary']:
            raise _err(400, 'ADD COLUMN 不支持 PRIMARY KEY；请改用 UNIQUE',
                       'unsupported_add_primary')
        if spec['auto_increment']:
            if not spec['unique']:
                raise _err(400, '新增 AUTO_INCREMENT 列必须勾选唯一键',
                           'auto_increment_requires_unique')
            if spec['type'] not in ('INT', 'BIGINT', 'SMALLINT', 'TINYINT',
                                    'MEDIUMINT'):
                raise _err(400, 'AUTO_INCREMENT 仅支持整数列',
                           'auto_increment_integer_only')
        anchor = _position_clause(spec['position'])
        if anchor.startswith('AFTER'):
            m = re.search(r'`([^`]+)`', anchor)
            if m:
                target = m.group(1)
                if target in removed:
                    raise _err(400, f'位置锚点列 {target} 将被删除',
                               'invalid_position_anchor')
                if target not in final_names:
                    raise _err(400, f'位置锚点列 {target} 不存在',
                               'invalid_position_anchor')
        statements.append(_column_add_sql(table, ColumnSpec(**spec)))
    if req.indexes is not None:
        current_idx = current['indexes']
        cur = {i['name']: i for i in current_idx}
        want = {}
        for idx in req.indexes:
            iname = api._ident(idx.name, '索引名')
            if iname in want:
                raise _err(400, f'索引名重复: {iname}', 'duplicate_index')
            cols = [api._ident(c, '索引列名') for c in idx.columns]
            if not cols:
                raise _err(400, f'索引 {iname} 没有列', 'empty_index')
            for c in cols:
                if c not in final_names:
                    raise _err(400, f'索引列不存在: {c}', 'invalid_index_column')
            want[iname] = {'name': iname, 'unique': bool(idx.unique),
                           'columns': cols}
        for name, item in want.items():
            old = cur.get(name)
            if old is None:
                unique = 'UNIQUE ' if item['unique'] else ''
                cols = ', '.join(f'`{c}`' for c in item['columns'])
                statements.append(
                    f'CREATE {unique}INDEX `{name}` ON `{table}` ({cols});')
            elif old['unique'] != item['unique'] or \
                    old['columns'] != item['columns']:
                raise _err(400, f'索引 {name} 已存在且定义不同；请先删除重建',
                           'unsupported_index_change')
        for name, item in cur.items():
            if name == 'PRIMARY':
                continue
            if name not in want:
                statements.append(f'DROP INDEX `{api._ident(name, "索引名")}` '
                                  f'ON `{table}`;')
    if req.auto_increment_start is not None:
        if not any(c['auto_increment'] for c in desired):
            raise _err(400, '没有 AUTO_INCREMENT 列，不能设置起始值',
                       'no_auto_increment')
        statements.append(f'ALTER TABLE `{table}` AUTO_INCREMENT = '
                          f'{int(req.auto_increment_start)};')
    return desired, statements, current


# 运行时连接注入（避免把 conn 传入纯函数；由端点设置）


@router.get('/databases/{db}/tables/{table}/design')
async def get_design(db: str, table: str,
                     connection: Optional[str] = None,
                     payload: dict = Depends(verify_token)):
    db = api._ident(db, '数据库名')
    table = api._ident(table, '表名')
    conn = _conn_for(db, connection, payload)
    return _read_design(conn, table)


@router.post('/databases/{db}/tables/{table}/design')
async def save_design(db: str, table: str, req: DesignRequest,
                      connection: Optional[str] = None,
                      payload: dict = Depends(require_permission('ddl'))):
    db = api._ident(db, '数据库名')
    table = api._ident(table, '表名')
    conn = _conn_for(db, connection, payload)
    _desired, statements, _current = _build_alter(conn, table, req)
    if req.dry_run:
        return {'ok': True, 'dry_run': True, 'statements': statements,
                'count': len(statements)}
    executed = []
    for i, sql in enumerate(statements):
        try:
            _n, err = conn.execute(sql)
        except Exception as exc:                  # noqa: BLE001
            err = str(exc)
        if err:
            raise _err(400, f'第 {i + 1}/{len(statements)} 条 ALTER 失败: {err}',
                       'design_apply_failed', sql=sql,
                       executed=executed, failed_index=i)
        executed.append(sql)
    return {'ok': True, 'dry_run': False, 'statements': statements,
            'executed': executed, 'count': len(statements)}


# ==================== 导入 / 导出 ====================

def _csv_text(columns, rows) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(list(columns))
    for row in rows or []:
        writer.writerow(['' if v is None else v for v in row])
    return '\ufeff' + buf.getvalue()


def _sql_literal(value) -> str:
    if value is None:
        return 'NULL'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    return "'" + str(value).replace('\\', '\\\\').replace("'", "''") + "'"


def _attachment(content: str, filename: str, media_type: str):
    return Response(
        content=content.encode('utf-8'),
        media_type=media_type,
        headers={'Content-Disposition':
                 f"attachment; filename*=UTF-8''{filename}"})


@router.get('/databases/{db}/tables/{table}/export')
async def export_table(db: str, table: str, fmt: str = Query('csv'),
                       limit: int = Query(10000, ge=1, le=100000),
                       connection: Optional[str] = None,
                       payload: dict = Depends(verify_token)):
    db = api._ident(db, '数据库名')
    table = api._ident(table, '表名')
    fmt = (fmt or 'csv').lower()
    if fmt not in ('csv', 'sql'):
        raise _err(400, "format 仅支持 csv/sql", 'invalid_format')
    conn = _conn_for(db, connection, payload)
    try:
        cols, rows = conn.query(f'SELECT * FROM `{table}` LIMIT {limit}')
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'导出查询失败: {exc}', 'export_failed')
    if fmt == 'csv':
        return _attachment(_csv_text(cols, rows),
                          f'{table}.csv', 'text/csv; charset=utf-8')
    try:
        _c, create_rows = conn.query(f'SHOW CREATE TABLE `{table}`')
        create_sql = str(create_rows[0][1]) if create_rows else ''
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'导出 SHOW CREATE 失败: {exc}', 'export_failed')
    lines = [f'-- LightDB Web export table `{table}`',
             'SET NAMES utf8mb4;', create_sql + ';']
    cols_sql = ', '.join(f'`{c}`' for c in cols)
    for row in rows or []:
        vals = ', '.join(_sql_literal(v) for v in row)
        lines.append(f'INSERT INTO `{table}` ({cols_sql}) VALUES ({vals});')
    return _attachment('\n'.join(lines) + '\n',
                       f'{table}.sql', 'application/sql; charset=utf-8')


@router.post('/databases/{db}/tables/{table}/import')
async def import_table(db: str, table: str, req: CsvImportRequest,
                       connection: Optional[str] = None,
                       payload: dict = Depends(require_permission('write'))):
    db = api._ident(db, '数据库名')
    table = api._ident(table, '表名')
    if not req.csv or not req.csv.strip():
        raise _err(400, 'CSV 内容为空', 'empty_import')
    conn = _conn_for(db, connection, payload)
    try:
        raw_cols = conn.get_columns(table) or []
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'读取列失败: {exc}', 'import_read_failed')
    table_cols = [str(c.get('name')) for c in raw_cols]
    if not table_cols:
        raise _err(404, f'表不存在: {table}', 'table_not_found')
    try:
        reader = csv.reader(io.StringIO(req.csv))
        rows = [r for r in reader if any(str(x).strip() for x in r)]
    except csv.Error as exc:
        raise _err(400, f'CSV 解析失败: {exc}', 'invalid_csv')
    if not rows:
        raise _err(400, 'CSV 没有数据行', 'empty_import')
    if req.has_header:
        header = [str(x).strip() for x in rows[0]]
        for name in header:
            if name not in table_cols:
                raise _err(400, f'CSV 表头列不存在: {name}', 'invalid_csv_header')
        data_rows = rows[1:]
        insert_cols = header
    else:
        data_rows = rows
        insert_cols = table_cols[:len(data_rows[0])]
    if not insert_cols or len(insert_cols) != len(data_rows[0]):
        raise _err(400, 'CSV 列数与表头不一致', 'invalid_csv')
    placeholders = ', '.join(['%s'] * len(insert_cols))
    cols_sql = ', '.join(f'`{api._ident(c, "列名")}`' for c in insert_cols)
    sql = f'INSERT INTO `{table}` ({cols_sql}) VALUES ({placeholders})'
    inserted, errors = 0, []
    for i, row in enumerate(data_rows, 1):
        if len(row) != len(insert_cols):
            errors.append({'row': i, 'error': '列数不一致'})
            continue
        try:
            n, err = conn.execute(sql, tuple(row))
        except Exception as exc:                  # noqa: BLE001
            n, err = 0, str(exc)
        if err:
            errors.append({'row': i, 'error': str(err)})
        else:
            inserted += int(n or 0) if n else 1
    return {'ok': not errors, 'inserted': inserted,
            'failed': len(errors), 'errors': errors[:20]}


@router.post('/query/export')
async def export_query(req: QueryExportRequest,
                       payload: dict = Depends(require_permission('query'))):
    fmt = (req.format or 'csv').lower()
    if fmt not in ('csv', 'sql'):
        raise _err(400, 'format 仅支持 csv/sql', 'invalid_format')
    up = (req.sql or '').strip().upper()
    if not up.startswith(('SELECT', 'SHOW', 'DESC', 'EXPLAIN', 'WITH')):
        raise _err(400, '仅允许导出查询结果（SELECT/SHOW/DESC/EXPLAIN/WITH）',
                   'unsupported_export_sql')
    if req.database:
        req.database = api._ident(req.database, '数据库名')
    conn = api._get_conn(payload['sub'], req.connection)
    if req.database:
        conn.switch_database(req.database)
    try:
        cols, rows = conn.query(req.sql)
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'查询失败: {exc}', 'query_export_failed')
    if fmt == 'csv':
        return _attachment(_csv_text(cols, rows), 'query_result.csv',
                          'text/csv; charset=utf-8')
    lines = ['-- LightDB Web query export', 'SET NAMES utf8mb4;']
    for row in rows or []:
        vals = ', '.join(_sql_literal(v) for v in row)
        lines.append(f'-- row: ({vals})')
    return _attachment('\n'.join(lines) + '\n', 'query_result.sql',
                       'application/sql; charset=utf-8')


# ==================== EXPLAIN / 备份 / 结构对比 ====================

@router.post('/explain')
async def explain_query(req: ExplainRequest,
                        payload: dict = Depends(require_permission('query'))):
    up = (req.sql or '').strip().upper()
    if not up.startswith(('SELECT', 'WITH')):
        raise _err(400, 'EXPLAIN 仅支持 SELECT/WITH 查询', 'invalid_explain_sql')
    if req.database:
        req.database = api._ident(req.database, '数据库名')
    conn = api._get_conn(payload['sub'], req.connection)
    if req.database:
        conn.switch_database(req.database)
    try:
        cols, rows = conn.query('EXPLAIN ' + req.sql)
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'EXPLAIN 失败: {exc}', 'explain_failed')
    return {'ok': True, 'columns': cols,
            'rows': [api._serialize_row(r) for r in rows],
            'row_count': len(rows or [])}


@router.post('/backup')
async def backup_database(req: BackupRequest,
                          payload: dict = Depends(require_permission('admin'))):
    if not req.database:
        raise _err(400, 'database 不能为空', 'missing_database')
    db = api._ident(req.database, '数据库名')
    conn = api._get_conn(payload['sub'], req.connection)
    try:
        conn.switch_database(db)
        tables = req.tables or conn.get_tables()
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'读取表失败: {exc}', 'backup_failed')
    tables = [api._ident(t, '表名') for t in (tables or [])]
    if not tables:
        raise _err(400, '数据库没有可备份的表', 'backup_empty')
    lines = [f'-- LightDB logical backup database `{db}`',
             f'-- generated by Web API at {time.strftime("%Y-%m-%d %H:%M:%S")}',
             'SET NAMES utf8mb4;']
    for table in tables:
        try:
            _c, create = conn.query(f'SHOW CREATE TABLE `{table}`')
            create_sql = str(create[0][1]) if create else ''
            cols, rows = conn.query(
                f'SELECT * FROM `{table}` LIMIT {int(req.limit_per_table)}')
        except Exception as exc:                  # noqa: BLE001
            raise _err(400, f'备份表 {table} 失败: {exc}', 'backup_failed')
        lines.append(create_sql + ';')
        cols_sql = ', '.join(f'`{c}`' for c in cols)
        for row in rows or []:
            vals = ', '.join(_sql_literal(v) for v in row)
            lines.append(f'INSERT INTO `{table}` ({cols_sql}) '
                         f'VALUES ({vals});')
    return _attachment('\n'.join(lines) + '\n', f'backup_{db}.sql',
                       'application/sql; charset=utf-8')


@router.get('/schema/compare')
async def schema_compare(source_db: str, target_db: str,
                         connection: Optional[str] = None,
                         payload: dict = Depends(verify_token)):
    source_db = api._ident(source_db, '源数据库名')
    target_db = api._ident(target_db, '目标数据库名')
    conn = api._get_conn(payload['sub'], connection)

    def tables_of(db):
        conn.switch_database(db)
        return [api._ident(t, '表名') for t in (conn.get_tables() or [])]

    try:
        source_tables = tables_of(source_db)
        target_tables = tables_of(target_db)
        source_set, target_set = set(source_tables), set(target_tables)
        differences = []
        for table in sorted(source_set & target_set):
            conn.switch_database(source_db)
            scols = {c['name']: _normalize_db_column(c)
                     for c in conn.get_columns(table)}
            conn.switch_database(target_db)
            tcols = {c['name']: _normalize_db_column(c)
                     for c in conn.get_columns(table)}
            changed = []
            for name in sorted(set(scols) | set(tcols)):
                if name not in tcols:
                    changed.append({'column': name, 'state': 'missing_in_target'})
                elif name not in scols:
                    changed.append({'column': name, 'state': 'missing_in_source'})
                elif (scols[name]['type'], scols[name]['length'],
                      scols[name]['notnull']) != (tcols[name]['type'],
                                                  tcols[name]['length'],
                                                  tcols[name]['notnull']):
                    changed.append({'column': name, 'state': 'changed',
                                    'source': scols[name],
                                    'target': tcols[name]})
            if changed:
                differences.append({'table': table, 'columns': changed})
        return {
            'ok': True,
            'source': {'database': source_db, 'tables': source_tables},
            'target': {'database': target_db, 'tables': target_tables},
            'summary': {
                'missing_in_target': sorted(source_set - target_set),
                'missing_in_source': sorted(target_set - source_set),
                'changed_tables': len(differences),
            },
            'differences': differences,
        }
    except HTTPException:
        raise
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'结构对比失败: {exc}', 'schema_compare_failed')


# ==================== task-63：ER 图 / 插件面板 / 流式导入导出 ====================

@router.get('/databases/{db}/er')
async def er_diagram(db: str,
                     connection: Optional[str] = None,
                     payload: dict = Depends(verify_token)):
    """基于 information_schema 的真实表/列/外键元数据生成 ER 图数据。"""
    db = api._ident(db, '数据库名')
    conn = _conn_for(db, connection, payload)
    notices = []
    table_source = 'information_schema'
    try:
        _cols, rows = conn.query(
            'SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES '
            'WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME', (db,))
        tables = [str(r[0]) for r in (rows or [])]
    except Exception as exc:                      # noqa: BLE001
        table_source = 'show_tables_fallback'
        notices.append(f'information_schema.TABLES 不可用，退回 SHOW TABLES: {exc}')
        try:
            tables = [api._ident(t, '表名') for t in (conn.get_tables() or [])]
        except Exception as exc2:                 # noqa: BLE001
            raise _err(400, f'读取表失败: {exc2}', 'er_tables_failed')
    nodes = []
    for table in tables:
        table = api._ident(table, '表名')
        columns = []
        try:
            _c, crows = conn.query(
                'SELECT COLUMN_NAME, DATA_TYPE, COLUMN_KEY '
                'FROM information_schema.COLUMNS '
                'WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s '
                'ORDER BY ORDINAL_POSITION', (db, table))
            for r in crows or []:
                columns.append({'name': str(r[0]),
                                'type': str(r[1]) if len(r) > 1 else '',
                                'key': str(r[2]) if len(r) > 2 else ''})
        except Exception:
            try:
                columns = [{'name': str(c.get('name')),
                            'type': str(c.get('type') or ''),
                            'key': str(c.get('key') or '')}
                           for c in (conn.get_columns(table) or [])]
            except Exception:
                columns = []
        nodes.append({'name': table, 'columns': columns})
    edges = []
    fk_supported = True
    try:
        _c, fkrows = conn.query(
            'SELECT TABLE_NAME, COLUMN_NAME, CONSTRAINT_NAME, '
            'REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME '
            'FROM information_schema.KEY_COLUMN_USAGE '
            'WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL',
            (db,))
        for r in fkrows or []:
            edges.append({
                'from': str(r[0]), 'column': str(r[1]),
                'constraint': str(r[2]) if len(r) > 2 else '',
                'to': str(r[3]), 'ref_column': str(r[4]) if len(r) > 4 else '',
            })
    except Exception as exc:                      # noqa: BLE001
        fk_supported = False
        notices.append(
            '当前引擎 information_schema 不提供外键元数据 '
            f'(KEY_COLUMN_USAGE): {str(exc).splitlines()[-1] if str(exc) else exc}')
    if not edges:
        notices.append('无外键关系（当前库没有可展示的 FOREIGN KEY 约束）')
    return {'ok': True, 'database': db, 'table_source': table_source,
            'nodes': nodes, 'edges': edges,
            'foreign_keys_supported': fk_supported,
            'notice': '；'.join(notices) if notices else ''}


def _plugin_manager():
    """惰性创建真实 PluginManager；插件加载失败在列表 error/status 中显式。"""
    from .server import app_state
    mgr = app_state.get('plugin_manager')
    if mgr is None:
        from lightdb_studio.core.plugin_manager import PluginManager
        from lightdb_studio.plugins import get_plugin_dir
        mgr = PluginManager(str(get_plugin_dir()),
                            app_context={'source': 'web'})
        mgr.load_all()
        app_state['plugin_manager'] = mgr
    return mgr


@router.get('/plugins')
async def list_plugins(payload: dict = Depends(verify_token)):
    """真实已加载插件（名称/版本/状态/错误）；不伪造插件。"""
    try:
        mgr = _plugin_manager()
        infos = mgr.get_all()
        plugins = []
        for info in infos:
            d = info.to_dict()
            d['loaded'] = bool(getattr(info, 'loaded', False))
            plugins.append(d)
        return {'ok': True, 'plugins': plugins, 'count': len(plugins),
                'warnings': list(getattr(mgr, 'warnings', [])[-5:]),
                'notice': '' if plugins else '插件目录为空：未发现已加载插件'}
    except Exception as exc:                      # noqa: BLE001
        raise _err(500, f'插件加载失败: {exc}', 'plugins_load_failed')


@router.get('/databases/{db}/tables/{table}/export/stream')
async def export_table_stream(
        db: str, table: str,
        limit: int = Query(100000, ge=1, le=1000000),
        chunk_size: int = Query(500, ge=1, le=5000),
        connection: Optional[str] = None,
        payload: dict = Depends(verify_token)):
    """task-63：分块流式 CSV 导出（不一次性拼大表到内存）。"""
    db = api._ident(db, '数据库名')
    table = api._ident(table, '表名')
    conn = _conn_for(db, connection, payload)
    try:
        cols = [str(c.get('name')) for c in (conn.get_columns(table) or [])]
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'读取列失败: {exc}', 'stream_export_failed')
    if not cols:
        raise _err(404, f'表不存在: {table}', 'table_not_found')

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        yield '\ufeff' + buf.getvalue()
        offset = 0
        exported = 0
        while exported < limit:
            n = min(chunk_size, limit - exported)
            try:
                _c, rows = conn.query(
                    f'SELECT * FROM `{table}` LIMIT {n} OFFSET {offset}')
            except Exception as exc:              # noqa: BLE001
                yield f'\n-- STREAM ERROR: {exc}\n'
                return
            if not rows:
                break
            out = io.StringIO()
            w = csv.writer(out)
            for row in rows:
                w.writerow(['' if v is None else v for v in row])
            yield out.getvalue()
            exported += len(rows)
            offset += len(rows)
            if len(rows) < n:
                break

    headers = {
        'Content-Disposition':
            f"attachment; filename*=UTF-8''{table}.csv",
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
        'X-Content-Type-Options': 'nosniff',
    }
    return StreamingResponse(generate(),
                             media_type='text/csv; charset=utf-8',
                             headers=headers)


_IMPORT_JOBS = {}
_IMPORT_LOCK = threading.Lock()


def _job_snapshot(job):
    with _IMPORT_LOCK:
        return {k: v for k, v in job.items()
                if k not in ('cancel_event', 'conn', 'sql')}


@router.post('/databases/{db}/tables/{table}/import/start')
async def start_import_job(
        db: str, table: str, req: CsvImportRequest,
        connection: Optional[str] = None,
        payload: dict = Depends(require_permission('write'))):
    """异步 CSV 导入任务：返回 job_id；轮询进度 / 取消。"""
    db = api._ident(db, '数据库名')
    table = api._ident(table, '表名')
    if not req.csv or not req.csv.strip():
        raise _err(400, 'CSV 内容为空', 'empty_import')
    conn = _conn_for(db, connection, payload)
    try:
        table_cols = [str(c.get('name'))
                      for c in (conn.get_columns(table) or [])]
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'读取列失败: {exc}', 'import_read_failed')
    if not table_cols:
        raise _err(404, f'表不存在: {table}', 'table_not_found')
    try:
        parsed = [r for r in csv.reader(io.StringIO(req.csv))
                  if any(str(x).strip() for x in r)]
    except csv.Error as exc:
        raise _err(400, f'CSV 解析失败: {exc}', 'invalid_csv')
    if not parsed:
        raise _err(400, 'CSV 没有数据', 'empty_import')
    if req.has_header:
        header = [str(x).strip() for x in parsed[0]]
        for name in header:
            if name not in table_cols:
                raise _err(400, f'CSV 表头列不存在: {name}',
                           'invalid_csv_header')
        data_rows = parsed[1:]
        insert_cols = header
    else:
        data_rows = parsed
        insert_cols = table_cols[:len(data_rows[0])]
    if not data_rows:
        raise _err(400, 'CSV 没有数据行', 'empty_import')
    cols_sql = ', '.join(f'`{api._ident(c, "列名")}`' for c in insert_cols)
    placeholders = ', '.join(['%s'] * len(insert_cols))
    sql = f'INSERT INTO `{table}` ({cols_sql}) VALUES ({placeholders})'
    job_id = uuid.uuid4().hex
    job = {'id': job_id, 'table': table, 'status': 'running',
           'total': len(data_rows), 'processed': 0, 'inserted': 0,
           'failed': 0, 'errors': [], 'cancel_event': threading.Event(),
           'conn': conn, 'sql': sql}
    with _IMPORT_LOCK:
        _IMPORT_JOBS[job_id] = job

    def worker():
        try:
            for i, row in enumerate(data_rows, 1):
                if job['cancel_event'].is_set():
                    job['status'] = 'cancelled'
                    return
                if len(row) != len(insert_cols):
                    job['failed'] += 1
                    job['errors'].append({'row': i, 'error': '列数不一致'})
                else:
                    try:
                        n, err = conn.execute(sql, tuple(row))
                    except Exception as exc:      # noqa: BLE001
                        n, err = 0, str(exc)
                    if err:
                        job['failed'] += 1
                        if len(job['errors']) < 50:
                            job['errors'].append({'row': i,
                                                  'error': str(err)})
                    else:
                        job['inserted'] += int(n or 0) if n else 1
                job['processed'] = i
            job['status'] = 'completed_with_errors' if job['failed'] \
                else 'completed'
        except Exception as exc:                  # noqa: BLE001
            job['status'] = 'failed'
            job['errors'].append({'row': 0, 'error': str(exc)})
        finally:
            job['cancel_event'].set()

    threading.Thread(target=worker, daemon=True,
                     name='web-csv-import').start()
    return {'ok': True, 'job_id': job_id, 'status': 'running',
            'total': len(data_rows)}


@router.get('/import/jobs/{job_id}')
async def import_job_status(job_id: str,
                            payload: dict = Depends(verify_token)):
    with _IMPORT_LOCK:
        job = _IMPORT_JOBS.get(job_id)
    if job is None:
        raise _err(404, f'导入任务不存在: {job_id}', 'import_job_not_found')
    snap = _job_snapshot(job)
    snap['progress'] = (round(snap['processed'] / snap['total'] * 100, 1)
                        if snap.get('total') else 100.0)
    return {'ok': True, **snap}


@router.post('/import/jobs/{job_id}/cancel')
async def cancel_import_job(job_id: str,
                            payload: dict = Depends(
                                require_permission('write'))):
    with _IMPORT_LOCK:
        job = _IMPORT_JOBS.get(job_id)
    if job is None:
        raise _err(404, f'导入任务不存在: {job_id}', 'import_job_not_found')
    if job['status'] in ('completed', 'completed_with_errors',
                         'cancelled', 'failed'):
        return {'ok': False, 'status': job['status'],
                'error': '任务已结束，无法取消'}
    job['cancel_event'].set()
    job['status'] = 'cancelling'
    return {'ok': True, 'status': 'cancelling', 'job_id': job_id}


@router.post('/databases/{db}/tables/{table}/import/upload')
async def upload_csv_import(
        db: str, table: str,
        file: UploadFile = File(...),
        has_header: bool = Form(True),
        connection: Optional[str] = Form(None),
        payload: dict = Depends(require_permission('write'))):
    """task-69：multipart/form-data CSV 分块写临时文件，再复用导入 job。

    - 不在内存中拼整个文件；分块读取，超过 MAX_IMPORT_BYTES 显式 413；
    - 写完后由后台 job 流式逐行 INSERT，复用进度/取消/逐行错误语义；
    - 临时文件在任务结束或出错时删除。
    """
    db = api._ident(db, '数据库名')
    table = api._ident(table, '表名')
    conn = _conn_for(db, connection or None, payload)
    try:
        table_cols = [str(c.get('name'))
                      for c in (conn.get_columns(table) or [])]
    except Exception as exc:                      # noqa: BLE001
        raise _err(400, f'读取列失败: {exc}', 'import_read_failed')
    if not table_cols:
        raise _err(404, f'表不存在: {table}', 'table_not_found')
    tmp_path = None
    job = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='lightdb_upload_', suffix='.csv')
        total = 0
        with os.fdopen(fd, 'wb') as out:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_IMPORT_BYTES:
                    raise _err(
                        413, f'上传超过限制 {MAX_IMPORT_BYTES} 字节',
                        'import_too_large', max_bytes=MAX_IMPORT_BYTES)
                out.write(chunk)
        if total == 0:
            raise _err(400, '上传 CSV 为空', 'empty_import')
        # 预解析表头并流式计数（不把整文件载入内存）
        with open(tmp_path, 'r', encoding='utf-8-sig', newline='') as fh:
            reader = csv.reader(fh)
            first = next(reader, None)
            if not first:
                raise _err(400, 'CSV 没有数据', 'empty_import')
            if has_header:
                header = [str(x).strip() for x in first]
                for name in header:
                    if name not in table_cols:
                        raise _err(400, f'CSV 表头列不存在: {name}',
                                   'invalid_csv_header')
                insert_cols = header
            else:
                insert_cols = table_cols[:len(first)]
            data_count = 0
            try:
                fh.seek(0)
            except OSError:
                pass
            reader = csv.reader(fh)
            if has_header:
                next(reader, None)
            for row in reader:
                if row and any(str(x).strip() for x in row):
                    data_count += 1
        if data_count <= 0:
            raise _err(400, 'CSV 没有数据行', 'empty_import')
        cols_sql = ', '.join(f'`{api._ident(c, "列名")}`'
                             for c in insert_cols)
        placeholders = ', '.join(['%s'] * len(insert_cols))
        sql = f'INSERT INTO `{table}` ({cols_sql}) VALUES ({placeholders})'
        job_id = uuid.uuid4().hex
        job = {'id': job_id, 'table': table, 'status': 'running',
               'total': data_count, 'processed': 0, 'inserted': 0,
               'failed': 0, 'errors': [], 'cancel_event': threading.Event(),
               'conn': conn, 'sql': sql, 'file_path': tmp_path,
               'insert_cols': insert_cols, 'has_header': bool(has_header)}
        with _IMPORT_LOCK:
            _IMPORT_JOBS[job_id] = job

        def worker():
            try:
                with open(tmp_path, 'r', encoding='utf-8-sig',
                          newline='') as fh:
                    reader = csv.reader(fh)
                    if has_header:
                        next(reader, None)
                    index = 0
                    for row in reader:
                        if not row or not any(str(x).strip() for x in row):
                            continue
                        index += 1
                        if job['cancel_event'].is_set():
                            job['status'] = 'cancelled'
                            return
                        if len(row) != len(insert_cols):
                            job['failed'] += 1
                            job['errors'].append({'row': index,
                                                  'error': '列数不一致'})
                        else:
                            try:
                                n, err = conn.execute(sql, tuple(row))
                            except Exception as exc:  # noqa: BLE001
                                n, err = 0, str(exc)
                            if err:
                                job['failed'] += 1
                                if len(job['errors']) < 50:
                                    job['errors'].append(
                                        {'row': index, 'error': str(err)})
                            else:
                                job['inserted'] += int(n or 0) if n else 1
                        job['processed'] = index
                job['status'] = 'completed_with_errors' if job['failed'] \
                    else 'completed'
            except Exception as exc:              # noqa: BLE001
                job['status'] = 'failed'
                job['errors'].append({'row': 0, 'error': str(exc)})
            finally:
                job['cancel_event'].set()
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                job['file_path'] = ''

        threading.Thread(target=worker, daemon=True,
                         name='web-csv-upload-import').start()
        return {'ok': True, 'job_id': job_id, 'status': 'running',
                'total': data_count, 'bytes': total,
                'upload_mode': 'multipart_stream'}
    except HTTPException:
        if job is None and tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise
    except Exception as exc:                      # noqa: BLE001
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise _err(400, f'上传导入失败: {exc}', 'import_upload_failed')
