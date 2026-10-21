"""P3 发布工程：更新检查/下载校验 + 发布 manifest 生成 的单元测试。

全部离线（file:// / tmp 文件），不依赖网络与真实 GUI。
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(version='4.0.1', channel='stable', artifact_url='',
              sha='', size=0, platform='windows-x64'):
    return {
        'schema': 1,
        'channel': channel,
        'version': version,
        'released_at': '2026-10-21',
        'notes': 'test release',
        'artifacts': [] if not artifact_url else [{
            'platform': platform,
            'name': 'LightDB-Studio.zip',
            'url': artifact_url,
            'sha256': sha,
            'size': size,
        }],
    }


def _write_manifest(tmp_path, obj):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    return p


# ==================== 版本比较 ====================

def test_parse_and_compare_versions():
    from lightdb_studio.utils.updater import parse_version, is_newer
    assert parse_version('4.0.0') == ((4, 0, 0), '')
    assert parse_version('v4.0.0-beta.18') == ((4, 0, 0), 'beta.18')
    assert parse_version('4.0') == ((4, 0, 0), '')
    assert is_newer('4.0.1', '4.0.0')
    assert is_newer('4.1.0', '4.0.9')
    assert not is_newer('4.0.0', '4.0.0')
    assert not is_newer('4.0.0-beta.18', '4.0.0')
    assert is_newer('4.0.0', '4.0.0-beta.18')
    assert is_newer('4.0.0-beta.19', '4.0.0-beta.18')
    assert not is_newer('4.0.0-beta.18', '4.0.0-beta.19')


def test_parse_version_rejects_bad():
    from lightdb_studio.utils.updater import parse_version, UpdateError
    for bad in ('', 'abc', '4.0.0.0', '4.x.0', None):
        with pytest.raises(UpdateError):
            parse_version(bad)


# ==================== manifest 解析/检查 ====================

def test_parse_manifest_valid_and_invalid():
    from lightdb_studio.utils.updater import parse_manifest, UpdateError
    ok = _manifest(artifact_url='https://x/y.zip', sha='a' * 64, size=10)
    assert parse_manifest(json.dumps(ok))['version'] == '4.0.1'

    with pytest.raises(UpdateError):
        parse_manifest('{bad json')
    with pytest.raises(UpdateError):
        parse_manifest({'schema': 2, 'version': '1.0.0', 'artifacts': []})
    with pytest.raises(UpdateError):
        parse_manifest({'schema': 1, 'version': 'bad', 'artifacts': []})
    with pytest.raises(UpdateError):
        parse_manifest({'schema': 1, 'version': '4.0.1', 'artifacts': {}})
    with pytest.raises(UpdateError):
        parse_manifest({'schema': 1, 'version': '4.0.1',
                        'artifacts': [{'url': 'x', 'sha256': 'abc'}]})


def test_check_update_available(tmp_path):
    from lightdb_studio.utils.updater import check_for_update
    url = 'https://example.invalid/studio.zip'
    m = _manifest(artifact_url=url, sha='a' * 64, size=10)
    p = _write_manifest(tmp_path, m)
    info = check_for_update(str(p), '4.0.0', channel='stable')
    assert info.available is True
    assert info.latest_version == '4.0.1'
    assert info.artifact is not None and info.artifact.url == url
    assert info.artifact.sha256 == 'a' * 64

    # file:// 形式也支持
    info2 = check_for_update(p.as_uri(), '4.0.0')
    assert info2.available is True


def test_check_update_not_available_reasons(tmp_path):
    from lightdb_studio.utils.updater import check_for_update
    # 已最新
    p = _write_manifest(tmp_path, _manifest(
        version='4.0.0', artifact_url='https://x/y.zip', sha='a' * 64))
    info = check_for_update(str(p), '4.0.0')
    assert info.available is False and '已是最新' in info.reason
    # 通道不匹配
    p = _write_manifest(tmp_path, _manifest(
        channel='beta', artifact_url='https://x/y.zip', sha='a' * 64))
    info = check_for_update(str(p), '4.0.0', channel='stable')
    assert info.available is False and '通道不匹配' in info.reason
    # 没有当前平台包
    p = _write_manifest(tmp_path, _manifest(
        artifact_url='https://x/y.zip', sha='a' * 64, platform='linux-x64'))
    info = check_for_update(str(p), '4.0.0', platform='windows-x64')
    assert info.available is False and 'windows-x64' in info.reason


def test_check_update_manifest_missing(tmp_path):
    from lightdb_studio.utils.updater import check_for_update, UpdateError
    with pytest.raises(UpdateError):
        check_for_update(str(tmp_path / 'nope.json'), '4.0.0')


# ==================== 下载 + 校验 ====================

def test_download_update_ok(tmp_path):
    from lightdb_studio.utils.updater import (
        Artifact, download_update, sha256_file)
    payload = b'fake-studio-zip-bytes'
    src = tmp_path / 'LightDB-Studio.zip'
    src.write_bytes(payload)
    art = Artifact(name='LightDB-Studio.zip', url=str(src),
                   sha256=_sha(payload), size=len(payload))
    out = download_update(art, tmp_path / 'dl')
    assert out.read_bytes() == payload
    assert sha256_file(out) == _sha(payload)


def test_download_update_rejects_bad_checksum_and_missing_sha(tmp_path):
    from lightdb_studio.utils.updater import Artifact, download_update, UpdateError
    payload = b'data'
    src = tmp_path / 'pkg.zip'
    src.write_bytes(payload)
    with pytest.raises(UpdateError) as ei:
        download_update(Artifact(name='pkg.zip', url=str(src),
                                 sha256='0' * 64, size=4), tmp_path / 'dl1')
    assert 'sha256' in str(ei.value)
    assert not (tmp_path / 'dl1' / 'pkg.zip').exists(), '校验失败不得留下文件'

    with pytest.raises(UpdateError) as ei:
        download_update(Artifact(name='pkg.zip', url=str(src), sha256=''),
                        tmp_path / 'dl2')
    assert 'sha256' in str(ei.value)


def test_download_update_rejects_traversal_name(tmp_path):
    from lightdb_studio.utils.updater import Artifact, download_update
    payload = b'x'
    src = tmp_path / 'pkg.zip'
    src.write_bytes(payload)
    art = Artifact(name='../../evil.zip', url=str(src),
                   sha256=_sha(payload), size=1)
    out = download_update(art, tmp_path / 'dl')
    assert out.name == 'evil.zip'
    assert out.parent == tmp_path / 'dl'


# ==================== 发布 manifest 生成脚本 ====================

def test_make_release_manifest_builds_valid_manifest(tmp_path):
    sys.path.insert(0, str(ROOT / 'scripts'))
    import make_release_manifest as mrm
    dist = tmp_path / 'dist'
    dist.mkdir()
    payload = b'zipdata'
    (dist / 'LightDB-Studio.zip').write_bytes(payload)
    manifest = mrm.build_manifest(
        version='4.0.1', channel='stable', dist_dir=str(dist),
        base_url='https://example.com/downloads',
        notes='notes', platform='windows-x64')
    assert manifest['schema'] == 1
    assert manifest['version'] == '4.0.1'
    art = manifest['artifacts'][0]
    assert art['sha256'] == _sha(payload)
    assert art['size'] == len(payload)
    assert art['url'] == 'https://example.com/downloads/LightDB-Studio.zip'
    # 生成的 manifest 必须能被 updater 解析
    from lightdb_studio.utils.updater import parse_manifest, check_for_update
    parse_manifest(manifest)
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps(manifest), encoding='utf-8')
    info = check_for_update(str(p), '4.0.0')
    assert info.available is True


def test_make_release_manifest_requires_artifacts(tmp_path):
    sys.path.insert(0, str(ROOT / 'scripts'))
    import make_release_manifest as mrm
    empty = tmp_path / 'empty'
    empty.mkdir()
    with pytest.raises(mrm.ReleaseManifestError):
        mrm.build_manifest(version='4.0.1', channel='stable',
                           dist_dir=str(empty), base_url='https://x')


# ==================== UI 提示文本 ====================

def test_format_update_notice_available_and_not(tmp_path):
    from lightdb_studio.utils.updater import (
        check_for_update, format_update_notice)
    url = 'https://example.invalid/studio.zip'
    m = _manifest(version='4.0.1', artifact_url=url, sha='a' * 64, size=123)
    p = _write_manifest(tmp_path, m)

    info = check_for_update(str(p), '4.0.0')
    text = format_update_notice(info)
    assert '发现新版本：4.0.1' in text
    assert url in text
    assert 'a' * 64 in text
    assert '不会自动下载/安装' in text

    info2 = check_for_update(str(p), '4.0.1')
    text2 = format_update_notice(info2)
    assert '当前版本：4.0.1' in text2
    assert '已是最新' in text2
