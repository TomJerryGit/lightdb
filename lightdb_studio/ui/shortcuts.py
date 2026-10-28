# -*- coding: utf-8 -*-
"""G3：快捷键自定义（默认表 / 冲突检测 / 应用到菜单与编辑器 / 持久化）。

- `settings.shortcuts` 仍是 dict（config 不改 schema），键 = 稳定 id（非中文
  标签），值为 Qt 键序列字符串；旧版按中文标签存的数据会被兼容读取；
- 冲突检测：同一个键绑定到多个动作 → 显式返回冲突列表，不静默覆盖；
- `apply_to_window()` 应用到主窗口菜单 QAction + 所有查询编辑器 SQLEditor；
- 默认表文档化在 `DEFAULT_SHORTCUTS`，设置页展示/修改/恢复默认。
"""
from collections import OrderedDict

from PySide6.QtGui import QAction, QKeySequence

# id -> {'label': 显示名, 'key': 默认键, 'menu': 菜单 action 文本（可选）}
DEFAULT_SHORTCUTS = OrderedDict([
    ('new_query', {'label': '新建查询', 'key': 'Ctrl+N',
                   'menu': '新建查询'}),
    ('run', {'label': '执行', 'key': 'Ctrl+Return', 'menu': '执行',
             'editor': True}),
    ('run_selection', {'label': '执行选中', 'key': 'Ctrl+Shift+Return',
                       'editor': True}),
    ('explain', {'label': 'EXPLAIN', 'key': 'Ctrl+E', 'menu': 'EXPLAIN'}),
    ('open_sql', {'label': '打开 SQL', 'key': 'Ctrl+O',
                  'menu': '打开 SQL 文件...'}),
    ('save_sql', {'label': '保存 SQL', 'key': 'Ctrl+S',
                  'menu': '保存 SQL...'}),
    ('close_tab', {'label': '关闭标签', 'key': 'Ctrl+W',
                   'menu': '关闭当前标签'}),
    ('refresh_tree', {'label': '刷新对象树', 'key': 'F9',
                      'menu': '刷新对象浏览器'}),
    ('settings', {'label': '设置', 'key': 'Ctrl+,',
                  'menu': '⚙️ 设置...'}),
    ('new_window', {'label': '新建窗口', 'key': 'Ctrl+Shift+N',
                    'menu': '🪟 新建窗口'}),
    ('find', {'label': '查找', 'key': 'Ctrl+F', 'editor': True}),
    ('replace', {'label': '查找替换', 'key': 'Ctrl+H', 'editor': True}),
    ('goto_line', {'label': '跳转行', 'key': 'Ctrl+L', 'editor': True}),
    ('toggle_comment', {'label': '注释/取消注释', 'key': 'Ctrl+/',
                        'editor': True}),
    ('duplicate_line', {'label': '复制当前行', 'key': 'Ctrl+D',
                        'editor': True}),
    ('move_line_up', {'label': '上移行', 'key': 'Alt+Up', 'editor': True}),
    ('move_line_down', {'label': '下移行', 'key': 'Alt+Down',
                        'editor': True}),
    ('execute_statement', {'label': '执行光标语句', 'key': 'Ctrl+Shift+E',
                           'editor': True}),
    ('completion', {'label': '补全', 'key': 'Ctrl+Space', 'editor': True}),
    # 编辑器兼容快捷键也纳入默认表/冲突检测（此前是未登记的隐藏 QShortcut）
    ('run_f5', {'label': '执行 (F5 备选)', 'key': 'F5', 'editor': True}),
    ('replace_alt', {'label': '查找替换 (Ctrl+R 备选)', 'key': 'Ctrl+R',
                     'editor': True}),
])

DEFAULT_KEYS = {k: v['key'] for k, v in DEFAULT_SHORTCUTS.items()}
_LABEL_TO_ID = {v['label']: k for k, v in DEFAULT_SHORTCUTS.items()}


def default_shortcuts():
    return dict(DEFAULT_KEYS)


def load_shortcuts(saved):
    """合并持久化值（兼容旧版中文 label 键；非法值保留默认）。"""
    result = default_shortcuts()
    for key, value in (saved or {}).items():
        action_id = key
        if action_id not in DEFAULT_SHORTCUTS:
            action_id = _LABEL_TO_ID.get(str(key))
        if action_id and isinstance(value, str):
            result[action_id] = value.strip()
    return result


def validate_shortcuts(mapping, allow_empty=True):
    """检测冲突/空绑定；返回 {'ok','conflicts','errors'}（不抛异常）。"""
    mapping = dict(mapping or {})
    conflicts = []
    errors = []
    seen = {}
    for action_id, key in mapping.items():
        if not isinstance(action_id, str) or action_id not in DEFAULT_SHORTCUTS:
            errors.append(f'未知快捷键 id: {action_id!r}')
            continue
        seq_text = str(key or '').strip()
        if not seq_text:
            if not allow_empty:
                errors.append(f'{DEFAULT_SHORTCUTS[action_id]["label"]} '
                              '不能为空')
            continue
        seq = QKeySequence(seq_text)
        if seq_text.endswith(('+', '-')) or seq_text == '+' :
            errors.append(
                f'{DEFAULT_SHORTCUTS[action_id]["label"]} 的快捷键非法: '
                f'{seq_text!r}')
            continue
        if seq.isEmpty() and seq_text:
            errors.append(
                f'{DEFAULT_SHORTCUTS[action_id]["label"]} 的快捷键非法: '
                f'{seq_text!r}')
            continue
        normalized = seq.toString()
        if normalized in seen:
            conflicts.append({
                'key': normalized,
                'ids': [seen[normalized], action_id],
                'labels': [DEFAULT_SHORTCUTS[seen[normalized]]['label'],
                           DEFAULT_SHORTCUTS[action_id]['label']],
            })
        else:
            seen[normalized] = action_id
    return {'ok': not conflicts and not errors,
            'conflicts': conflicts, 'errors': errors}


def _normalize_action_text(text):
    return str(text or '').replace('&', '').strip()


def apply_to_editor(editor, mapping):
    """应用编辑器快捷键（SQLEditor.apply_shortcut_map 存在时）。"""
    if editor is None or not hasattr(editor, 'apply_shortcut_map'):
        return 0
    return editor.apply_shortcut_map(mapping)


def apply_to_window(window, mapping):
    """应用到菜单 QAction + 所有查询编辑器；返回统计（不弹窗）。"""
    mapping = load_shortcuts(mapping)
    validation = validate_shortcuts(mapping)
    menu_applied = 0
    try:
        actions = window.findChildren(QAction)
    except Exception:
        actions = []
    by_text = {_normalize_action_text(a.text()): a for a in actions}
    for action_id, meta in DEFAULT_SHORTCUTS.items():
        menu_text = meta.get('menu')
        if not menu_text:
            continue
        action = by_text.get(_normalize_action_text(menu_text))
        if action is None:
            continue
        key = mapping.get(action_id, '')
        try:
            action.setShortcut(QKeySequence(key) if key else QKeySequence())
            menu_applied += 1
        except Exception:
            pass
    editor_applied = 0
    try:
        from .sql_editor import SQLEditor
        for editor in window.findChildren(SQLEditor):
            editor_applied += apply_to_editor(editor, mapping)
    except Exception:
        pass
    return {'ok': validation['ok'], 'menu_applied': menu_applied,
            'editor_applied': editor_applied,
            'conflicts': validation['conflicts'],
            'errors': validation['errors'], 'mapping': mapping}


def shortcuts_for_settings_page():
    """设置页用：[(id, label, default_key, editor_only)]。"""
    return [(action_id, meta['label'], meta['key'],
             bool(meta.get('editor'))) for action_id, meta in
            DEFAULT_SHORTCUTS.items()]


def format_default_table():
    lines = ['LightDB Studio 默认快捷键：']
    for action_id, meta in DEFAULT_SHORTCUTS.items():
        lines.append(f"  {meta['label']}: {meta['key']}")
    return '\n'.join(lines)
