"""自动更新下发端到端验证（发布打磨 / Wave 2）。

链路（**只下载 + SHA256 校验，不做自动安装**）：

    release_manifest.json（本地 HTTP 或 file://）
        → updater.check_for_update()    解析 / 版本比较 / 选择平台产物
        → updater.download_update()     下载到临时目录 + SHA256/大小校验
        → 报告下载路径（由用户决定是否安装；本工具不替换任何可执行文件）

三分支端到端验证：
1. **无更新**：manifest 版本 == 当前版本 → `available=False`（reason 明确）；
2. **新版本**：manifest 版本更高 → 可选可下载；下载后 SHA256/大小与 manifest
   一致，文件内容正确；
3. **校验失败**：manifest 的 sha256 与真实产物不符（或产物被篡改）→
   `download_update()` 抛 `UpdateError`（显式失败）、不留半成品文件。

用法::

    python scripts/verify_auto_update.py            # 跑三分支 + HTTP/file:// 两种传输，PASS/FAIL 摘要
    python scripts/verify_auto_update.py --json out.json

退出码：0 = 全部通过；1 = 有失败项（显式退出，不假成功）。
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from lightdb_studio.utils import updater          # noqa: E402
from make_release_manifest import (               # noqa: E402
    ReleaseManifestError, build_manifest, sha256_file)


def _free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):              # noqa: D401
        pass


class LocalHttpServer:
    """临时静态 HTTP 服务器（serve release 目录；只绑 127.0.0.1）。"""

    def __init__(self, directory):
        self.directory = str(directory)
        self.port = _free_port()
        handler = lambda *a, **kw: _QuietHandler(   # noqa: E731
            *a, directory=self.directory, **kw)
        self.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', self.port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    @property
    def base_url(self) -> str:
        return 'http://127.0.0.1:%d' % self.port

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
        return False


def _bump_patch(version: str) -> str:
    nums, pre = updater.parse_version(version)
    return '%d.%d.%d' % (nums[0], nums[1], nums[2] + 1)


def _make_release(root: Path, version: str, base_url: str,
                  tamper_sha: bool = False):
    """在 root 下造一个发布产物 + release_manifest.json。"""
    root.mkdir(parents=True, exist_ok=True)
    payload = ('LightDB Studio %s update payload\n' % version).encode('utf-8')
    artifact = root / ('LightDB-Studio-%s-win64.zip' % version)
    artifact.write_bytes(payload)
    manifest = build_manifest(
        version=version, channel='stable', dist_dir=str(root),
        base_url=base_url, notes='auto-update verification artifact')
    if tamper_sha:
        manifest['artifacts'][0]['sha256'] = '0' * 64
    (root / 'release_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    return artifact, manifest


def _check(checks, check_id, ok, detail, evidence=None):
    item = {'id': check_id, 'status': 'PASS' if ok else 'FAIL',
            'detail': detail}
    if evidence is not None:
        item['evidence'] = evidence
    checks.append(item)
    print('[%s] %-34s %s' % (item['status'], check_id, detail))
    return ok


def verify_no_update(work: Path, checks) -> None:
    """分支 1：manifest 版本 == 当前版本 -> 无更新。"""
    from lightdb import __version__ as current_version
    rel = work / 'file_release'
    artifact, manifest = _make_release(rel, current_version,
                                       'file://' + rel.as_posix())
    try:
        info = updater.check_for_update(rel / 'release_manifest.json',
                                        current_version)
        ok = (info.available is False and '已是最新' in (info.reason or ''))
        _check(checks, 'no_update_branch', ok,
               'manifest=%s current=%s available=%s reason=%r'
               % (manifest['version'], current_version, info.available,
                  (info.reason or '')[:80]))
        _check(checks, 'no_update_no_download', info.artifact is None,
               '无更新时不得给出下载 artifact（artifact=%r）' % (info.artifact,))
    except Exception as exc:                         # noqa: BLE001
        _check(checks, 'no_update_branch', False,
               '无更新分支异常: %s: %s' % (type(exc).__name__, exc))


def verify_new_version_http(work: Path, checks) -> Path:
    """分支 2：新版本 + 本地 HTTP 服务 -> 检查更新 -> 下载 -> SHA256 校验。"""
    from lightdb import __version__ as current_version
    new_version = _bump_patch(current_version)
    rel = work / 'http_release'
    rel.mkdir(parents=True, exist_ok=True)
    payload = ('LightDB Studio %s http payload\n' % new_version).encode()
    name = 'LightDB-Studio-%s-win64.zip' % new_version
    artifact = rel / name
    artifact.write_bytes(payload)
    with LocalHttpServer(rel) as srv:
        manifest = build_manifest(version=new_version, channel='stable',
                                  dist_dir=str(rel), base_url=srv.base_url,
                                  notes='http auto-update verification')
        (rel / 'release_manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        try:
            info = updater.check_for_update(srv.base_url + '/release_manifest.json',
                                            current_version)
            artifact_info = info.artifact
            ok = (info.available is True and artifact_info is not None
                  and artifact_info.name == name)
            _check(checks, 'new_version_check_http', ok,
                   'current=%s latest=%s available=%s artifact=%r'
                   % (current_version, info.latest_version, info.available,
                      artifact_info.name if artifact_info else None),
                   {'notice': updater.format_update_notice(info)[:300]})
            if ok:
                dest = work / 'downloads_http'
                got = updater.download_update(artifact_info, dest)
                actual = sha256_file(got)
                same = actual.lower() == artifact_info.sha256.lower()
                _check(checks, 'new_version_download_http',
                       got.is_file() and got.read_bytes() == payload and same,
                       '下载=%s size=%d sha256=%s… 与 manifest%s'
                       % (got.name, got.stat().st_size, actual[:16],
                          '一致' if same else '不一致'))
                leftovers = [p.name for p in dest.iterdir()
                             if p.name.startswith('.lightdb_update_')]
                _check(checks, 'download_no_leftover_temp', not leftovers,
                       '下载后无半成品临时文件（%r）' % leftovers)
                return got
        except Exception as exc:                     # noqa: BLE001
            _check(checks, 'new_version_check_http', False,
                   '新版本下载分支异常: %s: %s' % (type(exc).__name__, exc))
    return work / 'downloads_http' / name


def verify_file_transport_and_version_compare(work: Path, checks) -> None:
    """file:// 传输 + 版本比较辅助函数（新版本可下载）。"""
    from lightdb import __version__ as current_version
    new_version = _bump_patch(current_version)
    rel = work / 'file_update'
    rel.mkdir(parents=True, exist_ok=True)
    payload = b'file transport payload'
    artifact = rel / ('LightDB-Studio-%s-win64.zip' % new_version)
    artifact.write_bytes(payload)
    manifest = build_manifest(version=new_version, channel='stable',
                              dist_dir=str(rel),
                              base_url='file://' + rel.as_posix())
    (rel / 'release_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    try:
        info = updater.check_for_update(rel / 'release_manifest.json',
                                        current_version)
        ok = info.available is True and info.artifact is not None
        _check(checks, 'file_transport_check', ok,
               'file:// manifest 可解析并发现新版本 %s' % info.latest_version)
        if ok:
            got = updater.download_update(info.artifact, work / 'downloads_file')
            _check(checks, 'file_transport_download',
                   got.is_file() and got.read_bytes() == payload,
                   'file:// 下载成功: %s (%d B)' % (got.name,
                                                   got.stat().st_size))
    except Exception as exc:                         # noqa: BLE001
        _check(checks, 'file_transport_check', False,
               'file:// 分支异常: %s: %s' % (type(exc).__name__, exc))
    # 版本比较语义
    try:
        ok = (updater.is_newer(_bump_patch(current_version), current_version)
              and not updater.is_newer(current_version, current_version)
              and updater.parse_version('4.0.0') == ((4, 0, 0), ''))
        _check(checks, 'version_compare', ok,
               'is_newer/parse_version 语义正确（%s > %s）'
               % (_bump_patch(current_version), current_version))
    except Exception as exc:                         # noqa: BLE001
        _check(checks, 'version_compare', False,
               '版本比较异常: %s: %s' % (type(exc).__name__, exc))


def verify_checksum_failure(work: Path, checks) -> None:
    """分支 3：manifest 的 sha256 与真实产物不符 -> 显式失败、无半成品。"""
    from lightdb import __version__ as current_version
    new_version = _bump_patch(current_version)
    rel = work / 'bad_release'
    rel.mkdir(parents=True, exist_ok=True)
    payload = b'tampered-update-payload'
    name = 'LightDB-Studio-%s-win64.zip' % new_version
    (rel / name).write_bytes(payload)
    manifest = build_manifest(version=new_version, channel='stable',
                              dist_dir=str(rel),
                              base_url='file://' + rel.as_posix())
    manifest['artifacts'][0]['sha256'] = '0' * 64      # 故意写错校验和
    (rel / 'release_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    dest = work / 'downloads_bad'
    try:
        info = updater.check_for_update(rel / 'release_manifest.json',
                                        current_version)
        try:
            updater.download_update(info.artifact, dest)
            _check(checks, 'checksum_failure_explicit', False,
                   '校验和错误却下载成功（必须显式失败）')
        except updater.UpdateError as exc:
            text = str(exc)
            ok = 'sha256' in text.lower()
            _check(checks, 'checksum_failure_explicit', ok,
                   '校验和错误显式失败: %s' % text[:140])
        leftovers = [p.name for p in dest.iterdir()] if dest.exists() else []
        _check(checks, 'checksum_no_partial_file', not leftovers,
               '校验失败后目录为空（残留=%r）' % leftovers)
    except Exception as exc:                         # noqa: BLE001
        _check(checks, 'checksum_failure_explicit', False,
               '校验失败分支异常: %s: %s' % (type(exc).__name__, exc))


def verify_no_auto_install(checks) -> None:
    """确认更新链路不提供自动安装能力（只下载+校验）。"""
    forbidden = [n for n in dir(updater)
                 if any(k in n.lower() for k in ('install', 'apply', 'replace',
                                                 'execute'))]
    ok = not forbidden
    _check(checks, 'no_auto_install', ok,
           'updater 模块不暴露自动安装/替换 API（发现=%r）；下载后由用户手动安装'
           % forbidden)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='自动更新下发端到端验证（三分支；只下载+校验，不自动安装）')
    ap.add_argument('--json', default='',
                    help='把结果 JSON 写到该路径')
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    work = Path(tempfile.mkdtemp(prefix='lightdb_auto_update_'))
    checks = []
    print('=' * 78)
    print('LightDB 自动更新下发验证（local HTTP + file://；不自动安装）')
    print('=' * 78)
    try:
        verify_no_update(work, checks)
        verify_new_version_http(work, checks)
        verify_file_transport_and_version_compare(work, checks)
        verify_checksum_failure(work, checks)
        verify_no_auto_install(checks)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    n_fail = sum(1 for c in checks if c['status'] == 'FAIL')
    report = {
        'schema': 'lightdb-auto-update-verify/1',
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'duration_s': round(time.perf_counter() - t0, 2),
        'transport_tested': ['local http (127.0.0.1)', 'file://'],
        'auto_install': 'NOT implemented（仅下载+SHA256 校验，安装由用户手动执行）',
        'checks': checks,
        'summary': {'pass': len(checks) - n_fail, 'fail': n_fail,
                    'passed': n_fail == 0},
    }
    print('-' * 78)
    print('结果: PASS=%d FAIL=%d -> %s（%.2fs）'
          % (len(checks) - n_fail, n_fail,
             'PASS' if n_fail == 0 else 'FAIL', report['duration_s']))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        print('JSON: %s' % args.json)
    return 0 if n_fail == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
