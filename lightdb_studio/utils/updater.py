"""LightDB Studio 更新检查 / 更新包校验（P3 发布工程）。

设计原则（与本仓库其它模块一致）：
- **不静默、不自动安装**：只检查 + 下载到临时目录 + 校验 SHA256，是否替换由用户
  手动执行（或发布脚本处理）；任何网络/格式/校验失败都抛 `UpdateError`。
- **可离线测试**：manifest 与安装包都支持 `http(s)://`、`file://` 和本地路径，
  测试不用连外网。
- **不落敏感信息**：manifest/下载 URL 不含凭据；日志由调用方决定。

manifest 格式（schema=1）::

    {
      "schema": 1,
      "channel": "stable",
      "version": "4.0.0-beta.18",
      "released_at": "2026-10-21",
      "notes": "…",
      "notes_url": "https://…",
      "artifacts": [
        {"platform": "windows-x64", "name": "LightDB-Studio-4.0.0.zip",
         "url": "https://…/LightDB-Studio-4.0.0.zip",
         "sha256": "…", "size": 12345678}
      ]
    }
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

MANIFEST_SCHEMA = 1
DEFAULT_CHANNEL = 'stable'
DEFAULT_PLATFORM = 'windows-x64'


class UpdateError(Exception):
    """更新检查/下载失败（网络、manifest 格式、校验不通过等）。"""


@dataclass
class Artifact:
    platform: str = DEFAULT_PLATFORM
    name: str = ''
    url: str = ''
    sha256: str = ''
    size: int = 0

    def to_dict(self) -> dict:
        return {'platform': self.platform, 'name': self.name, 'url': self.url,
                'sha256': self.sha256, 'size': self.size}


@dataclass
class UpdateInfo:
    available: bool = False
    current_version: str = ''
    latest_version: str = ''
    channel: str = DEFAULT_CHANNEL
    notes: str = ''
    notes_url: str = ''
    released_at: str = ''
    artifact: Optional[Artifact] = None
    reason: str = ''
    manifest: dict = field(default_factory=dict)


# ==================== 版本比较 ====================

def parse_version(text: str):
    """把 `4.0.0-beta.18` / `v4.0.0` 解析为 (数字元组, 预发布标记)。

    只支持本项目的版本风格（`<major>.<minor>.<patch>[-<pre>]`，可带 v 前缀）；
    无法解析时抛 UpdateError（不猜）。
    """
    raw = str(text or '').strip()
    if not raw:
        raise UpdateError('版本号为空')
    if raw[:1] in ('v', 'V'):
        raw = raw[1:]
    main, _, pre = raw.partition('-')
    parts = main.split('.')
    if not 1 <= len(parts) <= 3:
        raise UpdateError(f'无法解析版本号: {text!r}')
    nums = []
    for p in parts:
        if not p.isdigit():
            raise UpdateError(f'无法解析版本号: {text!r}')
        nums.append(int(p))
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums), pre


def is_newer(remote: str, current: str) -> bool:
    """remote 是否比 current 新（预发布比正式版旧）。"""
    r_nums, r_pre = parse_version(remote)
    c_nums, c_pre = parse_version(current)
    if r_nums != c_nums:
        return r_nums > c_nums
    # 同一数字版本：无 pre 的正式版 > 有 pre 的预发布版
    if r_pre == c_pre:
        return False
    if not r_pre:
        return True
    if not c_pre:
        return False
    # 都是预发布：按点分段逐段比较，数字段按数字、其余按字符串
    def key(part):
        return [(0, int(x)) if x.isdigit() else (1, x)
                for x in part.split('.')]
    return key(r_pre) > key(c_pre)


# ==================== manifest ====================

def _read_source(source: Union[str, Path], timeout: float) -> bytes:
    """读取 http(s) / file:// / 本地路径。失败抛 UpdateError。"""
    text = str(source)
    if not text:
        raise UpdateError('manifest 地址为空')
    if text.startswith(('http://', 'https://')):
        try:
            req = urllib.request.Request(
                text, headers={'User-Agent': 'LightDB-Studio-Updater'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:                      # noqa: BLE001
            raise UpdateError(f'下载 manifest 失败: {exc}') from exc
    path = text
    if path.startswith('file://'):
        path = urllib.request.url2pathname(path[len('file://'):])
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise UpdateError(f'读取 manifest 失败: {exc}') from exc


def parse_manifest(data: Union[bytes, str, dict]) -> dict:
    """解析并校验 manifest；不合法抛 UpdateError（不猜字段）。"""
    if isinstance(data, dict):
        obj = data
    else:
        try:
            obj = json.loads(data)
        except (TypeError, ValueError) as exc:
            raise UpdateError(f'manifest 不是合法 JSON: {exc}') from exc
    if not isinstance(obj, dict):
        raise UpdateError('manifest 顶层必须是 JSON 对象')
    schema = obj.get('schema')
    if schema != MANIFEST_SCHEMA:
        raise UpdateError(
            f'不支持的 manifest schema: {schema!r}（期望 {MANIFEST_SCHEMA}）')
    version = obj.get('version')
    parse_version(version)                          # 校验
    channel = obj.get('channel') or DEFAULT_CHANNEL
    artifacts = obj.get('artifacts')
    if not isinstance(artifacts, list):
        raise UpdateError('manifest.artifacts 必须是数组')
    for i, a in enumerate(artifacts):
        if not isinstance(a, dict):
            raise UpdateError(f'artifacts[{i}] 必须是对象')
        if not a.get('url'):
            raise UpdateError(f'artifacts[{i}].url 缺失')
        sha = str(a.get('sha256') or '')
        if sha and len(sha) != 64:
            raise UpdateError(f'artifacts[{i}].sha256 长度不是 64: {sha!r}')
        size = a.get('size') or 0
        if not isinstance(size, int) or size < 0:
            raise UpdateError(f'artifacts[{i}].size 非法: {size!r}')
    return obj


def format_update_notice(info: UpdateInfo) -> str:
    """把 UpdateInfo 转成 UI 提示文本（不自动下载/安装，明确告知手动步骤）。"""
    if not info.available:
        return (f'当前版本：{info.current_version}\n'
                f'最新版本：{info.latest_version or "未知"}\n'
                f'说明：{info.reason or "无需更新"}')
    art = info.artifact
    lines = [
        f'发现新版本：{info.latest_version}（当前 {info.current_version}）',
        f'更新通道：{info.channel}',
    ]
    if info.released_at:
        lines.append(f'发布时间：{info.released_at}')
    if info.notes:
        lines.append(f'更新说明：{info.notes}')
    if info.notes_url:
        lines.append(f'详情：{info.notes_url}')
    if art is not None:
        lines.append('')
        lines.append(f'安装包：{art.name or art.url}')
        lines.append(f'下载地址：{art.url}')
        lines.append(f'SHA256：{art.sha256}')
        if art.size:
            lines.append(f'大小：{art.size:,} bytes')
    lines.append('')
    lines.append('本程序不会自动下载/安装更新；请下载后校验 SHA256 并手动替换。')
    return '\n'.join(lines)


def check_for_update(manifest_url: Union[str, Path], current_version: str,
                     channel: str = DEFAULT_CHANNEL,
                     platform: str = DEFAULT_PLATFORM,
                     timeout: float = 8.0) -> UpdateInfo:
    """读取 manifest，与当前版本比较。

    - 新版本 → `available=True` 且带对应平台 artifact；
    - 已是最新/通道不匹配/没有对应平台包 → `available=False` + `reason`；
    - manifest 格式/网络错误 → 抛 UpdateError（调用方显式提示，不静默）。
    """
    parse_version(current_version)
    raw = _read_source(manifest_url, timeout)
    manifest = parse_manifest(raw)
    info = UpdateInfo(current_version=str(current_version),
                      latest_version=str(manifest['version']),
                      channel=str(manifest.get('channel') or DEFAULT_CHANNEL),
                      notes=str(manifest.get('notes') or ''),
                      notes_url=str(manifest.get('notes_url') or ''),
                      released_at=str(manifest.get('released_at') or ''),
                      manifest=manifest)
    if info.channel != channel:
        info.reason = (f'通道不匹配：manifest channel={info.channel!r}, '
                       f'请求 channel={channel!r}')
        return info
    if not is_newer(info.latest_version, current_version):
        info.reason = f'已是最新（当前 {current_version}，最新 {info.latest_version}）'
        return info
    for a in manifest.get('artifacts') or []:
        if str(a.get('platform') or '') == platform:
            info.artifact = Artifact(
                platform=platform, name=str(a.get('name') or ''),
                url=str(a['url']), sha256=str(a.get('sha256') or ''),
                size=int(a.get('size') or 0))
            break
    if info.artifact is None:
        info.reason = f'manifest 没有 {platform!r} 平台的安装包'
        return info
    info.available = True
    return info


def sha256_file(path: Union[str, Path], chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def download_update(artifact: Artifact, dest_dir: Union[str, Path],
                    timeout: float = 60.0) -> Path:
    """下载 artifact 到 dest_dir 并校验 sha256/size；失败抛 UpdateError。

    - sha256 为空：**拒绝下载**（发布工程强制校验，不允许跳过）；
    - 校验失败：删除半成品文件并抛错（不留下不可信文件）。
    """
    if artifact is None or not artifact.url:
        raise UpdateError('artifact 为空或缺少下载地址')
    if not artifact.sha256:
        raise UpdateError('artifact 缺少 sha256，拒绝下载（发布必须提供校验和）')
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    name = artifact.name or Path(urllib.request.urlparse(artifact.url).path).name \
        or 'update.download'
    # 防目录穿越
    name = os.path.basename(name)
    target = dest / name
    fd, tmp_name = tempfile.mkstemp(prefix='.lightdb_update_', dir=str(dest))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if artifact.url.startswith(('http://', 'https://')):
            try:
                req = urllib.request.Request(
                    artifact.url, headers={'User-Agent': 'LightDB-Studio-Updater'})
                with urllib.request.urlopen(req, timeout=timeout) as resp, \
                        open(tmp, 'wb') as out:
                    shutil.copyfileobj(resp, out)
            except Exception as exc:                  # noqa: BLE001
                raise UpdateError(f'下载安装包失败: {exc}') from exc
        else:
            src = artifact.url
            if src.startswith('file://'):
                src = urllib.request.url2pathname(src[len('file://'):])
            try:
                shutil.copyfile(src, tmp)
            except OSError as exc:
                raise UpdateError(f'复制安装包失败: {exc}') from exc
        actual = sha256_file(tmp)
        if actual.lower() != artifact.sha256.lower():
            raise UpdateError(
                f'安装包 sha256 不匹配（期望 {artifact.sha256}，实际 {actual}）')
        if artifact.size and tmp.stat().st_size != artifact.size:
            raise UpdateError(
                f'安装包大小不匹配（期望 {artifact.size}，实际 {tmp.stat().st_size}）')
        if target.exists():
            target.unlink()
        tmp.replace(target)
        return target
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
