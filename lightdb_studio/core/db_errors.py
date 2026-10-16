"""连接失败 → 「可行动」的解释文本（**纯函数**：不弹窗、不写盘、无副作用）。

背景
----
`_new_connection()` 以前只把驱动原文丢给用户::

    无法连接:
    (2003, "Can't connect to MySQL server on '127.0.0.1' ([WinError 10061]
     由于目标计算机积极拒绝，无法连接。)")

用户看得懂字面，但看不出「该干什么」。本模块把 errno 翻成**结论 + 下一步**，
同时**一字不删**地保留驱动原文（`str(exc)`）与结构化字段块
（错误码 / SQLSTATE / 报文），所以加建议不会丢信息。

分支表（`explain_connection_error`）
-----------------------------------
===============  ============================================================
输入特征          结论 / 给用户的下一步
===============  ============================================================
2003、2002、      端口上**没有服务端在监听**（连接被拒绝）。本地 host 直接给
WinError 10061、  可照抄的启动命令；非本地 host 给「检查服务端/防火墙/端口」
ConnectionRefused
1045（Access      用户名或密码不正确（本引擎默认只有 root、密码为空）。
denied）
1049（Unknown     库不存在。本引擎是单库模型，库名可留空（留空=用默认库）。
database）
超时（timeout /   连接超时：多半是防火墙丢包或服务端卡住，而不是「拒绝」。
2013 / "timed out"）
其它              保留原始报文 + 一句下一步建议（本地 host 附启动命令）。
===============  ============================================================

原文保留规则：返回值里必定同时有 `str(exc)`（原样，含换行/引号/末尾换行）
和（取得到时）`错误码 / SQLSTATE / 报文` 三段。任何格式化异常都会被兜住并
退化成只返回原文 —— 报错路径自己崩掉是最坏的结果。
"""
from typing import Optional, Tuple

from ..db.connection import error_code, error_message, error_sqlstate

# 默认值：`ConnectionInfo` 的默认端口是 8888，`lightdb.main` 的默认监听也是
# 8888，所以拿不到端口时按 8888 给命令，绝不编一个"看起来像"的端口。
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8888

# 「本机」判定：只有这些 host 才给「直接照抄的启动命令」（抄了就能用）。
_LOCAL_HOSTS = frozenset(['127.0.0.1', 'localhost', '::1', '0.0.0.0', ''])

_REFUSED_TEXT_MARKERS = (
    'winerror 10061', 'errno 10061', 'connection refused',
    'connectionrefused', '积极拒绝', '拒绝连接',
)
_TIMEOUT_TEXT_MARKERS = (
    'timed out', 'timeout', '超时', 'read timeout', 'connect timeout',
)


def server_start_command(host: str = DEFAULT_HOST,
                         port: int = DEFAULT_PORT) -> str:
    """本机起一个单机服务端、可直接照抄的命令（原样复制就能跑）。"""
    return ('python -m lightdb.main --mode single --host %s --port %s '
            '--data-dir data_demo' % (host, port))


def is_local_host(host: Optional[str]) -> bool:
    """host 是否指向本机（决定给不给「照抄启动命令」）。"""
    return str(host or '').strip().lower() in _LOCAL_HOSTS


def _mro_names(exc) -> Tuple[str, ...]:
    try:
        return tuple(c.__name__ for c in type(exc).__mro__)
    except Exception:                                     # noqa: BLE001
        return ()


def _looks_refused(exc, message: str) -> bool:
    """连接被拒绝：驱动把 WinError 10061 塞进了报文里，只能靠文本 + 类型判。"""
    if isinstance(exc, ConnectionRefusedError):
        return True
    if 'ConnectionRefusedError' in _mro_names(exc):
        return True
    low = (message or '').lower()
    return any(m in low for m in _REFUSED_TEXT_MARKERS)


def _looks_timeout(exc, message: str) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    names = _mro_names(exc)
    if any('Timeout' in n for n in names):
        return True
    low = (message or '').lower()
    return any(m in low for m in _TIMEOUT_TEXT_MARKERS)


def _raw_text(exc) -> str:
    """驱动原文：`str(exc)` 一字不改（空的话退回 repr）。"""
    try:
        text = str(exc)
    except Exception:                                     # noqa: BLE001
        text = ''
    if not text:
        try:
            text = repr(exc)
        except Exception:                                 # noqa: BLE001
            text = '<无法渲染的异常对象>'
    return text


def _target(host: Optional[str], port: Optional[int]) -> str:
    h = str(host).strip() if host else DEFAULT_HOST
    p = port if isinstance(port, int) and port else DEFAULT_PORT
    return '%s:%s' % (h, p)


def _diagnose(exc, host, port):
    """返回 (head, steps) —— 一句话结论 + 下一步。内部用，不对外。"""
    code = error_code(exc)
    message = error_message(exc)
    # 归一化：命令要能直接照抄，所以 host/port 必须是具体值，不能是 None。
    host = str(host).strip() if host else DEFAULT_HOST
    port = port if isinstance(port, int) and port else DEFAULT_PORT
    target = _target(host, port)
    local = is_local_host(host)

    if _looks_refused(exc, message) or code in (2002, 2003, 2005, 2006):
        if _looks_timeout(exc, message) and not _looks_refused(exc, message):
            # 2003 也可能是超时（被防火墙丢弃），不能一律说"没有服务端"
            tail = ('    %s' % server_start_command(host, port)) if local else \
                '先在服务端所在机器上启动 LightDB 服务端，再回来重试。'
            return (
                '%s 连接超时：握手在超时时间内没有完成。' % target,
                ['① 确认服务端在跑（被防火墙「丢弃」是超时，被「拒绝」才是'
                 '没有服务端）；',
                 '② 确认网络/防火墙放通了 %s；' % target,
                 '③ 服务端没起来时，先把它起起来：', '', tail],
            )
        if local:
            return (
                '%s 上没有服务端在监听（连接被拒绝 = 该端口没有程序 accept）'
                '。' % target,
                ['服务端没起来。在项目目录里直接照抄下面这条命令即可起一个'
                 '单机服务端：', '',
                 '    %s' % server_start_command(host, port), '',
                 '它打印的监听地址应当就是 %s；端口若改过，请让 --port 与连接'
                 '里的端口保持一致，然后回到这里重试。' % target],
            )
        return (
            '%s 上没有可用的服务端（连接被拒绝）。' % target,
            ['① 确认服务端确实在 %s 上运行（远程机器上执行 '
             '`netstat -ano | findstr :%s`，看有没有 LISTENING）；'
             % (str(host).strip(), target.rsplit(':', 1)[1]),
             '② 确认防火墙 / 安全组放通了该端口，且服务端不是只绑在 127.0.0.1'
             '（绑 127.0.0.1 时外部连不进来）；',
             '③ 确认连接里填的主机名/端口与服务端实际监听的一致。'],
        )

    if code == 1045 or 'access denied' in (message or '').lower():
        return (
            '%s 拒绝了这次登录：用户名或密码不正确（错误码 1045）。' % target,
            ['① 核对用户名/密码。本引擎默认只有 root、密码为空（空密码就是'
             '密码框留空）；',
             '② 报文的区别：带 `(using password: YES)` = 口令对不上；不带 = '
             '用户名不存在；',
             '③ 需要别的账号时，先在服务端执行 CREATE USER ... 再连。'],
        )

    if code == 1049 or 'unknown database' in (message or '').lower():
        return (
            '%s 上没有这个库：库名不存在（错误码 1049，Unknown database）。'
            % target,
            ['① 本引擎是「单库模型」，库名可以留空 —— 留空就用引擎默认的那个'
             '库（常见做法）；',
             '② 要显式指定时，请填 SHOW DATABASES 里真实存在的库名'
             '（大小写不敏感）；',
             '③ 登录时带上的「默认库」也会被校验，写错一样是 1049。'],
        )

    if _looks_timeout(exc, message):
        return (
            '%s 连接超时：握手在超时时间内没有完成。' % target,
            ['① 先确认服务端在跑（「超时」多半是防火墙丢包或服务端卡住，'
             '「拒绝」才是没有服务端）；',
             '② 确认 %s 可达（换台机器 telnet/Test-NetConnection 试一下）；'
             % target,
             '③ 稍后重试；若服务端刚重启过，等它就绪。'],
        )

    head = '连接 %s 失败（错误码 %s）。' % (
        target, code if code is not None else '未提供')
    if local:
        steps = ['① 按下面的原始报文判断类型（连不上 / 认证失败 / 库不存在）；',
                 '② 服务端没起来时先启动它，再重试：', '',
                 '    %s' % server_start_command(host, port)]
    else:
        steps = ['① 确认服务端在 %s 上运行，且防火墙 / 安全组放通；' % target,
                 '② 确认连接里的用户名/密码/库名与服务端配置一致，然后重试。']
    return (head, steps)


def explain_connection_error(exc, host: Optional[str] = None,
                            port: Optional[int] = None) -> str:
    """把连接异常翻成「结论 + 下一步 + 原始报文」的完整文本。

    Args:
        exc: 任何连接期异常（`pymysql.err.OperationalError`、
            `ConnectionRefusedError`、`DBQueryError`、……）。
        host / port: 这次连接的目标（通常是 `ConnectionInfo.host/port`）。
            不传也不会崩：退回异常上的同名属性，再退回 127.0.0.1 / 8888。

    Returns:
        多行文本。**原报文一字未删**：末尾的「原始错误」段就是 `str(exc)`，
        另有「错误码 / SQLSTATE / 报文」字段块（取不到时写「(未提供)」）。
    """
    raw = _raw_text(exc)
    try:
        head, steps = _diagnose(exc, host, port)
        code = error_code(exc)
        state = error_sqlstate(exc)
        message = error_message(exc)
    except Exception:                                     # noqa: BLE001
        # 解释器自己出错时，绝不把有用信息变成 traceback。
        return raw

    lines = [head, '', '下一步：']
    lines.extend(steps or ['对照下面的原始报文排查后重试。'])
    lines.append('')
    lines.append('原始错误（一字未删，可直接复制给他人排查）：')
    lines.append(raw.rstrip('\n'))
    if code is not None or state:
        block = '\n'.join([
            '错误码  : %s' % (code if code is not None else '(未提供)'),
            'SQLSTATE: %s' % (state if state else '(未提供)'),
            '报文    : %s' % (message if message else '(未提供)'),
        ])
        # `DBQueryError.__str__` 本身就是这个字段块，别重复贴两遍。
        if block not in raw:
            lines.append(block)
    return '\n'.join(lines)


def connection_error_summary(exc, host: Optional[str] = None,
                             port: Optional[int] = None) -> str:
    """一行版结论（给状态栏用：不含换行，命令仍完整保留）。"""
    raw = _raw_text(exc)
    try:
        head, steps = _diagnose(exc, host, port)
    except Exception:                                     # noqa: BLE001
        return raw.splitlines()[0] if raw else '连接失败'
    one_line = ' '.join(s.strip() for s in steps if s.strip())
    text = '%s %s' % (head, one_line)
    return ' '.join(text.split())
