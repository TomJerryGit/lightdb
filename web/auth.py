"""JWT 认证 + 权限

密钥来源（按优先级，见 :func:`get_secret`）：

1. 环境变量 ``LIGHTDB_WEB_SECRET``（推荐：生产/CI 显式注入，最可控）；
2. ``~/.lightdb_studio/web_secret``：首次使用自动生成 48 字节随机密钥、
   以 0600 落盘，后续启动复用（重启后已签发的 token 仍然有效）；
3. 进程内随机密钥：上面两个来源都不可用时回退（只告警，不静默）。此时
   进程重启后旧 token 全部失效——这是安全的失败方向。

历史缺陷（P0-D）：本模块曾经硬编码
``SECRET = "lightdb_studio_secret_2025_change_me"``，源码公开意味着任何人
都能离线签出 ``role=admin`` 的 token。该常量已删除，模块不再暴露任何
固定密钥；所有签名/验签统一走 :func:`get_secret`。
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, Header, HTTPException

logger = logging.getLogger(__name__)

ALGO_HEADER = {"alg": "HS256", "typ": "JWT"}
ALGORITHM = 'HS256'

_ENV_SECRET = 'LIGHTDB_WEB_SECRET'
_SECRET_FILE_NAME = 'web_secret'
_STATE_DIR_NAME = '.lightdb_studio'

_secret_lock = threading.Lock()
_secret_cache: Optional[bytes] = None


# ==================== 私有文件 / 目录 ====================

def state_dir() -> Path:
    """``~/.lightdb_studio``（每次调用时解析，测试可改 HOME/USERPROFILE）。"""
    return Path.home() / _STATE_DIR_NAME


def write_private_file(path: Path, text: str) -> bool:
    """以 best-effort 0600 权限写文件（密钥/口令/含口令的连接配置用）。

    先写 ``*.tmp`` 再原子替换，避免写一半留下损坏的密钥文件。返回是否
    成功；失败只记日志，由调用方决定回退策略（绝不抛异常）。
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass                    # Windows 上 chmod 语义有限，尽力而为
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except OSError as e:
        logger.warning('无法写入私有文件 %s: %s', path, e)
        return False


# ==================== 密钥解析 ====================

def _secret_file_path() -> Path:
    return state_dir() / _SECRET_FILE_NAME


def _load_persistent_secret() -> Optional[bytes]:
    """读取/创建 ``~/.lightdb_studio/web_secret``；失败返回 None。"""
    path = _secret_file_path()
    try:
        if path.exists():
            raw = path.read_text(encoding='utf-8').strip()
            if raw:
                return raw.encode('utf-8')
            logger.warning('%s 为空，将重新生成 JWT 密钥', path)
    except OSError as e:
        logger.warning('读取 %s 失败（%s），回退到进程内随机密钥', path, e)
        return None

    generated = secrets.token_urlsafe(48)
    if write_private_file(path, generated + '\n'):
        logger.info('已生成 Web JWT 密钥并持久化到 %s（0600）', path)
        return generated.encode('utf-8')
    return None


def get_secret() -> bytes:
    """返回当前签名密钥（env > 持久化文件 > 进程内随机）。"""
    env_secret = os.environ.get(_ENV_SECRET)
    if env_secret:
        return env_secret.encode('utf-8')

    global _secret_cache
    with _secret_lock:
        if _secret_cache is None:
            persisted = _load_persistent_secret()
            if persisted is not None:
                _secret_cache = persisted
            else:
                _secret_cache = secrets.token_bytes(32)
                logger.warning(
                    '无法持久化 Web JWT 密钥（%s 未设置且 %s 不可写）：'
                    '使用进程内随机密钥，重启后已签发的 token 将全部失效。'
                    '生产环境请设置 %s。',
                    _ENV_SECRET, _secret_file_path(), _ENV_SECRET)
        return _secret_cache


def reset_secret_cache():
    """清空进程内密钥缓存（仅供测试：切换 env / HOME 后重新解析）。"""
    global _secret_cache
    with _secret_lock:
        _secret_cache = None


# ==================== JWT 编解码 ====================

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _b64url_decode(s: str) -> bytes:
    s += '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def create_token(username: str, role: str = 'user',
                 expires_in: int = 86400 * 7) -> str:
    """生成 JWT（函数签名保持不变，供其它模块/测试继续调用）。"""
    header = _b64url_encode(json.dumps(ALGO_HEADER).encode())
    payload = {
        'sub': username,
        'role': role,
        'iat': int(time.time()),
        'exp': int(time.time()) + expires_in,
    }
    payload_b = _b64url_encode(
        json.dumps(payload, separators=(',', ':')).encode())

    signing_input = f"{header}.{payload_b}".encode()
    sig = hmac.new(get_secret(), signing_input, hashlib.sha256).digest()
    sig_b = _b64url_encode(sig)

    return f"{header}.{payload_b}.{sig_b}"


def decode_token(token: str) -> dict:
    """验证并解码。

    校验链：结构 → header.alg 必须是 HS256（拒绝 ``alg=none``/算法混淆）→
    HMAC-SHA256 常量时间验签 → payload 结构 → exp 未过期。任何失败统一抛
    ``ValueError``（「无效 token: ...」），由 ``verify_token`` 转 401。
    """
    try:
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError("token 格式错误")

        header_b, payload_b, sig_b = parts

        try:
            header = json.loads(_b64url_decode(header_b))
        except Exception:
            raise ValueError("header 不是合法 JSON")
        if not isinstance(header, dict) or header.get('alg') != ALGORITHM:
            raise ValueError("不支持的签名算法")

        signing_input = f"{header_b}.{payload_b}".encode()
        expected_sig = hmac.new(
            get_secret(), signing_input, hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b)

        if not hmac.compare_digest(expected_sig, actual_sig):
            raise ValueError("签名无效")

        payload = json.loads(_b64url_decode(payload_b))
        if not isinstance(payload, dict):
            raise ValueError("payload 格式错误")
        if not isinstance(payload.get('sub'), str) or not payload['sub']:
            raise ValueError("payload 缺少 sub")

        exp = payload.get('exp')
        if not isinstance(exp, (int, float)) or exp < time.time():
            raise ValueError("token 已过期")

        return payload
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"无效 token: {e}")


async def verify_token(authorization: Optional[str] = Header(None)):
    """依赖：验证 Authorization 头"""
    if not authorization:
        raise HTTPException(401, "缺少 Authorization 头")

    token = authorization
    if token.startswith('Bearer '):
        token = token[7:]

    try:
        payload = decode_token(token)
        return payload
    except ValueError as e:
        raise HTTPException(401, str(e))


def require_permission(perm: str):
    """依赖工厂：要求指定权限"""
    async def _check(payload: dict = Depends(verify_token)):
        role = payload.get('role', 'user')
        perms = {
            'admin': {'query', 'write', 'ddl', 'admin'},
            'developer': {'query', 'write'},
            'analyst': {'query'},
            'viewer': {'query'},
        }
        if perm not in perms.get(role, set()):
            raise HTTPException(403, f"无 {perm} 权限")
        return payload
    return _check
