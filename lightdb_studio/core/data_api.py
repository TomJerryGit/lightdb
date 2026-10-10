"""
数据服务 API
- 表一键发布为 REST API
- 参数化查询
- API 密钥管理
- 限流
"""
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional, Callable


API_FILE = Path.home() / '.lightdb_studio' / 'data_apis.json'


@dataclass
class APIKey:
    key: str = ""
    label: str = ""
    enabled: bool = True
    rate_limit: int = 60        # 每分钟
    created_at: float = 0

    def __post_init__(self):
        if not self.key:
            self.key = "ldb_" + secrets.token_urlsafe(24)
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class DataAPI:
    """数据服务 API"""
    name: str = ""
    description: str = ""
    # 数据源
    source_type: str = "table"   # table / sql
    connection: str = ""
    table: str = ""
    sql: str = ""
    # 接口类型
    method: str = "GET"          # GET / POST
    path: str = ""               # /api/data/users
    # 参数
    query_params: List[Dict] = field(default_factory=list)
    # [{'name': 'min_age', 'type': 'int', 'where': 'age >= {value}'}]
    # 返回
    columns: List[str] = field(default_factory=list)
    max_rows: int = 1000
    # 认证
    require_key: bool = True
    allowed_keys: List[str] = field(default_factory=list)
    # 状态
    enabled: bool = True
    call_count: int = 0
    last_called: float = 0
    created_at: float = 0
    updated_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        if not self.path:
            self.path = f"/api/data/{self.name}"


class DataAPIManager:
    """数据 API 管理器"""

    def __init__(self, conn_provider: Callable = None):
        self.file = API_FILE
        self.conn_provider = conn_provider
        self.apis: Dict[str, DataAPI] = {}
        self.keys: Dict[str, APIKey] = {}
        # 调用记录（内存）
        self.call_log: List[Dict] = []
        self.rate_counters: Dict[str, List[float]] = {}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()
        self._ensure_default_key()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for name, ad in data.get('apis', {}).items():
                self.apis[name] = DataAPI(**ad)
            for k, kd in data.get('keys', {}).items():
                self.keys[k] = APIKey(**kd)
        except Exception:
            pass

    def _ensure_default_key(self):
        if not self.keys:
            k = APIKey(label='默认密钥')
            self.keys[k.key] = k
            self.save()

    def save(self):
        data = {
            'apis': {n: asdict(a) for n, a in self.apis.items()},
            'keys': {k: asdict(v) for k, v in self.keys.items()},
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 发布 API ====================

    def publish_table(self, name: str, connection: str, table: str,
                      description: str = "",
                      query_params: List[Dict] = None,
                      max_rows: int = 1000) -> DataAPI:
        """从表发布 API"""
        api = DataAPI(
            name=name,
            description=description,
            source_type='table',
            connection=connection,
            table=table,
            query_params=query_params or [],
            max_rows=max_rows,
        )

        # 获取列
        try:
            conn = self.conn_provider(connection) \
                if self.conn_provider else None
            if conn:
                cols = conn.get_columns(table)
                api.columns = [c['name'] for c in cols]
        except Exception:
            pass

        self.apis[name] = api
        self.save()
        return api

    def publish_sql(self, name: str, connection: str, sql: str,
                    description: str = "",
                    query_params: List[Dict] = None,
                    max_rows: int = 1000) -> DataAPI:
        api = DataAPI(
            name=name,
            description=description,
            source_type='sql',
            connection=connection,
            sql=sql,
            query_params=query_params or [],
            max_rows=max_rows,
        )
        self.apis[name] = api
        self.save()
        return api

    def remove_api(self, name: str):
        if name in self.apis:
            del self.apis[name]
            self.save()

    def get_api(self, name: str) -> Optional[DataAPI]:
        return self.apis.get(name)

    def list_apis(self) -> List[DataAPI]:
        return list(self.apis.values())

    def find_by_path(self, path: str) -> Optional[DataAPI]:
        for api in self.apis.values():
            if api.path == path:
                return api
        return None

    # ==================== API Key ====================

    def create_key(self, label: str = "",
                   rate_limit: int = 60) -> APIKey:
        k = APIKey(label=label or "新密钥", rate_limit=rate_limit)
        self.keys[k.key] = k
        self.save()
        return k

    def revoke_key(self, key: str):
        if key in self.keys:
            self.keys[key].enabled = False
            self.save()

    def delete_key(self, key: str):
        if key in self.keys:
            del self.keys[key]
            self.save()

    # ==================== 调用 ====================

    def call(self, name: str, params: Dict = None,
             api_key: str = None) -> Dict:
        """
        调用 API
        params: URL 查询参数
        api_key: 访问密钥
        """
        api = self.apis.get(name)
        if not api:
            return {'ok': False, 'error': 'API 不存在', 'code': 404}
        if not api.enabled:
            return {'ok': False, 'error': 'API 已禁用', 'code': 403}

        # 认证
        if api.require_key:
            if not api_key:
                return {'ok': False, 'error': '缺少 API Key',
                        'code': 401}
            key_obj = self.keys.get(api_key)
            if not key_obj or not key_obj.enabled:
                return {'ok': False, 'error': 'API Key 无效',
                        'code': 401}

            # 限流
            if not self._check_rate_limit(api_key, key_obj.rate_limit):
                return {'ok': False, 'error': '请求过于频繁',
                        'code': 429}

            # 白名单
            if api.allowed_keys and api_key not in api.allowed_keys:
                return {'ok': False, 'error': 'API Key 未授权',
                        'code': 403}

        # 构建 SQL
        where_parts = []
        params = params or {}

        for qp in api.query_params:
            pname = qp.get('name')
            if pname not in params:
                continue
            val = params[pname]
            ptype = qp.get('type', 'string')

            # 类型转换
            try:
                if ptype == 'int':
                    val = int(val)
                elif ptype == 'float':
                    val = float(val)
                elif ptype == 'bool':
                    val = str(val).lower() in ('true', '1', 'yes')
            except (ValueError, TypeError):
                continue

            where_expr = qp.get('where', '')
            if not where_expr:
                continue

            # 替换占位
            if isinstance(val, str):
                val_repr = "'" + val.replace("'", "''") + "'"
            elif isinstance(val, bool):
                val_repr = '1' if val else '0'
            else:
                val_repr = str(val)

            where_parts.append(
                where_expr.replace('{value}', val_repr))

        # 执行
        conn = self.conn_provider(api.connection) \
            if self.conn_provider else None
        if not conn:
            return {'ok': False, 'error': '连接不可用', 'code': 500}

        t0 = time.time()
        try:
            if api.source_type == 'table':
                sql = f"SELECT * FROM `{api.table}`"
                if where_parts:
                    sql += " WHERE " + ' AND '.join(where_parts)
                sql += f" LIMIT {api.max_rows}"

            elif api.source_type == 'sql':
                base = api.sql.rstrip(';')
                if where_parts:
                    # 简单包装
                    sql = f"SELECT * FROM ({base}) AS t " \
                          f"WHERE {' AND '.join(where_parts)} " \
                          f"LIMIT {api.max_rows}"
                else:
                    sql = f"SELECT * FROM ({base}) AS t " \
                          f"LIMIT {api.max_rows}"

            else:
                return {'ok': False, 'error': '未知源类型', 'code': 500}

            cols, rows = conn.query(sql)

        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            api.call_count += 1
            api.last_called = time.time()
            self._log_call(name, api_key, 500, elapsed,
                            str(e), len(where_parts))
            return {'ok': False, 'error': str(e), 'code': 500}

        elapsed = (time.time() - t0) * 1000

        # 序列化
        safe_rows = []
        for r in rows:
            safe = []
            for v in r:
                if v is None or isinstance(v, (int, float, str, bool)):
                    safe.append(v)
                elif isinstance(v, bytes):
                    safe.append(f"<binary {len(v)}B>")
                else:
                    safe.append(str(v))
            safe_rows.append(safe)

        api.call_count += 1
        api.last_called = time.time()
        self.save()
        self._log_call(name, api_key, 200, elapsed, '', len(where_parts))

        return {
            'ok': True,
            'columns': cols,
            'rows': safe_rows,
            'count': len(safe_rows),
            'elapsed_ms': elapsed,
            'code': 200,
        }

    def _check_rate_limit(self, key: str, limit: int) -> bool:
        now = time.time()
        window = now - 60
        if key not in self.rate_counters:
            self.rate_counters[key] = []
        # 清理窗口外
        self.rate_counters[key] = [
            t for t in self.rate_counters[key] if t > window]
        if len(self.rate_counters[key]) >= limit:
            return False
        self.rate_counters[key].append(now)
        return True

    def _log_call(self, api_name: str, key: str, code: int,
                  elapsed: float, error: str, param_count: int):
        self.call_log.append({
            'api': api_name,
            'key': (key or '')[:12] + '...' if key else '',
            'code': code,
            'elapsed': elapsed,
            'error': error,
            'params': param_count,
            'ts': time.time(),
        })
        if len(self.call_log) > 1000:
            self.call_log = self.call_log[-500:]

    def get_recent_calls(self, limit: int = 50) -> List[Dict]:
        return list(reversed(self.call_log[-limit:]))

    def stats(self) -> dict:
        return {
            'total_apis': len(self.apis),
            'enabled_apis': sum(1 for a in self.apis.values() if a.enabled),
            'total_keys': len(self.keys),
            'active_keys': sum(1 for k in self.keys.values() if k.enabled),
            'total_calls': sum(a.call_count for a in self.apis.values()),
        }