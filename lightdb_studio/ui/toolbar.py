# -*- coding: utf-8 -*-
"""N1：Ribbon 风格主工具栏（大图标 + 分组，保留现有动作）。

- 分组：连接 / 表 / 视图 / 查询 / 工具；
- 图标：优先 QtAwesome；未安装时**显式 fallback** 到 Qt 内置标准图标，
  `icon_backend()` 返回实际后端，绝不假装用了 QtAwesome；
- 动作可用性：需要连接的动作由 `update_availability(has_connection)` 显式
  禁用/启用；不依赖连接的按钮始终可用；
- `self.actions_by_id` / `self.buttons_by_id` / `self.groups` 供测试与主窗口使用。
"""
from dataclasses import dataclass

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QToolBar, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QToolButton,
    QFrame, QApplication, QStyle,
)

_FALLBACK_STANDARD = {
    'connect': QStyle.SP_DriveNetIcon,
    'edit': QStyle.SP_FileDialogDetailedView,
    'refresh': QStyle.SP_BrowserReload,
    'new': QStyle.SP_FileIcon,
    'table': QStyle.SP_FileDialogListView,
    'design': QStyle.SP_FileDialogInfoView,
    'import': QStyle.SP_ArrowDown,
    'export': QStyle.SP_ArrowUp,
    'backup': QStyle.SP_DialogSaveButton,
    'query': QStyle.SP_FileDialogContentsView,
    'run': QStyle.SP_MediaPlay,
    'explain': QStyle.SP_FileDialogDetailedView,
    'ai': QStyle.SP_ComputerIcon,
    'tool': QStyle.SP_FileDialogStart,
    'view': QStyle.SP_FileDialogNewFolder,
}

_ICON_BACKEND = None
_FALLBACK_LAST = []


def icons_available():
    try:
        import qtawesome  # noqa: F401
        return True
    except Exception:
        return False


def icon_backend():
    global _ICON_BACKEND
    if _ICON_BACKEND is None:
        _ICON_BACKEND = 'qtawesome' if icons_available() else 'fallback'
    return _ICON_BACKEND


def fallback_icon(kind='new'):
    style = QApplication.style()
    sp = _FALLBACK_STANDARD.get(kind, QStyle.SP_FileIcon)
    return style.standardIcon(sp)


def make_icon(qta_name='', fallback_kind='new', color=None):
    """优先 QtAwesome；缺失时显式 fallback（记录到 `_FALLBACK_LAST`）。"""
    if icons_available() and qta_name:
        try:
            import qtawesome as qta
            if color:
                return qta.icon(qta_name, color=color)
            return qta.icon(qta_name)
        except Exception as exc:                      # noqa: BLE001
            _FALLBACK_LAST.append((qta_name, str(exc)))
    else:
        _FALLBACK_LAST.append((qta_name, 'qtawesome 未安装'))
    return fallback_icon(fallback_kind)


def fallback_events():
    """返回 fallback 记录（测试/诊断用；空 list 表示全部用 QtAwesome）。"""
    return list(_FALLBACK_LAST)


@dataclass
class RibbonActionSpec:
    action_id: str
    label: str                 # QAction text（保留 i18n 兼容标签）
    clean_label: str           # 按钮显示文本（去掉 emoji）
    callback: object
    icon: str = ''
    fallback_kind: str = 'new'
    group: str = '工具'
    needs_connection: bool = False
    tooltip: str = ''


class RibbonBar(QToolBar):
    """Ribbon 主工具栏（QToolBar 子类，兼容 `_main_toolbar` 既有断言）。"""

    def __init__(self, specs=None, parent=None):
        super().__init__('Ribbon', parent)
        self.setObjectName('RibbonBar')
        self.setMovable(False)
        self.setFloatable(False)
        self.setIconSize(QSize(30, 30))
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.groups = []
        self.actions_by_id = {}
        self.buttons_by_id = {}
        self.group_frames = {}
        self._connection_dependent = []
        self._compat_actions = []
        self._specs = list(specs or [])
        self._build()
        self.update_availability(False)

    def _build(self):
        grouped = {}
        order = []
        for spec in self._specs:
            if spec.group not in grouped:
                grouped[spec.group] = []
                order.append(spec.group)
            grouped[spec.group].append(spec)
        for group_name in order:
            self._add_group(group_name, grouped[group_name])
        # 兼容既有测试：`_main_toolbar.actions()` 需要看到动作文本；
        # 真实视觉按钮在 RibbonGroup 里（QToolButton），这里是隐藏的
        # 同源 QAction，仅用于 i18n/可访问性/旧接口断言。
        for spec in self._specs:
            act = QAction(spec.label, self)
            act.setToolTip(spec.tooltip or spec.clean_label)
            act.setIcon(make_icon(spec.icon, spec.fallback_kind))
            act.triggered.connect(spec.callback)
            act.setVisible(False)
            self.addAction(act)
            self.actions_by_id[spec.action_id] = act
            if spec.needs_connection:
                self._connection_dependent.append(act)
            self._compat_actions.append(act)
        self.setWindowTitle('主功能区')
        self.setAccessibleName('主功能区 / Ribbon toolbar')

    def _add_group(self, group_name, specs):
        frame = QFrame()
        frame.setObjectName('RibbonGroup')
        v = QVBoxLayout(frame)
        v.setContentsMargins(4, 2, 4, 1)
        v.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        buttons = {}
        for spec in specs:
            btn = QToolButton()
            btn.setProperty('ribbon', True)
            btn.setText(spec.clean_label)
            btn.setToolTip(spec.tooltip or spec.clean_label)
            btn.setIcon(make_icon(spec.icon, spec.fallback_kind))
            btn.setIconSize(QSize(30, 30))
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setAccessibleName(spec.clean_label)
            btn.clicked.connect(spec.callback)
            if spec.needs_connection:
                btn.setProperty('needs_connection', True)
                self._connection_dependent_buttons = getattr(
                    self, '_connection_dependent_buttons', [])
                self._connection_dependent_buttons.append(btn)
            row.addWidget(btn)
            buttons[spec.action_id] = btn
            self.buttons_by_id[spec.action_id] = btn
        v.addLayout(row)
        label = QLabel(group_name)
        label.setProperty('ribbonGroup', True)
        label.setAlignment(Qt.AlignCenter)
        v.addWidget(label)
        self.addWidget(frame)
        self.addSeparator()
        self.groups.append(group_name)
        self.group_frames[group_name] = frame

    def update_availability(self, has_connection):
        """显式禁用/启用依赖连接的动作（不可用动作不假可点）。"""
        for act in self._connection_dependent:
            act.setEnabled(bool(has_connection))
        for btn in getattr(self, '_connection_dependent_buttons', []):
            btn.setEnabled(bool(has_connection))

    def action(self, action_id):
        return self.actions_by_id.get(action_id)

    def button(self, action_id):
        return self.buttons_by_id.get(action_id)

    def action_ids(self):
        return list(self.actions_by_id.keys())

    def icon_fallback_count(self):
        return len(_FALLBACK_LAST)
