"""LightDB Studio 打包脚本（PyInstaller）。

用法::

    python scripts/build_studio.py                 # onedir: dist/studio/LightDB-Studio/
    python scripts/build_studio.py --onefile       # onefile: dist/studio-onefile/LightDB-Studio.exe
    python scripts/build_studio.py --no-smoke      # 只构建，不跑 --smoke 验证
    python scripts/build_studio.py --manifest --base-url https://example.com/dl \
        --version 4.0.0-beta.19                    # 构建 + 生成 release_manifest.json

设计原则：
- 构建失败 / 冒烟失败 / 产物缺失 → 非 0 退出并打印原因（不假成功）；
- **打包产物必须通过 `--smoke` 启动验证**（offscreen 构造窗口后自动退出），
  否则视为构建失败；
- 不自动签名（签名需要证书；用 scripts/sign_windows.ps1 单独做，可选）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / 'packaging' / 'lightdb_studio.spec'
APP_NAME = 'LightDB-Studio'


def _artifact_path(dist_dir: Path, onefile: bool) -> Path:
    if onefile:
        return dist_dir / f'{APP_NAME}.exe'
    return dist_dir / APP_NAME / f'{APP_NAME}.exe'


def _smoke_test(exe: Path, timeout: int = 180) -> tuple:
    """用 `--smoke` 真实启动打包产物（offscreen），返回 (ok, output)。"""
    env = dict(os.environ)
    env['QT_QPA_PLATFORM'] = 'offscreen'
    env['LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN'] = '1'
    env.pop('LIGHTDB_STUDIO_STARTUP_TIMING', None)
    # 用临时 HOME，避免污染用户配置/连接文件
    tmp_home = tempfile.mkdtemp(prefix='lightdb_studio_smoke_')
    env['HOME'] = tmp_home
    env['USERPROFILE'] = tmp_home
    try:
        proc = subprocess.run(
            [str(exe), '--smoke'], cwd=str(exe.parent), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, text=True, encoding='utf-8', errors='replace')
        out = proc.stdout or ''
        return proc.returncode == 0, out[-4000:]
    except subprocess.TimeoutExpired as exc:
        return False, f'--smoke 超时（>{timeout}s）: {exc}'
    except Exception as exc:                          # noqa: BLE001
        return False, f'--smoke 启动失败: {exc}'
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)


def build(onefile: bool = False, clean: bool = True, smoke: bool = True,
          dist_root: Path = None, work_root: Path = None,
          onefile_tmpdir: str = '') -> dict:
    """执行 PyInstaller 构建；返回 {ok, artifact, size, smoke_output, log_tail}。"""
    if not SPEC.exists():
        raise SystemExit(f'spec 不存在: {SPEC}')
    dist_root = Path(dist_root or (ROOT / 'dist'))
    work_root = Path(work_root or (ROOT / 'build'))
    out_dist = dist_root / ('studio-onefile' if onefile else 'studio')
    out_work = work_root / ('studio-onefile' if onefile else 'studio')
    env = dict(os.environ)
    env['LIGHTDB_STUDIO_ONEFILE'] = '1' if onefile else '0'
    if onefile_tmpdir:
        env['LIGHTDB_STUDIO_ONEFILE_TMPDIR'] = str(Path(onefile_tmpdir).resolve())
    else:
        env.pop('LIGHTDB_STUDIO_ONEFILE_TMPDIR', None)
    cmd = [sys.executable, '-m', 'PyInstaller', str(SPEC),
           '--noconfirm', '--distpath', str(out_dist),
           '--workpath', str(out_work)]
    if clean:
        cmd.append('--clean')
    print('[build-studio] 模式:', 'onefile' if onefile else 'onedir')
    print('[build-studio] 命令:', ' '.join(cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace')
    log = proc.stdout or ''
    if proc.returncode != 0:
        print(log[-8000:])
        return {'ok': False, 'artifact': None, 'size': 0,
                'smoke_output': '', 'log_tail': log[-8000:]}
    exe = _artifact_path(out_dist, onefile)
    if not exe.exists():
        return {'ok': False, 'artifact': None, 'size': 0,
                'smoke_output': '', 'log_tail': '已构建但找不到产物: %s' % exe}
    size = exe.stat().st_size
    smoke_ok, smoke_out = (True, '(skipped)') if not smoke else _smoke_test(exe)
    return {'ok': smoke_ok, 'artifact': exe, 'size': size,
            'smoke_output': smoke_out, 'log_tail': log[-8000:]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='LightDB Studio PyInstaller 打包')
    ap.add_argument('--onefile', action='store_true', help='单文件模式')
    ap.add_argument('--no-clean', action='store_true', help='不清理缓存')
    ap.add_argument('--no-smoke', action='store_true', help='构建后不跑 --smoke 验证')
    ap.add_argument('--dist', default='', help='dist 根目录（默认 <repo>/dist）')
    ap.add_argument('--work', default='', help='work 根目录（默认 <repo>/build）')
    ap.add_argument('--onefile-tmpdir', default='',
                    help='单文件版解压目录（TEMP 被安全软件/沙箱阻止时指定可写目录）')
    ap.add_argument('--manifest', action='store_true', help='构建后生成 release_manifest.json')
    ap.add_argument('--version', default='', help='manifest 版本（--manifest 时必填）')
    ap.add_argument('--channel', default='stable', choices=['stable', 'beta', 'nightly'])
    ap.add_argument('--base-url', default='', help='产物下载基址（--manifest 时必填）')
    ap.add_argument('--notes', default='', help='发布说明')
    args = ap.parse_args(argv)

    if args.manifest and (not args.version or not args.base_url):
        print('[build-studio] --manifest 需要同时提供 --version/--base-url',
              file=sys.stderr)
        return 2

    result = build(onefile=args.onefile, clean=not args.no_clean,
                   smoke=not args.no_smoke,
                   dist_root=Path(args.dist).resolve() if args.dist else None,
                   work_root=Path(args.work).resolve() if args.work else None,
                   onefile_tmpdir=args.onefile_tmpdir)
    if not result['ok']:
        print('[build-studio] 构建或冒烟验证失败', file=sys.stderr)
        if result.get('smoke_output'):
            print(result['smoke_output'][-3000:], file=sys.stderr)
        return 1
    exe = result['artifact']
    print(f'[build-studio] 产物: {exe}  ({result["size"]:,} bytes)')
    if result['smoke_output'] != '(skipped)':
        print('[build-studio] --smoke 启动验证: OK')

    if args.manifest:
        sys.path.insert(0, str(ROOT / 'scripts'))
        from make_release_manifest import build_manifest, ReleaseManifestError
        dist_dir = exe.parent if args.onefile else exe.parent.parent
        try:
            manifest = build_manifest(
                version=args.version, channel=args.channel,
                dist_dir=str(dist_dir), base_url=args.base_url,
                notes=args.notes)
        except ReleaseManifestError as exc:
            print(f'[build-studio] manifest 生成失败: {exc}', file=sys.stderr)
            return 1
        import json
        out = dist_dir / 'release_manifest.json'
        out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                       encoding='utf-8')
        print(f'[build-studio] manifest: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
