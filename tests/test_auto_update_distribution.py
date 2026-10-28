"""Wave 2：自动更新下发链路的测试（三分支 + SHA256 篡改显式失败 + 版本比较）。

链路口径与 `scripts/verify_auto_update.py` 完全一致：

    release_manifest.json（file:// / 本地 HTTP）
        → updater.check_for_update() 解析/版本比较/选平台产物
        → updater.download_update() 下载到临时目录 + SHA256/大小校验
        → 只下载+校验，**不自动安装**（由用户手动安装）

覆盖：
* 无更新分支（manifest 版本 == 当前版本 → available=False + 明确 reason）；
* 新版本分支（file:// 与本地 HTTP 两种传输 → 可下载、SHA256/内容一致、
  无半成品临时文件、format_update_notice 文案）；
* 校验失败分支（manifest sha256 写错 / 产物被篡改 / 缺 sha256 → 显式
  UpdateError，且不留下不可信文件）；
* 版本比较（parse_version/is_newer）与 manifest 解析非法字段；
* updater 不暴露自动安装/替换 API。

运行：python -m pytest tests/test_auto_update_distribution.py -q
"""
import hashlib
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from lightdb_studio.utils import updater            # noqa: E402
from make_release_manifest import build_manifest, sha256_file  # noqa: E402

CURRENT = '4.0.0'
NEWER = '4.0.1'


# ==================== 工具 ====================

def _release_dir(tmp_path, version=NEWER, tamper_manifest_sha=False,
                 drop_sha=False, file_url=True):
    """造一个发布目录：产物 + release_manifest.json。"""
    rel = tmp_path / ('release_%s' % version)
    rel.mkdir()
    payload = ('LightDB Studio %s payload\n' % version).encode('utf-8')
    name = 'LightDB-Studio-%s-win64.zip' % version
    (rel / name).write_bytes(payload)
    base_url = rel.as_uri() if file_url else 'https://example.invalid/dl'
    manifest = build_manifest(version=version, channel='stable',
                              dist_dir=str(rel), base_url=base_url)
    if tamper_manifest_sha:
        manifest['artifacts'][0]['sha256'] = '0' * 64
    if drop_sha:
        manifest['artifacts'][0]['sha256'] = ''
    (rel / 'release_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    return rel, name, payload, manifest


def _file_manifest(rel: Path) -> str:
    return str(rel / 'release_manifest.json')


# ==================== 分支 1：无更新 ====================

def test_no_update_branch(tmp_path):
    rel, _name, _payload, manifest = _release_dir(tmp_path, version=CURRENT)
    info = updater.check_for_update(_file_manifest(rel), CURRENT)
    assert info.available is False
    assert info.artifact is None
    assert '已是最新' in (info.reason or '')
    assert info.latest_version == CURRENT
    # manifest 版本本身必须可解析
    assert updater.parse_manifest(manifest)['version'] == CURRENT


# ==================== 分支 2：新版本（file:// / HTTP） ====================

def test_new_version_file_transport_download_and_verify(tmp_path):
    rel, name, payload, _manifest = _release_dir(tmp_path)
    info = updater.check_for_update(_file_manifest(rel), CURRENT)
    assert info.available is True
    assert info.latest_version == NEWER
    assert info.artifact is not None and info.artifact.name == name
    notice = updater.format_update_notice(info)
    assert NEWER in notice and '不会自动下载/安装' in notice

    dest = tmp_path / 'downloads'
    got = updater.download_update(info.artifact, dest)
    assert got.is_file() and got.name == name
    assert got.read_bytes() == payload
    assert sha256_file(got) == info.artifact.sha256
    assert got.stat().st_size == info.artifact.size
    # 下载成功后不应残留半成品临时文件
    assert not [p for p in dest.iterdir()
                if p.name.startswith('.lightdb_update_')]


def test_new_version_http_transport(tmp_path):
    import http.server

    rel, name, payload, _manifest = _release_dir(tmp_path, file_url=False)
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(rel), **kw)
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        manifest = build_manifest(version=NEWER, channel='stable',
                                  dist_dir=str(rel),
                                  base_url='http://127.0.0.1:%d' % port)
        (rel / 'release_manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        info = updater.check_for_update(
            'http://127.0.0.1:%d/release_manifest.json' % port, CURRENT)
        assert info.available is True and info.artifact.name == name
        got = updater.download_update(info.artifact, tmp_path / 'dl_http')
        assert got.read_bytes() == payload
        assert sha256_file(got) == info.artifact.sha256
    finally:
        httpd.shutdown()
        httpd.server_close()


# ==================== 分支 3：校验失败 / 非法 manifest（显式失败） ====================

def test_checksum_mismatch_is_explicit_and_leaves_no_file(tmp_path):
    rel, _name, _payload, _manifest = _release_dir(
        tmp_path, tamper_manifest_sha=True)
    info = updater.check_for_update(_file_manifest(rel), CURRENT)
    assert info.available is True
    dest = tmp_path / 'downloads_bad'
    with pytest.raises(updater.UpdateError) as ei:
        updater.download_update(info.artifact, dest)
    assert 'sha256' in str(ei.value).lower()
    leftovers = list(dest.iterdir()) if dest.exists() else []
    assert leftovers == [], '校验失败后不得留下不可信/半成品文件'


def test_tampered_artifact_after_manifest_fails(tmp_path):
    rel, name, _payload, _manifest = _release_dir(tmp_path)
    (rel / name).write_bytes(b'tampered after manifest was written')
    info = updater.check_for_update(_file_manifest(rel), CURRENT)
    with pytest.raises(updater.UpdateError):
        updater.download_update(info.artifact, tmp_path / 'downloads_tamper')


def test_artifact_without_sha256_is_refused(tmp_path):
    rel, _name, _payload, _manifest = _release_dir(tmp_path, drop_sha=True)
    info = updater.check_for_update(_file_manifest(rel), CURRENT)
    with pytest.raises(updater.UpdateError):
        updater.download_update(info.artifact, tmp_path / 'downloads_nosha')


def test_bad_manifest_fields_raise(tmp_path):
    rel, _name, _payload, _manifest = _release_dir(tmp_path)
    path = rel / 'release_manifest.json'
    bad = json.loads(path.read_text(encoding='utf-8'))
    bad['artifacts'] = {'not': 'a list'}
    path.write_text(json.dumps(bad), encoding='utf-8')
    with pytest.raises(updater.UpdateError):
        updater.check_for_update(_file_manifest(rel), CURRENT)

    path.write_text('{broken json', encoding='utf-8')
    with pytest.raises(updater.UpdateError):
        updater.check_for_update(_file_manifest(rel), CURRENT)


# ==================== 版本比较 / 无自动安装 ====================

def test_version_comparison_semantics():
    assert updater.parse_version('4.0.0') == ((4, 0, 0), '')
    assert updater.parse_version('v4.0.0-beta.18') == ((4, 0, 0), 'beta.18')
    assert updater.is_newer(NEWER, CURRENT)
    assert not updater.is_newer(CURRENT, CURRENT)
    assert updater.is_newer('4.0.0', '4.0.0-beta.18')
    assert not updater.is_newer('4.0.0-beta.18', '4.0.0')


def test_updater_has_no_auto_install_surface():
    forbidden = [n for n in dir(updater)
                 if any(k in n.lower() for k in ('install', 'apply', 'replace',
                                                 'execute'))]
    assert forbidden == [], '更新链路不得提供自动安装/替换 API: %r' % forbidden
    # 只下载+校验：format_update_notice 文案明确告知不会自动安装
    rel, _name, _payload, _manifest = _release_dir(tmp_pathish := Path(
        __import__('tempfile').mkdtemp(prefix='ld_auto_upd_test_')))
    try:
        info = updater.check_for_update(_file_manifest(rel), CURRENT)
        assert '不会自动下载/安装' in updater.format_update_notice(info)
    finally:
        import shutil
        shutil.rmtree(tmp_pathish, ignore_errors=True)
