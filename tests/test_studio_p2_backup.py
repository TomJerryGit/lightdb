# -*- coding: utf-8 -*-
"""P2-5 快照/备份产品化：保留策略 + 定时 + 恢复预览/紧急快照/回滚。

offscreen + 假连接 + tmp_path；覆盖：
- prune：keep_last / max_age_days / dry_run / 真实删除文件+索引 /
  删除失败显式 / P0 同名唯一（dup/dup_2/dup_3）不被误删；
- schedule.json round-trip + due(now) + run_due 创建/更新 last_run/保留清理；
- preview_restore 语句/表/风险摘要；restore(dry_run=True) 不执行 SQL、不建紧急快照；
- 真实恢复前自动紧急快照 pre_restore_<ts>；恢复失败用它回滚并显式报告；
  紧急快照创建失败 → 中止恢复（不静默）；回滚也失败 → 显式 rollback_error；
- SnapshotDialog（offscreen）：设置保存/加载、prune 预览、恢复预览文本、立即创建入口。
"""
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from lightdb_studio.core.snapshot import SnapshotManager, Snapshot

_APP = None


def qapp():
    global _APP
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


class _MsgBox:
    def __init__(self):
        self.calls = []
        self.answer = None

    def _rec(self, kind):
        def fn(*args, **kwargs):
            self.calls.append({'kind': kind,
                               'text': ' | '.join(str(a) for a in args)})
            from PySide6.QtWidgets import QMessageBox
            return self.answer if self.answer is not None else QMessageBox.Yes
        return fn

    def texts(self, kind=None):
        return [c['text'] for c in self.calls
                if kind is None or c['kind'] == kind]


@pytest.fixture
def msgbox(monkeypatch):
    box = _MsgBox()
    from PySide6.QtWidgets import QMessageBox
    for name in ('information', 'warning', 'critical', 'question'):
        monkeypatch.setattr(QMessageBox, name, staticmethod(box._rec(name)))
    return box


# ==================== 假连接 ====================

class FakeConn:
    """最小快照接口；可注入执行失败（用于恢复失败/回滚路径）。"""

    def __init__(self, tables=None, rows=None, execute_fail_pattern=None,
                 fail_once=True, fail_tables=False):
        self.config = SimpleNamespace(name='fake-conn', database='fake_db')
        self.tables = list(tables or ['t1', 't2'])
        self.rows = rows or {'t1': [(1, 'a'), (2, 'b')],
                             't2': [(1, 'c')]}
        self.executed = []
        self.execute_fail_pattern = execute_fail_pattern
        self.fail_once = fail_once
        self._failed_once = False
        self.fail_tables = fail_tables

    def get_tables(self):
        if self.fail_tables:
            raise RuntimeError('SHOW TABLES failed')
        return list(self.tables)

    def get_columns(self, table):
        return [{'name': 'id', 'type': 'INT', 'null': 'NO'},
                {'name': 'name', 'type': 'VARCHAR(32)', 'null': 'YES'}]

    def query(self, sql, params=None):
        up = sql.strip().upper()
        if up.startswith('SHOW CREATE TABLE'):
            for t in self.tables:
                if f'`{t}`' in sql:
                    return (['Table', 'Create Table'],
                            [(t, f'CREATE TABLE `{t}` (`id` INT PRIMARY KEY)')])
            return ['Table', 'Create Table'], []
        if up.startswith('SELECT'):
            for t in self.tables:
                if f'`{t}`' in sql:
                    return ['id', 'name'], [tuple(r) for r in self.rows.get(t, [])]
            return [], []
        return [], []

    def execute(self, sql):
        self.executed.append(sql)
        if self.execute_fail_pattern and self.execute_fail_pattern in sql:
            if not self._failed_once or not self.fail_once:
                self._failed_once = True
                raise RuntimeError(f'injected failure: {sql[:40]}')
        return 1, None


def mgr(tmp_path, name='snaps'):
    return SnapshotManager(dir=str(tmp_path / name))


def create(manager, name, conn=None):
    return manager.create_snapshot(conn or FakeConn(), name=name)


# ==================== 1. prune 保留策略 ====================

def test_prune_keep_last_dry_run_and_real(tmp_path):
    m = mgr(tmp_path)
    for name in ('s1', 's2', 's3'):
        r = create(m, name)
        assert r.ok, r.error
    # 让 created_at 明确区分（Windows 时钟分辨率粗，不能依赖创建顺序）
    now = time.time()
    for s in m.list_all():
        s.created_at = {'s1': now - 200, 's2': now - 100,
                        's3': now}[s.name]
    report = m.prune(keep_last=2, dry_run=True)
    print(f"  → dry-run 将删除: {[c['name'] for c in report['candidates']]}")
    assert report['dry_run'] is True
    assert [c['name'] for c in report['candidates']] == ['s1']
    assert all(Path(s.file).exists() for s in m.list_all()), 'dry-run 不应删文件'
    assert len(m.list_all()) == 3
    # 真实删除
    report = m.prune(keep_last=2, dry_run=False)
    assert report['ok'] is True
    assert report['deleted'] == ['s1']
    names = [s.name for s in m.list_all()]
    assert names == ['s3', 's2'], names
    assert not Path(tmp_path / 'snaps' / 's1.sql').exists()
    # 重新加载索引仍是 2 条，文件与索引一致
    m2 = SnapshotManager(dir=str(tmp_path / 'snaps'))
    assert sorted(s.name for s in m2.list_all()) == ['s2', 's3']


def test_prune_max_age(tmp_path):
    m = mgr(tmp_path)
    old = create(m, 'old').snapshot
    new = create(m, 'new').snapshot
    now = time.time()
    old.created_at = now - 10 * 86400
    new.created_at = now - 1 * 86400
    dry = m.prune(keep_last=0, max_age_days=5, dry_run=True, now=now)
    assert [c['name'] for c in dry['candidates']] == ['old']
    real = m.prune(keep_last=0, max_age_days=5, dry_run=False, now=now)
    assert real['deleted'] == ['old']
    assert [s.name for s in m.list_all()] == ['new']


def test_prune_does_not_break_p0_unique_names(tmp_path):
    m = mgr(tmp_path)
    names = [create(m, 'dup').snapshot.name for _ in range(3)]
    assert names == ['dup', 'dup_2', 'dup_3'], names
    now = time.time()
    for s in m.list_all():
        s.created_at = {'dup': now - 20, 'dup_2': now - 10,
                        'dup_3': now}[s.name]
    report = m.prune(keep_last=2, dry_run=False)
    assert report['deleted'] == ['dup'], report
    remaining = [s.name for s in m.list_all()]
    assert remaining == ['dup_3', 'dup_2'], remaining
    assert not (tmp_path / 'snaps' / 'dup.sql').exists()
    assert (tmp_path / 'snaps' / 'dup_2.sql').exists()
    assert (tmp_path / 'snaps' / 'dup_3.sql').exists()


def test_prune_delete_failure_is_explicit(tmp_path, monkeypatch):
    import lightdb_studio.core.snapshot as snap_mod
    m = mgr(tmp_path)
    s1 = create(m, 'a').snapshot
    s2 = create(m, 'b').snapshot
    now = time.time()
    for s in m.list_all():
        s.created_at = {'a': now - 10, 'b': now}[s.name]
    target_file = s1.file
    real_unlink = os.unlink

    def fake_unlink(path):
        if str(path) == target_file:
            raise OSError('locked by test')
        return real_unlink(path)

    monkeypatch.setattr(snap_mod.os, 'unlink', fake_unlink)
    report = m.prune(keep_last=1, dry_run=False)
    print(f"  → 删除失败报告: ok={report['ok']} errors={report['errors']}")
    assert report['ok'] is False
    assert report['errors'] and 'locked by test' in report['errors'][0]
    assert Path(target_file).exists(), '删除失败时文件不该消失'
    # 失败条目仍在索引里（不静默丢记录）；b 仍在
    names = [s.name for s in m.list_all()]
    assert 'a' in names and 'b' in names, names


def test_prune_no_policy_is_noop(tmp_path):
    m = mgr(tmp_path)
    create(m, 'x')
    report = m.prune(dry_run=True)
    assert report['candidates'] == [] and report['deleted'] == []
    report2 = m.prune(dry_run=False)
    assert report2['ok'] is True and len(m.list_all()) == 1


# ==================== 2. 定时快照 ====================

def test_schedule_roundtrip_and_due(tmp_path):
    m = mgr(tmp_path)
    cfg = m.save_schedule({'enabled': True, 'interval_minutes': 5,
                           'keep_last': 3, 'max_age_days': 2,
                           'name_prefix': 'auto'})
    assert cfg['enabled'] is True
    assert (tmp_path / 'snaps' / 'schedule.json').exists()
    m2 = SnapshotManager(dir=str(tmp_path / 'snaps'))
    loaded = m2.load_schedule()
    assert loaded['interval_minutes'] == 5 and loaded['keep_last'] == 3
    assert loaded['max_age_days'] == 2 and loaded['name_prefix'] == 'auto'
    now = time.time()
    assert m2.due(now) is True, '从未运行过应到期'
    cfg = m2.load_schedule()
    cfg['last_run'] = now - 10 * 60
    m2.save_schedule(cfg)
    assert m2.due(now) is True, '10 分钟 > 5 分钟间隔'
    cfg = m2.load_schedule()
    cfg['last_run'] = now - 60
    m2.save_schedule(cfg)
    assert m2.due(now) is False
    # 关闭后不到期
    cfg = m2.load_schedule()
    cfg['enabled'] = False
    m2.save_schedule(cfg)
    assert m2.due(now) is False


def test_run_due_creates_snapshot_and_prunes(tmp_path):
    m = mgr(tmp_path)
    old = create(m, 'old_keep0')
    for i, s in enumerate(m.list_all()):
        s.created_at = time.time() - 1000
    now = time.time()
    m.save_schedule({'enabled': True, 'interval_minutes': 1,
                     'keep_last': 1, 'max_age_days': 0,
                     'name_prefix': 'auto'})
    report = m.run_due(FakeConn(), now=now)
    print(f"  → run_due: created={report['created']} name={report.get('snapshot')} "
          f"prune={report.get('prune', {}).get('message') if report.get('prune') else None}")
    assert report['ok'] is True and report['created'] is True
    assert report['snapshot'].startswith('auto_')
    assert Path(report['file']).exists()
    assert report['prune'] is not None
    assert m.due(now + 1) is False, '刚运行完不应立即到期'
    # 第二次调用未到期 → 不创建
    report2 = m.run_due(FakeConn(), now=now + 1)
    assert report2['created'] is False and report2['due'] is False
    # 关闭时不到期
    cfg = m.load_schedule()
    cfg['enabled'] = False
    m.save_schedule(cfg)
    report3 = m.run_due(FakeConn(), now=now + 99999)
    assert report3['created'] is False and '未启用' in report3['reason']


# ==================== 3. 恢复预览 / dry-run / 紧急快照回滚 ====================

def test_preview_restore_summary_and_risks(tmp_path):
    m = mgr(tmp_path)
    r = create(m, 'p')
    preview = m.preview_restore(r.snapshot)
    print(f"  → preview: {preview['summary']}")
    assert preview['ok'] is True
    assert preview['create_tables'] == 2
    assert preview['drop_tables'] == 2
    assert preview['inserts'] == 3
    assert set(preview['tables']) == {'t1', 't2'}
    assert any('DROP TABLE' in risk for risk in preview['risks'])
    assert any('CREATE TABLE' in risk for risk in preview['risks'])
    assert 'DROP TABLE' in preview['summary']
    # 缺少文件 → 显式失败
    bad = Snapshot(name='bad', file=str(tmp_path / 'nope.sql'))
    preview2 = m.preview_restore(bad)
    assert preview2['ok'] is False and '不存在' in preview2['error']


def test_restore_dry_run_does_not_execute_or_backup(tmp_path):
    m = mgr(tmp_path)
    r = create(m, 'd')
    conn = FakeConn()
    before = list(m.list_all())
    result = m.restore_snapshot(conn, r.snapshot, dry_run=True)
    assert result['dry_run'] is True and result['ok'] is True
    assert result['executed'] == 0 and conn.executed == []
    assert result['preview']['create_tables'] == 2
    assert [s.name for s in m.list_all()] == [s.name for s in before], \
        'dry_run 不应创建紧急快照'


def test_restore_success_creates_emergency_snapshot(tmp_path):
    m = mgr(tmp_path)
    r = create(m, 'base')
    conn = FakeConn()
    result = m.restore_snapshot(conn, r.snapshot)
    print(f"  → 恢复成功: {result['ok']} emergency={result['emergency_snapshot']}")
    assert result['ok'] is True
    assert result['emergency_snapshot'].startswith('pre_restore_')
    assert result['rolled_back'] is False
    em = m.get(result['emergency_snapshot'])
    assert em is not None and Path(em.file).exists()
    assert conn.executed, '恢复应实际执行语句'


def test_restore_failure_rolls_back_with_emergency_snapshot(tmp_path):
    m = mgr(tmp_path)
    r = create(m, 'base')
    conn = FakeConn(execute_fail_pattern='INSERT INTO `t1`', fail_once=True)
    result = m.restore_snapshot(conn, r.snapshot)
    print(f"  → 恢复失败: ok={result['ok']} rolled_back="
          f"{result['rolled_back']} rollback_ok="
          f"{result.get('rollback_result', {}).get('ok')}")
    assert result['ok'] is False
    assert result['emergency_snapshot'].startswith('pre_restore_')
    assert result['rolled_back'] is True
    assert result.get('rollback_result', {}).get('ok') is True
    assert m.get(result['emergency_snapshot']) is not None


def test_restore_emergency_failure_aborts(tmp_path):
    m = mgr(tmp_path)
    r = create(m, 'base')
    bad = FakeConn(fail_tables=True)
    result = m.restore_snapshot(bad, r.snapshot)
    print(f"  → 紧急快照失败中止: {result.get('error')}")
    assert result['ok'] is False
    assert '紧急快照' in result.get('error', '')
    assert bad.executed == [], '紧急快照失败时不应执行恢复 SQL'


def test_rollback_failure_is_explicit(tmp_path):
    m = mgr(tmp_path)
    r = create(m, 'base')
    # 第一次失败后，回滚时再次失败（fail_once=False 持续失败）→ 显式 rollback_error
    conn = FakeConn(execute_fail_pattern='INSERT INTO `t1`', fail_once=False)
    result = m.restore_snapshot(conn, r.snapshot)
    assert result['ok'] is False
    assert result['rolled_back'] is False
    assert result.get('rollback_error')


# ==================== 4. SnapshotDialog（offscreen） ====================

def _make_dialog(tmp_path, conn=None):
    from lightdb_studio.ui.snapshot_dialog import SnapshotDialog
    qapp()
    manager = SnapshotManager(dir=str(tmp_path / 'dialog_snaps'))
    conn = conn or FakeConn()
    dlg = SnapshotDialog(lambda: conn, manager=manager)
    return dlg, manager, conn


def test_snapshot_dialog_has_strategy_controls(tmp_path, msgbox):
    dlg, manager, _conn = _make_dialog(tmp_path)
    dlg.show()
    qapp().processEvents()
    for attr in ('keep_last_spin', 'max_age_spin', 'schedule_enabled',
                 'interval_spin', 'prefix_edit', 'save_schedule_btn',
                 'prune_preview_btn', 'prune_apply_btn', 'run_due_btn',
                 'restore_preview_btn'):
        assert hasattr(dlg, attr), attr
    # 保存设置 → 落盘 + 重新读取
    dlg.keep_last_spin.setValue(7)
    dlg.max_age_spin.setValue(3)
    dlg.schedule_enabled.setChecked(True)
    dlg.interval_spin.setValue(15)
    dlg.prefix_edit.setText('nightly')
    cfg = dlg.save_strategy_settings()
    assert cfg['keep_last'] == 7 and cfg['max_age_days'] == 3
    loaded = manager.load_schedule()
    assert loaded['enabled'] is True and loaded['interval_minutes'] == 15
    assert loaded['name_prefix'] == 'nightly'
    assert (tmp_path / 'dialog_snaps' / 'schedule.json').exists()
    dlg.close()


def test_snapshot_dialog_prune_preview_and_restore_preview(tmp_path, msgbox):
    dlg, manager, _conn = _make_dialog(tmp_path)
    dlg.show()
    qapp().processEvents()
    for name in ('one', 'two', 'three'):
        manager.create_snapshot(FakeConn(), name=name)
    dlg._refresh_list()
    assert dlg.list_widget.rowCount() == 3
    dlg.keep_last_spin.setValue(1)
    report = dlg.prune_dry_run()
    assert report['dry_run'] is True and report['candidates']
    assert dlg.strategy_status.text()
    # 恢复预览（选第一行）
    dlg.list_widget.setCurrentCell(0, 0)
    preview = dlg.preview_selected_restore()
    assert preview['ok'] is True
    assert 'CREATE TABLE' in preview['summary']
    assert 'DROP TABLE' in preview['summary']
    # 立即运行到期检查：未启用 → 不创建
    dlg.schedule_enabled.setChecked(False)
    dlg.save_strategy_settings()
    r = dlg.run_due_now()
    assert r.get('created') is False
    dlg.close()


def test_snapshot_dialog_run_due_enabled(tmp_path, msgbox):
    dlg, manager, _conn = _make_dialog(tmp_path)
    dlg.show()
    qapp().processEvents()
    dlg.schedule_enabled.setChecked(True)
    dlg.interval_spin.setValue(1)
    dlg.prefix_edit.setText('ui_auto')
    cfg = dlg.save_strategy_settings()
    cfg['last_run'] = 0
    manager.save_schedule(cfg)
    report = dlg.run_due_now()
    print(f"  → UI run_due: created={report.get('created')} "
          f"snapshot={report.get('snapshot')}")
    assert report.get('created') is True
    assert report.get('snapshot', '').startswith('ui_auto_')
    assert dlg.list_widget.rowCount() >= 1
    dlg.close()


# ==================== P2-5：主窗口定时器接线（不启动真实 GUI 事件循环） ====================

class _FakeConn:
    def is_connected(self):
        return True


class _FakeSnapshot:
    name = 'auto_20261021_1100'


def test_main_window_snapshot_tick_wires_run_due():
    """`MainWindow._tick_snapshot_schedule` 必须按 schedule 配置调用 run_due。

    未启用 → 不调用；启用 + 已连接 → 调用并更新状态栏；不支持 run_due 的
    manager → 直接返回不崩溃（定时器回调不能把 GUI 弄崩）。
    """
    from lightdb_studio.ui.main_window import MainWindow

    calls = []
    statuses = []

    class _Mgr:
        def __init__(self, enabled=True):
            self.enabled = enabled

        def schedule_status(self):
            return {'config': {'enabled': self.enabled}, 'due': True}

        def run_due(self, conn):
            calls.append(conn)
            return {'ok': True, 'created': True, 'snapshot': _FakeSnapshot()}

    class _Win:
        def __init__(self, mgr):
            self.snapshot_mgr = mgr

        def _get_current_conn(self):
            return _FakeConn()

        def _set_status(self, text, error=False):
            statuses.append((text, error))

    MainWindow._tick_snapshot_schedule(_Win(_Mgr(enabled=True)))
    assert len(calls) == 1
    assert statuses and '定时快照已创建' in statuses[-1][0]

    MainWindow._tick_snapshot_schedule(_Win(_Mgr(enabled=False)))
    assert len(calls) == 1, calls

    class _NoRun:
        pass

    MainWindow._tick_snapshot_schedule(_Win(_NoRun()))


if __name__ == '__main__':
    print('本文件需 pytest（fixture）；直接运行仅做导入检查')
    sys.exit(0)
