"""发布产物校验（P6 发布工程）。

检查项（任何一项 FAIL → 退出码 1）：

1. 期望版本可解析（`--version` 或 `lightdb.__version__`，用产品的
   `updater.parse_version` 校验）；
2. 发布目录里存在发布产物（`.zip/.exe/.msi/.7z`）；
3. `release_manifest.json` 存在、是合法 JSON、**可被
   `lightdb_studio.utils.updater.parse_manifest` 解析**；
4. manifest 版本 == 期望版本；产物文件名包含该版本号；
5. manifest 里每条 artifact：文件存在、大小一致、SHA256 一致；
6. 可选 `SHA256SUMS` 旁挂清单可校验（没有则 SKIP，manifest 内已含 sha256；
   `--write-sums` 可生成）；
7. `check_for_update()` 用本地 manifest 能跑通（不会抛异常）；
8. Authenticode 签名状态：**未签名显式标注“未签名（SmartScreen 风险）”**，
   默认只 WARN，`--require-signed` 时 FAIL；无证书时该步跳过并注明；
9. 可选：`dist/installer/build_report.json`（安装包构建报告）里的安装包
   产物存在且 SHA256 一致（没构建过则 SKIP 并说明原因）。

设计原则：**不猜、不假签名、不假通过** —— 缺产物/版本不一致/哈希不符一律
FAIL；签名缺失如实标注为“未签名”；工具缺失的步骤记 SKIP 并写明原因。

退出码：0 = 无 FAIL；1 = 有 FAIL；2 = 参数错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:                   # 允许直接 `python scripts/xxx.py`
    sys.path.insert(0, str(ROOT))
DEFAULT_RELEASE_DIR = ROOT / 'dist' / 'release'
DEFAULT_INSTALLER_DIR = ROOT / 'dist' / 'installer'
MANIFEST_NAME = 'release_manifest.json'
SUMS_NAME = 'SHA256SUMS'
ARTIFACT_SUFFIXES = ('.zip', '.exe', '.msi', '.7z')
SIGNING_NOTE = ('未签名（无代码签名证书；可运行 scripts/sign_windows.ps1，'
                'SmartScreen 可能告警）')
REPORT_SCHEMA = 'lightdb-release-verify/1'


# ==================== 工具 ====================

def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


def expected_version(explicit: str = '') -> str:
    """取期望版本：--version 优先，否则 lightdb.__version__；不合法抛 ValueError。"""
    if str(explicit or '').strip():
        version = str(explicit).strip()
    else:
        import lightdb
        version = str(lightdb.__version__).strip()
    try:
        from lightdb_studio.utils.updater import parse_version
        parse_version(version)
    except Exception as exc:                        # noqa: BLE001
        raise ValueError(f'非法版本号 {version!r}: {exc}') from exc
    return version


def collect_artifacts(release_dir: Path) -> list:
    if not release_dir.is_dir():
        return []
    return [p for p in sorted(release_dir.iterdir())
            if p.is_file() and p.suffix.lower() in ARTIFACT_SUFFIXES]


def authenticode_status(path: Path, timeout: int = 30) -> dict:
    """用 PowerShell `Get-AuthenticodeSignature` 读真实签名状态。

    返回 {checked, status, signed, signer, note}；PowerShell 不可用或查询
    失败时 checked=False（调用方记 SKIP，绝不假定已签名）。
    """
    powershell = shutil.which('powershell') or shutil.which('pwsh')
    if not powershell:
        return {'checked': False, 'status': 'unavailable', 'signed': False,
                'note': '未找到 powershell，无法查询 Authenticode 状态'}
    path_escaped = str(path).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "$s = Get-AuthenticodeSignature -LiteralPath '%s';"
        "@{status=\"$($s.Status)\"; message=\"$($s.StatusMessage)\"; "
        "signer=if ($s.SignerCertificate) { $s.SignerCertificate.Subject } "
        "else { '' }} | ConvertTo-Json -Compress" % path_escaped)
    try:
        proc = subprocess.run(
            [powershell, '-NoProfile', '-NonInteractive', '-Command', script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace', timeout=timeout)
        if proc.returncode != 0:
            return {'checked': False, 'status': 'unavailable', 'signed': False,
                    'note': 'Get-AuthenticodeSignature 失败: %s'
                            % (proc.stderr or '')[-500:]}
        data = json.loads((proc.stdout or '').strip() or '{}')
    except Exception as exc:                        # noqa: BLE001
        return {'checked': False, 'status': 'unavailable', 'signed': False,
                'note': '签名状态查询异常: %s' % exc}
    status = str(data.get('status') or '')
    signed = status == 'Valid'
    note = ''
    if not signed:
        note = SIGNING_NOTE if status in ('NotSigned', 'UnknownError') else (
            '签名状态异常: %s %s' % (status, data.get('message') or ''))
    return {'checked': True, 'status': status, 'signed': signed,
            'signer': data.get('signer') or '', 'note': note}


# ==================== 校验主体 ====================

def verify_release(release_dir=None, version: str = '', manifest_path=None,
                   require_signed: bool = False, check_authenticode: bool = True,
                   sha_sums_path=None, write_sums: bool = False,
                   installer_report=None, json_out: str = '') -> dict:
    """执行全部发布校验；返回报告 dict（含 checks/summary/exit_code）。"""
    release_dir = Path(release_dir or DEFAULT_RELEASE_DIR)
    report = {
        'schema': REPORT_SCHEMA,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'release_dir': str(release_dir),
        'expected_version': None,
        'manifest_path': None,
        'checks': [],
        'summary': {'passed': False, 'fail': 0, 'warn': 0, 'skip': 0,
                    'pass': 0},
        'exit_code': 1,
        'signed': False,
        'signing_note': SIGNING_NOTE,
    }

    def add(check_id, status, detail, evidence=None):
        item = {'id': check_id, 'status': status, 'detail': detail}
        if evidence is not None:
            item['evidence'] = evidence
        report['checks'].append(item)
        return status == 'PASS'

    # ---- 1. 期望版本 ----
    try:
        version = expected_version(version)
    except ValueError as exc:
        add('version_source', 'FAIL', str(exc))
        report['summary'] = _summarize(report['checks'])
        report['exit_code'] = 2
        return report
    report['expected_version'] = version
    add('version_source', 'PASS', '期望版本 = %s' % version)

    # ---- 2. 发布目录 + 产物 ----
    if not release_dir.is_dir():
        add('release_dir', 'FAIL', '发布目录不存在: %s' % release_dir)
        report['summary'] = _summarize(report['checks'])
        report['exit_code'] = 1
        _maybe_write_json(report, json_out)
        return report
    add('release_dir', 'PASS', str(release_dir))
    artifacts = collect_artifacts(release_dir)
    if not artifacts:
        add('artifacts_present', 'FAIL',
            '发布目录里没有发布产物（%s）' % '/'.join(ARTIFACT_SUFFIXES))
    else:
        add('artifacts_present', 'PASS',
            '发现 %d 个产物: %s' % (len(artifacts),
                                    ', '.join(p.name for p in artifacts)))

    # ---- 3. manifest 存在 / JSON / updater 解析 ----
    manifest_path = Path(manifest_path or (release_dir / MANIFEST_NAME))
    report['manifest_path'] = str(manifest_path)
    manifest = None
    if not manifest_path.is_file():
        add('manifest_present', 'FAIL', '缺少 %s' % manifest_path)
    else:
        add('manifest_present', 'PASS', str(manifest_path))
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            add('manifest_json', 'PASS', 'manifest 是合法 JSON')
        except Exception as exc:                    # noqa: BLE001
            add('manifest_json', 'FAIL', 'manifest 不是合法 JSON: %s' % exc)
        if manifest is not None:
            try:
                from lightdb_studio.utils.updater import parse_manifest
                parse_manifest(manifest)
                add('manifest_updater_parse', 'PASS',
                    'lightdb_studio.utils.updater.parse_manifest 解析通过')
            except Exception as exc:                # noqa: BLE001
                add('manifest_updater_parse', 'FAIL',
                    'updater 无法解析 manifest: %s' % exc)

    # ---- 4. 版本一致性 ----
    if manifest is not None:
        mver = str(manifest.get('version') or '')
        if mver == version:
            add('manifest_version', 'PASS', 'manifest 版本 = %s' % mver)
        else:
            add('manifest_version', 'FAIL',
                'manifest 版本 %r != 期望版本 %r' % (mver, version))
        names = [str(a.get('name') or '') for a in
                 (manifest.get('artifacts') or []) if isinstance(a, dict)]
        if not names:
            add('artifact_version_names', 'FAIL', 'manifest.artifacts 为空')
        else:
            bad = [n for n in names if version not in n]
            if bad:
                add('artifact_version_names', 'FAIL',
                    '产物文件名不含版本 %s: %s' % (version, bad))
            else:
                add('artifact_version_names', 'PASS',
                    '全部产物名含版本 %s' % version)

    # ---- 5. artifact 存在 / 大小 / SHA256 ----
    sha_ok = True
    if manifest is not None and isinstance(manifest.get('artifacts'), list):
        for i, art in enumerate(manifest.get('artifacts') or []):
            if not isinstance(art, dict):
                sha_ok = add('artifact_%d' % i, 'FAIL', 'artifact 不是对象') and sha_ok
                continue
            name = str(art.get('name') or '')
            path = release_dir / name
            if not name:
                sha_ok = add('artifact_%d' % i, 'FAIL', 'artifact.name 为空') and sha_ok
                continue
            if not path.is_file():
                sha_ok = add('artifact_%s_exists' % name, 'FAIL',
                             '产物不存在: %s' % path) and sha_ok
                continue
            ok = True
            expect_size = art.get('size')
            if isinstance(expect_size, int) and expect_size != path.stat().st_size:
                ok = add('artifact_%s_size' % name, 'FAIL',
                         '大小不一致: manifest=%s 实际=%s'
                         % (expect_size, path.stat().st_size))
            expect_sha = str(art.get('sha256') or '')
            actual_sha = sha256_file(path)
            if expect_sha:
                if actual_sha != expect_sha:
                    ok = add('artifact_%s_sha256' % name, 'FAIL',
                             'SHA256 不一致: manifest=%s… 实际=%s…'
                             % (expect_sha[:16], actual_sha[:16]))
            else:
                ok = add('artifact_%s_sha256' % name, 'FAIL',
                         'manifest 缺少 sha256')
            if ok and not expect_sha == '':
                add('artifact_%s_ok' % name, 'PASS',
                    '存在/大小/SHA256 一致（sha256=%s…）' % actual_sha[:16])
            sha_ok = sha_ok and ok

    # ---- 6. SHA256SUMS 旁挂清单（可选） ----
    sums_path = Path(sha_sums_path or (release_dir / SUMS_NAME))
    if write_sums and artifacts:
        lines = ['%s  %s' % (sha256_file(p), p.name) for p in artifacts]
        try:
            sums_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            add('sha256sums_write', 'PASS', '已生成 %s' % sums_path)
        except OSError as exc:                      # pragma: no cover
            add('sha256sums_write', 'FAIL', '生成 SHA256SUMS 失败: %s' % exc)
    if sums_path.is_file():
        bad = []
        try:
            for line in sums_path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    bad.append('格式错误: %r' % line)
                    continue
                digest, fname = parts[0].strip('*'), parts[-1]
                path = release_dir / fname
                if not path.is_file():
                    bad.append('缺少文件: %s' % fname)
                elif sha256_file(path) != digest:
                    bad.append('SHA256 不符: %s' % fname)
        except Exception as exc:                    # noqa: BLE001
            bad.append('读取失败: %s' % exc)
        if bad:
            add('sha256sums', 'FAIL', '; '.join(bad[:5]))
        else:
            add('sha256sums', 'PASS', '%s 校验通过' % sums_path.name)
    else:
        add('sha256sums', 'SKIP',
            '没有 %s（manifest 内已含 sha256；--write-sums 可生成）' % SUMS_NAME)

    # ---- 7. updater check_for_update 试跑 ----
    if manifest is not None and manifest_path.is_file():
        try:
            from lightdb_studio.utils.updater import check_for_update
            info = check_for_update(str(manifest_path), version)
            add('updater_check_for_update', 'PASS',
                'check_for_update 可解析本地 manifest（available=%s，latest=%s）'
                % (info.available, info.latest_version))
        except Exception as exc:                    # noqa: BLE001
            add('updater_check_for_update', 'FAIL',
                'check_for_update 无法使用该 manifest: %s' % exc)

    # ---- 8. Authenticode 签名状态（未签名 = WARN） ----
    signable = [p for p in artifacts if p.suffix.lower() in ('.exe', '.msi')]
    installer_art = _installer_artifact(installer_report, release_dir)
    if installer_art is not None:
        signable.append(installer_art)
    if not check_authenticode:
        add('authenticode', 'SKIP', '--no-authenticode：未查询签名状态')
    elif not signable:
        add('authenticode', 'SKIP',
            '发布产物里没有可签名的 .exe/.msi（zip 不参与 Authenticode）')
    else:
        unsigned, signed_ok, unavailable = [], [], []
        for path in signable:
            st = authenticode_status(path)
            if not st['checked']:
                unavailable.append((path, st.get('note', '')))
            elif st['signed']:
                signed_ok.append(path)
            else:
                unsigned.append((path, st))
        if signed_ok:
            add('authenticode_signed', 'PASS',
                '%d 个产物已签名（例如 %s）' % (len(signed_ok), signed_ok[0].name))
        if unavailable:
            add('authenticode_unavailable', 'SKIP',
                '无法查询签名状态: %s' % '; '.join(p.name for p, _ in unavailable))
        if unsigned:
            report['signed'] = False
            detail = '未签名（SmartScreen 风险）: ' + ', '.join(
                p.name for p, _ in unsigned)
            add('authenticode_unsigned', 'FAIL' if require_signed else 'WARN',
                detail, evidence=[{'file': p.name, 'status': st.get('status')}
                                  for p, st in unsigned])
        elif signable:
            report['signed'] = True
    # 证书配置状态（只影响说明，不影响判定）
    if not os.environ.get('LIGHTDB_SIGN_CERT'):
        add('signing_certificate', 'SKIP',
            '未配置 LIGHTDB_SIGN_CERT（无证书）：产物为未签名，SmartScreen 风险；'
            '可用 scripts/sign_windows.ps1 签名')
    else:
        add('signing_certificate', 'PASS',
            '检测到 LIGHTDB_SIGN_CERT=%s…' % os.environ['LIGHTDB_SIGN_CERT'][:8])

    # ---- 9. 安装包构建报告（可选） ----
    installer_report = _load_installer_report(installer_report)
    if installer_report is None:
        add('installer_build', 'SKIP',
            '没有 dist/installer/build_report.json（本机无 Inno/WiX 工具时'
            '安装包构建被显式跳过，见 scripts/build_installer.py）')
    else:
        st = str(installer_report.get('status') or '')
        art = str(installer_report.get('artifact') or '')
        if st == 'ok' and art and Path(art).is_file():
            actual = sha256_file(Path(art))
            expect = str(installer_report.get('artifact_sha256') or '')
            if expect and expect != actual:
                add('installer_artifact', 'FAIL',
                    '安装包 SHA256 与构建报告不一致: %s' % art)
            else:
                add('installer_artifact', 'PASS',
                    '安装包存在且 SHA256 一致: %s (%s bytes)'
                    % (Path(art).name, Path(art).stat().st_size))
            report_ver = str(installer_report.get('version') or '')
            if report_ver and report_ver != version:
                add('installer_version', 'FAIL',
                    '安装包版本 %r != 期望 %r' % (report_ver, version))
            elif report_ver:
                add('installer_version', 'PASS', '安装包版本 = %s' % report_ver)
            signing = installer_report.get('signing') or {}
            if signing.get('status') == 'not_signed':
                add('installer_signed', 'WARN', str(signing.get('note') or SIGNING_NOTE))
        elif st == 'skipped':
            add('installer_build', 'SKIP',
                '安装包构建被跳过: %s' % (installer_report.get('reason') or ''))
        else:
            add('installer_build', 'FAIL',
                '安装包构建状态异常: %r（artifact=%r）' % (st, art))

    report['artifacts'] = [p.name for p in artifacts]
    report['summary'] = _summarize(report['checks'])
    report['exit_code'] = 1 if report['summary']['fail'] else 0
    _maybe_write_json(report, json_out)
    return report


def _installer_artifact(installer_report, release_dir: Path):
    rep = _load_installer_report(installer_report)
    if rep and rep.get('artifact') and Path(rep['artifact']).is_file():
        return Path(rep['artifact'])
    return None


def _load_installer_report(path=None):
    path = Path(path or (DEFAULT_INSTALLER_DIR / 'build_report.json'))
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:                               # noqa: BLE001
        return None


def _summarize(checks) -> dict:
    summary = {'passed': False, 'fail': 0, 'warn': 0, 'skip': 0, 'pass': 0}
    for c in checks:
        status = c['status']
        if status == 'PASS':
            summary['pass'] += 1
        elif status == 'FAIL':
            summary['fail'] += 1
        elif status == 'WARN':
            summary['warn'] += 1
        else:
            summary['skip'] += 1
    summary['passed'] = summary['fail'] == 0
    return summary


def _maybe_write_json(report, json_out):
    if not json_out:
        return
    path = Path(json_out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')


# ==================== CLI ====================

def format_report(report: dict) -> str:
    lines = ['LightDB 发布校验: suite=%s  release_dir=%s'
             % (report.get('schema'), report.get('release_dir')),
             '期望版本: %s' % report.get('expected_version')]
    for check in report.get('checks', []):
        lines.append('  [%-4s] %-32s %s'
                     % (check['status'], check['id'], check['detail']))
    s = report.get('summary', {})
    lines.append('汇总: PASS=%s FAIL=%s WARN=%s SKIP=%s -> %s'
                 % (s.get('pass', 0), s.get('fail', 0), s.get('warn', 0),
                    s.get('skip', 0), 'PASS' if s.get('passed') else 'FAIL'))
    if not report.get('signed'):
        lines.append('签名: ' + SIGNING_NOTE)
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='LightDB Studio 发布产物校验')
    ap.add_argument('--release-dir', default='',
                    help='发布产物目录（默认 dist/release）')
    ap.add_argument('--version', default='',
                    help='期望版本（默认 lightdb.__version__）')
    ap.add_argument('--manifest', default='',
                    help='manifest 路径（默认 <release-dir>/release_manifest.json）')
    ap.add_argument('--require-signed', action='store_true',
                    help='要求 Authenticode 签名有效（未签名 -> FAIL）')
    ap.add_argument('--no-authenticode', action='store_true',
                    help='不查询签名状态（受限环境/加速用）')
    ap.add_argument('--sha256sums', default='',
                    help='SHA256SUMS 路径（默认 <release-dir>/SHA256SUMS）')
    ap.add_argument('--write-sums', action='store_true',
                    help='按产物重新生成 SHA256SUMS')
    ap.add_argument('--installer-report', default='',
                    help='安装包构建报告（默认 dist/installer/build_report.json）')
    ap.add_argument('--json', default='', help='把校验报告写到该路径')
    ap.add_argument('--quiet', action='store_true', help='只输出结论行')
    args = ap.parse_args(argv)

    try:
        report = verify_release(
            release_dir=args.release_dir or None, version=args.version,
            manifest_path=args.manifest or None,
            require_signed=args.require_signed,
            check_authenticode=not args.no_authenticode,
            sha_sums_path=args.sha256sums or None,
            write_sums=args.write_sums,
            installer_report=args.installer_report or None,
            json_out=args.json)
    except Exception as exc:                        # noqa: BLE001
        print('[verify-release] 内部错误: %s' % exc, file=sys.stderr)
        return 2
    if args.quiet:
        s = report['summary']
        print('%s (PASS=%d FAIL=%d WARN=%d SKIP=%d)'
              % ('PASS' if s['passed'] else 'FAIL', s['pass'], s['fail'],
                 s['warn'], s['skip']))
    else:
        print(format_report(report))
    return int(report['exit_code'])


if __name__ == '__main__':
    sys.exit(main())
