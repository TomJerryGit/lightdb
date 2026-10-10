"""数学函数"""
import math
import random


def abs_(x): return abs(x) if x is not None else None
def ceil_(x): return math.ceil(x) if x is not None else None
ceiling = ceil_
def floor_(x): return math.floor(x) if x is not None else None
def round_(x, n=0): return round(x, int(n)) if x is not None else None
def truncate_(x, n=0):
    if x is None: return None
    n = int(n)
    f = 10 ** n
    return math.floor(x * f) / f

def mod_(a, b):
    if a is None or b is None or b == 0:
        return None
    return a % b

def pow_(a, b): return a ** b if a is not None and b is not None else None
power = pow_
def sqrt_(x): return math.sqrt(x) if x is not None and x >= 0 else None
def exp_(x): return math.exp(x) if x is not None else None
def ln_(x): return math.log(x) if x is not None and x > 0 else None
def log_(x, base=None):
    if x is None: return None
    if base is None: return math.log10(x) if x > 0 else None
    return math.log(x, base) if x > 0 else None
def log2_(x): return math.log2(x) if x is not None and x > 0 else None
def log10_(x): return math.log10(x) if x is not None and x > 0 else None
def sign_(x):
    if x is None: return None
    return -1 if x < 0 else (1 if x > 0 else 0)

def rand_(seed=None):
    if seed is not None:
        random.seed(int(seed))
    return random.random()

def pi_(): return math.pi
def sin_(x): return math.sin(x) if x is not None else None
def cos_(x): return math.cos(x) if x is not None else None
def tan_(x): return math.tan(x) if x is not None else None
def asin_(x): return math.asin(x) if x is not None and -1 <= x <= 1 else None
def acos_(x): return math.acos(x) if x is not None and -1 <= x <= 1 else None
def atan_(x): return math.atan(x) if x is not None else None
def atan2_(y, x): return math.atan2(y, x) if y is not None and x is not None else None
def cot_(x): return 1 / math.tan(x) if x else None
def degrees_(x): return math.degrees(x) if x is not None else None
def radians_(x): return math.radians(x) if x is not None else None

def greatest_(*args):
    vals = [a for a in args if a is not None]
    return max(vals) if vals else None

def least_(*args):
    vals = [a for a in args if a is not None]
    return min(vals) if vals else None

def conv_(n, from_base, to_base):
    try:
        v = int(str(n), int(from_base))
        base = int(to_base)
        if base == 10:
            return str(v)
        digits = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        if v == 0:
            return '0'
        neg = v < 0
        v = abs(v)
        result = ''
        while v:
            result = digits[v % base] + result
            v //= base
        return ('-' if neg else '') + result
    except Exception:
        return None

def crc32_(s):
    import zlib
    if s is None: return None
    return zlib.crc32(str(s).encode()) & 0xFFFFFFFF


MATH_FUNCTIONS = {
    'ABS': abs_, 'CEIL': ceil_, 'CEILING': ceiling, 'FLOOR': floor_,
    'ROUND': round_, 'TRUNCATE': truncate_, 'MOD': mod_,
    'POW': pow_, 'POWER': power, 'SQRT': sqrt_, 'EXP': exp_,
    'LN': ln_, 'LOG': log_, 'LOG2': log2_, 'LOG10': log10_,
    'SIGN': sign_, 'RAND': rand_, 'PI': pi_,
    'SIN': sin_, 'COS': cos_, 'TAN': tan_,
    'ASIN': asin_, 'ACOS': acos_, 'ATAN': atan_, 'ATAN2': atan2_,
    'COT': cot_, 'DEGREES': degrees_, 'RADIANS': radians_,
    'GREATEST': greatest_, 'LEAST': least_,
    'CONV': conv_, 'CRC32': crc32_,
}