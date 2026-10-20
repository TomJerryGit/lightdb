"""连接口令的本地加密/解密（GUI P0-4）。

目标：`connections.json` 不再出现明文口令；旧文件（明文）首次加载自动迁移。

实现优先级（不强制新依赖）：
1. **Windows DPAPI**（`CryptProtectData` / `CryptUnprotectData`，ctypes 调
   crypt32，无需第三方包）：密文只能由**当前 Windows 用户**解开，绑定本机，
   防"拷走 connections.json 直接看口令"。
2. **非 Windows**：如装了 `cryptography` 用 Fernet（密钥文件
   `~/.lightdb_studio/credentials.key`，尽力设 0600）；否则退化为
   `plain:` 前缀并在 stderr 显式警告（不静默；这属于平台限制）。
3. 旧格式（无前缀的裸明文）在 `decrypt_password()` 中原样返回，由
   `AppConfig.load()` 检测到后 **重写文件** 完成迁移。

存储格式（都放在原有 8 字段 `password` 里，`connections.json` schema 不变）：
    dpapi:v1:<base64>     Windows DPAPI 密文
    fernet:v1:<token>     cryptography.Fernet 密文
    plain:<text>          显式明文回退（仅非 Windows 且无 cryptography）
    <text>                旧版明文（兼容，读时迁移）
"""
from __future__ import annotations

import base64
import ctypes
import os
import sys
from pathlib import Path
from typing import Optional

PREFIX_DPAPI = 'dpapi:v1:'
PREFIX_FERNET = 'fernet:v1:'
PREFIX_PLAIN = 'plain:'

#: DPAPI 的附加熵：把解密绑定到"LightDB Studio 的连接口令"这一用途。
_ENTROPY = b'LightDBStudio.connections.v1'

_WARNED_PLAINTEXT = False


class CredentialError(RuntimeError):
    """加密/解密失败（调用方必须显式处理，不允许静默当空口令）。"""


# ==================== Windows DPAPI ====================

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32),
                ('pbData', ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi_available() -> bool:
    if os.name != 'nt':
        return False
    try:
        crypt32 = ctypes.windll.crypt32        # noqa: F841
        return True
    except Exception:                          # noqa: BLE001
        return False


def dpapi_available() -> bool:
    return _dpapi_available()


def _dpapi_encrypt(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _blob(data)
    entropy = _blob(_ENTROPY)
    blob_out = _DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, ctypes.byref(entropy),
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise CredentialError(
            f'DPAPI 加密失败（GetLastError={kernel32.GetLastError()}）')
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_decrypt(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _blob(data)
    entropy = _blob(_ENTROPY)
    blob_out = _DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, ctypes.byref(entropy),
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise CredentialError(
            f'DPAPI 解密失败（GetLastError={kernel32.GetLastError()}；'
            f'密文可能来自其他 Windows 用户/机器）')
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ==================== 非 Windows 回退 ====================

def _fernet_key_file() -> Path:
    # 延迟 import，避免 config 与 credentials 循环依赖
    from ..config import CONFIG_DIR
    return Path(CONFIG_DIR) / 'credentials.key'


def _fernet():
    try:
        from cryptography.fernet import Fernet
        return Fernet
    except ImportError:
        return None


def _fernet_get_key() -> bytes:
    key_file = _fernet_key_file()
    if key_file.exists():
        data = key_file.read_bytes().strip()
        if data:
            return data
    Fernet = _fernet()
    if Fernet is None:
        raise CredentialError('cryptography 未安装')
    key = Fernet.generate_key()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(key)
    try:
        os.chmod(key_file, 0o600)
    except Exception:                          # noqa: BLE001
        pass
    return key


def _warn_plaintext_once(reason: str) -> None:
    global _WARNED_PLAINTEXT
    if _WARNED_PLAINTEXT:
        return
    _WARNED_PLAINTEXT = True
    print(f'[credentials] WARN 非 Windows 且无可用加密后端，口令以明文保存'
          f'（{reason}）；生产环境请安装 cryptography 或使用 Windows。',
          file=sys.stderr)


# ==================== 对外 API ====================

def is_encrypted(stored: str) -> bool:
    """存储值是否已经是密文（或显式 plain: 标记）。"""
    if not stored:
        return False
    return stored.startswith((PREFIX_DPAPI, PREFIX_FERNET, PREFIX_PLAIN))


def encrypt_password(plain: str) -> str:
    """明文 → 落盘字符串；空口令保持空串（不产生无意义密文）。"""
    if not plain:
        return ''
    try:
        if _dpapi_available():
            return PREFIX_DPAPI + base64.b64encode(
                _dpapi_encrypt(plain.encode('utf-8'))).decode('ascii')
        Fernet = _fernet()
        if Fernet is not None:
            token = Fernet(_fernet_get_key()).encrypt(plain.encode('utf-8'))
            return PREFIX_FERNET + token.decode('ascii')
    except CredentialError:
        raise
    except Exception as e:                     # noqa: BLE001
        raise CredentialError(f'口令加密失败: {e}') from e
    _warn_plaintext_once('无 DPAPI/cryptography')
    return PREFIX_PLAIN + plain


def decrypt_password(stored: str) -> str:
    """落盘字符串 → 明文。

    * 空串 → 空串；
    * 旧版裸明文 → 原样返回（调用方负责重写迁移）；
    * 密文解密失败 → 抛 `CredentialError`（**不允许**静默当空口令，
      否则用户会看到"连接突然认证失败"却不知道原因）。
    """
    if not stored:
        return ''
    try:
        if stored.startswith(PREFIX_DPAPI):
            raw = base64.b64decode(stored[len(PREFIX_DPAPI):])
            return _dpapi_decrypt(raw).decode('utf-8')
        if stored.startswith(PREFIX_FERNET):
            Fernet = _fernet()
            if Fernet is None:
                raise CredentialError('cryptography 未安装，无法解密 fernet 口令')
            return Fernet(_fernet_get_key()).decrypt(
                stored[len(PREFIX_FERNET):].encode('ascii')).decode('utf-8')
        if stored.startswith(PREFIX_PLAIN):
            return stored[len(PREFIX_PLAIN):]
    except CredentialError:
        raise
    except Exception as e:                     # noqa: BLE001
        raise CredentialError(f'口令解密失败: {e}') from e
    return stored                              # 旧版明文，调用方迁移


def backend_name() -> str:
    """当前实际使用的加密后端（测试/状态展示用）。"""
    if _dpapi_available():
        return 'dpapi'
    if _fernet() is not None:
        return 'fernet'
    return 'plain-fallback'
