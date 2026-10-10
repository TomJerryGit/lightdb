"""
字符串函数库（50+ 个 MySQL 兼容函数）
"""
import hashlib
import re
from typing import Any, Optional


def _s(v) -> str:
    """转字符串"""
    if v is None:
        return None
    return str(v)


# ==================== 基础 ====================

def concat(*args) -> Optional[str]:
    """CONCAT(a, b, ...)"""
    if any(a is None for a in args):
        return None
    return ''.join(str(a) for a in args)


def concat_ws(sep, *args) -> Optional[str]:
    """CONCAT_WS(sep, a, b, ...)"""
    if sep is None:
        return None
    parts = [str(a) for a in args if a is not None]
    return str(sep).join(parts)


def length(s) -> int:
    """LENGTH - 字节数"""
    if s is None:
        return None
    return len(str(s).encode('utf-8'))


def char_length(s) -> int:
    """CHAR_LENGTH - 字符数"""
    if s is None:
        return None
    return len(str(s))


# 别名
character_length = char_length


def bit_length(s) -> int:
    """BIT_LENGTH"""
    if s is None:
        return None
    return len(str(s).encode('utf-8')) * 8


def octet_length(s) -> int:
    """OCTET_LENGTH"""
    return length(s)


# ==================== 大小写 ====================

def upper(s) -> Optional[str]:
    """UPPER"""
    if s is None:
        return None
    return str(s).upper()


def lower(s) -> Optional[str]:
    """LOWER"""
    if s is None:
        return None
    return str(s).lower()


# 别名
ucase = upper
lcase = lower


# ==================== 裁剪 ====================

def trim(s) -> Optional[str]:
    """TRIM"""
    if s is None:
        return None
    return str(s).strip()


def ltrim(s) -> Optional[str]:
    if s is None:
        return None
    return str(s).lstrip()


def rtrim(s) -> Optional[str]:
    if s is None:
        return None
    return str(s).rstrip()


# ==================== 子串 ====================

def substring(s, pos, length=None) -> Optional[str]:
    """
    SUBSTRING(str, pos[, len])
    注意 MySQL 索引从 1 开始，pos 可为负
    """
    if s is None or pos is None:
        return None
    s = str(s)
    pos = int(pos)
    # 负数从末尾
    if pos < 0:
        start = len(s) + pos
    elif pos == 0:
        return ''
    else:
        start = pos - 1

    if length is None:
        return s[start:]
    return s[start:start + int(length)]


# 别名
substr = substring
mid = substring


def substring_index(s, delim, count) -> Optional[str]:
    """SUBSTRING_INDEX(str, delim, count)"""
    if s is None or delim is None:
        return None
    s = str(s)
    count = int(count)
    parts = s.split(str(delim))
    if count > 0:
        return str(delim).join(parts[:count])
    else:
        return str(delim).join(parts[count:])


def left(s, n) -> Optional[str]:
    """LEFT(str, n)"""
    if s is None:
        return None
    return str(s)[:int(n)]


def right(s, n) -> Optional[str]:
    """RIGHT(str, n)"""
    if s is None:
        return None
    n = int(n)
    return str(s)[-n:] if n > 0 else ''


# ==================== 查找 ====================

def locate(substr, s, start=1) -> int:
    """LOCATE(substr, str[, start])"""
    if substr is None or s is None:
        return None
    s = str(s)
    idx = s.find(str(substr), int(start) - 1)
    return idx + 1 if idx >= 0 else 0


def instr(s, substr) -> int:
    """INSTR(str, substr)"""
    return locate(substr, s)


def position(substr, s) -> int:
    """POSITION(substr IN str)"""
    return locate(substr, s)


def field(val, *args) -> int:
    """FIELD(val, a, b, c)"""
    if val is None:
        return 0
    for i, a in enumerate(args, 1):
        if val == a:
            return i
    return 0


def elt(n, *args) -> Optional[str]:
    """ELT(n, a, b, c) - 返回第 n 个"""
    if n is None:
        return None
    n = int(n)
    if 1 <= n <= len(args):
        return args[n - 1]
    return None


# ==================== 替换 ====================

def replace(s, from_str, to_str) -> Optional[str]:
    """REPLACE"""
    if s is None:
        return None
    return str(s).replace(str(from_str), str(to_str))


def insert_func(s, pos, length, newstr) -> Optional[str]:
    """INSERT(str, pos, len, newstr)"""
    if s is None:
        return None
    s = str(s)
    pos = int(pos)
    length = int(length)
    start = pos - 1
    return s[:start] + str(newstr) + s[start + length:]


def reverse(s) -> Optional[str]:
    """REVERSE"""
    if s is None:
        return None
    return str(s)[::-1]


def repeat(s, n) -> Optional[str]:
    """REPEAT"""
    if s is None:
        return None
    n = int(n)
    return str(s) * max(0, n)


# ==================== 填充 ====================

def lpad(s, n, pad) -> Optional[str]:
    """LPAD(str, len, pad)"""
    if s is None:
        return None
    s = str(s)
    n = int(n)
    pad = str(pad)
    if len(s) >= n:
        return s[:n]
    return (pad * ((n - len(s)) // len(pad) + 1))[:n - len(s)] + s


def rpad(s, n, pad) -> Optional[str]:
    """RPAD"""
    if s is None:
        return None
    s = str(s)
    n = int(n)
    pad = str(pad)
    if len(s) >= n:
        return s[:n]
    return s + (pad * ((n - len(s)) // len(pad) + 1))[:n - len(s)]


def space(n) -> str:
    """SPACE(n)"""
    return ' ' * max(0, int(n))


# ==================== 转换 ====================

def ascii_(s) -> int:
    """ASCII"""
    if s is None or len(str(s)) == 0:
        return 0
    return ord(str(s)[0])


def ord_(s) -> int:
    """ORD"""
    return ascii_(s)


def chr_(n) -> str:
    """CHAR"""
    return chr(int(n))


def hex_(v) -> Optional[str]:
    """HEX"""
    if v is None:
        return None
    if isinstance(v, (int,)):
        return format(int(v), 'X')
    try:
        return str(v).encode('utf-8').hex().upper()
    except Exception:
        return None


def unhex_(s) -> Optional[bytes]:
    """UNHEX"""
    if s is None:
        return None
    try:
        return bytes.fromhex(str(s))
    except Exception:
        return None


def bin_(n) -> str:
    """BIN - 二进制"""
    return format(int(n), 'b')


def oct_(n) -> str:
    """OCT - 八进制"""
    return format(int(n), 'o')


# ==================== 哈希 ====================

def md5(s) -> Optional[str]:
    """MD5"""
    if s is None:
        return None
    return hashlib.md5(str(s).encode('utf-8')).hexdigest()


def sha1(s) -> Optional[str]:
    """SHA1"""
    if s is None:
        return None
    return hashlib.sha1(str(s).encode('utf-8')).hexdigest()


def sha2(s, bits=256) -> Optional[str]:
    """SHA2"""
    if s is None:
        return None
    bits = int(bits)
    if bits == 224:
        return hashlib.sha224(str(s).encode('utf-8')).hexdigest()
    if bits == 256:
        return hashlib.sha256(str(s).encode('utf-8')).hexdigest()
    if bits == 384:
        return hashlib.sha384(str(s).encode('utf-8')).hexdigest()
    if bits == 512:
        return hashlib.sha512(str(s).encode('utf-8')).hexdigest()
    return None


# ==================== 正则 ====================

def regexp_like(s, pat) -> int:
    """REGEXP_LIKE(str, pat) - 1/0"""
    if s is None or pat is None:
        return 0
    try:
        return 1 if re.search(str(pat), str(s)) else 0
    except re.error:
        return 0


def regexp_replace(s, pat, repl) -> Optional[str]:
    """REGEXP_REPLACE"""
    if s is None or pat is None:
        return None
    try:
        return re.sub(str(pat), str(repl or ''), str(s))
    except re.error:
        return str(s)


def regexp_substr(s, pat) -> Optional[str]:
    """REGEXP_SUBSTR"""
    if s is None or pat is None:
        return None
    try:
        m = re.search(str(pat), str(s))
        return m.group(0) if m else None
    except re.error:
        return None


# ==================== 判断 ====================

def strcmp(a, b) -> Optional[int]:
    """STRCMP - 比较"""
    if a is None or b is None:
        return None
    a, b = str(a), str(b)
    if a == b:
        return 0
    return -1 if a < b else 1


def find_in_set(val, s) -> int:
    """FIND_IN_SET(val, 'a,b,c')"""
    if val is None or s is None:
        return 0
    parts = str(s).split(',')
    for i, p in enumerate(parts, 1):
        if p.strip() == str(val):
            return i
    return 0


def make_set(bits, *args) -> str:
    """MAKE_SET"""
    bits = int(bits)
    result = []
    for i, a in enumerate(args):
        if bits & (1 << i):
            result.append(str(a))
    return ','.join(result)


# ==================== 导出 ====================

STRING_FUNCTIONS = {
    'CONCAT': concat,
    'CONCAT_WS': concat_ws,
    'LENGTH': length,
    'CHAR_LENGTH': char_length,
    'CHARACTER_LENGTH': character_length,
    'BIT_LENGTH': bit_length,
    'OCTET_LENGTH': octet_length,
    'UPPER': upper,
    'UCASE': ucase,
    'LOWER': lower,
    'LCASE': lcase,
    'TRIM': trim,
    'LTRIM': ltrim,
    'RTRIM': rtrim,
    'SUBSTRING': substring,
    'SUBSTR': substr,
    'MID': mid,
    'SUBSTRING_INDEX': substring_index,
    'LEFT': left,
    'RIGHT': right,
    'LOCATE': locate,
    'INSTR': instr,
    'POSITION': position,
    'FIELD': field,
    'ELT': elt,
    'REPLACE': replace,
    'INSERT': insert_func,
    'REVERSE': reverse,
    'REPEAT': repeat,
    'LPAD': lpad,
    'RPAD': rpad,
    'SPACE': space,
    'ASCII': ascii_,
    'ORD': ord_,
    'CHAR': chr_,
    'HEX': hex_,
    'UNHEX': unhex_,
    'BIN': bin_,
    'OCT': oct_,
    'MD5': md5,
    'SHA1': sha1,
    'SHA': sha1,
    'SHA2': sha2,
    'REGEXP_LIKE': regexp_like,
    'REGEXP_REPLACE': regexp_replace,
    'REGEXP_SUBSTR': regexp_substr,
    'STRCMP': strcmp,
    'FIND_IN_SET': find_in_set,
    'MAKE_SET': make_set,
}