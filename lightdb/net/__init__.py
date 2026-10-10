"""
LightDB 网络层

包含:
- MySQL Wire Protocol v10 实现
- ThreadingTCPServer 兼容服务器
- SO_REUSEPORT / Master 监听器
- 多进程 Master / Worker
"""

from .mysql_protocol import (
    # 常量
    CLIENT_LONG_PASSWORD,
    CLIENT_PROTOCOL_41,
    CLIENT_SECURE_CONNECTION,
    CLIENT_PLUGIN_AUTH,
    SERVER_CAPABILITIES,
    # 命令
    COM_QUIT,
    COM_INIT_DB,
    COM_QUERY,
    COM_FIELD_LIST,
    COM_PING,
    COM_STMT_PREPARE,
    COM_STMT_EXECUTE,
    COM_STMT_CLOSE,
    # 列类型
    MYSQL_TYPE_DECIMAL,
    MYSQL_TYPE_LONG,
    MYSQL_TYPE_LONGLONG,
    MYSQL_TYPE_VAR_STRING,
    MYSQL_TYPE_STRING,
    # 状态
    SERVER_STATUS_AUTOCOMMIT,
    SERVER_STATUS_IN_TRANS,
    CHARSET_UTF8MB4,
    # 工具函数
    write_lenenc_int,
    read_lenenc_int,
    write_lenenc_str,
    read_lenenc_str,
    write_null_str,
    build_packet,
    make_handshake_v10,
    make_ok_packet,
    make_err_packet,
    make_eof_packet,
    make_column_definition,
    make_text_row,
    HandshakeResponse,
    mysql_native_password,
    hash_password,
)

from .server import (
    MySQLServer,
    MySQLHandler,
)

from .listener import (
    ReusePortListener,
    MasterListener,
    create_listener,
    HAS_REUSEPORT,
)

__all__ = [
    # mysql_protocol
    'CLIENT_LONG_PASSWORD', 'CLIENT_PROTOCOL_41',
    'CLIENT_SECURE_CONNECTION', 'CLIENT_PLUGIN_AUTH',
    'SERVER_CAPABILITIES',
    'COM_QUIT', 'COM_INIT_DB', 'COM_QUERY', 'COM_FIELD_LIST',
    'COM_PING', 'COM_STMT_PREPARE', 'COM_STMT_EXECUTE', 'COM_STMT_CLOSE',
    'MYSQL_TYPE_DECIMAL', 'MYSQL_TYPE_LONG', 'MYSQL_TYPE_LONGLONG',
    'MYSQL_TYPE_VAR_STRING', 'MYSQL_TYPE_STRING',
    'SERVER_STATUS_AUTOCOMMIT', 'SERVER_STATUS_IN_TRANS',
    'CHARSET_UTF8MB4',
    'write_lenenc_int', 'read_lenenc_int',
    'write_lenenc_str', 'read_lenenc_str', 'write_null_str',
    'build_packet',
    'make_handshake_v10', 'make_ok_packet', 'make_err_packet',
    'make_eof_packet', 'make_column_definition', 'make_text_row',
    'HandshakeResponse', 'mysql_native_password', 'hash_password',

    # server
    'MySQLServer', 'MySQLHandler',

    # listener
    'ReusePortListener', 'MasterListener', 'create_listener',
    'HAS_REUSEPORT',
]