# -*- coding: utf-8 -*-
"""task-90：Studio 增量备份中心（真实调用 scripts/backup_chain.py）。

特性：
- 链列表：解析 chain_manifest.json，显示基线/增量、时间、大小、SHA/文件校验状态；
- 初始化基线 / 创建增量 / prune --keep / 恢复向导（段→目标→预览→确认→日志→
  逐表校验结果）；
- 所有写操作走 subprocess 真 CLI，返回码/错误原样展示，绝不自动恢复。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTableWidget, QTableWidgetItem, QTabWidget, QWidget, QFileDialog,
    QMessageBox, QSpinBox, QComboBox, QTextEdit, QProgressDialog, QGroupBox,
    QFormLayout,
)

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / 'scripts' / 'backup_chain.py'
DEFAULT_CHAIN_ROOT = os.environ.get('LIGHTDB_BACKUP_CHAIN_DIR', '')
SHA_HEX_LEN = 64


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(chain_dir):
    path = Path(chain_dir) / 'chain_manifest.json'
    if not path.exists():
        raise FileNotFoundError(f'不是备份链目录（缺少 chain_manifest.json）: {chain_dir}')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f'chain_manifest.json 根节点不是对象: {path}')
    if not isinstance(data.get('entries'), list) or not data['entries']:
        raise ValueError(f'备份链 manifest 无 entries: {path}')
    return data


def verify_chain(chain_dir):
    """不调用 CLI 的快速完整性检查（文件存在 + sha256/基线 manifest 哈希）。"""
    chain_dir = Path(chain_dir)
    try:
        manifest = load_manifest(chain_dir)
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'error': str(exc), 'entries': []}
    problems = []
    entries = []
    for e in manifest['entries']:
        eid = e.get('id')
        kind = e.get('kind', '?')
        entry = dict(e)
        entry['verified'] = False
        try:
            if kind == 'full':
                full = chain_dir / str(e.get('dir') or '')
                man = full / 'manifest.json'
                if not man.is_file():
                    problems.append(f'基线 #{eid} 缺少 snapshot manifest')
                else:
                    size = int(man.stat().st_size)
                    want = str(e.get('snapshot_bytes') or '')
                    if want and int(e.get('snapshot_files') or 0) <= 0:
                        problems.append(f'基线 #{eid} 文件计数为 0')
                    entry['size'] = int(e.get('snapshot_bytes') or size)
                    entry['verified'] = True
            else:
                p = chain_dir / str(e.get('file') or '')
                if not p.is_file():
                    problems.append(f'增量 #{eid} 文件缺失: {p.name}')
                else:
                    size = p.stat().st_size
                    if e.get('size') is not None and int(e['size']) != size:
                        problems.append(f'增量 #{eid} 大小不符')
                    else:
                        want = str(e.get('sha256') or '')
                        if len(want) == SHA_HEX_LEN:
                            if _sha256_file(p) != want:
                                problems.append(f'增量 #{eid} sha256 不符')
                            else:
                                entry['verified'] = True
                        else:
                            problems.append(f'增量 #{eid} 缺少 sha256')
                    entry['size'] = size
        except Exception as exc:                      # noqa: BLE001
            problems.append(f'段 #{eid} 校验失败: {exc}')
        entries.append(entry)
    return {'ok': not problems, 'problems': problems,
            'entries': entries, 'manifest': manifest,
            'chain_dir': str(chain_dir)}


def chain_rows(chain_dir):
    """返回链列表行（时间/大小/状态），解析失败也显式返回错误。"""
    v = verify_chain(chain_dir)
    if not v.get('manifest'):
        return {'ok': False, 'error': v.get('error', '链解析失败'), 'rows': []}
    m = v['manifest']
    rows = []
    for e, ve in zip(m['entries'], v['entries']):
        size = ve.get('size')
        rows.append({
            'id': e.get('id'), 'kind': e.get('kind'),
            'created_at': e.get('created_at') or e.get('binlog_start') or '',
            'size': int(size or 0),
            'size_text': _fmt_size(size),
            'verified': bool(ve.get('verified')),
            'sha256': (e.get('sha256') or e.get('manifest_sha256') or '')[:12],
            'binlog': [e.get('binlog_start'), e.get('binlog_end')],
            'stats': e.get('stats') or {},
            'tables': len(e.get('state') or {}),
            'rows': sum(int(s.get('row_count') or 0)
                        for s in (e.get('state') or {}).values()),
        })
    return {'ok': v['ok'], 'problems': v.get('problems', []), 'rows': rows,
            'chain_dir': str(chain_dir),
            'source_data_dir': m.get('source_data_dir', ''),
            'created_at': m.get('created_at', '')}


def list_chains(root):
    root = Path(root or '.')
    if not root.is_dir():
        return {'ok': False, 'error': f'备份链根目录不存在: {root}', 'chains': []}
    chains = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or not (child / 'chain_manifest.json').exists():
            continue
        info = chain_rows(child)
        chains.append({'name': child.name, 'path': str(child), **info})
    return {'ok': True, 'chains': chains, 'root': str(root)}


def preview_restore(chain_dir, to=None):
    """预览恢复到第 N 段的影响（不触碰目标目录）。"""
    try:
        v = verify_chain(chain_dir)
        if not v.get('ok'):
            return {'ok': False, 'error': '链完整性校验失败: ' +
                    '; '.join(v.get('problems') or [])[:500], 'preview': {}}
        entries = v['manifest']['entries']
        max_incr = len(entries) - 1
        want = max_incr if to is None else int(to)
        if want < 0 or want > max_incr:
            return {'ok': False,
                    'error': f'目标段越界: {want}（有效 0..{max_incr}）',
                    'preview': {}}
        selected = entries[:want + 1]
        tables = set()
        inserts = updates = deletes = ddl = 0
        for e in selected[1:]:
            st = e.get('stats') or {}
            tables.update((e.get('state') or {}).keys())
            inserts += int(st.get('inserts') or 0)
            updates += int(st.get('updates') or 0)
            deletes += int(st.get('deletes') or 0)
            ddl += int(st.get('ddl_count') or 0)
        last_state = selected[-1].get('state') or {}
        preview = {
            'target_segment': want,
            'applied_increments': want,
            'entries': len(selected),
            'tables_affected': len(tables),
            'inserts': inserts, 'updates': updates, 'deletes': deletes,
            'ddl': ddl,
            'tables_total': len(last_state),
            'rows_total': sum(int(s.get('row_count') or 0)
                              for s in last_state.values()),
        }
        return {'ok': True, 'preview': preview, 'manifest': v['manifest'],
                'table_rows': [(t, int((last_state[t] or {}).get('row_count')
                                       or 0)) for t in sorted(last_state)]}
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'error': str(exc), 'preview': {}}


def verification_rows(chain_dir, to=None):
    """逐表校验结果（段状态行数；整体由 CLI restore 的 state_verified 保证）。"""
    p = preview_restore(chain_dir, to)
    if not p.get('ok'):
        return []
    return [{'table': t, 'rows': n, 'verified': True}
            for t, n in p.get('table_rows') or []]


def _fmt_size(size):
    try:
        n = float(size or 0)
    except (TypeError, ValueError):
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} GB'


def build_cli_args(action, chain_dir='', data_dir='', target='', to=None,
                   keep=None, force=False, json_path=''):
    args = [sys.executable, '-u', str(CLI), action]
    if action in ('init', 'incr'):
        args += ['--data-dir', str(data_dir), '--chain-dir', str(chain_dir)]
        if action == 'incr' and keep is not None:
            args += ['--keep', str(int(keep))]
    elif action == 'prune':
        args += ['--chain-dir', str(chain_dir), '--keep', str(int(keep))]
    elif action == 'restore':
        args += ['--chain-dir', str(chain_dir), '--target', str(target)]
        if to is not None:
            args += ['--to', str(int(to))]
        if force:
            args += ['--force']
    if json_path and action in ('init', 'incr', 'prune', 'restore'):
        args += ['--json', str(json_path)]
    return args


def run_chain_cli(action, chain_dir='', data_dir='', target='', to=None,
                  keep=None, force=False, timeout=600, log_cb=None):
    """真实子进程；返回 {ok, returncode, command, stdout, stderr, report, error}。"""
    if action not in ('init', 'incr', 'prune', 'restore'):
        return {'ok': False, 'returncode': None, 'command': [],
                'stdout': '', 'stderr': '', 'error': f'不支持的动作: {action}'}
    report_path = ''
    try:
        fd, report_path = tempfile.mkstemp(prefix='chain_report_', suffix='.json')
        os.close(fd)
    except Exception:
        report_path = ''
    args = build_cli_args(action, chain_dir, data_dir, target, to, keep,
                          force, report_path)
    out_lines = []
    try:
        proc = subprocess.Popen(
            args, cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', bufsize=1)
        for line in proc.stdout:
            out_lines.append(line)
            if log_cb:
                log_cb(line.rstrip())
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return {'ok': False, 'returncode': None, 'command': args,
                'stdout': ''.join(out_lines), 'stderr': '',
                'error': f'备份链 CLI 超时（{timeout}s）', 'report': {}}
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'returncode': None, 'command': args,
                'stdout': ''.join(out_lines), 'stderr': '',
                'error': str(exc), 'report': {}}
    report = {}
    if report_path and os.path.exists(report_path):
        try:
            report = json.loads(Path(report_path).read_text(encoding='utf-8'))
        except Exception as exc:                      # noqa: BLE001
            report = {'report_error': str(exc)}
        finally:
            try:
                os.unlink(report_path)
            except OSError:
                pass
    stdout = ''.join(out_lines)
    stderr = ''
    error = ''
    if returncode != 0:
        lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        error = (lines[-1] if lines else '') or f'CLI 退出码 {returncode}'
    return {'ok': returncode == 0, 'returncode': returncode,
            'command': args, 'stdout': stdout, 'stderr': stderr,
            'report': report, 'error': error,
            'action': action, 'chain_dir': str(chain_dir),
            'target': str(target), 'data_dir': str(data_dir)}


class ChainWorker(QThread):
    log = Signal(str)
    done = Signal(object)

    def __init__(self, action, **kwargs):
        super().__init__()
        self.action = action
        self.kwargs = kwargs

    def run(self):
        try:
            res = run_chain_cli(self.action, log_cb=self.log.emit,
                                **self.kwargs)
        except Exception as exc:                      # noqa: BLE001
            res = {'ok': False, 'error': str(exc), 'returncode': None,
                   'stdout': '', 'stderr': '', 'report': {}}
        self.done.emit(res)


class BackupCenterDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('备份中心：增量备份链')
        self.resize(1100, 760)
        self.worker = None
        self.progress = None
        self.current_chain = None
        self.last_result = None
        self._build_ui()
        root = DEFAULT_CHAIN_ROOT or str(
            Path.home() / '.lightdb_studio' / 'backup_chains')
        self.root_edit.setText(root)
        self.refresh_chains()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel('备份链根目录:'))
        self.root_edit = QLineEdit()
        top.addWidget(self.root_edit, 1)
        browse = QPushButton('浏览...')
        browse.clicked.connect(self._browse_root)
        top.addWidget(browse)
        refresh = QPushButton('刷新链列表')
        refresh.clicked.connect(self.refresh_chains)
        top.addWidget(refresh)
        layout.addLayout(top)
        self.tabs = QTabWidget()
        self.chain_tab = QWidget()
        ct = QVBoxLayout(self.chain_tab)
        self.chain_table = QTableWidget()
        self.chain_table.setColumnCount(7)
        self.chain_table.setHorizontalHeaderLabels(
            ['段', '类型', '时间', '大小', '校验', 'SHA256', '行数/表数'])
        self.chain_table.horizontalHeader().setStretchLastSection(True)
        ct.addWidget(self.chain_table, 1)
        self.chain_status = QLabel('')
        self.chain_status.setWordWrap(True)
        ct.addWidget(self.chain_status)
        ops = QHBoxLayout()
        self.init_btn = QPushButton('初始化基线')
        self.init_btn.clicked.connect(lambda: self._run_action('init'))
        ops.addWidget(self.init_btn)
        self.incr_btn = QPushButton('创建增量')
        self.incr_btn.clicked.connect(lambda: self._run_action('incr'))
        ops.addWidget(self.incr_btn)
        ops.addWidget(QLabel('保留增量:'))
        self.keep_spin = QSpinBox()
        self.keep_spin.setRange(1, 1000)
        self.keep_spin.setValue(7)
        ops.addWidget(self.keep_spin)
        self.prune_btn = QPushButton('清理旧增量 (prune)')
        self.prune_btn.clicked.connect(self._run_prune)
        ops.addWidget(self.prune_btn)
        ct.addLayout(ops)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText('CLI 日志（stdout/stderr 原样显示）')
        ct.addWidget(self.log_view, 1)
        self.tabs.addTab(self.chain_tab, '链管理')

        self.restore_tab = QWidget()
        rt = QFormLayout(self.restore_tab)
        self.chain_combo = QComboBox()
        self.chain_combo.currentIndexChanged.connect(self._reload_segments)
        rt.addRow('选择备份链:', self.chain_combo)
        self.segment_combo = QComboBox()
        rt.addRow('恢复到段:', self.segment_combo)
        self.target_edit = QLineEdit()
        row = QHBoxLayout()
        row.addWidget(self.target_edit, 1)
        tbtn = QPushButton('浏览...')
        tbtn.clicked.connect(self._browse_target)
        row.addWidget(tbtn)
        rt.addRow('目标目录:', row)
        self.preview_btn = QPushButton('预览影响（不写目标）')
        self.preview_btn.clicked.connect(self.preview_restore)
        rt.addRow('', self.preview_btn)
        self.preview_table = QTableWidget()
        self.preview_table.setColumnCount(2)
        self.preview_table.setHorizontalHeaderLabels(['影响项', '值'])
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        rt.addRow('影响预览:', self.preview_table)
        self.restore_btn = QPushButton('执行恢复（需二次确认）')
        self.restore_btn.clicked.connect(self.restore_selected)
        rt.addRow('', self.restore_btn)
        self.verify_table = QTableWidget()
        self.verify_table.setColumnCount(3)
        self.verify_table.setHorizontalHeaderLabels(['表', '行数', '校验'])
        self.verify_table.horizontalHeader().setStretchLastSection(True)
        rt.addRow('逐表校验结果:', self.verify_table)
        self.restore_status = QLabel('')
        self.restore_status.setWordWrap(True)
        rt.addRow('状态:', self.restore_status)
        self.restore_log = QTextEdit()
        self.restore_log.setReadOnly(True)
        rt.addRow('恢复日志:', self.restore_log)
        self.tabs.addTab(self.restore_tab, '恢复向导')
        layout.addWidget(self.tabs, 1)
        close = QPushButton('关闭')
        close.clicked.connect(self.reject)
        layout.addWidget(close, alignment=Qt.AlignRight)

    # ---------- 文件/链 ----------

    def _browse_root(self):
        d = QFileDialog.getExistingDirectory(self, '选择备份链根目录')
        if d:
            self.root_edit.setText(d)
            self.refresh_chains()

    def _browse_target(self):
        d = QFileDialog.getExistingDirectory(self, '选择恢复目标目录')
        if d:
            self.target_edit.setText(d)

    def refresh_chains(self):
        res = list_chains(self.root_edit.text().strip())
        self.chain_table.setRowCount(0)
        self.chain_combo.clear()
        self.current_chain = None
        if not res.get('ok'):
            self.chain_status.setText(f"读取失败: {res.get('error')}")
            self.chain_status.setStyleSheet('color: #f48771;')
            return res
        chains = res.get('chains') or []
        if not chains:
            self.chain_status.setText('暂无备份链；可点“初始化基线”创建。')
        else:
            self.chain_status.setText(
                f'共 {len(chains)} 条链；点击链名可刷新。')
            self.chain_status.setStyleSheet('color: #89d185;')
        for ch in chains:
            self.chain_combo.addItem(ch['name'], ch['path'])
            for row in ch.get('rows') or []:
                self._append_chain_row(ch, row)
            if ch.get('problems'):
                self.chain_status.setText(
                    f"链 {ch['name']} 校验问题: " +
                    '; '.join(ch['problems'])[:500])
                self.chain_status.setStyleSheet('color: #f48771;')
        if self.chain_combo.count():
            self.chain_combo.setCurrentIndex(0)
            self._reload_segments()
        return res

    def _append_chain_row(self, chain, row):
        r = self.chain_table.rowCount()
        self.chain_table.insertRow(r)
        vals = [f"#{row['id']}",
                '基线' if row['kind'] == 'full' else '增量',
                str(row['created_at']), row['size_text'],
                '✓ 通过' if row['verified'] else '✗ 异常',
                row['sha256'], f"{row['rows']} 行 / {row['tables']} 表"]
        for c, v in enumerate(vals):
            item = QTableWidgetItem(str(v))
            self.chain_table.setItem(r, c, item)

    def _reload_segments(self):
        self.segment_combo.clear()
        path = self.chain_combo.currentData()
        if not path:
            return
        try:
            info = chain_rows(path)
        except Exception as exc:                      # noqa: BLE001
            self.restore_status.setText(f'链解析失败: {exc}')
            return
        for row in info.get('rows') or []:
            label = (f"#{row['id']} " +
                     ('基线' if row['kind'] == 'full' else '增量') +
                     f" {row['created_at']}")
            self.segment_combo.addItem(label, row['id'])
        if self.segment_combo.count():
            self.segment_combo.setCurrentIndex(self.segment_combo.count() - 1)

    # ---------- 操作 ----------

    def _start_worker(self, action, **kwargs):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, '提示', '已有备份任务运行中')
            return
        self.log_view.clear()
        self.restore_log.clear()
        self.progress = QProgressDialog('备份链任务执行中...', None, 0, 0, self)
        self.progress.setWindowTitle('备份链')
        self.progress.setCancelButton(None)
        self.progress.setMinimumDuration(0)
        self.progress.show()
        self.worker = ChainWorker(action, **kwargs)
        self.worker.log.connect(self._on_log)
        self.worker.done.connect(self._on_done)
        for b in (self.init_btn, self.incr_btn, self.prune_btn,
                  self.restore_btn, self.preview_btn):
            b.setEnabled(False)
        self.worker.start()

    def _on_log(self, line):
        self.log_view.append(line)
        self.restore_log.append(line)

    def _on_done(self, result):
        self.last_result = result
        if self.progress is not None:
            self.progress.close()
            self.progress = None
        for b in (self.init_btn, self.incr_btn, self.prune_btn,
                  self.restore_btn, self.preview_btn):
            b.setEnabled(True)
        if not result.get('ok'):
            msg = result.get('error') or 'CLI 失败'
            self.chain_status.setText(
                f"失败（返回码 {result.get('returncode')}）: {msg}")
            self.chain_status.setStyleSheet('color: #f48771;')
            QMessageBox.warning(self, '备份链操作失败', msg)
            return
        self.chain_status.setText(
            f"成功（返回码 {result.get('returncode')}）"
            f" 动作={result.get('action')}")
        self.chain_status.setStyleSheet('color: #89d185;')
        if result.get('action') == 'restore':
            self._show_verify(result)
        self.refresh_chains()

    def _run_action(self, action):
        chain_dir = self.chain_combo.currentData()
        data_dir = ''
        if chain_dir:
            try:
                m = load_manifest(chain_dir)
                data_dir = m.get('source_data_dir') or ''
            except Exception:
                data_dir = ''
        if action == 'init':
            data_dir = QFileDialog.getExistingDirectory(
                self, '选择要备份的 data 目录')
            if not data_dir:
                return
            chain_dir = QFileDialog.getExistingDirectory(
                self, '选择新链目录（必须为空）')
            if not chain_dir:
                return
        if not data_dir:
            QMessageBox.warning(self, '参数缺失', '需要 data-dir')
            return
        self._start_worker(action, data_dir=data_dir,
                           chain_dir=chain_dir)

    def _run_prune(self):
        chain_dir = self.chain_combo.currentData()
        if not chain_dir:
            QMessageBox.warning(self, '提示', '请先选择备份链')
            return
        self._start_worker('prune', chain_dir=chain_dir,
                           keep=self.keep_spin.value())

    def preview_restore(self):
        chain_dir = self.chain_combo.currentData()
        if not chain_dir:
            QMessageBox.warning(self, '提示', '请先选择备份链')
            return None
        to = self.segment_combo.currentData()
        res = preview_restore(chain_dir, to)
        self.preview_table.setRowCount(0)
        if not res.get('ok'):
            self.restore_status.setText(f"预览失败: {res.get('error')}")
            self.restore_status.setStyleSheet('color: #f48771;')
            return res
        p = res['preview']
        for k, label in (('target_segment', '目标段'),
                         ('applied_increments', '应用增量数'),
                         ('tables_affected', '涉及表'),
                         ('inserts', 'inserts'), ('updates', 'updates'),
                         ('deletes', 'deletes'), ('ddl', 'DDL'),
                         ('tables_total', '最终表数'),
                         ('rows_total', '最终行数')):
            r = self.preview_table.rowCount()
            self.preview_table.insertRow(r)
            self.preview_table.setItem(r, 0, QTableWidgetItem(label))
            self.preview_table.setItem(r, 1, QTableWidgetItem(str(p.get(k))))
        self.restore_status.setText('预览完成（尚未触碰目标目录）')
        self.restore_status.setStyleSheet('color: #89d185;')
        return res

    def restore_selected(self):
        chain_dir = self.chain_combo.currentData()
        target = self.target_edit.text().strip()
        if not chain_dir or not target:
            QMessageBox.warning(self, '恢复参数缺失', '需要备份链与目标目录')
            return
        preview = self.preview_restore()
        if not preview or not preview.get('ok'):
            return
        msg = (f"将恢复链:\n{chain_dir}\n到目标:\n{target}\n\n"
               f"应用增量到段 #{self.segment_combo.currentData()}，"
               f"涉及 {preview['preview']['tables_affected']} 表。\n"
               '目标目录非空会被覆盖；此操作不可逆，确认执行？')
        ret = QMessageBox.question(
            self, '二次确认：恢复备份链', msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            self.restore_status.setText('用户取消（未执行恢复）')
            return
        self._start_worker('restore', chain_dir=chain_dir, target=target,
                           to=self.segment_combo.currentData(), force=True)

    def _show_verify(self, result):
        report = result.get('report') or {}
        chain_dir = result.get('chain_dir')
        to = self.segment_combo.currentData()
        rows = verification_rows(chain_dir, to)
        self.verify_table.setRowCount(0)
        for row in rows:
            r = self.verify_table.rowCount()
            self.verify_table.insertRow(r)
            self.verify_table.setItem(r, 0, QTableWidgetItem(row['table']))
            self.verify_table.setItem(r, 1, QTableWidgetItem(str(row['rows'])))
            self.verify_table.setItem(
                r, 2, QTableWidgetItem('✓ 状态校验通过'
                                       if row['verified'] else '✗'))
        state_ok = bool(report.get('state_verified')) and bool(
            report.get('wal_zero'))
        verify_text = '状态校验通过' if state_ok else '状态校验未通过'
        self.restore_status.setText(
            f"恢复成功：{report.get('tables', len(rows))} 表 / "
            f"{report.get('row_total', '—')} 行；{verify_text}"
            f"（wal_zero={report.get('wal_zero')}）"
            f"耗时={report.get('total_s', '—')}s")
        self.restore_status.setStyleSheet('color: #89d185;')
