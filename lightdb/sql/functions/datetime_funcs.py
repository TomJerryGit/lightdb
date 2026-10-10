"""
日期时间函数（40+ 个 MySQL 兼容）
"""
import re
import time
from datetime import datetime, date, timedelta
from typing import Any, Optional


# ==================== 当前时间 ====================

def now() -> str:
    """NOW()"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def sysdate() -> str:
    """SYSDATE()"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def curdate() -> str:
    """CURDATE()"""
    return datetime.now().strftime('%Y-%m-%d')


def curtime() -> str:
    """CURTIME()"""
    return datetime.now().strftime('%H:%M:%S')


def unix_timestamp(dt=None) -> int:
    """UNIX_TIMESTAMP([dt])"""
    if dt is None:
        return int(time.time())
    try:
        if isinstance(dt, str):
            return int(time.mktime(
                datetime.strptime(dt[:19], '%Y-%m-%d %H:%M:%S').timetuple()))
        return int(dt.timestamp())
    except Exception:
        return 0


def from_unixtime(ts, fmt='%Y-%m-%d %H:%M:%S') -> Optional[str]:
    """FROM_UNIXTIME"""
    try:
        return datetime.fromtimestamp(int(ts)).strftime(fmt)
    except Exception:
        return None


# ==================== 日期提取 ====================

def _parse_date(v) -> Optional[datetime]:
    """解析成 datetime"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    s = str(v).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y/%m/%d %H:%M:%S',
                '%Y/%m/%d', '%Y%m%d%H%M%S', '%Y%m%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def year(d) -> Optional[int]:
    dt = _parse_date(d)
    return dt.year if dt else None


def month(d) -> Optional[int]:
    dt = _parse_date(d)
    return dt.month if dt else None


def day(d) -> Optional[int]:
    dt = _parse_date(d)
    return dt.day if dt else None


def hour(d) -> Optional[int]:
    dt = _parse_date(d)
    return dt.hour if dt else None


def minute(d) -> Optional[int]:
    dt = _parse_date(d)
    return dt.minute if dt else None


def second(d) -> Optional[int]:
    dt = _parse_date(d)
    return dt.second if dt else None


def dayofweek(d) -> Optional[int]:
    """1=Sunday, 2=Monday, ..., 7=Saturday"""
    dt = _parse_date(d)
    if not dt:
        return None
    return (dt.weekday() + 1) % 7 + 1


def dayofmonth(d) -> Optional[int]:
    return day(d)


def dayofyear(d) -> Optional[int]:
    dt = _parse_date(d)
    return dt.timetuple().tm_yday if dt else None


def weekday(d) -> Optional[int]:
    """0=Monday, 6=Sunday"""
    dt = _parse_date(d)
    return dt.weekday() if dt else None


def monthname(d) -> Optional[str]:
    dt = _parse_date(d)
    if not dt:
        return None
    names = ['January', 'February', 'March', 'April', 'May', 'June',
             'July', 'August', 'September', 'October', 'November', 'December']
    return names[dt.month - 1]


def dayname(d) -> Optional[str]:
    dt = _parse_date(d)
    if not dt:
        return None
    names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
             'Friday', 'Saturday', 'Sunday']
    return names[dt.weekday()]


def quarter(d) -> Optional[int]:
    dt = _parse_date(d)
    return (dt.month - 1) // 3 + 1 if dt else None


def week(d) -> Optional[int]:
    """WEEK()"""
    dt = _parse_date(d)
    return dt.isocalendar()[1] if dt else None


def weekofyear(d) -> Optional[int]:
    return week(d)


def last_day(d) -> Optional[str]:
    """LAST_DAY - 当月最后一天"""
    dt = _parse_date(d)
    if not dt:
        return None
    if dt.month == 12:
        last = dt.replace(day=31)
    else:
        last = dt.replace(month=dt.month + 1, day=1) - timedelta(days=1)
    return last.strftime('%Y-%m-%d')


# ==================== 格式化 ====================

def date_format(d, fmt) -> Optional[str]:
    """
    DATE_FORMAT(dt, fmt)
    MySQL 格式 %i=分钟, %s=秒, %d=日, %m=月, %Y=年 ...
    Python 格式 %M=分钟, %S=秒
    """
    dt = _parse_date(d)
    if not dt or fmt is None:
        return None

    fmt = str(fmt)
    # MySQL → Python
    fmt = fmt.replace('%i', '%M')   # MySQL %i 分钟 → Python %M
    fmt = fmt.replace('%s', '%S')   # MySQL %s 秒 → Python %S
    # %r 12小时制 → %I:%M:%S %p
    fmt = fmt.replace('%r', '%I:%M:%S %p')
    # %T 24小时制
    fmt = fmt.replace('%T', '%H:%M:%S')

    try:
        return dt.strftime(fmt)
    except Exception:
        return None


def time_format(d, fmt) -> Optional[str]:
    return date_format(d, fmt)


def str_to_date(s, fmt) -> Optional[str]:
    """STR_TO_DATE"""
    if s is None or fmt is None:
        return None
    fmt = str(fmt).replace('%i', '%M').replace('%s', '%S')
    try:
        dt = datetime.strptime(str(s), fmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


# ==================== 计算 ====================

def date_add(d, interval_str) -> Optional[str]:
    """
    DATE_ADD(dt, INTERVAL n unit)
    MySQL: DATE_ADD('2025-01-01', INTERVAL 1 DAY)
    我们支持: date_add('2025-01-01', '1 DAY')
    """
    return _add_interval(d, interval_str, 1)


def date_sub(d, interval_str) -> Optional[str]:
    return _add_interval(d, interval_str, -1)


def _add_interval(d, interval_str, sign=1):
    dt = _parse_date(d)
    if not dt or interval_str is None:
        return None

    s = str(interval_str).strip().upper()
    # 支持 "1 DAY" / "DAY 1" / "1" + unit 分离
    m = re.match(r'(\d+)\s+(\w+)', s)
    if not m:
        m = re.match(r'(\w+)\s+(\d+)', s)
        if m:
            unit, n = m.group(1), int(m.group(2))
        else:
            return None
    else:
        n, unit = int(m.group(1)), m.group(2)

    n *= sign
    try:
        if unit in ('SECOND', 'SECONDS'):
            result = dt + timedelta(seconds=n)
        elif unit in ('MINUTE', 'MINUTES'):
            result = dt + timedelta(minutes=n)
        elif unit in ('HOUR', 'HOURS'):
            result = dt + timedelta(hours=n)
        elif unit in ('DAY', 'DAYS'):
            result = dt + timedelta(days=n)
        elif unit in ('WEEK', 'WEEKS'):
            result = dt + timedelta(weeks=n)
        elif unit in ('MONTH', 'MONTHS'):
            m2 = dt.month - 1 + n
            y2 = dt.year + m2 // 12
            m2 = m2 % 12 + 1
            day2 = min(dt.day, [31, 29 if y2 % 4 == 0 else 28,
                                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m2 - 1])
            result = dt.replace(year=y2, month=m2, day=day2)
        elif unit in ('YEAR', 'YEARS'):
            result = dt.replace(year=dt.year + n)
        else:
            return None
        return result.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


# 别名
adddate = date_add
subdate = date_sub


def datediff(d1, d2) -> Optional[int]:
    """DATEDIFF(d1, d2) - 天数差"""
    dt1 = _parse_date(d1)
    dt2 = _parse_date(d2)
    if not dt1 or not dt2:
        return None
    return (dt1.date() - dt2.date()).days


def timediff(t1, t2) -> Optional[str]:
    """TIMEDIFF"""
    dt1 = _parse_date(t1)
    dt2 = _parse_date(t2)
    if not dt1 or not dt2:
        return None
    delta = dt1 - dt2
    total_s = int(delta.total_seconds())
    sign = '-' if total_s < 0 else ''
    total_s = abs(total_s)
    h = total_s // 3600
    m = (total_s % 3600) // 60
    s = total_s % 60
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


def timestampdiff(unit, d1, d2) -> Optional[int]:
    """TIMESTAMPDIFF"""
    dt1 = _parse_date(d1)
    dt2 = _parse_date(d2)
    if not dt1 or not dt2:
        return None
    delta = dt2 - dt1
    u = str(unit).upper()
    total_s = int(delta.total_seconds())
    if u == 'SECOND': return total_s
    if u == 'MINUTE': return total_s // 60
    if u == 'HOUR': return total_s // 3600
    if u == 'DAY': return total_s // 86400
    if u == 'WEEK': return total_s // (86400 * 7)
    if u == 'MONTH':
        return (dt2.year - dt1.year) * 12 + (dt2.month - dt1.month)
    if u == 'YEAR':
        return dt2.year - dt1.year
    return None


def timestampadd(unit, n, d) -> Optional[str]:
    return _add_interval(d, f"{n} {unit}", 1)


def to_days(d) -> Optional[int]:
    """TO_DAYS - 从公元 0 年到现在的天数"""
    dt = _parse_date(d)
    if not dt:
        return None
    base = datetime(1, 1, 1)
    return (dt - base).days + 366


def from_days(n) -> Optional[str]:
    """FROM_DAYS"""
    try:
        base = datetime(1, 1, 1)
        return (base + timedelta(days=int(n) - 366)).strftime('%Y-%m-%d')
    except Exception:
        return None


def to_seconds(d) -> Optional[int]:
    dt = _parse_date(d)
    return int(dt.timestamp()) if dt else None


def from_seconds(s) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(s)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def makedate(y, n) -> Optional[str]:
    """MAKEDATE"""
    try:
        base = datetime(int(y), 1, 1)
        return (base + timedelta(days=int(n) - 1)).strftime('%Y-%m-%d')
    except Exception:
        return None


def maketime(h, m, s) -> str:
    """MAKETIME"""
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"


def period_add(p, n) -> int:
    """PERIOD_ADD"""
    y = int(p) // 100
    m = int(p) % 100
    m += int(n)
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return y * 100 + m


def period_diff(p1, p2) -> int:
    """PERIOD_DIFF"""
    y1, m1 = int(p1) // 100, int(p1) % 100
    y2, m2 = int(p2) // 100, int(p2) % 100
    return (y1 - y2) * 12 + (m1 - m2)


# ==================== 导出 ====================

DATETIME_FUNCTIONS = {
    'NOW': now,
    'SYSDATE': sysdate,
    'CURRENT_TIMESTAMP': now,
    'CURDATE': curdate,
    'CURRENT_DATE': curdate,
    'CURTIME': curtime,
    'CURRENT_TIME': curtime,
    'UNIX_TIMESTAMP': unix_timestamp,
    'FROM_UNIXTIME': from_unixtime,
    'YEAR': year,
    'MONTH': month,
    'DAY': day,
    'DAYOFMONTH': dayofmonth,
    'HOUR': hour,
    'MINUTE': minute,
    'SECOND': second,
    'DAYOFWEEK': dayofweek,
    'DAYOFYEAR': dayofyear,
    'WEEKDAY': weekday,
    'MONTHNAME': monthname,
    'DAYNAME': dayname,
    'QUARTER': quarter,
    'WEEK': week,
    'WEEKOFYEAR': weekofyear,
    'LAST_DAY': last_day,
    'DATE_FORMAT': date_format,
    'TIME_FORMAT': time_format,
    'STR_TO_DATE': str_to_date,
    'DATE_ADD': date_add,
    'ADDDATE': adddate,
    'DATE_SUB': date_sub,
    'SUBDATE': subdate,
    'DATEDIFF': datediff,
    'TIMEDIFF': timediff,
    'TIMESTAMPDIFF': timestampdiff,
    'TIMESTAMPADD': timestampadd,
    'TO_DAYS': to_days,
    'FROM_DAYS': from_days,
    'TO_SECONDS': to_seconds,
    'FROM_SECONDS': from_seconds,
    'MAKEDATE': makedate,
    'MAKETIME': maketime,
    'PERIOD_ADD': period_add,
    'PERIOD_DIFF': period_diff,
}