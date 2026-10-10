"""JWT 认证 + 权限"""
import time
import hashlib
import hmac
import json
import base64
from typing import Optional
from fastapi import HTTPException, Header, Depends


SECRET = "lightdb_studio_secret_2025_change_me"
ALGO_HEADER = {"alg": "HS256", "typ": "JWT"}


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _b64url_decode(s: str) -> bytes:
    s += '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def create_token(username: str, role: str = 'user',
                 expires_in: int = 86400 * 7) -> str:
    """生成 JWT"""
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
    sig = hmac.new(SECRET.encode(), signing_input, hashlib.sha256).digest()
    sig_b = _b64url_encode(sig)

    return f"{header}.{payload_b}.{sig_b}"


def decode_token(token: str) -> dict:
    """验证并解码"""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError("token 格式错误")

        header_b, payload_b, sig_b = parts
        signing_input = f"{header_b}.{payload_b}".encode()

        expected_sig = hmac.new(
            SECRET.encode(), signing_input, hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b)

        if not hmac.compare_digest(expected_sig, actual_sig):
            raise ValueError("签名无效")

        payload = json.loads(_b64url_decode(payload_b))
        if payload.get('exp', 0) < time.time():
            raise ValueError("token 已过期")

        return payload
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