"""备份自动化：保留策略单元测试 + 备份/校验/演练集成测试。"""
import json
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import backup_automation as auto  # noqa: E402


def test_select_retention_keeps_latest():
    paths = [Path(f'full-2026010{i}-000000') for i in range(1, 6)]
    keep, delete = auto.select_retention(paths, 2)
    assert [p.name for p in keep] == [f'full-20260104-000000',
                                      f'full-20260105-000000']
    assert [p.name for p in delete] == [f'full-2026010{i}-000000'
                                        for i in range(1, 4)]


def test_select_retention_no_delete_when_within_keep():
    paths = [Path('full-20260101-000000'), Path('full-20260102-000000')]
    keep, delete = auto.select_retention(paths, 5)
    assert len(keep) == 2 and delete == []


def test_list_backups_only_manifest_dirs(tmp_path):
    (tmp_path / 'full-20260101-000000').mkdir()
    (tmp_path / 'full-20260101-000000' / 'manifest.json').write_text('{}')
    (tmp_path / 'full-20260102-000000').mkdir()          # 无 manifest：忽略
    (tmp_path / 'other').mkdir()
    names = [p.name for p in auto.list_backups(tmp_path)]
    assert names == ['full-20260101-000000']


def _make_db(data_dir, rows=30):
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    db = Database(str(data_dir))
    ex = Executor(db)
    ex.execute_sql('CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))', {})
    for i in range(1, rows + 1):
        ex.execute_sql(f"INSERT INTO t VALUES ({i}, 'v{i}')", {})
    db.shutdown()


def test_backup_run_with_drill_and_retention(tmp_path):
    data = tmp_path / 'data'
    _make_db(data, rows=30)
    out = tmp_path / 'backup'
    rc = auto.main(['run', '--data-dir', str(data), '--out', str(out),
                    '--keep', '2', '--drill'])
    assert rc == 0
    status = json.loads((out / 'status.json').read_text(encoding='utf-8'))
    assert status['ok'] is True
    assert status['verify']['ok'] is True
    assert Path(status['backup']).is_dir()
    assert (Path(status['backup']) / 'manifest.json').is_file()
    # 再跑两次，保留策略应只留最新 2 份
    for _ in range(2):
        assert auto.main(['run', '--data-dir', str(data), '--out', str(out),
                          '--keep', '2']) == 0
    backups = auto.list_backups(out)
    assert len(backups) == 2, [p.name for p in backups]
    # 演练最新备份应成功
    assert auto.main(['drill', '--out', str(out)]) == 0
    status = json.loads((out / 'status.json').read_text(encoding='utf-8'))
    assert status['ok'] is True and status['drill'] is True


def test_drill_fails_on_corrupt_backup(tmp_path):
    data = tmp_path / 'data'
    _make_db(data, rows=10)
    out = tmp_path / 'backup'
    assert auto.main(['run', '--data-dir', str(data), '--out', str(out)]) == 0
    backups = auto.list_backups(out)
    assert backups
    manifest = backups[-1] / 'manifest.json'
    obj = json.loads(manifest.read_text(encoding='utf-8'))
    # 篡改一个数据文件的 sha256 让恢复自检失败（manifest 的 files 是列表）
    files = obj.get('files') or []
    assert files, 'manifest 缺少 files 列表'
    files[0]['sha256'] = '0' * 64
    manifest.write_text(json.dumps(obj), encoding='utf-8')
    rc = auto.main(['drill', '--out', str(out)])
    assert rc == 1
    status = json.loads((out / 'status.json').read_text(encoding='utf-8'))
    assert status['ok'] is False


def test_status_and_missing_data_dir(tmp_path):
    out = tmp_path / 'backup'
    assert auto.main(['run', '--data-dir', str(tmp_path / 'nope'),
                      '--out', str(out)]) == 1
    status = json.loads((out / 'status.json').read_text(encoding='utf-8'))
    assert status['ok'] is False
    assert auto.main(['status', '--out', str(out)]) == 0
