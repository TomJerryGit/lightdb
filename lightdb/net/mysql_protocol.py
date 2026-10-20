"""
MySQL Wire Protocol v10 实现
参考: https://dev.mysql.com/doc/internals/en/client-server-protocol.html
"""
import struct
import os
import re

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
COM_STMT_SEND_LONG_DATA = 0x18
COM_STMT_CLOSE = 0x19
COM_STMT_RESET = 0x1a
COM_STMT_FETCH = 0x1c

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
MYSQL_TYPE_ENUM        = 0xf7
MYSQL_TYPE_SET         = 0xf8
MYSQL_TYPE_TINY_BLOB   = 0xf9
MYSQL_TYPE_MEDIUM_BLOB = 0xfa
MYSQL_TYPE_LONG_BLOB   = 0xfb
MYSQL_TYPE_BLOB        = 0xfc
MYSQL_TYPE_VAR_STRING  = 0xfd
MYSQL_TYPE_STRING      = 0xfe
MYSQL_TYPE_GEOMETRY    = 0xff
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


# ==================== 预处理语句（COM_STMT_*） ====================
#
# 参考:
#   https://dev.mysql.com/doc/internals/en/com-stmt-prepare.html
#   https://dev.mysql.com/doc/internals/en/com-stmt-execute.html
#   https://dev.mysql.com/doc/internals/en/binary-protocol-value.html
#
# 本引擎只实现「非游标」形态：
#   * COM_STMT_PREPARE → PREPARE OK 包（参数/结果列定义）；
#   * COM_STMT_EXECUTE → 二进制参数解码 → 交给文本执行器 → 二进制结果集/OK/ERR；
#   * COM_STMT_CLOSE   → 无响应包（协议规定，客户端不读响应）；
#   * COM_STMT_RESET   → OK 包（清掉绑定参数类型缓存）。
#
# 未实现（显式边界，见 server 侧说明）：
#   * COM_STMT_SEND_LONG_DATA：不累积长数据（该命令按协议无响应，这里忽略）；
#   * COM_STMT_FETCH / 游标：EXECUTE 忽略 CURSOR flag，直接回全量结果集；
#   * MYSQL_TYPE_BIT / GEOMETRY 等二进制语义类型参数：显式报 1235，不猜。

#: EXECUTE 参数里**不支持**的类型（报 1235，绝不按字符串猜一个值）
_STMT_UNSUPPORTED_PARAM_TYPES = {
    MYSQL_TYPE_BIT: 'BIT',
    MYSQL_TYPE_GEOMETRY: 'GEOMETRY',
}

#: 二进制协议里「长度编码字符串」形态的类型（按 UTF-8 文本解码）
_STMT_STRING_PARAM_TYPES = {
    MYSQL_TYPE_STRING: 'STRING',
    MYSQL_TYPE_VAR_STRING: 'VAR_STRING',
    MYSQL_TYPE_VARCHAR: 'VARCHAR',
    MYSQL_TYPE_DECIMAL: 'DECIMAL',
    MYSQL_TYPE_NEWDECIMAL: 'NEWDECIMAL',
    MYSQL_TYPE_ENUM: 'ENUM',
    MYSQL_TYPE_SET: 'SET',
    MYSQL_TYPE_JSON: 'JSON',
}

#: 二进制协议里「长度编码字节串」形态、保留原始字节的类型（服务端再决定
#: 是否 UTF-8 文本；解码失败时显式报 1235，不做有损替换）
_STMT_BYTES_PARAM_TYPES = {
    MYSQL_TYPE_TINY_BLOB: 'TINY_BLOB',
    MYSQL_TYPE_MEDIUM_BLOB: 'MEDIUM_BLOB',
    MYSQL_TYPE_LONG_BLOB: 'LONG_BLOB',
    MYSQL_TYPE_BLOB: 'BLOB',
}

_STMT_PARAM_TYPE_NAMES = {
    MYSQL_TYPE_DECIMAL: 'DECIMAL',
    MYSQL_TYPE_TINY: 'TINY',
    MYSQL_TYPE_SHORT: 'SHORT',
    MYSQL_TYPE_LONG: 'LONG',
    MYSQL_TYPE_FLOAT: 'FLOAT',
    MYSQL_TYPE_DOUBLE: 'DOUBLE',
    MYSQL_TYPE_NULL: 'NULL',
    MYSQL_TYPE_TIMESTAMP: 'TIMESTAMP',
    MYSQL_TYPE_LONGLONG: 'LONGLONG',
    MYSQL_TYPE_INT24: 'INT24',
    MYSQL_TYPE_DATE: 'DATE',
    MYSQL_TYPE_TIME: 'TIME',
    MYSQL_TYPE_DATETIME: 'DATETIME',
    MYSQL_TYPE_YEAR: 'YEAR',
    MYSQL_TYPE_VARCHAR: 'VARCHAR',
    MYSQL_TYPE_BIT: 'BIT',
    MYSQL_TYPE_NEWDECIMAL: 'NEWDECIMAL',
    MYSQL_TYPE_ENUM: 'ENUM',
    MYSQL_TYPE_SET: 'SET',
    MYSQL_TYPE_TINY_BLOB: 'TINY_BLOB',
    MYSQL_TYPE_MEDIUM_BLOB: 'MEDIUM_BLOB',
    MYSQL_TYPE_LONG_BLOB: 'LONG_BLOB',
    MYSQL_TYPE_BLOB: 'BLOB',
    MYSQL_TYPE_VAR_STRING: 'VAR_STRING',
    MYSQL_TYPE_STRING: 'STRING',
    MYSQL_TYPE_GEOMETRY: 'GEOMETRY',
    MYSQL_TYPE_JSON: 'JSON',
}


def stmt_type_name(code: int) -> str:
    """把列/参数类型码转成可读名字（报错信息用）"""
    try:
        return _STMT_PARAM_TYPE_NAMES.get(int(code), f'0x{int(code):02x}')
    except (TypeError, ValueError):
        return '?'


#: 预处理语句错误码 → MySQL 的 SQLSTATE（1235 ER_NOT_SUPPORTED_YET 是 42000）
_STMT_ERR_SQLSTATE = {
    1210: 'HY000',      # ER_WRONG_ARGUMENTS
    1235: '42000',      # ER_NOT_SUPPORTED_YET
    1243: 'HY000',      # ER_UNKNOWN_STMT_HANDLER
    1461: '42000',      # ER_MAX_PREPARED_STMT_COUNT_REACHED
}


class StmtProtocolError(Exception):
    """预处理语句协议层错误（由 server 翻译成 ERR 包）

    errno 语义与 MySQL 对齐：
      1210 = ER_WRONG_ARGUMENTS（EXECUTE 参数个数/参数类型数据不合法）
      1235 = ER_NOT_SUPPORTED_YET（参数类型/形态本引擎未实现）
      1243 = ER_UNKNOWN_STMT_HANDLER（statement_id 不存在/已被 CLOSE）
      1461 = ER_MAX_PREPARED_STMT_COUNT_REACHED（单连接语句数超限）
    """

    def __init__(self, errno: int, message: str, sqlstate: str = None):
        super().__init__(message)
        self.errno = int(errno)
        self.sqlstate = str(
            sqlstate or _STMT_ERR_SQLSTATE.get(self.errno) or 'HY000')
        self.message = str(message)


def make_prepare_ok_packet(statement_id: int, num_columns: int,
                           num_params: int) -> bytes:
    """COM_STMT_PREPARE 的 PREPARE OK 包（列/参数定义由调用方随后发送）

    格式：status(1=0x00) + statement_id(4) + num_columns(2) +
    num_params(2) + reserved(1=0x00) + warning_count(2)。
    """
    payload = b'\x00'                                   # status = OK
    payload += struct.pack('<I', int(statement_id) & 0xffffffff)
    payload += struct.pack('<H', int(num_columns) & 0xffff)
    payload += struct.pack('<H', int(num_params) & 0xffff)
    payload += b'\x00'                                  # reserved
    payload += struct.pack('<H', 0)                     # warning_count
    return payload


# ---------- EXECUTE 参数解码（客户端 → 服务端） ----------

def _decode_lenenc_bytes(data: bytes, offset: int):
    """读一个长度编码字节串 → (bytes, 新 offset)"""
    if offset >= len(data):
        raise StmtProtocolError(1210, 'EXECUTE 参数数据不完整（缺少长度前缀）')
    first = data[offset]
    if first < 251:
        n, offset = first, offset + 1
    elif first == 0xfc:
        if offset + 3 > len(data):
            raise StmtProtocolError(1210, 'EXECUTE 参数长度前缀不完整')
        n, offset = struct.unpack_from('<H', data, offset + 1)[0], offset + 3
    elif first == 0xfd:
        if offset + 4 > len(data):
            raise StmtProtocolError(1210, 'EXECUTE 参数长度前缀不完整')
        n = struct.unpack_from('<I', data, offset + 1)[0] & 0xffffff
        offset += 4
    elif first == 0xfe:
        if offset + 9 > len(data):
            raise StmtProtocolError(1210, 'EXECUTE 参数长度前缀不完整')
        n, offset = struct.unpack_from('<Q', data, offset + 1)[0], offset + 9
    else:
        raise StmtProtocolError(1210, 'EXECUTE 参数长度前缀非法')
    if offset + n > len(data):
        raise StmtProtocolError(1210, 'EXECUTE 参数字节数不足（包被截断）')
    return data[offset:offset + n], offset + n


def _unpack_value(fmt: str, data: bytes, offset: int):
    size = struct.calcsize(fmt)
    if offset + size > len(data):
        raise StmtProtocolError(1210, 'EXECUTE 参数数据不完整（数值越界）')
    return struct.unpack_from(fmt, data, offset)[0], offset + size


def _fmt_temporal(year, month, day, hour=0, minute=0, second=0,
                  microsecond=0):
    date = f'{int(year):04d}-{int(month):02d}-{int(day):02d}'
    if hour == 0 and minute == 0 and second == 0 and not microsecond:
        return date
    out = (f'{date} {int(hour):02d}:{int(minute):02d}:'
           f'{int(second):02d}')
    if microsecond:
        out += f'.{int(microsecond):06d}'
    return out


def _decode_temporal_param(data: bytes, offset: int, type_code: int):
    """解码 DATE/DATETIME/TIMESTAMP/TIME 参数 → (文本, 新 offset)

    二进制协议：1 字节长度 + 紧凑结构
      DATE：长度 0（零日期）或 4 = year(2) month(1) day(1)
      DATETIME/TIMESTAMP：0 / 4 / 7 / 11（+微秒）
      TIME：0 / 8（sign, days, h, m, s）/ 12（+微秒）
    """
    if offset >= len(data):
        raise StmtProtocolError(1210, 'EXECUTE 时间参数数据不完整')
    length = data[offset]
    offset += 1
    end = offset + length
    if end > len(data):
        raise StmtProtocolError(1210, 'EXECUTE 时间参数字节数不足')
    if length == 0:
        zero = '0000-00-00' if type_code == MYSQL_TYPE_DATE else (
            '-00:00:00' if type_code == MYSQL_TYPE_TIME
            else '0000-00-00 00:00:00')
        return zero, end
    if type_code == MYSQL_TYPE_TIME:
        if length < 8:
            raise StmtProtocolError(1210, 'EXECUTE TIME 参数长度非法')
        neg = data[offset]
        days = struct.unpack_from('<I', data, offset + 1)[0]
        hour = data[offset + 5]
        minute = data[offset + 6]
        second = data[offset + 7]
        micro = (struct.unpack_from('<I', data, offset + 8)[0]
                 if length >= 12 else 0)
        total_h = days * 24 + hour
        text = f'{"-" if neg else ""}{total_h:02d}:{minute:02d}:{second:02d}'
        if micro:
            text += f'.{micro:06d}'
        return text, end
    # DATE / DATETIME / TIMESTAMP
    if length < 4:
        raise StmtProtocolError(1210, 'EXECUTE 日期参数长度非法')
    year, month, day = struct.unpack_from('<HBB', data, offset)
    if type_code == MYSQL_TYPE_DATE:
        return f'{year:04d}-{month:02d}-{day:02d}', end
    hour = minute = second = micro = 0
    if length >= 7:
        hour, minute, second = struct.unpack_from(
            '<BBB', data, offset + 4)
    if length >= 11:
        micro = struct.unpack_from('<I', data, offset + 7)[0]
    return _fmt_temporal(year, month, day, hour, minute, second, micro), end


def decode_binary_param(data: bytes, offset: int, type_code: int,
                        unsigned: bool = False):
    """解码一个 EXECUTE 二进制参数 → (Python 值, 新 offset)

    支持的 Python 值形态：int / float / str / bytes / None。
    `bytes` 只来自 BLOB 家族（由上层决定是否按 UTF-8 文本内联）。
    """
    code = int(type_code)
    if code in _STMT_UNSUPPORTED_PARAM_TYPES:
        raise StmtProtocolError(
            1235,
            f"预处理参数类型 {stmt_type_name(code)} 暂不支持："
            f"本引擎的 COM_STMT_EXECUTE 只接受"
            f"数值/字符串/时间/NULL 参数（二进制语义类型不做猜测）")
    if code == MYSQL_TYPE_NULL:
        return None, offset
    if code == MYSQL_TYPE_TINY:
        return _unpack_value('<B' if unsigned else '<b', data, offset)
    if code == MYSQL_TYPE_SHORT:
        return _unpack_value('<H' if unsigned else '<h', data, offset)
    if code in (MYSQL_TYPE_LONG, MYSQL_TYPE_INT24):
        return _unpack_value('<I' if unsigned else '<i', data, offset)
    if code == MYSQL_TYPE_LONGLONG:
        return _unpack_value('<Q' if unsigned else '<q', data, offset)
    if code == MYSQL_TYPE_YEAR:
        return _unpack_value('<H', data, offset)
    if code == MYSQL_TYPE_FLOAT:
        return _unpack_value('<f', data, offset)
    if code == MYSQL_TYPE_DOUBLE:
        return _unpack_value('<d', data, offset)
    if code in _STMT_BYTES_PARAM_TYPES:
        return _decode_lenenc_bytes(data, offset)
    if code in _STMT_STRING_PARAM_TYPES:
        raw, offset = _decode_lenenc_bytes(data, offset)
        try:
            return raw.decode('utf-8'), offset
        except UnicodeDecodeError:
            raise StmtProtocolError(
                1235,
                f"预处理参数类型 {stmt_type_name(code)} 不是合法 UTF-8 文本："
                f"本引擎以文本字面量内联参数，二进制内容无法无损表达")
    if code in (MYSQL_TYPE_DATE, MYSQL_TYPE_DATETIME,
                MYSQL_TYPE_TIMESTAMP, MYSQL_TYPE_TIME):
        return _decode_temporal_param(data, offset, code)
    raise StmtProtocolError(
        1235,
        f"预处理参数类型 {stmt_type_name(code)} 暂不支持")


class StmtExecuteRequest:
    """COM_STMT_EXECUTE 请求体解析器

    包体结构（statement_id 之后）：
        flags(1) iteration_count(4)
        [num_params > 0 时]
          null_bitmap((num_params+7)//8)
          new_params_bound_flag(1)
          [=1 时] 每个参数 2 字节类型（type, unsigned_flag）
          参数值（非 NULL，按类型紧凑编码）

    `new_params_bound_flag=0` 表示复用**上一次**绑定的参数类型（MySQL 允许
    客户端只发值）；服务端必须缓存参数类型，否则回 1210。
    """

    def __init__(self, payload: bytes, num_params: int,
                 cached_param_types=None):
        self.payload = payload or b''
        if len(self.payload) < 9:
            raise StmtProtocolError(
                1210, 'Incorrect arguments to mysqld_stmt_execute'
                      '（EXECUTE 包体过短）')
        self.statement_id = struct.unpack_from('<I', self.payload, 0)[0]
        self.flags = self.payload[4]
        self.iteration_count = struct.unpack_from(
            '<I', self.payload, 5)[0]
        self.num_params = int(num_params)
        self.param_types = (list(cached_param_types)
                            if cached_param_types else None)
        self.params = []
        self._parse(self.payload[9:])

    def _parse(self, data: bytes):
        n = self.num_params
        if n == 0:
            if data:
                raise StmtProtocolError(
                    1210, 'Incorrect arguments to mysqld_stmt_execute'
                          '（语句不需要参数，但 EXECUTE 带了参数数据）')
            self.param_types = []
            return
        nbits = (n + 7) // 8
        if len(data) < nbits + 1:
            raise StmtProtocolError(
                1210, 'EXECUTE 参数个数与占位符不匹配（NULL bitmap 不完整）')
        null_bitmap = data[:nbits]
        offset = nbits
        new_bound = data[offset]
        offset += 1
        if new_bound:
            need = n * 2
            if len(data) < offset + need:
                raise StmtProtocolError(
                    1210, 'EXECUTE 参数类型表不完整')
            types = []
            for _ in range(n):
                code = data[offset]
                flag = data[offset + 1]
                types.append((code, bool(flag)))
                offset += 2
            self.param_types = types
        elif not self.param_types:
            raise StmtProtocolError(
                1210, 'EXECUTE 未绑定参数类型：这是本语句第一次执行，'
                      '客户端必须发送参数类型（new_params_bound_flag=1）')
        if len(self.param_types) != n:
            raise StmtProtocolError(
                1210, 'EXECUTE 绑定的参数类型个数与占位符不匹配')

        params = []
        for i in range(n):
            if null_bitmap[i // 8] & (1 << (i % 8)):
                params.append(None)
                continue
            code, flag = self.param_types[i]
            value, offset = decode_binary_param(data, offset, code, flag)
            params.append(value)
        if offset != len(data):
            raise StmtProtocolError(
                1210, 'EXECUTE 参数个数与占位符不匹配'
                      '（包内有多余字节，客户端给了更多参数值）')
        self.params = params


# ---------- 二进制结果集行编码（服务端 → 客户端） ----------

def _encode_temporal_value(value, type_code: int) -> bytes:
    text = str(value).strip()
    if type_code == MYSQL_TYPE_TIME:
        m = re.match(
            r'^(-)?(\d+):(\d{1,2}):(\d{1,2})(?:\.(\d+))?$', text)
        if not m:
            raise ValueError(f'无法编码 TIME 值: {text!r}')
        neg = 1 if m.group(1) else 0
        total = int(m.group(2))
        days, hour = divmod(total, 24)
        micro = int((m.group(5) or '0').ljust(6, '0')[:6])
        payload = struct.pack('<BIbBB', neg, days, hour,
                              int(m.group(3)), int(m.group(4)))
        if micro:
            return struct.pack('<B', 12) + payload + struct.pack('<I', micro)
        return struct.pack('<B', 8) + payload
    m = re.match(
        r'^(\d{4})-(\d{2})-(\d{2})'
        r'(?:[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?)?$', text)
    if not m:
        raise ValueError(f'无法编码日期时间值: {text!r}')
    year, month, day = (int(m.group(i)) for i in (1, 2, 3))
    hour, minute, second = (int(m.group(i) or 0) for i in (4, 5, 6))
    micro = int((m.group(7) or '0').ljust(6, '0')[:6])
    body = struct.pack('<HBB', year, month, day)
    if type_code == MYSQL_TYPE_DATE or not (hour or minute or second):
        if micro:
            raise ValueError(f'DATE 值不能带微秒: {text!r}')
        return struct.pack('<B', 4) + body
    body += struct.pack('<BBB', hour, minute, second)
    if micro:
        return (struct.pack('<B', 11) + body
                + struct.pack('<I', micro))
    return struct.pack('<B', 7) + body


def _encode_binary_value(value, type_code: int) -> bytes:
    code = int(type_code)
    if code == MYSQL_TYPE_TINY:
        return struct.pack('<b', int(value))
    if code == MYSQL_TYPE_SHORT:
        return struct.pack('<h', int(value))
    if code in (MYSQL_TYPE_LONG, MYSQL_TYPE_INT24):
        return struct.pack('<i', int(value))
    if code == MYSQL_TYPE_LONGLONG:
        return struct.pack('<q', int(value))
    if code == MYSQL_TYPE_FLOAT:
        return struct.pack('<f', float(value))
    if code == MYSQL_TYPE_DOUBLE:
        return struct.pack('<d', float(value))
    if code in (MYSQL_TYPE_DATE, MYSQL_TYPE_DATETIME,
                MYSQL_TYPE_TIMESTAMP, MYSQL_TYPE_TIME):
        return _encode_temporal_value(value, code)
    # 其余（字符串/BLOB/JSON/DECIMAL…）都按长度编码字节串发送
    if code in (MYSQL_TYPE_BLOB, MYSQL_TYPE_TINY_BLOB,
                MYSQL_TYPE_MEDIUM_BLOB, MYSQL_TYPE_LONG_BLOB,
                MYSQL_TYPE_GEOMETRY):
        raw = value if isinstance(value, (bytes, bytearray)) else str(
            value).encode('utf-8')
        return write_lenenc_int(len(raw)) + bytes(raw)
    text = value if isinstance(value, str) else str(value)
    raw = text.encode('utf-8')
    return write_lenenc_int(len(raw)) + raw


def make_binary_row(values, columns) -> bytes:
    """构造二进制协议行

    `columns`：与文本结果集同一份 `[(name, ctype, length), ...]`。
    行格式：0x00 + NULL bitmap（偏移 2）+ 非 NULL 列值（按列类型编码）。
    """
    n = len(columns)
    nbits = (n + 7 + 2) // 8
    bitmap = bytearray(nbits)
    parts = []
    for i, col in enumerate(columns):
        value = values[i] if i < len(values) else None
        ctype = col[1] if len(col) > 1 else MYSQL_TYPE_VAR_STRING
        if value is None or int(ctype) == MYSQL_TYPE_NULL:
            bitmap[(i + 2) // 8] |= 1 << ((i + 2) % 8)
            continue
        parts.append(_encode_binary_value(value, ctype))
    return b'\x00' + bytes(bitmap) + b''.join(parts)