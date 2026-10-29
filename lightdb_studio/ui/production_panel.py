# -*- coding: utf-8 -*-
"""task-83：Studio 生产化面板（复制状态 / metrics 摘要 / 二进制快照 CLI）。

真实数据源，不伪造：
- 复制：当前连接 `SHOW SLAVE STATUS`，无数据/失败时读复制状态 JSON；
  缺失字段显示 "—"，并展示 task-81 新增字段（last_event_type/applied/
  idempotent/errors 等）。
- metrics：用户配置 Prometheus URL（env `LIGHTDB_METRICS_URL`），真实 HTTP
  拉取 + 文本解析；不可达显式错误，Retry 复用同一按钮。
- 快照：子进程调用现有 `scripts/snapshot_backup.py` / `snapshot_restore.py`；
  恢复必须 QMessageBox 二次确认，不自动执行；日志/返回码原样展示。
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTabWidget,
    QWidget, QTableWidget, QTableWidgetItem, QLineEdit, QFormLayout,
    QTextEdit, QCheckBox, QMessageBox, QGroupBox,
)

ROOT = Path(__file__).resolve().parents[2]

_REPL_FIELDS = [
    ('Master_Host', 'master_host', '主库 Host'),
    ('Master_Port', 'master_port', '主库 Port'),
    ('Master_Log_File', 'master_file', 'Master 文件'),
    ('Read_Master_Log_Pos', 'read_pos', 'Read 位点'),
    ('Exec_Master_Log_Pos', 'exec_pos', 'Exec 位点'),
    ('Seconds_Behind_Master', 'lag_seconds', '延迟(秒)'),
    ('Slave_IO_Running', 'io_running', 'IO 线程'),
    ('Slave_SQL_Running', 'sql_running', 'SQL 线程'),
    ('Last_Error', 'last_error', 'Last_Error'),
    ('Last_IO_Error', 'last_io_error', 'Last_IO_Error'),
    ('Last_SQL_Error', 'last_sql_error', 'Last_SQL_Error'),
]


def parse_prometheus(text: str) -> dict:
    samples = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            left, value = line.rsplit(None, 1)
            value = float(value)
        except (ValueError, IndexError):
            continue
        name, labels = left, ''
        if '{' in left:
            name, rest = left.split('{', 1)
            labels = rest.rsplit('}', 1)[0]
        samples.append({'name': name.strip(), 'labels': labels,
                        'value': value})
    aliases = {
        'qps': ('queries_per_second', 'qps'),
        'connections': ('connections_current', 'connections',
                        'threads_connected'),
        'errors': ('error', 'errors', 'aborted', 'failed'),
        'slow': ('slow',),
        'wal': ('wal',),
        'checkpoint': ('checkpoint',),
        'pool': ('buffer_pool', 'pool'),
    }
    summary = {}
    for cat, patterns in aliases.items():
        hit = next((s for s in samples
                    if any(p in s['name'].lower() for p in patterns)), None)
        summary[cat] = (dict(hit, labels=hit.get('labels', ''))
                        if hit else {'name': None, 'value': None,
                                     'labels': '', 'missing': '—'})
    summary['sample_count'] = len(samples)
    return summary


def fetch_metrics(url: str, timeout: float = 5.0) -> str:
    parsed = urllib.parse.urlparse(url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError(f'非法 metrics URL（仅 http/https）: {url!r}')
    req = urllib.request.Request(url, headers={'Accept': 'text/plain'})
    with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
        return resp.read(2 * 1024 * 1024).decode('utf-8', errors='replace')


def _show_slave_status(conn):
    cols, rows = conn.query('SHOW SLAVE STATUS')
    if not rows:
        return None
    row = rows[0]
    idx = {str(c).lower(): i for i, c in enumerate(cols or [])}
    out = {}
    for db_name, key, _label in _REPL_FIELDS:
        i = idx.get(db_name.lower())
        out[key] = row[i] if i is not None and i < len(row) else None
    return out


def _from_state(path):
    p = Path(path or '')
    if not path or not p.exists():
        return None, (f'复制状态文件不存在: {path}' if path else '未指定复制状态文件')
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception as exc:                          # noqa: BLE001
        return None, f'复制状态文件解析失败: {exc}'
    if not isinstance(raw, dict):
        return None, '复制状态文件格式非法'
    fields = {
        'master_host': raw.get('master_host'),
        'master_port': raw.get('master_port'),
        'master_file': raw.get('master_file') or raw.get('relay_file'),
        'read_pos': raw.get('master_position') or raw.get('read_pos'),
        'exec_pos': raw.get('last_seq') or raw.get('seq') or raw.get('lsn'),
        'lag_seconds': (round(float(raw.get('lag_ms')) / 1000.0, 3)
                        if raw.get('lag_ms') is not None
                        else raw.get('lag_seconds')),
        'io_running': raw.get('io_running'),
        'sql_running': raw.get('sql_running'),
        'last_error': raw.get('last_error') or raw.get('error'),
        'last_io_error': raw.get('last_io_error'),
        'last_sql_error': raw.get('last_sql_error'),
        'last_event_type': raw.get('last_event_type'),
        'applied': raw.get('applied'),
        'idempotent': raw.get('idempotent'),
        'errors': raw.get('errors'),
        'state_path': str(p),
    }
    return {'fields': fields, 'raw': raw,
            'unknown_keys': sorted(set(raw) - set(fields))}, ''


def replication_status(conn=None, state_file=''):
    show, show_error = None, ''
    if conn is not None:
        try:
            show = _show_slave_status(conn)
        except Exception as exc:                      # noqa: BLE001
            show_error = str(exc)
    state, state_error = _from_state(state_file)
    if show:
        fields, source, unknown = show, 'SHOW SLAVE STATUS', []
    elif state:
        fields = state['fields']
        source = 'state_file:' + fields.get('state_path', '')
        unknown = state['unknown_keys']
    else:
        return {'configured': False, 'source': None, 'fields': {},
                'missing': [k for _n, k, _l in _REPL_FIELDS],
                'show_slave_error': show_error,
                'state_file_error': state_error,
                'notice': '未配置复制：SHOW SLAVE STATUS 无数据且无可用复制状态文件'}
    display = {}
    for _name, key, label in _REPL_FIELDS:
        value = fields.get(key)
        display[key] = {'label': label,
                        'value': '—' if value is None or value == ''
                        else str(value)}
    for key, label in (('last_event_type', '最后事件类型'),
                       ('applied', '已应用事件'),
                       ('idempotent', '幂等跳过'),
                       ('errors', '错误数')):
        if key in fields:
            value = fields.get(key)
            display[key] = {'label': label,
                            'value': '—' if value is None else str(value)}
    return {'configured': True, 'source': source, 'fields': display,
            'last_error': str(fields.get('last_error') or ''),
            'unknown_fields': unknown,
            'show_slave_error': show_error,
            'state_file_error': state_error}


def run_snapshot_cli(mode: str, data_dir='', out='', snapshot='', table='',
                     online=False, force=False, timeout=120) -> dict:
    script = ('snapshot_backup.py' if mode == 'backup'
              else 'snapshot_restore.py')
    args = [sys.executable, str(ROOT / 'scripts' / script)]
    if mode == 'backup':
        if not data_dir or not out:
            return {'ok': False, 'error': '备份需要 data_dir 与 out',
                    'command': [], 'stdout': '', 'stderr': ''}
        args += ['--data-dir', str(data_dir), '--out', str(out)]
        if table:
            args += ['--table', str(table)]
        if online:
            args += ['--online']
        if force:
            args += ['--force']
    else:
        if not snapshot or not data_dir:
            return {'ok': False, 'error': '恢复需要 snapshot 与 data_dir',
                    'command': [], 'stdout': '', 'stderr': ''}
        args += ['--snapshot', str(snapshot), '--data-dir', str(data_dir)]
        if force:
            args += ['--force']
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=float(timeout), cwd=str(ROOT))
    except subprocess.TimeoutExpired as exc:
        return {'ok': False, 'returncode': None, 'command': args,
                'stdout': '', 'stderr': f'超时 {timeout}s',
                'error': f'快照 CLI 超时 {timeout}s'}
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'returncode': None, 'command': args,
                'stdout': '', 'stderr': str(exc), 'error': str(exc)}
    return {'ok': proc.returncode == 0, 'returncode': proc.returncode,
            'command': args, 'stdout': proc.stdout or '',
            'stderr': proc.stderr or '',
            'error': '' if proc.returncode == 0 else
                     (proc.stderr or proc.stdout or f'exit {proc.returncode}')}


class ReplicationPanel(QWidget):
    def __init__(self, conn_getter=None, state_file='', parent=None):
        super().__init__(parent)
        self.conn_getter = conn_getter
        self.state_file = state_file or os.environ.get(
            'LIGHTDB_REPLICA_STATE', '')
        self.last_status = None
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.state_edit = QLineEdit(self.state_file)
        self.state_edit.setPlaceholderText(
            '复制状态文件路径（可选；无 SHOW SLAVE STATUS 时读取）')
        top.addWidget(QLabel('状态文件:'))
        top.addWidget(self.state_edit, 1)
        self.refresh_btn = QPushButton('🔄 刷新复制状态')
        self.refresh_btn.clicked.connect(self.refresh)
        top.addWidget(self.refresh_btn)
        layout.addLayout(top)
        self.info = QLabel('点击“刷新复制状态”')
        self.info.setWordWrap(True)
        self.info.setStyleSheet('color: #9cdcfe;')
        layout.addWidget(self.info)
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(['字段', '值'])
        self.table.setColumnWidth(0, 220)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.error = QLabel('')
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color: #f48771;')
        layout.addWidget(self.error)

    def refresh(self):
        self.state_file = self.state_edit.text().strip()
        conn = self.conn_getter() if self.conn_getter else None
        try:
            status = replication_status(conn, self.state_file)
        except Exception as exc:                      # noqa: BLE001
            self.error.setText(f'读取复制状态失败: {exc}')
            return None
        self.last_status = status
        self.error.setText('')
        self.table.setRowCount(0)
        if not status.get('configured'):
            self.info.setText(status.get('notice', '未配置复制'))
            if status.get('show_slave_error'):
                self.info.setText(self.info.text() +
                                  f"（SHOW SLAVE STATUS: {status['show_slave_error']}）")
            if status.get('state_file_error'):
                self.info.setText(self.info.text() +
                                  f"（状态文件: {status['state_file_error']}）")
            return status
        self.info.setText(f"来源: {status['source']}")
        for key, item in status.get('fields', {}).items():
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(item['label']))
            self.table.setItem(r, 1, QTableWidgetItem(item['value']))
        if status.get('unknown_fields'):
            self.info.setText(self.info.text() +
                              f"；新增字段: {', '.join(status['unknown_fields'])}")
        return status


class MetricsPanel(QWidget):
    def __init__(self, url='', parent=None):
        super().__init__(parent)
        self.url = url or os.environ.get('LIGHTDB_METRICS_URL', '')
        self.last_summary = None
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.url_edit = QLineEdit(self.url)
        self.url_edit.setPlaceholderText(
            'Prometheus /metrics URL，如 http://127.0.0.1:8080/metrics')
        top.addWidget(QLabel('Metrics URL:'))
        top.addWidget(self.url_edit, 1)
        self.fetch_btn = QPushButton('🔄 拉取指标')
        self.fetch_btn.clicked.connect(self.refresh)
        top.addWidget(self.fetch_btn)
        layout.addLayout(top)
        self.status = QLabel('输入 metrics URL 后拉取')
        self.status.setWordWrap(True)
        self.status.setStyleSheet('color: #9cdcfe;')
        layout.addWidget(self.status)
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(['指标', '值'])
        self.table.setColumnWidth(0, 220)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        layout.addWidget(QLabel('原始文本（截断）：'))
        self.raw = QTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setFont(QFont('Consolas', 9))
        self.raw.setMaximumHeight(150)
        layout.addWidget(self.raw)

    def refresh(self):
        url = self.url_edit.text().strip()
        self.url = url
        self.table.setRowCount(0)
        self.raw.clear()
        if not url:
            self.status.setText('未配置 metrics URL（可设置 LIGHTDB_METRICS_URL）')
            return None
        try:
            text = fetch_metrics(url)
        except Exception as exc:                      # noqa: BLE001
            self.status.setText(f'metrics 不可达，可重试: {exc}')
            self.status.setStyleSheet('color: #f48771;')
            return None
        self.raw.setPlainText(text[:8000])
        summary = parse_prometheus(text)
        self.last_summary = summary
        labels = [('qps', 'QPS'), ('connections', '连接'),
                  ('errors', '错误'), ('slow', '慢查询'),
                  ('wal', 'WAL'), ('checkpoint', 'Checkpoint'),
                  ('pool', '连接/缓冲池')]
        for key, label in labels:
            item = summary[key]
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(label))
            value = '—' if item.get('value') is None else str(item['value'])
            self.table.setItem(r, 1, QTableWidgetItem(value))
        self.status.setText(
            f'已拉取 {summary["sample_count"]} 个样本（缺失指标显示 —）')
        self.status.setStyleSheet('color: #89d185;')
        return summary


class SnapshotCLIPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.last_result = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.data_edit = QLineEdit()
        self.data_edit.setPlaceholderText('要备份/恢复的 data 目录')
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText('快照输出目录')
        self.table_edit = QLineEdit()
        self.table_edit.setPlaceholderText('仅单表快照（可选）')
        self.online_check = QCheckBox('在线热备（--online）')
        self.force_check = QCheckBox('--force（覆盖/危险）')
        form.addRow('data-dir:', self.data_edit)
        form.addRow('out:', self.out_edit)
        form.addRow('table:', self.table_edit)
        form.addRow('', self.online_check)
        form.addRow('', self.force_check)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.backup_btn = QPushButton('💾 二进制快照备份')
        self.backup_btn.clicked.connect(self.run_backup)
        row.addWidget(self.backup_btn)
        self.snapshot_edit = QLineEdit()
        self.snapshot_edit.setPlaceholderText('快照目录（恢复前需二次确认）')
        row.addWidget(self.snapshot_edit, 1)
        self.restore_btn = QPushButton('↩ 恢复（需确认）')
        self.restore_btn.clicked.connect(self.run_restore)
        row.addWidget(self.restore_btn)
        layout.addLayout(row)
        self.status = QLabel('快照 CLI 结果会显示在这里')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont('Consolas', 9))
        layout.addWidget(self.log, 1)

    def _show_result(self, result):
        self.last_result = result
        self.status.setText(
            f"命令: {' '.join(str(x) for x in result.get('command', []))}\n"
            f"返回码: {result.get('returncode')} / ok={result.get('ok')}")
        self.log.setPlainText(
            (result.get('stdout') or '') + '\n' +
            (result.get('stderr') or '') + '\n' +
            (result.get('error') or ''))

    def run_backup(self):
        result = run_snapshot_cli(
            'backup', data_dir=self.data_edit.text().strip(),
            out=self.out_edit.text().strip(),
            table=self.table_edit.text().strip(),
            online=self.online_check.isChecked(),
            force=self.force_check.isChecked())
        self._show_result(result)
        return result

    def run_restore(self):
        snapshot = self.snapshot_edit.text().strip()
        data_dir = self.data_edit.text().strip()
        if not snapshot or not data_dir:
            self.status.setText('恢复需要快照目录与 data-dir')
            return {'ok': False, 'error': '恢复需要 snapshot 与 data_dir'}
        ret = QMessageBox.question(
            self, '确认恢复快照',
            f'恢复是不可逆操作：\n{snapshot} → {data_dir}\n'
            '将覆盖目标数据目录；是否确认执行恢复？',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            self.status.setText('用户取消恢复（未执行）')
            return {'ok': False, 'cancelled': True}
        result = run_snapshot_cli(
            'restore', snapshot=snapshot, data_dir=data_dir,
            force=self.force_check.isChecked())
        self._show_result(result)
        return result


class ProductionPanel(QDialog):
    def __init__(self, conn_getter=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('生产化能力：复制 / 指标 / 快照')
        self.resize(1050, 720)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        self.replication_panel = ReplicationPanel(conn_getter, parent=self)
        self.metrics_panel = MetricsPanel(parent=self)
        self.snapshot_panel = SnapshotCLIPanel(parent=self)
        tabs.addTab(self.replication_panel, '复制状态')
        tabs.addTab(self.metrics_panel, '指标摘要')
        tabs.addTab(self.snapshot_panel, '快照备份/恢复')
        layout.addWidget(tabs)
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignRight)
