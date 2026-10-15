"""
MySQL Wire Protocol v10 实现
参考: https://dev.mysql.com/doc/internals/en/client-server-protocol.html
"""
import struct
import os

# ========== 客户端能力标志 ==========
CLIENT_LONG_PASSWORD     = 0x00000001
CLIENT_FOUND_ROWS        = 0x00000002
CLIENT_LONG_FLAG         = 0x00000004
CLIENT_CONNECT_WITH_DB   = 0x00000008
CLIENT_NO_SCHEMA         = 0x00000010
CLIENT_COMPRESS          = 0x00000020
CLIENT_ODBC              = 0x00000040
CLIENT_LOCAL_FILES       = 0x00000080
CLIENT_IGNORE_SPACE      = 0x00000100
CLIENT_PROTOCOL_41       = 0x00000200
CLIENT_INTERACTIVE       = 0x00000400
CLIENT_SSL               = 0x00000800
CLIENT_TRANSACTIONS      = 0x00002000
CLIENT_SECURE_CONNECTION = 0x00008000
CLIENT_MULTI_STATEMENTS  = 0x00010000
CLIENT_MULTI_RESULTS     = 0x00020000
CLIENT_PS_MULTI_RESULTS  = 0x00040000
CLIENT_PLUGIN_AUTH       = 0x00080000
CLIENT_CONNECT_ATTRS     = 0x00100000

# 服务端声明支持的能力
#
# ★ `CLIENT_CONNECT_WITH_DB` 原实现**漏了**：客户端（pymysql 2.2.8 实测）
# 只有在服务端握手包声明了该位时才会把 `database=` 写进握手响应
# （见 pymysql `_request_authentication`：
# `if self.db and self.server_capabilities & CLIENT.CONNECT_WITH_DB`）。
# 漏掉这一位造成的连锁后果是：
#   1. 客户端**不发送** database 字段，于是服务端按字段顺序读到的
#      "库名"其实是紧随其后的**认证插件名** ——
#      `SELECT DATABASE()` 返回 `'mysql_native_password'`，
#      `SHOW PROCESSLIST` 的 db 列同样错（这就是那个"明显错值"的根因）；
#   2. 登录时指定一个不存在的库也不会报错（服务端根本没收到库名）。
# 这里补上；`_resolve_database` 再把收到的库名**校验**一遍（不存在 → 1049）。
SERVER_CAPABILITIES = (
    CLIENT_LONG_PASSWORD
    | CLIENT_FOUND_ROWS
    | CLIENT_LONG_FLAG
    | CLIENT_CONNECT_WITH_DB
    | CLIENT_PROTOCOL_41
    | CLIENT_TRANSACTIONS
    | CLIENT_SECURE_CONNECTION
    | CLIENT_MULTI_STATEMENTS
    | CLIENT_MULTI_RESULTS
    | CLIENT_PLUGIN_AUTH
)

# ========== 命令 ==========
COM_QUIT = 0x01
COM_INIT_DB = 0x02
COM_QUERY = 0x03
COM_FIELD_LIST = 0x04
COM_PING = 0x0e
COM_STMT_PREPARE = 0x16
COM_STMT_EXECUTE = 0x17
COM_STMT_CLOSE = 0x19

# ========== 列类型 ==========
MYSQL_TYPE_DECIMAL     = 0x00
MYSQL_TYPE_TINY        = 0x01
MYSQL_TYPE_SHORT       = 0x02
MYSQL_TYPE_LONG        = 0x03
MYSQL_TYPE_FLOAT       = 0x04
MYSQL_TYPE_DOUBLE      = 0x05
MYSQL_TYPE_NULL        = 0x06
MYSQL_TYPE_TIMESTAMP   = 0x07
MYSQL_TYPE_LONGLONG    = 0x08
MYSQL_TYPE_INT24       = 0x09
MYSQL_TYPE_DATE        = 0x0a
MYSQL_TYPE_TIME        = 0x0b
MYSQL_TYPE_DATETIME    = 0x0c
MYSQL_TYPE_YEAR        = 0x0d
MYSQL_TYPE_VARCHAR     = 0x0f
MYSQL_TYPE_BIT         = 0x10
MYSQL_TYPE_NEWDECIMAL  = 0xf6
MYSQL_TYPE_BLOB        = 0xfc
MYSQL_TYPE_VAR_STRING  = 0xfd
MYSQL_TYPE_STRING      = 0xfe
MYSQL_TYPE_JSON        = 0xf5

# 状态标志
SERVER_STATUS_IN_TRANS   = 0x0001
SERVER_STATUS_AUTOCOMMIT = 0x0002

# 字符集
CHARSET_UTF8MB4 = 45


# ========== 长度编码整数 ==========
def write_lenenc_int(n: int) -> bytes:
    if n < 251:
        return bytes([n])
    elif n < 2**16:
        return b'\xfc' + struct.pack('<H', n)
    elif n < 2**24:
        return b'\xfd' + struct.pack('<I', n)[:3]
    else:
        return b'\xfe' + struct.pack('<Q', n)


def read_lenenc_int(data: bytes, offset: int = 0):
    if offset >= len(data):
        return 0, offset
    first = data[offset]
    if first < 251:
        return first, offset + 1
    elif first == 0xfc:
        return struct.unpack_from('<H', data, offset + 1)[0], offset + 3
    elif first == 0xfd:
        v = struct.unpack_from('<I', data, offset + 1)[0] & 0xffffff
        return v, offset + 4
    elif first == 0xfe:
        return struct.unpack_from('<Q', data, offset + 1)[0], offset + 9
    elif first == 0xfb:
        return None, offset + 1
    return 0, offset + 1


def write_lenenc_str(s) -> bytes:
    if s is None:
        return b'\xfb'
    if isinstance(s, str):
        s = s.encode('utf-8')
    return write_lenenc_int(len(s)) + s


def read_lenenc_str(data: bytes, offset: int = 0):
    n, offset = read_lenenc_int(data, offset)
    if n is None:
        return None, offset
    s = data[offset:offset + n]
    return s.decode('utf-8', errors='replace'), offset + n


def write_null_str(s: str) -> bytes:
    if s is None:
        return b'\x00'
    if isinstance(s, str):
        s = s.encode('utf-8')
    return s + b'\x00'


# ========== 包构建 ==========
def build_packet(payload: bytes, seq_id: int = 0) -> bytes:
    """MySQL包: [3B长度][1B序号][payload]"""
    length = len(payload)
    header = struct.pack('<I', length)[:3] + bytes([seq_id & 0xff])
    return header + payload


# ========== 服务端 -> 客户端 包 ==========

def make_handshake_v10(connection_id: int, auth_seed: bytes) -> bytes:
    """
    HandshakeV10 包
    auth_seed 必须是 20 字节
    """
    assert len(auth_seed) == 20
    payload = b''
    payload += b'\x0a'                          # protocol version = 10
    payload += b'5.7.30-LightDB\x00'            # server version
    payload += struct.pack('<I', connection_id) # connection id
    payload += auth_seed[:8]                    # auth-plugin-data-part-1
    payload += b'\x00'                          # filler
    payload += struct.pack('<H', SERVER_CAPABILITIES & 0xffff)  # capability_flags_1
    payload += bytes([CHARSET_UTF8MB4])         # character_set
    payload += struct.pack('<H', SERVER_STATUS_AUTOCOMMIT)      # status_flags
    payload += struct.pack('<H', (SERVER_CAPABILITIES >> 16) & 0xffff)  # capability_flags_2
    payload += bytes([21])                      # auth_plugin_data_len
    payload += b'\x00' * 10                     # reserved
    payload += auth_seed[8:]                    # auth-plugin-data-part-2 (12 bytes)
    payload += b'\x00'                          # 结束符
    payload += b'mysql_native_password\x00'     # auth plugin name
    return payload


def make_ok_packet(affected_rows: int = 0, last_insert_id: int = 0,
                   status_flags: int = SERVER_STATUS_AUTOCOMMIT,
                   message: str = "") -> bytes:
    """OK包"""
    payload = b'\x00'  # OK header
    payload += write_lenenc_int(affected_rows)
    payload += write_lenenc_int(last_insert_id)
    payload += struct.pack('<H', status_flags)
    payload += struct.pack('<H', 0)  # warnings
    if message:
        payload += message.encode('utf-8')
    return payload


def make_err_packet(code: int = 1064, sql_state: str = "42000", message: str = "Error") -> bytes:
    """ERR包"""
    payload = b'\xff'
    payload += struct.pack('<H', code)
    payload += b'#' + sql_state.encode('ascii')[:5]
    payload += message.encode('utf-8')
    return payload


def make_eof_packet(status_flags: int = SERVER_STATUS_AUTOCOMMIT) -> bytes:
    """EOF包"""
    payload = b'\xfe'
    payload += struct.pack('<H', 0)  # warnings
    payload += struct.pack('<H', status_flags)
    return payload


def make_column_definition(name: str, col_type: int, length: int = 255,
                           flags: int = 0, decimals: int = 0) -> bytes:
    """列定义（结果集用）"""
    payload = b''
    payload += write_lenenc_str(b'def')         # catalog
    payload += write_lenenc_str(b'lightdb')     # schema
    payload += write_lenenc_str(b'')            # table (alias)
    payload += write_lenenc_str(b'')            # org_table
    payload += write_lenenc_str(name)           # name
    payload += write_lenenc_str(name)           # org_name
    payload += b'\x0c'                          # length of fixed fields
    payload += struct.pack('<H', CHARSET_UTF8MB4)
    payload += struct.pack('<I', length)
    payload += bytes([col_type])
    payload += struct.pack('<H', flags)
    payload += bytes([decimals])
    payload += b'\x00\x00'                       # filler
    return payload


def make_text_row(values: list) -> bytes:
    """文本协议行"""
    payload = b''
    for v in values:
        if v is None:
            payload += b'\xfb'
        else:
            payload += write_lenenc_str(str(v))
    return payload


# ========== 客户端 -> 服务端 包 ==========

class HandshakeResponse:
    """解析客户端握手响应"""
    def __init__(self, data: bytes):
        self.capabilities = 0
        self.max_packet_size = 0
        self.charset = 0
        self.username = ""
        self.auth_response = b""
        self.database = ""
        self.plugin_name = ""
        self._parse(data)
    
    def _parse(self, data: bytes):
        offset = 0
        # capability flags (4 bytes)
        self.capabilities = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        # max packet size (4 bytes)
        self.max_packet_size = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        # charset (1 byte)
        self.charset = data[offset]
        offset += 1
        # reserved (23 bytes)
        offset += 23
        # username (null-terminated)
        end = data.index(b'\x00', offset)
        self.username = data[offset:end].decode('utf-8', errors='replace')
        offset = end + 1
        # auth response (length-encoded)
        if self.capabilities & CLIENT_PLUGIN_AUTH:
            auth_len = data[offset]
            offset += 1
            self.auth_response = data[offset:offset + auth_len]
            offset += auth_len
        else:
            end = data.index(b'\x00', offset)
            self.auth_response = data[offset:end]
            offset = end + 1
        # database
        if self.capabilities & CLIENT_CONNECT_WITH_DB:
            end = data.index(b'\x00', offset)
            self.database = data[offset:end].decode('utf-8', errors='replace')
            offset = end + 1
        # plugin name
        if self.capabilities & CLIENT_PLUGIN_AUTH:
            if offset < len(data):
                end = data.index(b'\x00', offset) if b'\x00' in data[offset:] else len(data)
                self.plugin_name = data[offset:end].decode('utf-8', errors='replace')


def mysql_native_password(auth_response: bytes, auth_seed: bytes,
                          stored_hash: bytes) -> bool:
    """
    验证 mysql_native_password
    stored_hash 是 SHA1(SHA1(password)) (20 bytes)
    auth_response = SHA1(password) XOR SHA1(auth_seed + SHA1(SHA1(password)))
    """
    import hashlib

    # 空密码必须放在长度校验之前：空密码用户的 stored_hash 就是 b''，
    # 原实现先判 len(stored_hash) != 20 直接 return False，导致空密码永远登录失败。
    if len(auth_response) == 0:
        # 空密码
        return stored_hash in (b'', b'\x00' * 20)

    if len(stored_hash) != 20:
        return False

    # SHA1(auth_seed + stored_hash)
    temp = hashlib.sha1(auth_seed + stored_hash).digest()
    if len(auth_response) != len(temp):
        return False
    # auth_response XOR temp 得到 SHA1(password)，再取一次 SHA1 才能和 stored_hash 比较
    # （原实现漏掉了最后一次 SHA1，导致任何非空密码都被判为错误）
    candidate = bytes(a ^ b for a, b in zip(auth_response, temp))
    return hashlib.sha1(candidate).digest() == stored_hash


def hash_password(password: str) -> bytes:
    """计算 mysql_native_password 的存储哈希"""
    import hashlib
    if not password:
        return b''
    stage1 = hashlib.sha1(password.encode()).digest()
    stage2 = hashlib.sha1(stage1).digest()
    return stage2