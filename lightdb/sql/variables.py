"""
会话变量（@@var）和用户变量（@var）
"""
import threading
from typing import Any, Dict, Optional


# 系统会话变量的默认值
DEFAULT_SESSION_VARS = {
    # 字符集
    'character_set_client': 'utf8mb4',
    'character_set_connection': 'utf8mb4',
    'character_set_results': 'utf8mb4',
    'character_set_database': 'utf8mb4',
    'character_set_server': 'utf8mb4',
    'collation_connection': 'utf8mb4_general_ci',
    'collation_database': 'utf8mb4_general_ci',
    'collation_server': 'utf8mb4_general_ci',

    # 事务
    'autocommit': 1,
    'transaction_isolation': 'READ-COMMITTED',
    'tx_isolation': 'READ-COMMITTED',
    'transaction_read_only': 0,

    # SQL 模式
    'sql_mode': 'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES',
    'sql_safe_updates': 0,

    # 时区
    'time_zone': 'SYSTEM',
    'system_time_zone': 'CST',

    # 系统信息（只读）
    'version': '5.7.30-LightDB',
    'version_comment': 'LightDB Enterprise Server',
    'version_compile_machine': 'x86_64',
    'version_compile_os': 'Linux',
    'protocol_version': 10,

    # 连接信息
    'max_allowed_packet': 67108864,
    'net_buffer_length': 16384,
    'wait_timeout': 28800,
    'interactive_timeout': 28800,

    # 优化相关
    'sql_select_limit': 18446744073709551615,
    'sql_big_selects': 1,

    # 性能
    'innodb_buffer_pool_size': 134217728,
    'innodb_page_size': 16384,
    'have_query_cache': 'NO',
    'query_cache_size': 0,
    'query_cache_type': 'OFF',
}


class SessionVariables:
    """会话变量（每个会话独立）"""

    def __init__(self):
        self.session: Dict[str, Any] = dict(DEFAULT_SESSION_VARS)
        self.user_vars: Dict[str, Any] = {}    # @var
        # 事务相关
        self.last_insert_id = 0
        self.last_found_rows = 0
        self.row_count = 0

    # ==================== 会话变量 @@var ====================

    def get_session(self, name: str) -> Any:
        return self.session.get(name.lower())

    def set_session(self, name: str, value: Any):
        self.session[name.lower()] = value

    def get_session_all(self) -> Dict[str, Any]:
        return dict(self.session)

    def is_readonly(self, name: str) -> bool:
        """是否只读"""
        readonly = {'version', 'version_comment',
                    'version_compile_machine', 'version_compile_os',
                    'protocol_version', 'system_time_zone',
                    'have_query_cache'}
        return name.lower() in readonly

    # ==================== 用户变量 @var ====================

    def get_user(self, name: str) -> Any:
        # 去掉 @ 前缀
        if name.startswith('@'):
            name = name[1:]
        return self.user_vars.get(name)

    def set_user(self, name: str, value: Any):
        if name.startswith('@'):
            name = name[1:]
        self.user_vars[name] = value

    def clear_user(self):
        self.user_vars.clear()

    # ==================== 特殊变量 ====================

    def get_special(self, name: str) -> Any:
        """@@IDENTITY / @@ROW_COUNT / @@LAST_INSERT_ID"""
        n = name.lower().replace('@', '')
        if n == 'identity':
            return self.last_insert_id
        if n == 'row_count':
            return self.row_count
        if n == 'last_insert_id':
            return self.last_insert_id
        if n == 'found_rows':
            return self.last_found_rows
        return None

    def set_last_insert_id(self, v: int):
        self.last_insert_id = v

    def set_row_count(self, v: int):
        self.row_count = v

    def set_found_rows(self, v: int):
        self.last_found_rows = v


class VariableManager:
    """变量管理器（管理所有会话）"""

    def __init__(self):
        self._sessions: Dict[int, SessionVariables] = {}
        self._lock = threading.RLock()

    def get_session(self, session_id: int) -> SessionVariables:
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionVariables()
            return self._sessions[session_id]

    def clear_session(self, session_id: int):
        with self._lock:
            self._sessions.pop(session_id, None)

    def stats(self) -> dict:
        return {'active_sessions': len(self._sessions)}


# 全局变量管理器
_vm = VariableManager()


def get_vm() -> VariableManager:
    return _vm