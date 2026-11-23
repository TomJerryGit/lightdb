"""Web 增量备份链接口：链列表读取与不存在的目录显式空。"""
import json

from web.backup_chain import chain_rows, list_chains


def test_list_chains_missing_root_returns_not_ok(tmp_path):
    r = list_chains(str(tmp_path / 'nope'))
    assert r['ok'] is False and r['chains'] == []


def test_chain_rows_on_missing_chain(tmp_path):
    r = chain_rows(str(tmp_path / 'chain'))
    assert r['ok'] is False and r['rows'] == []


def test_list_chains_ignores_non_chain_dirs(tmp_path):
    (tmp_path / 'plain').mkdir()
    r = list_chains(str(tmp_path))
    assert r['ok'] is True
    assert r['chains'] == []
