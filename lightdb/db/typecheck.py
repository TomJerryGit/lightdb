"""声明式类型约束校验（P1 修复 C：范围 / 长度 / 日期 / 精度边界）

为什么单独一个模块：`db/table.py::coerce()` 原来只做 `int()/float()/str()`
转换，于是 **INT 越界（2147483648）、VARCHAR(3) 超长（'abcdef'）、非法
DATETIME（'not-a-date'）、DECIMAL(5,2) 的 1.999 / 1234.56** 全部静默入库
并原样读回 —— 声明了约束却完全不生效。规则本身（范围表、多格式日期、
按字符数而不是字节数、DECIMAL 精度/标度）放进 `table.py` 会继续堆高那个
已经很长的存储文件，所以集中到本模块，`coerce()` 保持薄。

约定（与项目"要么真实、要么显式报错"的口径一致）：

* 合法值通过校验，并返回**转换后的值**（与旧 `coerce` 的返回类型一致：
  整数列 `int`、浮点/定点列 `float`、字符/时间列 `str`），调用方无需再转；
* 非法值抛 `TypeCheckError`（`ValueError` 子类）—— **异常自带 MySQL 语义
  的 `errno` / `sqlstate`**，`sql/executor.py` 的 ValueError 分支优先取这
  两个属性，不再按报文猜错误码（旧实现会把 NOT NULL 违反说成 1062）；
* `None` 原样返回（NULL 合法性由 `NOT NULL` / 主键约束另行判断，
  `table.py::_insert_locked` / `_apply_set_pairs`）；
* **认不出的类型保持现状**（`str(value)`）：本模块只校验能识别的声明，
  绝不对不认识的类型编造规则（那会把"没实现"变成"暗规则"）。

错误码对照（MySQL）：

===================  =========  ======================================
errno  / sqlstate     名称        本模块对应子类
===================  =========  ======================================
1264   / 22003        Out of range   `OutOfRangeError`（整数越界、DECIMAL 精度/标度）
1406   / 22001        Data too long  `DataTooLongError`（VARCHAR/CHAR 字符数超限）
1292   / 22007        Incorrect datetime value  `InvalidDatetimeError`
1366   / HY000        Incorrect integer value   `IncorrectIntegerError`
1048   / 23000        Column cannot be null     `NotNullViolation`
3140   / 22032        Invalid JSON text         `InvalidJsonError`
===================  =========  ======================================
"""
import datetime
import json
import math
import re
from decimal import Decimal, InvalidOperation


# ==================== 异常 ====================

class TypeCheckError(ValueError):
    """类型约束违反（errno / sqlstate 直接对应给客户端的错误包）

    为什么是 `ValueError` 子类：与 `Table.UniqueViolationError`、
    `DdlError` 的既有兼容策略一致 —— 旧调用方（例如
    `Database.alter_add_column` 的 `except (ValueError, TypeError)`、
    `Table._find_pks` / `_match` 的防御性 except）无需改动即可捕获；
    而执行器优先读 `errno` / `sqlstate` 属性，拿到的是**确定的**
    语义码，不是按报文猜出来的。
    """

    #: 默认按"取值不合法"归类（1366 / HY000）；子类覆盖成精确码。
    errno = 1366
    sqlstate = 'HY000'

    def __init__(self, message, errno=None, sqlstate=None):
        super().__init__(str(message))
        # 允许单点覆盖（例如 `raise TypeCheckError(msg, errno=1264,
        # sqlstate='22003')`），但子类按类属性给默认值，最常用路径不用传参。
        if errno is not None:
            self.errno = int(errno)
        if sqlstate is not None:
            self.sqlstate = str(sqlstate)

    @property
    def message(self) -> str:
        return str(self)


class OutOfRangeError(TypeCheckError):
    """1264 / 22003：整数越界、DECIMAL 精度/标度越界（MySQL Out of range）"""

    errno = 1264
    sqlstate = '22003'


class DataTooLongError(TypeCheckError):
    """1406 / 22001：VARCHAR(n)/CHAR(n) 超长（按**字符数**，不是字节数）"""

    errno = 1406
    sqlstate = '22001'


class InvalidDatetimeError(TypeCheckError):
    """1292 / 22007：DATE/DATETIME/TIMESTAMP/TIME 格式非法"""

    errno = 1292
    sqlstate = '22007'


class IncorrectIntegerError(TypeCheckError):
    """1366 / HY000：无法转换成整数的字符串（Incorrect integer value）"""

    errno = 1366
    sqlstate = 'HY000'


class NotNullViolation(TypeCheckError):
    """1048 / 23000：NOT NULL / 主键列收到 NULL（Column cannot be null）"""

    errno = 1048
    sqlstate = '23000'


class InvalidJsonError(TypeCheckError):
    """3140 / 22032：JSON 列收到非法 JSON 文本（Invalid JSON text）"""

    errno = 3140
    sqlstate = '22032'


# ==================== 规则表 ====================

#: 整数类型 -> (有符号下界, 有符号上界, UNSIGNED 下界, UNSIGNED 上界)
#: 与 MySQL 的 TINYINT/SMALLINT/MEDIUMINT/INT/BIGINT 一致；解析器把
#: SMALLINT/TINYINT/MEDIUMINT 归一成 INT，这里仍按真实声明校验 —— 直接
#: 用 Python API 建表（cols 里写 'TINYINT'）也受约束。
_INT_RANGES = {
    'TINYINT': (-128, 127, 0, 255),
    'SMALLINT': (-32768, 32767, 0, 65535),
    'MEDIUMINT': (-8388608, 8388607, 0, 16777215),
    'INT': (-2147483648, 2147483647, 0, 4294967295),
    'INTEGER': (-2147483648, 2147483647, 0, 4294967295),
    'BIGINT': (-9223372036854775808, 9223372036854775807,
               0, 18446744073709551615),
}

#: DATE/DATETIME/TIMESTAMP/TIME 用 `datetime.strptime` **多格式**尝试：
#: 只做"是不是一个合法日期"的判断，**不改变原值形态**（旧行为按字符串
#: 原样存储，读回/比较/日期函数都依赖原文，改成归一格式会引出新的不一致）。
_DATE_FORMATS = (
    '%Y-%m-%d',
    '%Y/%m/%d',
    '%Y.%m.%d',
    '%Y%m%d',
)

_DATETIME_FORMATS = (
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M:%S.%f',
    '%Y-%m-%d %H:%M',
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%dT%H:%M:%S.%f',
    '%Y/%m/%d %H:%M:%S',
    '%Y/%m/%d %H:%M',
    '%Y/%m/%d',
    '%Y%m%d%H%M%S',
    '%Y-%m-%d',
    '%Y%m%d',
)

_TIME_FORMATS = (
    '%H:%M:%S',
    '%H:%M:%S.%f',
    '%H:%M',
    '%H%M%S',
)

#: 大文本类：只做字符串化，没有长度上限（TEXT/BLOB 家族的声明本身就是
#: "不限长"；解析器已把 LONGTEXT/MEDIUMTEXT/TINYTEXT 归一成 TEXT，
#: 这里保留原名是给直接 API 建表兜底）。
_TEXT_TYPES = frozenset((
    'TEXT', 'TINYTEXT', 'MEDIUMTEXT', 'LONGTEXT',
    'BLOB', 'TINYBLOB', 'MEDIUMBLOB', 'LONGBLOB',
    'BINARY', 'VARBINARY',
))


def _type_parts(col_type) -> tuple:
    """拆分类型声明：返回 `(基类型, 括号里的整数参数列表, 是否 UNSIGNED)`

    兼容解析器保留的真实声明形态：`VARCHAR(20)`、`DECIMAL(10, 2)`、
    `INT(11) UNSIGNED`。类型名本身认不出时返回 `(None, [], False)`。
    """
    raw = str(col_type or '').strip().upper()
    m = re.match(r'^([A-Z_][A-Z0-9_]*)', raw)
    base = m.group(1) if m else None
    params = []
    pm = re.search(r'\(([^)]*)\)', raw)
    if pm:
        params = [int(x) for x in re.findall(r'\d+', pm.group(1))]
    unsigned = bool(re.search(r'\bUNSIGNED\b', raw))
    return base, params, unsigned


def _short(value, limit: int = 40) -> str:
    """报错里展示取值（截断，避免超长字符串把报文撑爆）"""
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + '…'
    return repr(text)


# ==================== 各类型校验 ====================

def _check_integer(value, base, ranges, unsigned):
    """整数范围校验 + 转换成 int

    转换语义与旧 `coerce` 保持一致（`int(value)`），**只补上**：
    无法转换的字符串 → 1366（原来抛 ValueError，执行器按报文猜成 1105）；
    超出类型范围 → 1264。
    """
    lo, hi = (ranges[2], ranges[3]) if unsigned else (ranges[0], ranges[1])
    if isinstance(value, bool):
        iv = int(value)
    elif isinstance(value, int):
        iv = value
    elif isinstance(value, float):
        # `int(1.9) == 1` 与旧实现一致（向零截断）；非有限值直接越界。
        if math.isnan(value) or math.isinf(value):
            raise OutOfRangeError(
                f"Out of range value for {base}{' UNSIGNED' if unsigned else ''}"
                f": 取值 {value!r} 不是有限数")
        iv = int(value)
    else:
        text = str(value).strip()
        try:
            iv = int(text)
        except (TypeError, ValueError):
            raise IncorrectIntegerError(
                f"无法将 {value!r} 转换为 {base}: "
                f"Incorrect integer value: {value!r}")
    if iv < lo or iv > hi:
        raise OutOfRangeError(
            f"Out of range value for {base}{' UNSIGNED' if unsigned else ''}"
            f": {iv}（取值范围 [{lo}, {hi}]）")
    return iv


def _check_chars(value, base, params):
    """VARCHAR(n)/CHAR(n)：按**字符数**校验（MySQL 语义；不是 UTF-8 字节数）

    `VARCHAR(3)` 收 `'中文'`（2 字符 / 6 字节）必须通过 —— 这是分析报告
    专门点名的验收项。没写长度的 `VARCHAR`（旧元数据可能没有参数）无法
    判定上限，保持现状不做长度校验。
    """
    s = value if isinstance(value, str) else str(value)
    if params:
        n = params[0]
        if len(s) > n:
            raise DataTooLongError(
                f"Data too long for {base}({n}): 取值 {_short(s)} 有 "
                f"{len(s)} 个字符（上限 {n}，按字符数计）")
    return s


def _check_temporal(value, base):
    """DATE/DATETIME/TIMESTAMP/TIME：多格式校验，合法后**保留原值形态**

    * 已经是 `datetime.date/datetime/time/datetime` 对象 → 合法（旧实现
      走 `str(value)` 存成标准文本，这里保持同样返回）；
    * 字符串/数字 → 用一组常见格式逐个 `strptime`，都失败报 1292；
    * NAME 不改写：读回、比较、日期函数都按原文处理，归一格式会引出新的
      不一致（例如 `DATE '2024/01/02'` 读回仍是 `'2024/01/02'`）。
    """
    if isinstance(value, datetime.datetime):
        return str(value)
    if isinstance(value, (datetime.date, datetime.time)):
        return str(value)
    text = str(value).strip()
    if base == 'DATE':
        formats = _DATE_FORMATS
    elif base == 'TIME':
        formats = _TIME_FORMATS
    else:
        formats = _DATETIME_FORMATS
    for fmt in formats:
        try:
            datetime.datetime.strptime(text, fmt)
            return str(value)
        except (ValueError, TypeError):
            continue
    raise InvalidDatetimeError(
        f"Incorrect datetime value: {_short(value)} for {base}"
        f"（支持的格式示例: {formats[0]}）")


def _check_decimal(value, params):
    """DECIMAL(p,s)/NUMERIC(p,s)：用 Decimal 校验总位数/小数位，返回 float

    保持旧 `coerce` 的返回类型（float）：行载荷是 JSON，Decimal 不能直接
    序列化；存储层与既有断言都按 float 处理。校验本身用 `Decimal(str(v))`，
    避免二进制浮点误差（`1.999` 不会被看成 1.998999...）。
    """
    try:
        if isinstance(value, bool):
            d = Decimal(int(value))
        elif isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                raise OutOfRangeError(
                    f"Out of range value for DECIMAL: {value!r}")
            d = Decimal(str(value))
        elif isinstance(value, Decimal):
            d = value
        else:
            d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise TypeCheckError(
            f"无法将 {value!r} 转换为 DECIMAL: Incorrect decimal value: "
            f"{value!r}")
    if not params:
        return float(d)
    precision = params[0]
    scale = params[1] if len(params) > 1 else 0
    sign, digits, exp = d.as_tuple()
    if exp >= 0:
        int_digits = len(digits) + exp
        frac_digits = 0
    else:
        frac_digits = -exp
        int_digits = len(digits) - frac_digits
        if int_digits < 0:
            int_digits = 0
    if frac_digits > scale or int_digits > precision - scale:
        raise OutOfRangeError(
            f"Out of range value for DECIMAL({precision},{scale}): {value!r}"
            f"（整数位上限 {precision - scale}，小数位上限 {scale}）")
    return float(d)


def _check_float(value, base):
    """FLOAT/DOUBLE：只做"能转成浮点数"的校验（旧实现同样不做范围限制）"""
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise TypeCheckError(
            f"无法将 {value!r} 转换为 {base}: Incorrect double value: "
            f"{value!r}")


def _check_boolean(value):
    """BOOLEAN：**保持旧行为**（字符串按 MySQL 的 1/TRUE/YES/ON 判真，其余
    非字符串走 `bool()`）。不改语义，免得把既有 `BOOLEAN` 用法整体改契约。"""
    if isinstance(value, str):
        return value.upper() in ('1', 'TRUE', 'YES', 'ON')
    return bool(value)


def _check_json(value):
    """JSON：字符串必须是合法 JSON 文本（否则 3140），非字符串保持现状

    本引擎把 JSON 按文本存储；合法性的唯一可依赖事实是"能不能被
    `json.loads` 解析"。非法文本存入后 `->` / `JSON_EXTRACT` 只会静默
    得到 None，属于"声明了 JSON 却存了非 JSON"的静默错值，因此显式报错。
    非字符串（dict/list）与旧实现一样交给 `str()`，不在这里改变存储形态。
    """
    if isinstance(value, str):
        try:
            json.loads(value)
        except (ValueError, TypeError) as e:
            raise InvalidJsonError(
                f"Invalid JSON text: {_short(value)}（{e}）")
        return value
    if isinstance(value, (dict, list)):
        # 直接 API 传入的结构：序列化成 JSON 文本（而不是 Python repr，
        # 那样存进去以后 JSON 函数解析不了）。
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ==================== 对外入口 ====================

def validate_value(value, col_type):
    """按列声明校验一个写入值，返回转换后的值；违规抛 `TypeCheckError`

    这是存储层唯一的"声明 -> 规则"入口（`Table.coerce` 委托到这里，
    INSERT / UPDATE / DEFAULT 填充全部经过它）。`None` 原样返回 ——
    NULL 是否合法由 NOT NULL / 主键约束判断，不在类型校验里越权。
    """
    if value is None:
        return None
    base, params, unsigned = _type_parts(col_type)
    if not base:
        # 与旧 `coerce` 一致：类型名本身无法解析就显式报错，绝不把任意值
        # 当字符串接受（那是静默的类型欺骗）。
        raise ValueError(f"无法识别的列类型: {col_type!r}")

    if base in _INT_RANGES:
        return _check_integer(value, base, _INT_RANGES[base], unsigned)
    if base in ('VARCHAR', 'CHAR'):
        return _check_chars(value, base, params)
    if base in _TEXT_TYPES:
        return value if isinstance(value, str) else str(value)
    if base in ('DATE', 'DATETIME', 'TIMESTAMP', 'TIME'):
        return _check_temporal(value, base)
    if base in ('DECIMAL', 'NUMERIC', 'FIXED'):
        return _check_decimal(value, params)
    if base in ('FLOAT', 'DOUBLE', 'REAL'):
        return _check_float(value, base)
    if base in ('BOOLEAN', 'BOOL'):
        return _check_boolean(value)
    if base == 'JSON':
        return _check_json(value)

    # 未知类型：保持旧 `coerce` 的最后一条分支（一律转成字符串），
    # 不编造校验规则 —— "没实现"不能被伪装成"已约束"。
    return str(value)
