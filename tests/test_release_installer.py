"""P6：Windows 安装包构建 / 安装卸载冒烟 / 发布校验的单元测试。

覆盖：
* 安装器工具检测（Inno / WiX v3 / WiX v4-only / 都没有）；
* `build_installer.py`：缺工具 -> 显式 skip（exit 3）+ 生成可复现脚本、
  fake 工具成功/失败/「命令成功但没产物 -> FAIL 不伪造」；
* `smoke_install()`：fake 安装器跑通「安装 -> 检查 exe/卸载项 -> exe --smoke
  -> 卸载 -> 清理」，以及安装失败/缺主程序等失败分支；
* `verify_release.py`：PASS 全链路、版本不一致、SHA256 不一致、缺产物、
  manifest 非法、updater 不可解析、未签名 WARN / --require-signed FAIL、
  SHA256SUMS、安装包构建报告；
* 真实环境无工具时「skip 不假过」。

运行：python -m pytest tests/test_release_installer.py -q
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import build_installer as bi       # noqa: E402
import verify_release as vr        # noqa: E402
import make_release_manifest as mrm  # noqa: E402

VERSION = '4.0.0'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ==================== 工具检测 ====================

def test_detect_tools_none(monkeypatch):
    det = bi.detect_installer_tools(which=lambda name: None,
                                    candidates={'inno': [], 'wix': []})
    assert det['inno']['available'] is False
    assert det['wix']['available'] is False
    tool, reason = bi.choose_tool(det)
    assert tool is None
    assert '未检测到' in reason


def test_detect_tools_fake_inno(tmp_path):
    iscc = tmp_path / 'ISCC.exe'
    iscc.write_bytes(b'fake')
    det = bi.detect_installer_tools(
        which=lambda name: str(iscc) if name.lower() == 'iscc.exe' else None,
        candidates={'inno': [], 'wix': []})
    assert det['inno']['available'] is True
    assert det['inno']['command'] == str(iscc)
    tool, _ = bi.choose_tool(det)
    assert tool == 'inno'


def test_detect_tools_fake_wix_v3_and_v4_only(tmp_path):
    candle = tmp_path / 'candle.exe'
    light = tmp_path / 'light.exe'
    candle.write_bytes(b'fake')
    light.write_bytes(b'fake')

    def which_v3(name):
        return {'candle.exe': str(candle), 'candle': str(candle),
                'light.exe': str(light), 'light': str(light)}.get(name)

    det = bi.detect_installer_tools(which=which_v3,
                                    candidates={'inno': [], 'wix': []})
    assert det['wix']['available'] is True
    assert det['wix']['variant'] == 'v3' and det['wix']['supported'] is True
    assert bi.choose_tool(det)[0] == 'wix'

    # 只有 WiX v4 的 wix.exe：检测到但本脚本按 v3 编写 -> 不能用，明确说明
    wix4 = tmp_path / 'wix.exe'
    wix4.write_bytes(b'fake')
    det4 = bi.detect_installer_tools(
        which=lambda name: str(wix4) if name in ('wix.exe', 'wix') else None,
        candidates={'inno': [], 'wix': []})
    assert det4['wix']['available'] is True
    assert det4['wix']['supported'] is False
    tool, reason = bi.choose_tool(det4)
    assert tool is None and 'WiX v4' in reason


def test_get_version_and_invalid():
    assert bi.get_version() == bi.get_version('')          # 默认 lightdb.__version__
    assert bi.get_version('4.2.0-beta.1') == '4.2.0-beta.1'
    with pytest.raises(ValueError):
        bi.get_version('not-a-version')


# ==================== build_installer：fake 工具路径 ====================

def _fake_source(tmp_path):
    src = tmp_path / 'onedir' / 'LightDB-Studio'
    src.mkdir(parents=True)
    (src / 'LightDB-Studio.exe').write_bytes(b'MZ-fake-exe')
    (src / '_internal').mkdir()
    (src / '_internal' / 'x.dll').write_bytes(b'fake-dll')
    return src


def _fake_detection(tmp_path, tool='inno'):
    if tool == 'inno':
        iscc = tmp_path / 'fake' / 'ISCC.exe'
        iscc.parent.mkdir(parents=True, exist_ok=True)
        iscc.write_bytes(b'fake')
        return {'inno': {'available': True, 'command': str(iscc)},
                'wix': {'available': False, 'variant': None,
                        'candle': None, 'light': None, 'supported': False}}
    candle = tmp_path / 'fake' / 'candle.exe'
    light = tmp_path / 'fake' / 'light.exe'
    candle.parent.mkdir(parents=True, exist_ok=True)
    candle.write_bytes(b'fake')
    light.write_bytes(b'fake')
    return {'inno': {'available': False, 'command': None},
            'wix': {'available': True, 'variant': 'v3',
                    'candle': str(candle), 'light': str(light),
                    'supported': True}}


def _fake_inno_runner(version):
    def runner(cmd, cwd=None):
        cmd = [str(c) for c in cmd]
        out_dir = next(c.split('=', 1)[1] for c in cmd
                       if c.startswith('/DOutputDir='))
        base = next(c.split('=', 1)[1] for c in cmd
                    if c.startswith('/DOutputBaseFilename='))
        target = Path(out_dir) / (base + '.exe')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'installer-bytes-' + version.encode())
        return 0, 'fake ISCC ok'
    return runner


def test_build_installer_fake_inno_ok(tmp_path):
    src = _fake_source(tmp_path)
    out = tmp_path / 'installer'
    report = bi.build_installer(
        version=VERSION, source=src, out=out,
        detect=lambda: _fake_detection(tmp_path, 'inno'),
        runner=_fake_inno_runner(VERSION))
    assert report['status'] == 'ok' and report['exit_code'] == 0
    art = Path(report['artifact'])
    assert art.is_file() and art.name == 'LightDB-Studio-Setup-4.0.0.exe'
    assert report['artifact_sha256'] == _sha(art.read_bytes())
    assert report['tool'] == 'inno'
    assert any('/DAppVersion=4.0.0' in c for c in report['commands_preview'])
    assert (out / 'build_report.json').is_file()
    assert report['signing']['status'] == 'not_signed'
    assert '未签名' in report['signing']['note']


def test_build_installer_fake_inno_failure(tmp_path):
    src = _fake_source(tmp_path)
    report = bi.build_installer(
        version=VERSION, source=src, out=tmp_path / 'out',
        detect=lambda: _fake_detection(tmp_path, 'inno'),
        runner=lambda cmd, cwd=None: (1, 'compiler error: boom'))
    assert report['status'] == 'failed' and report['exit_code'] == 1
    assert '失败' in report['reason']
    assert report['artifact'] is None


def test_build_installer_rc0_but_no_artifact_is_failure(tmp_path):
    src = _fake_source(tmp_path)
    report = bi.build_installer(
        version=VERSION, source=src, out=tmp_path / 'out',
        detect=lambda: _fake_detection(tmp_path, 'inno'),
        runner=lambda cmd, cwd=None: (0, 'pretend ok'))
    assert report['status'] == 'failed' and report['exit_code'] == 1
    assert '不伪造产物' in report['reason']


def test_build_installer_fake_wix_v3(tmp_path):
    src = _fake_source(tmp_path)

    def runner(cmd, cwd=None):
        cmd = [str(c) for c in cmd]
        if any('light.exe' in c.lower() for c in cmd):
            target = Path(cmd[cmd.index('-o') + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'msi-bytes')
        return 0, 'ok'

    report = bi.build_installer(
        version=VERSION, source=src, out=tmp_path / 'out',
        detect=lambda: _fake_detection(tmp_path, 'wix'), runner=runner)
    assert report['status'] == 'ok'
    assert report['tool'] == 'wix'
    assert Path(report['artifact']).name == 'LightDB-Studio-Setup-4.0.0.msi'
    assert len(report['commands_preview']) == 2       # candle + light 两条


def test_build_installer_missing_tool_skips_and_generates_repro(tmp_path):
    src = _fake_source(tmp_path)
    out = tmp_path / 'installer'
    report = bi.build_installer(
        version=VERSION, source=src, out=out,
        detect=lambda: {'inno': {'available': False, 'command': None},
                        'wix': {'available': False, 'variant': None,
                                'candle': None, 'light': None,
                                'supported': False}})
    assert report['status'] == 'skipped' and report['exit_code'] == 3
    assert '未检测到' in report['reason']
    assert report['artifact'] is None                  # 不伪造产物
    ps1 = out / 'build_installer.ps1'
    md = out / 'BUILD_INSTALLER.md'
    assert ps1.is_file() and md.is_file()
    text = ps1.read_text(encoding='ascii', errors='replace')
    assert 'ISCC.exe' in text and '--smoke' in text
    assert '未生成任何安装包' in md.read_text(encoding='utf-8')
    saved = json.loads((out / 'build_report.json').read_text(encoding='utf-8'))
    assert saved['status'] == 'skipped'
    # 复现脚本必须是 ASCII（Windows PowerShell 5.1 兼容）
    ps1.read_bytes().decode('ascii')


def test_build_installer_missing_source_is_explicit(tmp_path):
    report = bi.build_installer(
        version=VERSION, source=tmp_path / 'nope', out=tmp_path / 'out',
        detect=lambda: _fake_detection(tmp_path, 'inno'))
    assert report['status'] == 'failed' and report['exit_code'] == 1
    assert 'onedir 产物不存在' in report['reason']


def test_build_installer_dry_run_prints_commands(tmp_path):
    src = _fake_source(tmp_path)
    report = bi.build_installer(
        version=VERSION, source=src, out=tmp_path / 'out', dry_run=True,
        detect=lambda: _fake_detection(tmp_path, 'inno'))
    assert report['status'] == 'planned' and report['exit_code'] == 0
    assert report['commands_preview']
    assert not (tmp_path / 'out' / 'LightDB-Studio-Setup-4.0.0.exe').exists()


# ==================== 静默安装 / 卸载冒烟（fake 安装器） ====================

class _FakeInstallEnv:
    def __init__(self, fail_install=False, omit_exe=False):
        self.fail_install = fail_install
        self.omit_exe = omit_exe
        self.calls = []
        self.install_dir = None
        self.registry_calls = 0

    def runner(self, cmd, cwd=None, timeout=None):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        joined = ' '.join(cmd)
        first = Path(cmd[0]).name.lower()
        if '/VERYSILENT' in cmd and '/DIR=' in joined and not first.startswith('unins'):
            if self.fail_install:
                return 1, 'installer failed'
            self.install_dir = Path(
                next(c.split('=', 1)[1] for c in cmd if c.startswith('/DIR=')))
            self.install_dir.mkdir(parents=True, exist_ok=True)
            if not self.omit_exe:
                (self.install_dir / 'LightDB-Studio.exe').write_bytes(b'MZ')
            (self.install_dir / 'unins000.exe').write_bytes(b'MZ')
            return 0, 'installed'
        if '--smoke' in cmd:
            return 0, 'smoke ok'
        if first.startswith('unins000'):
            if self.install_dir:
                for name in ('LightDB-Studio.exe', 'unins000.exe'):
                    try:
                        (self.install_dir / name).unlink()
                    except OSError:
                        pass
            return 0, 'uninstalled'
        return 0, 'ok'

    def lookup(self, install_dir):
        self.registry_calls += 1
        return ([{'display_name': 'LightDB Studio'}]
                if self.registry_calls == 1 else None)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows 安装包冒烟')
def test_smoke_install_and_uninstall_ok(tmp_path):
    installer = tmp_path / 'LightDB-Studio-Setup-4.0.0.exe'
    installer.write_bytes(b'fake-installer')
    env = _FakeInstallEnv()
    result = bi.smoke_install(installer, workdir=tmp_path / 'work',
                              runner=env.runner,
                              registry_lookup=env.lookup, no_icons=True)
    assert result['status'] == 'ok' and result['exit_code'] == 0
    steps = {s['name']: s['status'] for s in result['steps']}
    assert steps['silent_install'] == 'PASS'
    assert steps['installed_exe'] == 'PASS'
    assert steps['exe_smoke'] == 'PASS'
    assert steps['silent_uninstall'] == 'PASS'
    assert steps['cleanup_install_dir'] == 'PASS'
    assert steps['uninstall_registry'] == 'PASS'
    assert steps['start_menu_shortcut'] == 'SKIP'      # /NOICONS
    assert env.registry_calls >= 2


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows 安装包冒烟')
def test_smoke_install_failure_branches(tmp_path):
    installer = tmp_path / 'setup.exe'
    installer.write_bytes(b'x')
    bad = bi.smoke_install(installer, workdir=tmp_path / 'w1',
                           runner=_FakeInstallEnv(fail_install=True).runner,
                           registry_lookup=lambda d: None, no_icons=True)
    assert bad['status'] == 'failed' and bad['exit_code'] == 1

    env = _FakeInstallEnv(omit_exe=True)
    noexe = bi.smoke_install(installer, workdir=tmp_path / 'w2',
                             runner=env.runner, registry_lookup=env.lookup,
                             no_icons=True)
    assert noexe['status'] == 'failed'
    assert '主程序' in noexe['reason']

    missing = bi.smoke_install(tmp_path / 'nope.exe')
    assert missing['status'] == 'skipped' and missing['exit_code'] == 3


# ==================== verify_release ====================

def _make_release_dir(tmp_path, version=VERSION, artifact_name=None,
                      manifest_version='', mutate_sha=False,
                      include_manifest=True, extra_exe=False):
    rel = tmp_path / 'release'
    rel.mkdir()
    name = artifact_name or ('LightDB-Studio-%s-win64.zip' % VERSION)
    payload = b'fake-release-zip-bytes'
    (rel / name).write_bytes(payload)
    if extra_exe:
        (rel / ('LightDB-Studio-%s.exe' % VERSION)).write_bytes(b'MZ-fake-exe')
    manifest = mrm.build_manifest(
        version=manifest_version or version, channel='stable',
        dist_dir=str(rel), base_url='https://example.invalid/dl')
    if mutate_sha:
        (rel / name).write_bytes(b'tampered')
    if include_manifest:
        (rel / 'release_manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
    return rel, manifest


def test_verify_release_pass(tmp_path):
    rel, _ = _make_release_dir(tmp_path)
    report = vr.verify_release(release_dir=rel, version=VERSION,
                               check_authenticode=False,
                               installer_report=str(tmp_path / 'none.json'),
                               json_out=str(tmp_path / 'report.json'))
    assert report['exit_code'] == 0, report['checks']
    assert report['summary']['fail'] == 0
    checks = {c['id']: c for c in report['checks']}
    assert checks['manifest_updater_parse']['status'] == 'PASS'
    assert checks['updater_check_for_update']['status'] == 'PASS'
    assert checks['artifact_version_names']['status'] == 'PASS'
    assert checks['installer_build']['status'] == 'SKIP'
    assert (tmp_path / 'report.json').is_file()


def test_verify_release_version_mismatch_fails(tmp_path):
    rel, _ = _make_release_dir(tmp_path, manifest_version='4.0.1')
    report = vr.verify_release(release_dir=rel, version=VERSION,
                               check_authenticode=False)
    assert report['exit_code'] == 1
    checks = {c['id']: c for c in report['checks']}
    assert checks['manifest_version']['status'] == 'FAIL'
    assert '4.0.1' in checks['manifest_version']['detail']


def test_verify_release_sha256_mismatch_fails(tmp_path):
    rel, _ = _make_release_dir(tmp_path, mutate_sha=True)
    report = vr.verify_release(release_dir=rel, version=VERSION,
                               check_authenticode=False)
    assert report['exit_code'] == 1
    assert any('sha256' in c['id'].lower() and c['status'] == 'FAIL'
               for c in report['checks'])


def test_verify_release_missing_artifact_fails(tmp_path):
    rel, _ = _make_release_dir(tmp_path)
    next(rel.glob('*.zip')).unlink()
    report = vr.verify_release(release_dir=rel, version=VERSION,
                               check_authenticode=False)
    assert report['exit_code'] == 1
    assert any('exists' in c['id'] and c['status'] == 'FAIL'
               for c in report['checks'])


def test_verify_release_bad_manifest_json_and_unparseable(tmp_path):
    rel, _ = _make_release_dir(tmp_path)
    (rel / 'release_manifest.json').write_text('{not json', encoding='utf-8')
    report = vr.verify_release(release_dir=rel, version=VERSION,
                               check_authenticode=False)
    assert report['exit_code'] == 1
    checks = {c['id']: c for c in report['checks']}
    assert checks['manifest_json']['status'] == 'FAIL'

    rel2 = tmp_path / 'release2'
    rel2.mkdir()
    (rel2 / 'LightDB-Studio-4.0.0-win64.zip').write_bytes(b'z')
    bad = {'schema': 2, 'version': '4.0.0', 'artifacts': []}
    (rel2 / 'release_manifest.json').write_text(json.dumps(bad),
                                                encoding='utf-8')
    report2 = vr.verify_release(release_dir=rel2, version=VERSION,
                                check_authenticode=False)
    assert report2['exit_code'] == 1
    checks2 = {c['id']: c for c in report2['checks']}
    assert checks2['manifest_updater_parse']['status'] == 'FAIL'


def test_verify_release_unsigned_warn_and_require_signed(tmp_path, monkeypatch):
    rel, _ = _make_release_dir(tmp_path, extra_exe=True)

    def fake_status(path, timeout=30):
        return {'checked': True, 'status': 'NotSigned', 'signed': False,
                'note': vr.SIGNING_NOTE}

    monkeypatch.setattr(vr, 'authenticode_status', fake_status)
    ok = vr.verify_release(release_dir=rel, version=VERSION)
    assert ok['exit_code'] == 0                     # 未签名默认仅 WARN
    checks = {c['id']: c for c in ok['checks']}
    assert checks['authenticode_unsigned']['status'] == 'WARN'
    assert '未签名' in checks['authenticode_unsigned']['detail']
    assert ok['signed'] is False

    strict = vr.verify_release(release_dir=rel, version=VERSION,
                               require_signed=True)
    assert strict['exit_code'] == 1
    checks2 = {c['id']: c for c in strict['checks']}
    assert checks2['authenticode_unsigned']['status'] == 'FAIL'


def test_verify_release_sha256sums(tmp_path):
    rel, _ = _make_release_dir(tmp_path)
    report = vr.verify_release(release_dir=rel, version=VERSION,
                               check_authenticode=False, write_sums=True)
    assert report['exit_code'] == 0
    assert (rel / 'SHA256SUMS').is_file()
    checks = {c['id']: c for c in report['checks']}
    assert checks['sha256sums']['status'] == 'PASS'
    # 篡改后应 FAIL
    lines = (rel / 'SHA256SUMS').read_text(encoding='utf-8').splitlines()
    (rel / 'SHA256SUMS').write_text(
        lines[0].replace(lines[0].split()[0], '0' * 64) + '\n',
        encoding='utf-8')
    bad = vr.verify_release(release_dir=rel, version=VERSION,
                            check_authenticode=False)
    assert bad['exit_code'] == 1


def test_verify_release_installer_report_checks(tmp_path):
    rel, _ = _make_release_dir(tmp_path)
    installer_dir = tmp_path / 'installer'
    installer_dir.mkdir()
    exe = installer_dir / 'LightDB-Studio-Setup-4.0.0.exe'
    exe.write_bytes(b'setup-bytes')
    report_path = installer_dir / 'build_report.json'
    report_path.write_text(json.dumps({
        'status': 'ok', 'version': VERSION, 'artifact': str(exe),
        'artifact_sha256': _sha(exe.read_bytes()),
        'signing': {'status': 'not_signed', 'note': vr.SIGNING_NOTE},
    }), encoding='utf-8')
    ok = vr.verify_release(release_dir=rel, version=VERSION,
                           check_authenticode=False,
                           installer_report=str(report_path))
    checks = {c['id']: c for c in ok['checks']}
    assert checks['installer_artifact']['status'] == 'PASS'
    assert checks['installer_version']['status'] == 'PASS'
    assert checks['installer_signed']['status'] == 'WARN'

    exe.write_bytes(b'tampered-setup')
    bad = vr.verify_release(release_dir=rel, version=VERSION,
                            check_authenticode=False,
                            installer_report=str(report_path))
    assert bad['exit_code'] == 1
    assert any(c['id'] == 'installer_artifact' and c['status'] == 'FAIL'
               for c in bad['checks'])


def test_verify_release_skipped_installer_report(tmp_path):
    rel, _ = _make_release_dir(tmp_path)
    report_path = tmp_path / 'build_report.json'
    report_path.write_text(json.dumps({
        'status': 'skipped', 'reason': '未检测到 Inno Setup（ISCC.exe）或 WiX v3',
    }), encoding='utf-8')
    report = vr.verify_release(release_dir=rel, version=VERSION,
                               check_authenticode=False,
                               installer_report=str(report_path))
    checks = {c['id']: c for c in report['checks']}
    assert checks['installer_build']['status'] == 'SKIP'
    assert report['exit_code'] == 0


# ==================== CLI 退出码 ====================

def test_build_installer_cli_dry_run_and_bad_version(tmp_path, monkeypatch):
    src = _fake_source(tmp_path)
    monkeypatch.setattr(bi, 'detect_installer_tools',
                        lambda: _fake_detection(tmp_path, 'inno'))
    rc = bi.main(['--version', VERSION, '--source', str(src),
                  '--out', str(tmp_path / 'out'), '--dry-run'])
    assert rc == 0
    assert bi.main(['--version', 'bogus']) == 2


def test_verify_release_cli_pass_and_fail(tmp_path):
    rel, _ = _make_release_dir(tmp_path)
    out = tmp_path / 'verify.json'
    assert vr.main(['--release-dir', str(rel), '--version', VERSION,
                    '--no-authenticode', '--json', str(out),
                    '--quiet']) == 0
    assert json.loads(out.read_text(encoding='utf-8'))['summary']['passed']
    assert vr.main(['--release-dir', str(rel), '--version', '9.9.9',
                    '--no-authenticode', '--quiet']) == 1


# ==================== 真实环境：工具缺失时 skip 不假过 ====================

def test_real_build_skips_honestly_without_installer_tool(tmp_path):
    source = ROOT / 'dist' / 'studio' / 'LightDB-Studio'
    if not source.is_dir():
        pytest.skip('本机没有 onedir 产物（由 scripts/build_studio.py 生成）')
    det = bi.detect_installer_tools()
    tool, reason = bi.choose_tool(det)
    if tool is not None:
        pytest.skip('本机检测到安装器工具（%s），真实构建由 '
                    '_analysis_tmp/gui_full/p6_installer_check.py 覆盖' % tool)
    report = bi.build_installer(source=source, out=tmp_path / 'installer')
    assert report['status'] == 'skipped'
    assert report['exit_code'] == 3
    assert '未检测到' in report['reason']
    assert report['artifact'] is None
    assert (tmp_path / 'installer' / 'BUILD_INSTALLER.md').is_file()
