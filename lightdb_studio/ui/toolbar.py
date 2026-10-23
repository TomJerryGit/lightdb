# -*- coding: utf-8 -*-
"""N1：Ribbon 风格主工具栏（大图标 + 分组，保留现有动作）。

task-52：图标加载优先级 **本地单色 SVG → QtAwesome（若安装）→ Qt 标准图标**，
每次加载都显式记录来源（local / qtawesome / qt-standard），绝不假成功；
本地 SVG 用主题 `icon_color()` token 着色，换主题时调用 `refresh_icons()` 重新渲染。

保留 N1 既有接口：`icon_backend()` 仍表示 QtAwesome 是否可用，`make_icon()`
不传 `local_name` 时行为不变（qtawesome → qt-standard，并记录 fallback）。
"""
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import (
    QToolBar, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QToolButton,
    QFrame, QApplication, QStyle,
)

from .theme import DESIGN_TOKENS, icon_color

ASSET_ICON_DIR = Path(__file__).resolve().parent.parent / 'assets' / 'icons'

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
_FALLBACK_LAST = []       # N1 兼容：fallback 原因记录
_ICON_SOURCES = []        # task-52：每次加载的来源记录
_LOCAL_MISSES = []        # 本地 SVG 缺失记录（显式 fallback 证据）


def icons_available():
    try:
        import qtawesome  # noqa: F401
        return True
    except Exception:
        return False


def icon_backend():
    """N1 兼容：qtawesome 是否可用（fallback 表示未安装）。"""
    global _ICON_BACKEND
    if _ICON_BACKEND is None:
        _ICON_BACKEND = 'qtawesome' if icons_available() else 'fallback'
    return _ICON_BACKEND


def fallback_icon(kind='new'):
    style = QApplication.style()
    sp = _FALLBACK_STANDARD.get(kind, QStyle.SP_FileIcon)
    return style.standardIcon(sp)


def _record_source(name, source, detail=''):
    event = {'name': name, 'source': source, 'detail': detail}
    _ICON_SOURCES.append(event)
    return event


def icon_sources():
    """返回所有图标加载来源记录（local / qtawesome / qt-standard）。"""
    return [dict(e) for e in _ICON_SOURCES]


def icon_source(name=None):
    """指定名称最后一次加载来源；name 为空时返回最后一条。"""
    if name is None:
        return _ICON_SOURCES[-1]['source'] if _ICON_SOURCES else None
    for event in reversed(_ICON_SOURCES):
        if event['name'] == name:
            return event['source']
    return None


def icon_source_counts():
    counts = {}
    for event in _ICON_SOURCES:
        counts[event['source']] = counts.get(event['source'], 0) + 1
    return counts


def local_icon_path(name):
    return ASSET_ICON_DIR / f'{name}.svg'


def _render_svg(path, color=None, sizes=(16, 24, 30, 32)):
    """渲染单色 SVG，并按主题色着色；返回 QIcon 或 None。"""
    try:
        from PySide6.QtSvg import QSvgRenderer
    except Exception as exc:                          # noqa: BLE001
        _LOCAL_MISSES.append((str(path), f'QtSvg 不可用: {exc}'))
        return None
    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        _LOCAL_MISSES.append((str(path), 'SVG 无法解析'))
        return None
    icon = QIcon()
    for size in sizes:
        pix = QPixmap(int(size), int(size))
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        try:
            renderer.render(painter)
        finally:
            painter.end()
        if color:
            painter = QPainter(pix)
            try:
                painter.setCompositionMode(
                    QPainter.CompositionMode_SourceIn)
                painter.fillRect(pix.rect(), QColor(color))
            finally:
                painter.end()
        icon.addPixmap(pix)
    return icon


def local_icon(name, color=None, sizes=(16, 24, 30, 32)):
    """加载本地 SVG；不存在/无效时返回 None（由调用方显式 fallback）。"""
    if not name:
        return None
    path = local_icon_path(name)
    if not path.is_file() or path.stat().st_size <= 0:
        _LOCAL_MISSES.append((str(path), '本地 SVG 缺失'))
        return None
    return _render_svg(path, color=color, sizes=sizes)


def make_icon(qta_name='', fallback_kind='new', color=None, local_name=None):
    """本地 SVG → QtAwesome → Qt 标准；每次显式记录来源。

    - `local_name` 为空：保持 N1 行为（QtAwesome → Qt 标准）；
    - `local_name` 指定但文件缺失：记录 `_LOCAL_MISSES` 后继续降级，
      最终来源为 qtawesome/qt-standard（缺失不假成功）。
    """
    if local_name:
        icon = local_icon(local_name, color=color)
        if icon is not None:
            _record_source(local_name, 'local', str(local_icon_path(local_name)))
            return icon
        _FALLBACK_LAST.append(
            (local_name, f'本地 SVG 缺失: {local_icon_path(local_name)}'))
    if icons_available() and qta_name:
        try:
            import qtawesome as qta
            icon = qta.icon(qta_name, color=color) if color \
                else qta.icon(qta_name)
            _record_source(local_name or qta_name, 'qtawesome')
            return icon
        except Exception as exc:                      # noqa: BLE001
            _FALLBACK_LAST.append((qta_name, str(exc)))
            _record_source(local_name or qta_name, 'qtawesome-failed',
                           str(exc))
    else:
        _FALLBACK_LAST.append((qta_name, 'qtawesome 未安装'))
    _record_source(local_name or qta_name or fallback_kind, 'qt-standard',
                   fallback_kind)
    return fallback_icon(fallback_kind)


def fallback_events():
    """N1 兼容：fallback 原因记录（空 list 表示全部用 QtAwesome）。"""
    return list(_FALLBACK_LAST)


def local_misses():
    """本地 SVG 缺失/无效记录（task-52 显式 fallback 证据）。"""
    return list(_LOCAL_MISSES)


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
    local_icon: str = ''       # task-52：本地 SVG 名（不含 .svg）


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
        self.icon_sources_by_id = {}
        self._connection_dependent = []
        self._compat_actions = []
        self._specs = list(specs or [])
        self._build()
        self.update_availability(False)

    def _icon_for(self, spec, color=None):
        return make_icon(spec.icon, spec.fallback_kind,
                         color=color, local_name=spec.local_icon or None)

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
        color = icon_color()
        for spec in self._specs:
            act = QAction(spec.label, self)
            act.setToolTip(spec.tooltip or spec.clean_label)
            act.setIcon(self._icon_for(spec, color))
            act.triggered.connect(spec.callback)
            act.setVisible(False)
            self.addAction(act)
            self.actions_by_id[spec.action_id] = act
            self.icon_sources_by_id[spec.action_id] = icon_source(
                spec.local_icon) if spec.local_icon else icon_source()
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
        color = icon_color()
        for spec in specs:
            btn = QToolButton()
            btn.setProperty('ribbon', True)
            btn.setText(spec.clean_label)
            btn.setToolTip(spec.tooltip or spec.clean_label)
            btn.setIcon(self._icon_for(spec, color))
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

    def refresh_icons(self, color=None):
        """task-52：主题切换后按新 icon token 重新渲染本地 SVG 图标。"""
        color = color or icon_color()
        for spec in self._specs:
            icon = self._icon_for(spec, color)
            btn = self.buttons_by_id.get(spec.action_id)
            if btn is not None:
                btn.setIcon(icon)
            act = self.actions_by_id.get(spec.action_id)
            if act is not None:
                act.setIcon(icon)
            self.icon_sources_by_id[spec.action_id] = (
                icon_source(spec.local_icon) if spec.local_icon
                else icon_source())
        return self.icon_sources_by_id

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

    def icon_source(self, action_id=None):
        if action_id is None:
            return dict(self.icon_sources_by_id)
        return self.icon_sources_by_id.get(action_id)

    def local_icon_count(self):
        return sum(1 for source in self.icon_sources_by_id.values()
                   if source == 'local')

    def icon_fallback_count(self):
        return len(_FALLBACK_LAST)
