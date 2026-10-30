# -*- coding: utf-8 -*-
"""task-88：Studio 面板分栏比例持久化（轻量 JSON，不碰 config schema）。"""
import json
import os
from pathlib import Path


def layout_path() -> Path:
    root = Path(os.environ.get('LIGHTDB_STUDIO_LAYOUT_DIR') or
                (Path.home() / '.lightdb_studio'))
    return root / 'ui_layout.json'


def _load() -> dict:
    try:
        p = layout_path()
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding='utf-8'))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def save_splitter_ratios(key: str, splitter) -> bool:
    """保存 QSplitter 当前比例（按总尺寸归一化，窗口大小变化仍可用）。"""
    try:
        sizes = [max(0, int(x)) for x in splitter.sizes()]
        total = sum(sizes)
        if total <= 0:
            return False
        data = _load()
        data[str(key)] = [round(s / total, 6) for s in sizes]
        p = layout_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding='utf-8')
        tmp.replace(p)
        return True
    except Exception:
        return False


def restore_splitter_ratios(key: str, splitter,
                            default_sizes=None) -> list:
    """恢复比例；无记录时使用 default_sizes（缺省各占一半）。"""
    ratios = _load().get(str(key))
    total = sum(max(0, int(x)) for x in splitter.sizes())
    if total <= 0:
        total = 600
    if isinstance(ratios, list) and len(ratios) == splitter.count():
        sizes = [max(0, int(total * float(r))) for r in ratios]
        splitter.setSizes(sizes)
        return sizes
    if default_sizes:
        splitter.setSizes(list(default_sizes))
        return list(default_sizes)
    return splitter.sizes()


def connect_splitter_persistence(key: str, splitter):
    """拖动结束即保存；返回连接槽以便测试调用。"""
    def _save(*_args):
        return save_splitter_ratios(key, splitter)
    try:
        splitter.splitterMoved.connect(_save)
    except Exception:
        pass
    return _save
