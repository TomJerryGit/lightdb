"""预处理语句（COM_STMT_*）协议测试

背景：本引擎此前只实现文本查询协议（COM_QUERY），完全没有
`COM_STMT_PREPARE / EXECUTE / CLOSE / RESET`。pymysql 默认客户端参数化
（客户端把参数内联成文本）不受影响，但 JDBC / mysql-connector 等
**server-side prepare** 客户端会直接失败。本文件锁定补齐后的行为。

测试方式：进程内启动**真实 MySQLServer**，再用本文件自带的**最小二进制
协议客户端**（自实现握手/认证/COM_STMT_* 编解码）做字节级验证 ——
这样断言的是"客户端真正收到的包"，而不是服务端内部函数返回值。

覆盖：
  1. PREPARE INSERT（8 个 `?`）→ EXECUTE 多种二进制参数类型（NULL/中文/
     负整数/浮点/BLOB 文本/DATE/DATETIME/TIMESTAMP）→ 文本查询验证写入；
  2. TINY/SHORT/LONG/LONGLONG/INT24/NEWDECIMAL 参数解码；unsigned 标志；
  3. PREPARE SELECT WHERE id=? → EXECUTE 返回**二进制结果集**，值正确；
     第二次 EXECUTE 复用缓存的参数类型（new_params_bound_flag=0）；
  4. 引号/注释里的 `?` 不计数、不替换；
  5. 参数个数不符 → 1210；未知/CLOSE 后的 statement → 1243；
  6. 同连接多 statement 并存、RESET 清缓存后可复用；
  7. 不支持的参数类型（BIT）→ 显式 1235（不静默猜值）；
  8. 开启过 prepared 后文本协议仍正常（回归）；
  9. 如环境安装 mysql-connector-python，额外用真实 server-side prepare
     客户端跑一遍（未安装则 skip）。
"""
import os
import socket
import struct
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from tests.conftest import track
except ImportError:                       # 直接 python tests/test_prepared_statements.py
    from conftest import track

from lightdb.net import mysql_protocol as mp


# ==================== 最小 MySQL 二进制协议客户端 ====================

CLIENT_LONG_PASSWORD = 0x00000001
CLIENT_LONG_FLAG = 0x00000004
CLIENT_CONNECT_WITH_DB = 0x00000008
CLIENT_PROTOCOL_41 = 0x00000200
CLIENT_TRANSACTIONS = 0x00002000
CLIENT_SECURE_CONNECTION = 0x00008000
CLIENT_PLUGIN_AUTH = 0x00080000


def _lenenc_int(data, offset):
    first = data[offset]
    if first < 251:
        return first, offset + 1
    if first == 0xFC:
        return struct.unpack_from('<H', data, offset + 1)[0], offset + 3
    if first == 0xFD:
        return (struct.unpack_from('<I', data, offset + 1)[0] & 0xffffff,
                offset + 4)
    if first == 0xFE:
        return struct.unpack_from('<Q', data, offset + 1)[0], offset + 9
    return None, offset + 1


def _lenenc_string(data, offset):
    n, offset = _lenenc_int(data, offset)
    if n is None:
        return None, offset
    return data[offset:offset + n], offset + n


def _lenenc_prefix(n):
    if n < 251:
        return bytes([n])
    if n < 2 ** 16:
        return b'\xfc' + struct.pack('<H', n)
    if n < 2 ** 24:
        return b'\xfd' + struct.pack('<I', n)[:3]
    return b'\xfe' + struct.pack('<Q', n)


class PreparedClient:
    """自写最小客户端：握手 + 文本查询 + COM_STMT_*（二进制结果集解码）"""

    def __init__(self, port, user='root', password='', database='default',
                 timeout=10):
        self.sock = socket.create_connection(
            ('127.0.0.1', port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._seq = 0
        self._handshake(user, password, database)

    # ---------- 包收发 ----------

    def _recv_exact(self, n):
        chunks = []
        while n > 0:
            chunk = self.sock.recv(n)
            if not chunk:
                raise ConnectionError('服务端关闭了连接')
            chunks.append(chunk)
            n -= len(chunk)
        return b''.join(chunks)

    def _read_packet(self):
        header = self._recv_exact(4)
        length = header[0] | (header[1] << 8) | (header[2] << 16)
        payload = self._recv_exact(length) if length else b''
        return payload

    def _write_packet(self, payload):
        self.sock.sendall(
            struct.pack('<I', len(payload))[:3]
            + bytes([self._seq & 0xFF]) + payload)
        self._seq = (self._seq + 1) & 0xFF

    def _command(self, cmd, data=b''):
        self._seq = 0
        self._write_packet(bytes([cmd]) + data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    # ---------- 握手/认证 ----------

    @staticmethod
    def _native_password(password, seed):
        """mysql_native_password 认证响应（sha1(pw) XOR sha1(seed+sha1(sha1(pw))）"""
        if not password:
            return b''
        import hashlib
        stage1 = hashlib.sha1(password.encode('utf-8')).digest()
        stage2 = hashlib.sha1(stage1).digest()
        temp = hashlib.sha1(seed + stage2).digest()
        return bytes(a ^ b for a, b in zip(stage1, temp))

    def _handshake(self, user, password, database):
        payload = self._read_packet()
        assert payload[0] == 10, payload[:8]       # protocol version 10
        off = 1
        end = payload.index(b'\x00', off)           # server version
        off = end + 1
        self.connection_id = struct.unpack_from('<I', payload, off)[0]
        off += 4
        seed1 = payload[off:off + 8]                # auth-plugin-data-part-1
        off += 8
        off += 1                                    # filler
        cap1 = struct.unpack_from('<H', payload, off)[0]
        off += 2
        off += 1                                    # charset
        off += 2                                    # status
        cap2 = struct.unpack_from('<H', payload, off)[0]
        off += 2
        self.server_capabilities = cap1 | (cap2 << 16)
        auth_len = payload[off]
        off += 1
        off += 10                                   # reserved
        part2_len = max(13, auth_len - 8)
        seed2 = payload[off:off + part2_len]
        seed = seed1 + seed2[:12]                   # 20 字节挑战
        self.auth_response = self._native_password(password, seed)

        client_flags = (
            CLIENT_LONG_PASSWORD | CLIENT_LONG_FLAG
            | CLIENT_CONNECT_WITH_DB | CLIENT_PROTOCOL_41
            | CLIENT_TRANSACTIONS | CLIENT_SECURE_CONNECTION
            | CLIENT_PLUGIN_AUTH)
        response = struct.pack('<I', client_flags)
        response += struct.pack('<I', 1 << 20)      # max_packet_size
        response += bytes([45])                     # utf8mb4_general_ci
        response += b'\x00' * 23                    # reserved
        response += user.encode('utf-8') + b'\x00'
        response += bytes([len(self.auth_response)]) + self.auth_response
        response += (database or '').encode('utf-8') + b'\x00'
        response += b'mysql_native_password\x00'
        self._seq = 1
        self._write_packet(response)
        ok = self._read_packet()
        assert ok[0] == 0x00, ('握手失败', ok[:64])

    # ---------- 响应解析 ----------

    @staticmethod
    def _parse_err(payload):
        code = struct.unpack_from('<H', payload, 1)[0]
        if payload[3:4] == b'#':
            sqlstate = payload[4:9].decode('ascii')
            message = payload[9:].decode('utf-8', errors='replace')
        else:
            sqlstate = None
            message = payload[3:].decode('utf-8', errors='replace')
        return {'type': 'error', 'code': code,
                'sqlstate': sqlstate, 'message': message}

    @staticmethod
    def _parse_ok(payload):
        affected, off = _lenenc_int(payload, 1)
        insert_id, off = _lenenc_int(payload, off)
        status, warnings = struct.unpack_from('<HH', payload, off)
        return {'type': 'ok', 'affected': affected, 'insert_id': insert_id,
                'status': status, 'warnings': warnings,
                'message': payload[off + 4:].decode('utf-8', errors='replace')}

    @staticmethod
    def _parse_coldef(payload):
        fields = []
        off = 0
        for _ in range(6):
            raw, off = _lenenc_string(payload, off)
            fields.append(None if raw is None
                          else raw.decode('utf-8', errors='replace'))
        assert payload[off] == 0x0C, payload[:32]
        off += 1
        charset = struct.unpack_from('<H', payload, off)[0]
        off += 2
        length = struct.unpack_from('<I', payload, off)[0]
        off += 4
        ctype = payload[off]
        off += 1
        flags = struct.unpack_from('<H', payload, off)[0]
        off += 2
        decimals = payload[off]
        return {'name': fields[4], 'type': ctype, 'length': length,
                'charset': charset, 'flags': flags, 'decimals': decimals}

    def _read_result(self, binary=False):
        payload = self._read_packet()
        first = payload[0]
        if first == 0xFF:
            return self._parse_err(payload)
        if first == 0x00 and len(payload) >= 7:
            return self._parse_ok(payload)
        ncols, _off = _lenenc_int(payload, 0)
        assert ncols and ncols < 251, payload[:32]
        columns = [self._parse_coldef(self._read_packet())
                   for _ in range(ncols)]
        eof = self._read_packet()
        assert eof[0] == 0xFE and len(eof) < 9, eof[:16]
        rows = []
        while True:
            row = self._read_packet()
            if row[0] == 0xFE and len(row) < 9:
                break
            if binary:
                rows.append(self._read_binary_row(row, columns))
            else:
                rows.append(self._read_text_row(row, len(columns)))
        return {'type': 'resultset', 'columns': columns, 'rows': rows}

    @staticmethod
    def _read_text_row(payload, ncols):
        values = []
        off = 0
        for _ in range(ncols):
            raw, off = _lenenc_string(payload, off)
            values.append(None if raw is None
                          else raw.decode('utf-8', errors='replace'))
        return tuple(values)

    @staticmethod
    def _read_binary_value(payload, offset, ctype):
        if ctype == mp.MYSQL_TYPE_TINY:
            return struct.unpack_from('<b', payload, offset)[0], offset + 1
        if ctype == mp.MYSQL_TYPE_SHORT:
            return struct.unpack_from('<h', payload, offset)[0], offset + 2
        if ctype in (mp.MYSQL_TYPE_LONG, mp.MYSQL_TYPE_INT24):
            return struct.unpack_from('<i', payload, offset)[0], offset + 4
        if ctype == mp.MYSQL_TYPE_LONGLONG:
            return struct.unpack_from('<q', payload, offset)[0], offset + 8
        if ctype == mp.MYSQL_TYPE_FLOAT:
            return struct.unpack_from('<f', payload, offset)[0], offset + 4
        if ctype == mp.MYSQL_TYPE_DOUBLE:
            return struct.unpack_from('<d', payload, offset)[0], offset + 8
        if ctype in (mp.MYSQL_TYPE_VAR_STRING, mp.MYSQL_TYPE_STRING,
                     mp.MYSQL_TYPE_VARCHAR, mp.MYSQL_TYPE_BLOB,
                     mp.MYSQL_TYPE_TINY_BLOB, mp.MYSQL_TYPE_MEDIUM_BLOB,
                     mp.MYSQL_TYPE_LONG_BLOB, mp.MYSQL_TYPE_NEWDECIMAL,
                     mp.MYSQL_TYPE_DECIMAL, mp.MYSQL_TYPE_JSON):
            raw, offset = _lenenc_string(payload, offset)
            if raw is None:
                return None, offset
            return raw.decode('utf-8', errors='replace'), offset
        raise AssertionError(f'测试客户端不支持解码结果类型 0x{ctype:02x}')

    def _read_binary_row(self, payload, columns):
        assert payload[0] == 0x00, payload[:16]
        n = len(columns)
        nbits = (n + 7 + 2) // 8
        bitmap = payload[1:1 + nbits]
        off = 1 + nbits
        values = []
        for i, col in enumerate(columns):
            if bitmap[(i + 2) // 8] & (1 << ((i + 2) % 8)):
                values.append(None)
                continue
            value, off = self._read_binary_value(payload, off, col['type'])
            values.append(value)
        assert off == len(payload), (
            '二进制行有多余字节（列类型与编码不一致）',
            off, len(payload), payload.hex())
        return tuple(values)

    # ---------- 文本查询（回归用） ----------

    def query(self, sql):
        self._command(mp.COM_QUERY, sql.encode('utf-8'))
        return self._read_result(binary=False)

    def query_ok(self, sql):
        result = self.query(sql)
        assert result['type'] == 'ok', (sql, result)
        return result

    def query_rows(self, sql):
        result = self.query(sql)
        assert result['type'] == 'resultset', (sql, result)
        return result['rows']

    # ---------- COM_STMT_* ----------

    def prepare(self, sql):
        self._command(mp.COM_STMT_PREPARE, sql.encode('utf-8'))
        payload = self._read_packet()
        if payload[0] == 0xFF:
            return self._parse_err(payload)
        assert payload[0] == 0x00, payload[:16]
        stmt_id = struct.unpack_from('<I', payload, 1)[0]
        num_columns = struct.unpack_from('<H', payload, 5)[0]
        num_params = struct.unpack_from('<H', payload, 7)[0]
        param_columns = []
        columns = []
        if num_params:
            param_columns = [self._parse_coldef(self._read_packet())
                             for _ in range(num_params)]
            eof = self._read_packet()
            assert eof[0] == 0xFE and len(eof) < 9, eof[:16]
        if num_columns:
            columns = [self._parse_coldef(self._read_packet())
                       for _ in range(num_columns)]
            eof = self._read_packet()
            assert eof[0] == 0xFE and len(eof) < 9, eof[:16]
        return {'type': 'prepare', 'statement_id': stmt_id,
                'num_columns': num_columns, 'num_params': num_params,
                'param_columns': param_columns, 'columns': columns}

    @staticmethod
    def _encode_param_value(ctype, value):
        if ctype == mp.MYSQL_TYPE_TINY:
            return struct.pack('<b', int(value))
        if ctype == mp.MYSQL_TYPE_SHORT:
            return struct.pack('<h', int(value))
        if ctype in (mp.MYSQL_TYPE_LONG, mp.MYSQL_TYPE_INT24):
            return struct.pack('<i', int(value))
        if ctype == mp.MYSQL_TYPE_LONGLONG:
            return struct.pack('<q', int(value))
        if ctype == mp.MYSQL_TYPE_FLOAT:
            return struct.pack('<f', float(value))
        if ctype == mp.MYSQL_TYPE_DOUBLE:
            return struct.pack('<d', float(value))
        if ctype in (mp.MYSQL_TYPE_DATE,):
            year, month, day = value
            return bytes([4]) + struct.pack('<HBB', year, month, day)
        if ctype in (mp.MYSQL_TYPE_DATETIME, mp.MYSQL_TYPE_TIMESTAMP):
            year, month, day, hour, minute, second = value
            return (bytes([7]) + struct.pack('<HBB', year, month, day)
                    + struct.pack('<BBB', hour, minute, second))
        if isinstance(value, str):
            value = value.encode('utf-8')
        value = bytes(value)
        return _lenenc_prefix(len(value)) + value

    @staticmethod
    def _encode_execute(statement_id, params, bind_types=True):
        """构造 EXECUTE 包体；params=[(type_code, value[, unsigned])...]"""
        data = struct.pack('<I', statement_id) + b'\x00'
        data += struct.pack('<I', 1)                    # iteration_count
        n = len(params)
        if n:
            null_bitmap = bytearray((n + 7) // 8)
            type_data = b''
            value_data = b''
            for i, item in enumerate(params):
                ctype, value = item[0], item[1]
                unsigned = bool(item[2]) if len(item) > 2 else False
                if value is None:
                    null_bitmap[i // 8] |= 1 << (i % 8)
                    type_data += bytes([mp.MYSQL_TYPE_NULL, 0])
                    continue
                type_data += bytes([ctype, 1 if unsigned else 0])
                value_data += PreparedClient._encode_param_value(
                    ctype, value)
            data += bytes(null_bitmap)
            data += bytes([1 if bind_types else 0])
            if bind_types:
                data += type_data
            data += value_data
        return data

    def stmt_execute(self, statement_id, params, bind_types=True):
        self._command(mp.COM_STMT_EXECUTE,
                      self._encode_execute(statement_id, params, bind_types))
        return self._read_result(binary=True)

    def stmt_execute_raw(self, statement_id, extra_body: bytes):
        """直接发送拼接好的 EXECUTE 包体（构造畸形包用）"""
        self._command(mp.COM_STMT_EXECUTE,
                      struct.pack('<I', statement_id) + extra_body)
        return self._read_result(binary=True)

    def stmt_close(self, statement_id):
        """COM_STMT_CLOSE：协议规定**没有响应包**，因此这里也不读"""
        self._command(mp.COM_STMT_CLOSE, struct.pack('<I', statement_id))

    def stmt_send_long_data(self, statement_id, param_id, data):
        """COM_STMT_SEND_LONG_DATA：协议规定**没有响应包**，因此这里也不读"""
        if isinstance(data, str):
            data = data.encode('utf-8')
        self._command(mp.COM_STMT_SEND_LONG_DATA,
                      struct.pack('<IH', statement_id, param_id) + bytes(data))

    def stmt_reset(self, statement_id):
        self._command(mp.COM_STMT_RESET, struct.pack('<I', statement_id))
        payload = self._read_packet()
        if payload[0] == 0xFF:
            return self._parse_err(payload)
        return self._parse_ok(payload)


# ==================== fixture：真实 MySQLServer ====================

def _free_port():
    sock = socket.socket()
    try:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


@pytest.fixture()
def env(tmp_path):
    from lightdb.config import Config
    from lightdb.db.database import Database
    from lightdb.net.server import MySQLServer
    from lightdb.sql.executor import Executor

    port = _free_port()
    data_dir = str(tmp_path / 'data_prepared')
    cfg = Config()
    cfg.host = '127.0.0.1'
    cfg.port = port
    db = track(Database(data_dir))
    ex = track(Executor(db))
    srv = track(MySQLServer(cfg, db, ex))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)

    clients = []

    def open_client(user='root', password=''):
        client = PreparedClient(port, user=user, password=password)
        clients.append(client)
        return client

    yield {'port': port, 'db': db, 'srv': srv, 'open': open_client}

    for client in clients:
        try:
            client.close()
        except Exception:                     # noqa: BLE001 - 清理兜底
            pass


DDL = (
    "CREATE TABLE ps_t ("
    " id INT PRIMARY KEY,"
    " big BIGINT,"
    " f FLOAT,"
    " d DOUBLE,"
    " name VARCHAR(50),"
    " note VARCHAR(100),"
    " dt DATETIME,"
    " day DATE)")


def _setup_table(client):
    client.query_ok(DDL)


# ==================== 1. INSERT 参数类型 + 写入验证 ====================

def test_prepare_insert_execute_all_param_types(env):
    """8 个 `?` 的 INSERT：NULL/中文/负整数/浮点/日期全部正确写入"""
    client = env['open']()
    _setup_table(client)

    res = client.prepare(
        "INSERT INTO ps_t (id, big, f, d, name, note, dt, day)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
    assert res['type'] == 'prepare', res
    assert res['num_params'] == 8, res
    assert res['num_columns'] == 0, res      # INSERT 没有结果列
    sid = res['statement_id']
    assert sid >= 1

    result = client.stmt_execute(sid, [
        (mp.MYSQL_TYPE_LONG, 5),
        (mp.MYSQL_TYPE_LONGLONG, -1234567890123),
        (mp.MYSQL_TYPE_FLOAT, 1.5),
        (mp.MYSQL_TYPE_DOUBLE, -2.25),
        (mp.MYSQL_TYPE_STRING, '中文名'),
        (mp.MYSQL_TYPE_NULL, None),
        (mp.MYSQL_TYPE_DATETIME, (2024, 1, 2, 3, 4, 5)),
        (mp.MYSQL_TYPE_DATE, (2024, 12, 31)),
    ])
    assert result['type'] == 'ok', result
    assert result['affected'] == 1, result

    rows = client.query_rows(
        "SELECT id, big, f, d, name, note, dt, day FROM ps_t")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row[0] == '5', row
    assert row[1] == '-1234567890123', row
    assert abs(float(row[2]) - 1.5) < 1e-6, row
    assert abs(float(row[3]) - (-2.25)) < 1e-9, row
    assert row[4] == '中文名', row
    assert row[5] is None, row                       # NULL bitmap 生效
    assert row[6] == '2024-01-02 03:04:05', row      # DATETIME 参数
    assert row[7] == '2024-12-31', row               # DATE 参数

    # 第二条：TIMESTAMP 参数（二进制编码与 DATETIME 相同）+ BLOB(UTF-8 文本)
    # 注意 note 里同时有 `?` 和反斜杠：内联/解析后必须**逐字保留**
    # （本引擎的字符串语义是"反斜杠按字面保留、只有成对引号是转义"）。
    blob_text = 'b?lob \\ 文本'
    result = client.stmt_execute(sid, [
        (mp.MYSQL_TYPE_SHORT, 7),
        (mp.MYSQL_TYPE_LONGLONG, 42),
        (mp.MYSQL_TYPE_FLOAT, -0.5),
        (mp.MYSQL_TYPE_DOUBLE, 3.75),
        (mp.MYSQL_TYPE_VAR_STRING, "O'Brien 引号"),
        (mp.MYSQL_TYPE_BLOB, blob_text),
        (mp.MYSQL_TYPE_TIMESTAMP, (2023, 6, 7, 8, 9, 10)),
        (mp.MYSQL_TYPE_DATE, (2023, 6, 7)),
    ])
    assert result['type'] == 'ok' and result['affected'] == 1, result

    rows = client.query_rows(
        "SELECT id, name, note, dt FROM ps_t WHERE id = 7")
    assert rows == [('7', "O'Brien 引号", blob_text,
                     '2023-06-07 08:09:10')], rows

    # 带引号的字符串参数走 WHERE 比较：内联时自动选双引号包裹，
    # 因此 `O'Brien` 不会被当成语句结束（参数值不可能逃逸出字面量）。
    res = client.prepare("SELECT id FROM ps_t WHERE name = ?")
    got = client.stmt_execute(
        res['statement_id'], [(mp.MYSQL_TYPE_STRING, "O'Brien 引号")])
    assert got['type'] == 'resultset' and got['rows'] == [(7,)], got

    # 全 NULL 列（二进制行必须用 NULL bitmap 表达，而不是发空字符串）
    res = client.prepare("SELECT note FROM ps_t WHERE id = ?")
    got = client.stmt_execute(
        res['statement_id'], [(mp.MYSQL_TYPE_LONG, 5)])
    assert got['type'] == 'resultset' and got['rows'] == [(None,)], got


def test_prepare_insert_narrow_and_unsigned_int_params(env):
    """TINY/SHORT/INT24/LONG/LONGLONG/NEWDECIMAL 参数解码 + unsigned 标志"""
    client = env['open']()
    client.query_ok(
        "CREATE TABLE ps_nums (id INT PRIMARY KEY, a INT, b INT, "
        "c BIGINT, d DECIMAL(10,2))")
    res = client.prepare(
        "INSERT INTO ps_nums (id, a, b, c, d) VALUES (?, ?, ?, ?, ?)")
    assert res['num_params'] == 5, res
    sid = res['statement_id']

    result = client.stmt_execute(sid, [
        (mp.MYSQL_TYPE_TINY, 1),
        (mp.MYSQL_TYPE_TINY, -12),
        (mp.MYSQL_TYPE_SHORT, -300),
        (mp.MYSQL_TYPE_LONGLONG, -123456789012),
        (mp.MYSQL_TYPE_NEWDECIMAL, '12.34'),
    ])
    assert result['type'] == 'ok' and result['affected'] == 1, result

    result = client.stmt_execute(sid, [
        (mp.MYSQL_TYPE_SHORT, 2),
        (mp.MYSQL_TYPE_INT24, -100),
        (mp.MYSQL_TYPE_LONG, 70000),
        (mp.MYSQL_TYPE_LONGLONG, 5, True),           # unsigned 标志
        (mp.MYSQL_TYPE_NEWDECIMAL, '-0.50'),
    ])
    assert result['type'] == 'ok' and result['affected'] == 1, result

    rows = client.query_rows(
        "SELECT a, b, c, d FROM ps_nums ORDER BY id")
    assert rows == [('-12', '-300', '-123456789012', '12.34'),
                    ('-100', '70000', '5', '-0.5')], rows


# ==================== 2. SELECT WHERE 参数 + 二进制结果集 ====================

def test_prepare_select_where_param_binary_resultset(env):
    """SELECT WHERE id=? → EXECUTE 回二进制结果集，值/列名正确"""
    client = env['open']()
    _setup_table(client)
    client.query_ok(
        "INSERT INTO ps_t (id, name, d) VALUES (5, 'five', 5.5), "
        "(6, 'six', 6.5)")

    res = client.prepare(
        "SELECT id, name, d FROM ps_t WHERE id = ?")
    assert res['type'] == 'prepare', res
    assert res['num_params'] == 1, res
    assert res['num_columns'] == 3, res
    assert [c['name'] for c in res['columns']] == ['id', 'name', 'd'], res
    # PREPARE 的静态元数据来自真实表定义：id 是整型（不是 VAR_STRING）
    assert res['columns'][0]['type'] in (mp.MYSQL_TYPE_LONG,
                                         mp.MYSQL_TYPE_LONGLONG), res
    sid = res['statement_id']

    got = client.stmt_execute(sid, [(mp.MYSQL_TYPE_LONGLONG, 5)])
    assert got['type'] == 'resultset', got
    assert got['rows'] == [(5, 'five', 5.5)], got

    # 第二次 EXECUTE 复用缓存的参数类型（new_params_bound_flag=0）
    got = client.stmt_execute(
        sid, [(mp.MYSQL_TYPE_LONGLONG, 6)], bind_types=False)
    assert got['type'] == 'resultset', got
    assert got['rows'] == [(6, 'six', 6.5)], got

    # 参数值不同 → 空结果集（而不是返回全表）
    got = client.stmt_execute(sid, [(mp.MYSQL_TYPE_LONGLONG, 999)])
    assert got['type'] == 'resultset' and got['rows'] == [], got


def test_prepare_select_star_metadata_and_zero_param(env):
    """`SELECT *` 的静态列元数据 + 0 参数语句（EXECUTE 不带参数数据）"""
    client = env['open']()
    _setup_table(client)
    client.query_ok(
        "INSERT INTO ps_t (id, big, name, d) VALUES (5, 123, 'five', 5.5)")

    res = client.prepare("SELECT * FROM ps_t")
    assert res['type'] == 'prepare', res
    assert res['num_params'] == 0, res
    assert res['num_columns'] == 8, res          # `*` 按真实表定义展开
    assert [c['name'] for c in res['columns']] == [
        'id', 'big', 'f', 'd', 'name', 'note', 'dt', 'day'], res

    # 0 参数的 EXECUTE：包体里没有 NULL bitmap / 类型表
    got = client.stmt_execute(res['statement_id'], [])
    assert got['type'] == 'resultset', got
    assert len(got['rows']) == 1, got
    row = got['rows'][0]
    assert row[0] == 5 and row[4] == 'five', got
    assert row[5] is None and row[6] is None and row[7] is None, got

    # 字面量参数（无表 SELECT）：`SELECT ?` → 二进制结果集返回参数值本身
    res = client.prepare("SELECT ? AS value")
    assert res['num_params'] == 1, res
    got = client.stmt_execute(res['statement_id'], [
        (mp.MYSQL_TYPE_LONGLONG, 42)])
    assert got['type'] == 'resultset' and got['rows'] == [(42,)], got

    # 占位符不在 WHERE（而在 LIMIT）也要被正确内联
    res = client.prepare("SELECT id FROM ps_t ORDER BY id LIMIT ?")
    assert res['num_params'] == 1, res
    got = client.stmt_execute(res['statement_id'],
                              [(mp.MYSQL_TYPE_LONG, 1)])
    assert got['type'] == 'resultset' and got['rows'] == [(5,)], got


def test_prepare_update_delete_return_affected(env):
    """预处理 UPDATE/DELETE：OK 包的 affected_rows 是真实行数"""
    client = env['open']()
    _setup_table(client)
    client.query_ok(
        "INSERT INTO ps_t (id, name) VALUES (1, 'a'), (2, 'b'), (3, 'c')")

    upd = client.prepare("UPDATE ps_t SET name = ? WHERE id = ?")
    result = client.stmt_execute(
        upd['statement_id'],
        [(mp.MYSQL_TYPE_STRING, 'changed'), (mp.MYSQL_TYPE_LONG, 2)])
    assert result['type'] == 'ok' and result['affected'] == 1, result
    assert client.query_rows(
        "SELECT name FROM ps_t WHERE id = 2") == [('changed',)]

    dele = client.prepare("DELETE FROM ps_t WHERE id = ?")
    result = client.stmt_execute(dele['statement_id'],
                                 [(mp.MYSQL_TYPE_LONG, 3)])
    assert result['type'] == 'ok' and result['affected'] == 1, result
    assert client.query_rows(
        "SELECT COUNT(*) FROM ps_t") == [('2',)]
    # 不存在的行 → affected=0（不是报错）
    result = client.stmt_execute(dele['statement_id'],
                                 [(mp.MYSQL_TYPE_LONG, 99)])
    assert result['type'] == 'ok' and result['affected'] == 0, result


def test_execute_null_bitmap_multibyte_and_long_string(env):
    """10 个参数：NULL bitmap 跨 2 字节；长字符串走 2 字节长度前缀"""
    client = env['open']()
    placeholder = ', '.join(f'? AS p{i}' for i in range(10))
    res = client.prepare('SELECT ' + placeholder)
    assert res['type'] == 'prepare', res
    assert res['num_params'] == 10, res
    long_text = '长文本-' + ('x' * 300)      # > 250 → lenenc 0xfc 前缀
    got = client.stmt_execute(res['statement_id'], [
        (mp.MYSQL_TYPE_NULL, None),          # 第 1 个 NULL（bitmap 第 1 字节）
        (mp.MYSQL_TYPE_LONG, 1),
        (mp.MYSQL_TYPE_STRING, long_text),
        (mp.MYSQL_TYPE_LONGLONG, -2),
        (mp.MYSQL_TYPE_DOUBLE, 3.5),
        (mp.MYSQL_TYPE_TINY, 4),
        (mp.MYSQL_TYPE_SHORT, 5),
        (mp.MYSQL_TYPE_VAR_STRING, 'seven'),
        (mp.MYSQL_TYPE_LONG, 8),
        (mp.MYSQL_TYPE_NULL, None),          # 第 10 个 NULL（bitmap 第 2 字节）
    ])
    assert got['type'] == 'resultset', got
    assert len(got['rows']) == 1, got
    row = got['rows'][0]
    assert len(row) == 10, row
    assert row[0] is None, row
    assert row[1] == 1 and row[3] == -2, row
    assert row[2] == long_text, (len(row[2] or ''), row[:3])
    assert row[4] == 3.5 and row[5] == 4 and row[6] == 5, row
    assert row[7] == 'seven' and row[8] == 8, row
    assert row[9] is None, row


# ==================== 3. 引号/注释里的 `?` 不计数 ====================

def test_placeholders_in_strings_and_comments_ignored(env):
    """`?` 在字符串/注释里只是普通字符：不计数、不被替换"""
    client = env['open']()
    _setup_table(client)

    sql = "INSERT INTO ps_t (id, note) VALUES (?, 'q?mark')"
    res = client.prepare(sql)
    assert res['type'] == 'prepare', res
    assert res['num_params'] == 1, (sql, res)
    sid = res['statement_id']
    assert client.stmt_execute(sid, [(mp.MYSQL_TYPE_LONG, 100)])['type'] \
        == 'ok'
    assert client.query_rows(
        "SELECT note FROM ps_t WHERE id = 100") == [('q?mark',)]

    sql2 = ("SELECT id FROM ps_t WHERE id = ? "
            "/* ? in block comment */")
    res2 = client.prepare(sql2)
    assert res2['type'] == 'prepare', res2
    # 注释里的 `?` 不计数（只做静态断言：本引擎执行器的 WHERE 解析不支持
    # 尾随注释，这里验证的是"PREPARE 不把它当占位符"这一件事）
    assert res2['num_params'] == 1, (sql2, res2)

    # 行注释里的 `?`（`-- ` 到行尾、`#` 到行尾）同样不算
    res3 = client.prepare("SELECT id FROM ps_t WHERE id = ? -- ?\n")
    assert res3['type'] == 'prepare', res3
    assert res3['num_params'] == 1, res3
    res4 = client.prepare("SELECT id FROM ps_t WHERE id = ? # ?\n")
    assert res4['type'] == 'prepare', res4
    assert res4['num_params'] == 1, res4
    # 反引号里的 `?` 也不是占位符（标识符里出现的 `?` 不计数）
    res5 = client.prepare("SELECT `id` FROM ps_t WHERE `id` = ?")
    assert res5['type'] == 'prepare', res5
    assert res5['num_params'] == 1, res5


# ==================== 4. 错误路径：1210 / 1243 / 1235 ====================

def test_param_count_mismatch_returns_1210(env):
    """EXECUTE 参数个数与占位符不符 → 1210（且语句之后仍可正常执行）"""
    client = env['open']()
    _setup_table(client)
    client.query_ok("INSERT INTO ps_t (id) VALUES (1)")
    res = client.prepare("SELECT id FROM ps_t WHERE id = ?")
    sid = res['statement_id']

    # 多给一个参数：服务端解析完 1 个参数后还有多余字节
    err = client.stmt_execute(
        sid, [(mp.MYSQL_TYPE_LONG, 1), (mp.MYSQL_TYPE_LONG, 2)])
    assert err['type'] == 'error', err
    assert err['code'] == 1210, err
    assert err['sqlstate'] == 'HY000', err

    # 包被截断（只声明类型、没有参数字节）
    extra = (b'\x00' + struct.pack('<I', 1) + b'\x00' + b'\x01'
             + bytes([mp.MYSQL_TYPE_LONG, 0]))
    err = client.stmt_execute_raw(sid, extra)
    assert err['type'] == 'error' and err['code'] == 1210, err

    # 包体短到连 flags/iteration_count 都没有（只有 statement_id）
    err = client.stmt_execute_raw(sid, b'')
    assert err['type'] == 'error' and err['code'] == 1210, err

    # 报错之后语句仍然可用
    got = client.stmt_execute(sid, [(mp.MYSQL_TYPE_LONG, 1)])
    assert got['type'] == 'resultset' and got['rows'] == [(1,)], got


def test_unknown_and_closed_statement_returns_1243(env):
    """未知 statement_id / CLOSE 之后 → 1243；CLOSE 本身无响应包"""
    client = env['open']()
    _setup_table(client)

    # 未知 id：EXECUTE / RESET 都报 1243
    err = client.stmt_execute(99, [(mp.MYSQL_TYPE_LONG, 1)])
    assert err['type'] == 'error' and err['code'] == 1243, err
    err = client.stmt_reset(99)
    assert err['type'] == 'error' and err['code'] == 1243, err

    # PREPARE → CLOSE → EXECUTE 必须 1243
    res = client.prepare("SELECT id FROM ps_t WHERE id = ?")
    sid = res['statement_id']
    client.stmt_close(sid)
    # CLOSE 没有响应包：紧接着的文本查询如果读到一个"多余 OK"就会解析错，
    # 因此这条断言同时验证 COM_STMT_CLOSE 没有多发包。
    rows = client.query_rows("SELECT 1")
    assert rows == [('1',)], rows
    err = client.stmt_execute(sid, [(mp.MYSQL_TYPE_LONG, 1)])
    assert err['type'] == 'error' and err['code'] == 1243, err
    assert str(sid) in err['message'], err


def test_unsupported_param_type_returns_1235(env):
    """BIT 参数类型 → 显式 1235（不静默按字符串/位图猜一个值）"""
    client = env['open']()
    _setup_table(client)
    res = client.prepare("SELECT id FROM ps_t WHERE id = ?")
    err = client.stmt_execute(
        res['statement_id'], [(mp.MYSQL_TYPE_BIT, b'\x01')])
    assert err['type'] == 'error', err
    assert err['code'] == 1235, err
    assert err['sqlstate'] == '42000', err
    assert 'BIT' in err['message'], err

    # 非 UTF-8 的 BLOB 参数：本引擎以文本字面量内联参数，无法无损表达
    # 二进制内容 → 显式 1235（绝不有损 decode 后静默写错数据）
    err = client.stmt_execute(
        res['statement_id'],
        [(mp.MYSQL_TYPE_BLOB, b'\xff\xfe\x00\x01')])
    assert err['type'] == 'error', err
    assert err['code'] == 1235, err
    assert 'UTF-8' in err['message'], err

    # 报错之后语句仍可正常执行
    got = client.stmt_execute(res['statement_id'],
                              [(mp.MYSQL_TYPE_LONG, 1)])
    assert got['type'] == 'resultset', got


# ==================== 5. 多语句并存 / RESET / 文本协议回归 ====================

def test_multiple_statements_and_reset(env):
    """同连接多个 prepared statement 并存；RESET 清参数类型后可复用"""
    client = env['open']()
    _setup_table(client)
    client.query_ok(
        "INSERT INTO ps_t (id, name) VALUES (5, 'five'), (6, 'six')")

    by_id = client.prepare("SELECT name FROM ps_t WHERE id = ?")
    by_name = client.prepare("SELECT id FROM ps_t WHERE name = ?")
    assert by_id['statement_id'] != by_name['statement_id']

    got = client.stmt_execute(
        by_id['statement_id'], [(mp.MYSQL_TYPE_LONG, 5)])
    assert got['rows'] == [('five',)], got
    got = client.stmt_execute(
        by_name['statement_id'], [(mp.MYSQL_TYPE_STRING, 'six')])
    assert got['rows'] == [(6,)], got
    # 两个语句的缓存参数类型互不影响
    got = client.stmt_execute(
        by_id['statement_id'], [(mp.MYSQL_TYPE_LONG, 6)])
    assert got['rows'] == [('six',)], got

    # RESET：清参数类型缓存 → 再用 new_params_bound_flag=0 必须 1210
    assert client.stmt_reset(by_id['statement_id'])['type'] == 'ok'
    err = client.stmt_execute(
        by_id['statement_id'], [(mp.MYSQL_TYPE_LONG, 5)],
        bind_types=False)
    assert err['type'] == 'error' and err['code'] == 1210, err
    # 重新绑定类型后照常复用
    got = client.stmt_execute(
        by_id['statement_id'], [(mp.MYSQL_TYPE_LONG, 5)])
    assert got['rows'] == [('five',)], got


def test_send_long_data_unsupported_is_explicit(env):
    """COM_STMT_SEND_LONG_DATA 未实现：EXECUTE 显式 1235，且可恢复"""
    client = env['open']()
    _setup_table(client)
    res = client.prepare("INSERT INTO ps_t (id, note) VALUES (?, ?)")
    sid = res['statement_id']

    # 长数据命令本身按协议无响应；服务端只登记"收到过长数据"
    client.stmt_send_long_data(sid, 1, 'x' * 200)
    err = client.stmt_execute(
        sid, [(mp.MYSQL_TYPE_LONG, 1), (mp.MYSQL_TYPE_STRING, 'n')])
    assert err['type'] == 'error', err
    assert err['code'] == 1235, err
    assert 'LONG_DATA' in err['message'].upper(), err

    # 长数据标记已被消费：再用正常 EXECUTE 可以成功
    ok = client.stmt_execute(
        sid, [(mp.MYSQL_TYPE_LONG, 2), (mp.MYSQL_TYPE_STRING, 'ok')])
    assert ok['type'] == 'ok' and ok['affected'] == 1, ok
    assert client.query_rows(
        "SELECT id, note FROM ps_t ORDER BY id") == [('2', 'ok')]

    # RESET 也会清掉长数据标记
    client.stmt_send_long_data(sid, 0, 'y' * 10)
    assert client.stmt_reset(sid)['type'] == 'ok'
    ok = client.stmt_execute(
        sid, [(mp.MYSQL_TYPE_LONG, 3), (mp.MYSQL_TYPE_STRING, 'after-reset')])
    assert ok['type'] == 'ok' and ok['affected'] == 1, ok


def test_prepared_statements_are_connection_local(env):
    """prepared statement 是**连接级**资源：别的连接看不到本连接的语句"""
    first = env['open']()
    second = env['open']()
    _setup_table(first)
    first.query_ok("INSERT INTO ps_t (id, name) VALUES (1, 'one')")

    res = first.prepare("SELECT id FROM ps_t WHERE id = ?")
    err = second.stmt_execute(res['statement_id'],
                              [(mp.MYSQL_TYPE_LONG, 1)])
    assert err['type'] == 'error' and err['code'] == 1243, err

    # 第二条连接自己 PREPARE，互不影响
    res2 = second.prepare("SELECT 1 AS one")
    got = second.stmt_execute(res2['statement_id'], [])
    assert got['type'] == 'resultset' and got['rows'] == [(1,)], got
    # 第一条连接的语句仍可用
    got = first.stmt_execute(res['statement_id'], [(mp.MYSQL_TYPE_LONG, 1)])
    assert got['type'] == 'resultset' and got['rows'] == [(1,)], got


def test_prepared_dml_participates_in_transaction(env):
    """预处理 DML 与文本协议共用连接级 session：事务回滚真的撤销写入"""
    client = env['open']()
    _setup_table(client)
    res = client.prepare("INSERT INTO ps_t (id, name) VALUES (?, ?)")
    sid = res['statement_id']

    client.query_ok('BEGIN')
    ok = client.stmt_execute(
        sid, [(mp.MYSQL_TYPE_LONG, 50), (mp.MYSQL_TYPE_STRING, 'txn')])
    assert ok['type'] == 'ok', ok
    # 同一连接（同一事务）里文本查询可见
    assert client.query_rows(
        "SELECT name FROM ps_t WHERE id = 50") == [('txn',)]
    client.query_ok('ROLLBACK')
    # 回滚后不可见
    assert client.query_rows(
        "SELECT name FROM ps_t WHERE id = 50") == []


def test_prepare_privilege_checked_at_prepare_and_execute(env):
    """PREPARE 的静态元数据受权限约束；DML 在执行期鉴权（与文本协议同口径）"""
    root = env['open']()
    _setup_table(root)
    root.query_ok("INSERT INTO ps_t (id, name) VALUES (1, 'one')")
    root.query_ok(
        "CREATE USER 'ps_reader' IDENTIFIED BY 'readerpass'")
    reader = env['open'](user='ps_reader', password='readerpass')

    # 没有任何权限：PREPARE SELECT 在**准备阶段**就 1142，
    # 而不是先返回列定义（否则受限用户能用 PREPARE 探表结构）
    denied = reader.prepare("SELECT id, name FROM ps_t WHERE id = ?")
    assert denied['type'] == 'error', denied
    assert denied['code'] == 1142, denied

    # 授予库级 SELECT 后，同一语句 PREPARE/EXECUTE 正常
    root.query_ok("GRANT SELECT ON default.* TO 'ps_reader'")
    res = reader.prepare("SELECT id FROM ps_t WHERE id = ?")
    assert res['type'] == 'prepare', res
    got = reader.stmt_execute(res['statement_id'],
                              [(mp.MYSQL_TYPE_LONG, 1)])
    assert got['type'] == 'resultset' and got['rows'] == [(1,)], got

    # DML 权限在**执行期**检查：PREPARE 成功（不读数据、不执行 DML），
    # EXECUTE 时被 1142 拒绝，且数据确实没有被写入
    ins = reader.prepare("INSERT INTO ps_t (id) VALUES (?)")
    assert ins['type'] == 'prepare', ins
    err = reader.stmt_execute(ins['statement_id'],
                              [(mp.MYSQL_TYPE_LONG, 2)])
    assert err['type'] == 'error', err
    assert err['code'] == 1142, err
    assert root.query_rows(
        "SELECT COUNT(*) FROM ps_t") == [('1',)]


def test_text_protocol_still_works_after_prepared(env):
    """开启过 prepared 之后，文本查询协议（COM_QUERY）仍完全正常"""
    client = env['open']()
    _setup_table(client)

    # 文本 CREATE/INSERT/SELECT
    assert client.query("SELECT 1")['rows'] == [('1',)]
    client.query_ok("INSERT INTO ps_t (id, name) VALUES (1, 'text')")

    res = client.prepare("INSERT INTO ps_t (id, name) VALUES (?, ?)")
    assert client.stmt_execute(
        res['statement_id'],
        [(mp.MYSQL_TYPE_LONG, 2), (mp.MYSQL_TYPE_STRING, 'prepared')]
    )['type'] == 'ok'

    # 文本查询能看到 prepared 写入的行
    rows = client.query_rows(
        "SELECT id, name FROM ps_t ORDER BY id")
    assert rows == [('1', 'text'), ('2', 'prepared')], rows
    # 再走一轮文本 → prepared → 文本
    assert client.query("SELECT 'still-text' AS s")['rows'] == [
        ('still-text',)]
    res = client.prepare("SELECT id FROM ps_t WHERE id = ?")
    assert client.stmt_execute(
        res['statement_id'], [(mp.MYSQL_TYPE_LONG, 1)])['rows'] == [(1,)]
    assert client.query("SELECT 2")['rows'] == [('2',)]


# ==================== 6. 真实 server-side prepare 客户端（可选） ====================

def test_mysql_connector_server_side_prepare(env):
    """如装了 mysql-connector-python，用真实 server-side prepare 客户端验证

    本仓库依赖里没有 mysql-connector；环境缺省时 skip（不把"没装库"
    伪装成通过）。已装环境会真正走 COM_STMT_* 路径：

      * PREPARE 响应（statement_id / 列定义 / 参数定义）能被真实客户端解析；
      * EXECUTE 二进制参数（int/float/str/None/中文/带引号）被正确内联；
      * SELECT 的**二进制结果集**被真实客户端解码（含 NULL）；
      * 同一条 SQL 重复执行（客户端复用 prepared statement）、UPDATE/DELETE
        的 affected rows、CLOSE 无响应包、文本协议并存。
    """
    connector = pytest.importorskip('mysql.connector')
    conn = track(connector.connect(
        host='127.0.0.1', port=env['port'], user='root', password='',
        database='default', autocommit=True, connection_timeout=10))
    cur = conn.cursor(prepared=True)
    try:
        cur.execute("CREATE TABLE mc_ps ("
                    "id INT PRIMARY KEY, name VARCHAR(50),"
                    "score DOUBLE, note VARCHAR(50))")
        cur.execute("INSERT INTO mc_ps VALUES (?, ?, ?, ?)",
                    (1, '张三', 1.5, None))
        cur.execute("INSERT INTO mc_ps VALUES (?, ?, ?, ?)",
                    (2, "O'Brien", -2.25, 'x'))
        cur.execute("INSERT INTO mc_ps VALUES (?, ?, ?, ?)",
                    (3, 'b?b', 3.0, None))

        # 同一条 SELECT 文本第二次执行时客户端会复用 prepared statement
        cur.execute("SELECT id, name FROM mc_ps ORDER BY id")
        assert cur.fetchall() == [(1, '张三'), (2, "O'Brien"), (3, 'b?b')]
        cur.execute("SELECT id, name FROM mc_ps ORDER BY id")
        assert cur.fetchall() == [(1, '张三'), (2, "O'Brien"), (3, 'b?b')]

        # 带参数 SELECT + NULL 列（二进制结果集的 NULL bitmap）
        cur.execute("SELECT id, note FROM mc_ps WHERE id = ?", (1,))
        assert cur.fetchall() == [(1, None)]

        # UPDATE / DELETE 的 affected rows 真实
        cur.execute("UPDATE mc_ps SET score = ? WHERE id = ?", (9.5, 1))
        assert cur.rowcount == 1, cur.rowcount
        cur.execute("DELETE FROM mc_ps WHERE id = ?", (3,))
        assert cur.rowcount == 1, cur.rowcount
        cur.close()

        # 文本协议在同一条连接上仍正常（回归）
        text_cur = conn.cursor()
        try:
            text_cur.execute("SELECT COUNT(*) FROM mc_ps")
            assert text_cur.fetchone() == (2,), text_cur.fetchone()
        finally:
            text_cur.close()
    finally:
        try:
            cur.close()        # 已关闭时再 close 在 sqlite 连接器里是 no-op/可忽略
        except Exception:      # noqa: BLE001 - 清理兜底
            pass
        conn.close()
