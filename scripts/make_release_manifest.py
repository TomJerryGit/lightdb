"""生成 LightDB Studio 发布 manifest（供自动更新通道使用）。

用法::

    python scripts/make_release_manifest.py \
        --version 4.0.0-beta.19 --channel stable \
        --dist dist/studio --base-url https://example.com/downloads \
        --out dist/release_manifest.json

每个 `--dist` 目录下的发布产物（*.zip / *.exe / *.msi）都会生成一条 artifact，
带 sha256 与 size；manifest 可被 `lightdb_studio.utils.updater` 直接解析和校验。

设计原则：不猜、不缺项 —— 没有产物 / 没有版本号 / 没有下载基址都显式失败。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

MANIFEST_SCHEMA = 1
ARTIFACT_SUFFIXES = ('.zip', '.exe', '.msi', '.7z')


class ReleaseManifestError(Exception):
    pass


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def collect_artifacts(dist_dir: str) -> list:
    """收集发布产物（按文件名排序，排除 .json/.sha256 等校验文件）。"""
    root = Path(dist_dir)
    if not root.exists():
        raise ReleaseManifestError(f'产物目录不存在: {root}')
    files = [p for p in sorted(root.iterdir())
             if p.is_file() and p.suffix.lower() in ARTIFACT_SUFFIXES]
    if not files:
        raise ReleaseManifestError(
            f'产物目录里没有可发布文件（{"/".join(ARTIFACT_SUFFIXES)}）: {root}')
    return files


def build_manifest(version: str, channel: str, dist_dir: str, base_url: str,
                   notes: str = '', platform: str = 'windows-x64',
                   released_at: str = '') -> dict:
    """构造 manifest dict（不写盘；便于测试与脚本复用）。"""
    if not str(version or '').strip():
        raise ReleaseManifestError('version 不能为空')
    if channel not in ('stable', 'beta', 'nightly'):
        raise ReleaseManifestError(f'不支持的 channel: {channel!r}')
    if not str(base_url or '').strip():
        raise ReleaseManifestError('base_url 不能为空（artifact 需要可下载地址）')
    base = base_url.rstrip('/')
    artifacts = []
    for path in collect_artifacts(dist_dir):
        artifacts.append({
            'platform': platform,
            'name': path.name,
            'url': f'{base}/{path.name}',
            'sha256': sha256_file(path),
            'size': path.stat().st_size,
        })
    manifest = {
        'schema': MANIFEST_SCHEMA,
        'channel': channel,
        'version': str(version),
        'released_at': released_at or time.strftime('%Y-%m-%d'),
        'notes': notes,
        'artifacts': artifacts,
    }
    _self_validate(manifest)
    return manifest


def _self_validate(manifest: dict) -> None:
    """用产品的 updater 解析器自校验一次，避免生成不可用 manifest。"""
    try:
        from lightdb_studio.utils.updater import parse_manifest
    except Exception:                                # 脚本可独立运行
        return
    try:
        parse_manifest(manifest)
    except Exception as exc:                         # noqa: BLE001
        raise ReleaseManifestError(f'生成的 manifest 未通过产品校验: {exc}') from exc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='生成 LightDB Studio 发布 manifest')
    ap.add_argument('--version', required=True, help='发布版本，如 4.0.0-beta.19')
    ap.add_argument('--channel', default='stable',
                    choices=['stable', 'beta', 'nightly'])
    ap.add_argument('--dist', required=True, help='发布产物目录')
    ap.add_argument('--base-url', required=True, help='产物下载基址（http(s)://）')
    ap.add_argument('--platform', default='windows-x64')
    ap.add_argument('--notes', default='')
    ap.add_argument('--released-at', default='')
    ap.add_argument('--out', default='', help='输出路径（默认 <dist>/release_manifest.json）')
    args = ap.parse_args(argv)

    try:
        manifest = build_manifest(
            version=args.version, channel=args.channel, dist_dir=args.dist,
            base_url=args.base_url, notes=args.notes,
            platform=args.platform, released_at=args.released_at)
    except ReleaseManifestError as exc:
        print(f'[release-manifest] 失败: {exc}', file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else Path(args.dist) / 'release_manifest.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    print(f'[release-manifest] 写入 {out}（{len(manifest["artifacts"])} 个产物）')
    for a in manifest['artifacts']:
        print(f'  - {a["name"]}  {a["size"]} bytes  sha256={a["sha256"][:16]}…')
    return 0


if __name__ == '__main__':
    sys.exit(main())
