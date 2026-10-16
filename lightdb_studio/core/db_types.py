"""「数据库类型」下拉的**纯函数表**（GUI 无关、零 Qt 依赖、可单测）。

为什么需要它
------------
`ConnectionInfo` 的默认端口是 **8888**，而本机 LightDB 服务端默认监听
**8899**；新建连接对话框直接用 8888 预填 → 用户点「连接」就撞上
``127.0.0.1:8888 上没有服务端在监听（连接被拒绝…）``。这里把「类型 →
默认值」集中成一张表，对话框与测试共用同一份事实，不再各写一遍。

支持面（**不假装能连**）
------------------------
本 Studio 只有 `pymysql` 一个驱动，全部连接都走 MySQL 协议：

============  ==========================================================
kind          说明
============  ==========================================================
lightdb       本引擎（MySQL 协议），本机默认端口 8899 —— **默认选中**
mysql /       MySQL 系（端口 3306）
mariadb
mysql_compat  其它 MySQL 兼容（PolarDB/TiDB/OceanBase…）：只加提示，
              默认值保留引擎自己的语义（端口 3306），用户按需改
postgresql /  协议不同、没有驱动 → `supported=False`，对话框里禁用「确定」
oracle /
sqlite
============  ==========================================================

类型**不落盘**：`connections.json` 的 schema 不变（每条恰好 8 键），
重开对话框时靠 :func:`kind_for_port` 从端口反推。反推规则
（`kind_for_port`）::

    8899 -> lightdb          3306 -> mysql
    其它  -> mysql_compat    取不到端口 -> None

反推有歧义（3306 上可能是 MariaDB），所以它只用来**预选**下拉项，
绝不回写配置。
"""
from types import MappingProxyType
from typing import Dict, List, Optional, Tuple

#: 本机 LightDB 服务端的默认监听端口（`lightdb.main --mode single` 默认 8899）。
#: 注意与 `ConnectionInfo.port` 的历史默认值 8888 的区别：那个是数据类默认，
#: 这里给的是「用户照抄就能连上本机服务端」的端口。
LIGHTDB_LOCAL_PORT = 8899

#: MySQL 系的默认端口（MySQL / MariaDB / 兼容引擎）。
MYSQL_PORT = 3306

#: 「其它 MySQL 兼容」的 kind（不做能力承诺，只加提示）。
COMPAT_KIND = 'mysql_compat'

#: 掉进「其它 MySQL 兼容」时字段的落点（端口仍是 MySQL 系的 3306）。
_COMPAT_FALLBACK = MappingProxyType({
    'host': '127.0.0.1',
    'port': MYSQL_PORT,
    'user': 'root',
    'password': '',
    'database': '',
    'charset': 'utf8mb4',
})


def _entry(kind: str, display: str, supported: bool,
           defaults: Dict[str, object], reason: str = '') -> dict:
    """一条类型定义（内部构造用；缺省字段补空串，保证键集一致）。"""
    full = {
        'host': '127.0.0.1', 'port': MYSQL_PORT, 'user': 'root',
        'password': '', 'database': '', 'charset': 'utf8mb4',
    }
    full.update(defaults)
    return {
        'kind': kind,
        'display': display,
        'supported': supported,
        'reason': reason,          # 不支持的原因 / 支持的边界说明（永不为 None）
        'defaults': MappingProxyType(full),
    }


#: 类型表：**唯一事实来源**（键 = kind，顺序 = 下拉展示顺序）。
#: 每条恰好 5 个键：kind / display / supported / reason / defaults。
DB_TYPES: Dict[str, dict] = {
    'lightdb': _entry(
        'lightdb', 'LightDB（本机）', True,
        {'port': LIGHTDB_LOCAL_PORT, 'user': 'root', 'password': ''},
        '本机服务端默认监听 8899；对话框里那一行提示会给出可照抄的启动命令。'),
    'mysql': _entry(
        'mysql', 'MySQL', True,
        {'port': MYSQL_PORT},
        'MySQL 协议；本 Studio 用 pymysql 连接，请确认目标主机已启动 MySQL。'),
    'mariadb': _entry(
        'mariadb', 'MariaDB', True,
        {'port': MYSQL_PORT},
        '与 MySQL 同协议同端口（3306）；3306 若实际是 MariaDB，按本项选即可。'),
    COMPAT_KIND: _entry(
        COMPAT_KIND, '其它 MySQL 兼容（PolarDB/TiDB 等）', True,
        dict(_COMPAT_FALLBACK),
        '能连的条件是对方说 MySQL 协议；保留当前已填的值，只提示，不保证兼容。'),
    'postgresql': _entry(
        'postgresql', 'PostgreSQL（暂不支持）', False, {},
        '本 Studio 只有 pymysql 驱动，走 MySQL 协议；连 PostgreSQL 需要 psycopg2 '
        '等驱动，当前没有，所以连不上。'),
    'oracle': _entry(
        'oracle', 'Oracle（暂不支持）', False, {},
        '本 Studio 只有 pymysql 驱动，走 MySQL 协议；连 Oracle 需要 cx_Oracle/'
        'oracledb 驱动，当前没有，所以连不上。'),
    'sqlite': _entry(
        'sqlite', 'SQLite（暂不支持）', False, {},
        'SQLite 是本地文件、没有网络协议，本 Studio 的连接层是网络连接'
        '（host/port），没有 sqlite3 文件驱动，所以连不上。'),
}

#: 默认类型（对话框打开时选中它）。
DEFAULT_KIND = 'lightdb'

#: 字段顺序与 `ConnectionInfo` 一致（测试/文档用）。
FIELD_ORDER: Tuple[str, ...] = (
    'name', 'host', 'port', 'user', 'password', 'database', 'charset', 'ssl')


def kinds() -> List[str]:
    """类型 key 列表（顺序即下拉展示顺序，`lightdb` 第一）。"""
    return list(DB_TYPES)


def get(kind: Optional[str]) -> Optional[dict]:
    """按 kind 取定义；取不到返回 None（不抛异常，不留半截状态）。"""
    return DB_TYPES.get(str(kind or ''))


def display_name(kind: Optional[str]) -> str:
    """下拉里显示的名字（未知 kind 退回「其它 MySQL 兼容」的名字）。"""
    entry = get(kind) or DB_TYPES[COMPAT_KIND]
    return str(entry['display'])


def is_supported(kind: Optional[str]) -> bool:
    """这个类型当前能不能连（False = 对话框要禁用「确定」并说明原因）。"""
    entry = get(kind) or DB_TYPES[COMPAT_KIND]
    return bool(entry['supported'])


def unsupported_reason(kind: Optional[str]) -> str:
    """不支持的具体原因（支持的 kind 返回空串）。"""
    entry = get(kind)
    if entry is None or entry['supported']:
        return ''
    return str(entry['reason'])


def note(kind: Optional[str]) -> str:
    """类型提示（有用的边界说明；任何 kind 都不会取不到）。"""
    entry = get(kind) or DB_TYPES[COMPAT_KIND]
    return str(entry['reason'])


def defaults_for(kind: Optional[str]) -> Dict[str, object]:
    """该类型的默认字段值 —— **只含 6 个连接字段，绝不含 name/ssl**。

    未知 kind 退回「其它 MySQL 兼容」的默认值（安全方向：端口 3306 而不是
    编一个看起来像的端口）。
    """
    entry = get(kind) or DB_TYPES[COMPAT_KIND]
    return dict(entry['defaults'])


def default_port_for(kind: Optional[str]) -> int:
    """该类型的默认端口（提示行/表单预填用）。"""
    return int(defaults_for(kind)['port'])


def kind_for_port(port: Optional[int]) -> Optional[str]:
    """按端口**反推**类型（类型不落盘，重开对话框时用它预选）。

    规则（写死、可测，不做猜测性扩展）::

        8899 -> lightdb      3306 -> mysql
        其它数值 -> mysql_compat        取不到/非法 -> None

    歧义说明：3306 到底是 MySQL 还是 MariaDB 从端口看不出来，所以这里返回
    `mysql`（同协议同默认值），用户可自行改选；反推结果**只用于预选下拉**，
    不写进 `connections.json`。
    """
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    if port == LIGHTDB_LOCAL_PORT:
        return 'lightdb'
    if port == MYSQL_PORT:
        return 'mysql'
    return COMPAT_KIND


def saved_kind_for_port(port: Optional[int]) -> Optional[str]:
    """给 `ConnectionInfo` 用的别名：**没落盘类型时**按端口反推。

    与 `kind_for_port` 同一实现，只是名字在「打开对话框」的场景更直白。
    """
    return kind_for_port(port)


def current_kind(combo) -> Optional[str]:
    """Qt 下拉的当前 kind（`currentData()` 的薄封装，便于测试传假对象）。

    取不到数据时退回 `DEFAULT_KIND`，保证对话框永远有一个确定的状态。
    """
    try:
        code = combo.currentData()
    except Exception:                                     # noqa: BLE001
        return DEFAULT_KIND
    return code if code in DB_TYPES else DEFAULT_KIND


def run_hint_text(kind: Optional[str], host: str, port: int,
                  start_command: str) -> str:
    """类型 + 主机 + 端口 → 对话框里那一行即时提示（**纯文本、单行**）。

    只有 LightDB 才说「本机服务端没起来就照抄这条命令」——MySQL/MariaDB/
    兼容引擎的启动方式不是本引擎的命令，绝不冒充；不支持的类型直接给原因。
    """
    if not is_supported(kind):
        return '暂不支持：%s' % unsupported_reason(kind)
    if kind == 'lightdb':
        return ('LightDB（本机）当前将连接 %s:%s；若本机服务端未运行，在项目目录'
                '里照抄这条命令启动：%s' % (host, port, start_command))
    return ('%s：将连接 %s:%s —— 目标端口没有服务端在监听时，会出现「连接被拒绝」；'
            '本 Studio 不能替你启动它，请先确认远端服务端已在运行（%s）。'
            % (display_name(kind), host, port, note(kind)))


def validate_table() -> List[str]:
    """自检类型表；返回问题列表（空 = 表合法）。测试与开发期都能调。"""
    problems: List[str] = []
    if DEFAULT_KIND not in DB_TYPES:
        problems.append('默认类型 %r 不在表里' % DEFAULT_KIND)
    if kinds()[0] != DEFAULT_KIND:
        problems.append('默认类型不是下拉第一项')
    for kind, entry in DB_TYPES.items():
        if set(entry) != {'kind', 'display', 'supported', 'reason', 'defaults'}:
            problems.append('%s: 键集异常 %s' % (kind, sorted(entry)))
        if entry['kind'] != kind:
            problems.append('%s: kind 与键不一致' % kind)
        if not str(entry['display']).strip():
            problems.append('%s: 显示名为空' % kind)
        if not str(entry['reason']).strip():
            problems.append('%s: 没有说明（reason 为空）' % kind)
        if set(entry['defaults']) - set(FIELD_ORDER):
            problems.append('%s: 默认值里有非连接字段 %s'
                            % (kind, sorted(set(entry['defaults']) - set(FIELD_ORDER))))
        if not entry['supported'] and '暂不支持' not in entry['display']:
            problems.append('%s: 不支持却没在下拉里标注「暂不支持」' % kind)
    if default_port_for('lightdb') != LIGHTDB_LOCAL_PORT:
        problems.append('lightdb 默认端口不是 %d' % LIGHTDB_LOCAL_PORT)
    if default_port_for('mysql') != MYSQL_PORT:
        problems.append('mysql 默认端口不是 %d' % MYSQL_PORT)
    return problems
