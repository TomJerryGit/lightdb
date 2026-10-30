"""主题管理 - 深色 / 浅色 / 高对比度（N1：design tokens + Navicat 风格 QSS）"""
import os

from PySide6.QtGui import QColor

# N1 design tokens：间距/圆角/字号/图标尺寸（QSS 与控件共用，避免魔法数）
DESIGN_TOKENS = {
    'spacing': {'xs': 2, 'sm': 4, 'md': 8, 'lg': 12, 'xl': 16},
    'radius': {'sm': 3, 'md': 5, 'lg': 7},
    'font_size': {'xs': 11, 'sm': 12, 'base': 13, 'lg': 15},
    'icon_size': {'toolbar': 18, 'ribbon': 30, 'menu': 16},
    # task-88：分割线/高 DPI 也走 token，避免各面板魔法数
    'divider': {'width': 1, 'handle_width': 5},
    'dpi': {'base': 96, 'scales': [1.0, 1.25, 1.5, 1.75, 2.0]},
    # task-52：内置 SVG 图标 token（本地优先，按主题色着色）
    'icon': {'asset_dir': 'assets/icons', 'size': 24, 'stroke': 1.8,
             'theme_adaptive': True},
    'ribbon_button': {'min_width': 60, 'min_height': 58},
}


def token(group, key, default=None):
    """安全读取 design token（控件/测试共用）。"""
    return (DESIGN_TOKENS.get(group) or {}).get(key, default)


def high_dpi_info(app=None):
    """返回当前高 DPI 缩放信息（125%/150% 验证用，不创建应用）。"""
    ratio = 1.0
    dpi = float(DESIGN_TOKENS['dpi']['base'])
    try:
        if app is None:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
        if app is not None:
            screen = app.primaryScreen()
            if screen is not None:
                ratio = float(screen.devicePixelRatio() or 1.0)
                dpi = float(screen.logicalDotsPerInch() or dpi)
    except Exception:
        pass
    return {
        'ratio': ratio,
        'percent': int(round(ratio * 100)),
        'dpi': dpi,
        'base_dpi': DESIGN_TOKENS['dpi']['base'],
        'scale_percent': int(round(dpi / DESIGN_TOKENS['dpi']['base'] * 100)),
        'spacing_md': max(1, int(round(DESIGN_TOKENS['spacing']['md'] * ratio))),
        'icon_toolbar': max(1, int(round(
            DESIGN_TOKENS['icon_size']['toolbar'] * ratio))),
        'rounding_policy': 'PassThrough',
    }


def scale_token(value, ratio=None):
    """按 DPI 比例缩放 token（125%/150% 下图标/间距不糊不截断）。"""
    if ratio is None:
        ratio = high_dpi_info()['ratio']
    return max(1, int(round(float(value) * float(ratio))))


class Theme:
    def __init__(self, name, colors, extra_qss=""):
        self.name = name
        self.colors = colors
        self.extra_qss = extra_qss

    def qss(self):
        c = self.colors
        base = f"""
QMainWindow, QDialog {{
    background-color: {c['bg']};
    color: {c['fg']};
}}
QWidget {{
    font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
    font-size: 13px;
    color: {c['fg']};
}}
QDockWidget::title {{
    background: {c['panel']};
    padding: 6px;
    border-bottom: 1px solid {c['border']};
    font-weight: bold;
}}
QTreeWidget, QTreeView, QTableView, QTableWidget {{
    background-color: {c['bg']};
    alternate-background-color: {c['alt_bg']};
    color: {c['fg']};
    border: 1px solid {c['border']};
    gridline-color: {c['border']};
    selection-background-color: {c['selection']};
    selection-color: {c['selection_fg']};
}}
QTreeWidget::item, QTreeView::item {{
    padding: 3px 4px;
}}
QTreeWidget::item:hover, QTreeView::item:hover {{
    background-color: {c['hover']};
}}
QHeaderView::section {{
    background-color: {c['panel']};
    color: {c['fg_dim']};
    padding: 5px 8px;
    border: none;
    border-right: 1px solid {c['border']};
    border-bottom: 1px solid {c['border']};
    font-weight: bold;
}}
QTabWidget::pane {{
    border: 1px solid {c['border']};
    background: {c['bg']};
}}
QTabBar::tab {{
    background: {c['panel']};
    color: {c['fg_dim']};
    padding: 6px 14px;
    border: 1px solid {c['border']};
    border-bottom: none;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {c['bg']};
    color: {c['fg']};
    border-bottom: 2px solid {c['accent']};
}}
QTabBar::tab:hover:!selected {{
    background: {c['hover']};
}}
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background-color: {c['input_bg']};
    color: {c['fg']};
    border: 1px solid {c['input_border']};
    border-radius: 3px;
    padding: 4px 6px;
    selection-background-color: {c['selection']};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {c['accent']};
}}
QPushButton {{
    background-color: {c['button']};
    color: {c['button_fg']};
    border: none;
    border-radius: 3px;
    padding: 6px 14px;
    min-width: 70px;
}}
QPushButton:hover {{
    background-color: {c['button_hover']};
}}
QPushButton:pressed {{
    background-color: {c['button_pressed']};
}}
QPushButton:disabled {{
    background-color: {c['panel']};
    color: {c['fg_dim']};
}}
QToolBar {{
    background: {c['panel']};
    border-bottom: 1px solid {c['border']};
    spacing: 3px;
    padding: 3px;
}}
QToolBar QToolButton {{
    background: transparent;
    color: {c['fg']};
    padding: 5px 10px;
    border-radius: 3px;
}}
QToolBar QToolButton:hover {{
    background: {c['hover']};
}}
QToolBar QToolButton:pressed {{
    background: {c['selection']};
}}
QStatusBar {{
    background: {c['accent']};
    color: #ffffff;
}}
QMenuBar {{
    background: {c['panel']};
    color: {c['fg']};
}}
QMenuBar::item:selected {{
    background: {c['hover']};
}}
QMenu {{
    background: {c['panel']};
    color: {c['fg']};
    border: 1px solid {c['border']};
}}
QMenu::item:selected {{
    background: {c['selection']};
}}
QSplitter::handle {{
    background: {c['border']};
}}
QSplitter::handle:hover {{
    background: {c['accent']};
}}
QScrollBar:vertical {{
    background: {c['bg']};
    width: 12px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {c['scrollbar']};
    border-radius: 6px;
    min-height: 20px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{
    background: {c['scrollbar_hover']};
}}
QScrollBar:horizontal {{
    background: {c['bg']};
    height: 12px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: {c['scrollbar']};
    border-radius: 6px;
    min-width: 20px;
    margin: 2px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0px;
    width: 0px;
}}
QGroupBox {{
    border: 1px solid {c['border']};
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 10px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {c['accent']};
}}
QCheckBox {{
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {c['input_border']};
    border-radius: 3px;
    background: {c['input_bg']};
}}
QCheckBox::indicator:checked {{
    background: {c['button']};
    border: 1px solid {c['button']};
}}
QLabel {{
    color: {c['fg']};
    background: transparent;
}}
QProgressBar {{
    background: {c['input_bg']};
    border: 1px solid {c['border']};
    border-radius: 3px;
    text-align: center;
    color: {c['fg']};
}}
QProgressBar::chunk {{
    background: {c['accent']};
}}
QListWidget {{
    background: {c['bg']};
    color: {c['fg']};
    border: 1px solid {c['border']};
}}
QListWidget::item {{
    padding: 4px 6px;
}}
QListWidget::item:selected {{
    background: {c['selection']};
    color: {c['selection_fg']};
}}
QListWidget::item:hover {{
    background: {c['hover']};
}}
"""
        return base + self.extra_qss + self._navicat_qss()

    def _navicat_qss(self):
        """N1：Ribbon / 焦点环 / 选中态 / 分栏等 Navicat 风格补充样式。"""
        c = self.colors
        t = DESIGN_TOKENS
        r = t['radius']
        return f"""
/* ==================== N1 theme tokens ==================== */
QToolBar#RibbonBar {{
    background: {c['ribbon_bg']};
    border-bottom: 1px solid {c['border']};
    spacing: {t['spacing']['xs']}px;
    padding: {t['spacing']['xs']}px {t['spacing']['sm']}px;
}}
QToolBar#RibbonBar::separator {{
    width: 1px;
    background: {c['border']};
    margin: 4px 3px;
}}
QToolButton[ribbon="true"] {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {r['md']}px;
    padding: {t['spacing']['xs']}px {t['spacing']['sm']}px;
    min-width: {t['ribbon_button']['min_width']}px;
    min-height: {t['ribbon_button']['min_height']}px;
    color: {c['ribbon_text']};
    font-size: {t['font_size']['xs']}px;
}}
QToolButton[ribbon="true"]:hover {{
    background: {c['ribbon_hover']};
    border-color: {c['border_soft']};
}}
QToolButton[ribbon="true"]:pressed {{
    background: {c['selection']};
    color: {c['selection_fg']};
}}
QToolButton[ribbon="true"]:disabled {{
    color: {c['fg_dim']};
    background: transparent;
}}
QLabel[ribbonGroup="true"] {{
    color: {c['fg_dim']};
    font-size: {t['font_size']['xs']}px;
    padding: 1px {t['spacing']['sm']}px;
}}
QFrame#RibbonGroup {{
    background: transparent;
    border-right: 1px solid {c['border_soft']};
    padding: 2px {t['spacing']['sm']}px;
}}
QPushButton:focus, QToolButton:focus, QComboBox:focus, QSpinBox:focus {{
    border: 2px solid {c['focus_ring']};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border: 2px solid {c['focus_ring']};
}}
QTableView::item:selected, QTableWidget::item:selected,
QTreeView::item:selected, QTreeWidget::item:selected {{
    background: {c['selection']};
    color: {c['selection_fg']};
}}
QTableView::item:focus, QTableWidget::item:focus {{
    border: 1px solid {c['focus_ring']};
}}
QTabBar::tab {{
    border-top-left-radius: {r['sm']}px;
    border-top-right-radius: {r['sm']}px;
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
    border-radius: {r['sm']}px;
}}
QPushButton {{
    border-radius: {r['sm']}px;
}}
QSplitter::handle:horizontal {{
    width: 4px;
}}
QSplitter::handle:vertical {{
    height: 4px;
}}
"""


DARK_THEME = Theme('dark', {
    'bg': '#1e1e1e', 'fg': '#d4d4d4', 'fg_dim': '#969696',
    'panel': '#252526', 'border': '#3c3c3c',
    'hover': '#2a2d2e', 'selection': '#094771', 'selection_fg': '#ffffff',
    'input_bg': '#3c3c3c', 'input_border': '#555555',
    'button': '#0e639c', 'button_fg': '#ffffff',
    'button_hover': '#1177bb', 'button_pressed': '#0a4d78',
    'accent': '#007acc',
    'alt_bg': '#252526',
    'scrollbar': '#424242', 'scrollbar_hover': '#4e4e4e',
    'ribbon_bg': '#2b2b2b', 'ribbon_hover': '#37373d',
    'ribbon_text': '#d4d4d4', 'border_soft': '#4a4a4a',
    'focus_ring': '#4daafc', 'icon': '#d4d4d4',
})

LIGHT_THEME = Theme('light', {
    'bg': '#ffffff', 'fg': '#2c2c2c', 'fg_dim': '#666666',
    'panel': '#f3f3f3', 'border': '#dcdcdc',
    'hover': '#e8e8e8', 'selection': '#cce4f7', 'selection_fg': '#000000',
    'input_bg': '#ffffff', 'input_border': '#c0c0c0',
    'button': '#0078d4', 'button_fg': '#ffffff',
    'button_hover': '#106ebe', 'button_pressed': '#005a9e',
    'accent': '#0078d4',
    'alt_bg': '#f8f8f8',
    'scrollbar': '#c0c0c0', 'scrollbar_hover': '#a0a0a0',
    'ribbon_bg': '#eef1f5', 'ribbon_hover': '#e0e6ee',
    'ribbon_text': '#2c2c2c', 'border_soft': '#cdd3da',
    'focus_ring': '#0078d4', 'icon': '#2c2c2c',
})

HC_THEME = Theme('highcontrast', {
    'bg': '#000000', 'fg': '#ffffff', 'fg_dim': '#cccccc',
    'panel': '#1a1a1a', 'border': '#ffff00',
    'hover': '#333333', 'selection': '#ffff00', 'selection_fg': '#000000',
    'input_bg': '#1a1a1a', 'input_border': '#ffff00',
    'button': '#ffff00', 'button_fg': '#000000',
    'button_hover': '#ffff66', 'button_pressed': '#cccc00',
    'accent': '#ffff00',
    'alt_bg': '#1a1a1a',
    'scrollbar': '#ffff00', 'scrollbar_hover': '#ffff66',
    'ribbon_bg': '#1a1a1a', 'ribbon_hover': '#333333',
    'ribbon_text': '#ffffff', 'border_soft': '#ffff00',
    'focus_ring': '#ffff00', 'icon': '#ffffff',
})


THEMES = {
    'dark': DARK_THEME,
    'light': LIGHT_THEME,
    'highcontrast': HC_THEME,
}

# 仅供性能对照测量：置 1 时关掉本轮"幂等短路"，回到每个启动点都重设样式表
# 的老行为（便于在同一个代码状态下做 before/after 计时）。
_LEGACY = os.environ.get('LIGHTDB_STUDIO_LEGACY_STARTUP', '') \
    .strip().lower() not in ('', '0', 'false', 'no', 'off') \
    or os.environ.get('LIGHTDB_STUDIO_LEGACY_THEME', '') \
    .strip().lower() not in ('', '0', 'false', 'no', 'off')


def available_themes():
    return list(THEMES.keys())


def theme_tokens(theme_name='dark'):
    """N1/task-88：返回该主题的 design tokens + 颜色（供控件/测试使用）。"""
    theme = THEMES.get(theme_name, DARK_THEME)
    tokens = {k: (dict(v) if isinstance(v, dict) else v)
              for k, v in DESIGN_TOKENS.items()}
    tokens['colors'] = dict(theme.colors)
    tokens['theme'] = theme.name
    tokens['dpi'] = dict(DESIGN_TOKENS['dpi'])
    return tokens


def theme_qss(theme_name='dark'):
    return THEMES.get(theme_name, DARK_THEME).qss()


def icon_color(theme_name=None):
    """task-52：图标着色 token；未指定时跟随 QApplication 当前主题。"""
    if not theme_name:
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            theme_name = getattr(app, '_lightdb_theme_name', 'dark') \
                if app is not None else 'dark'
        except Exception:
            theme_name = 'dark'
    theme = THEMES.get(theme_name, DARK_THEME)
    return theme.colors.get('icon') or theme.colors.get('fg')


def apply_theme(app, theme_name: str):
    theme = THEMES.get(theme_name, DARK_THEME)

    # 幂等短路：`app.setStyleSheet()` 会让 Qt 重新 polish **应用下所有**
    # 控件的样式表，实测每次 60~420ms（控件越多越贵）。启动路径上
    # `app.py` 已经按 `config.settings.theme` 应用过一次，随后
    # `MainWindow._apply_settings()` 又对同一个 app 应用**完全相同**的
    # 主题，第二次是纯浪费（end state 一模一样）。
    #
    # 只有「请求的主题和当前已应用的主题一模一样」时才跳过；换主题、
    # 极端情况下样式表被别处改掉（比对 `styleSheet()` 文本）都会照常应用，
    # 所以显示结果与以前完全一致。
    if not _LEGACY and getattr(app, '_lightdb_theme_name', None) == theme.name \
            and app.styleSheet() == theme.qss():
        return theme
    app.setStyleSheet(theme.qss())
    # 保留高亮风格
    from PySide6.QtGui import QPalette
    pal = app.palette()
    pal.setColor(QPalette.Base, QColor(theme.colors['bg']))
    pal.setColor(QPalette.Text, QColor(theme.colors['fg']))
    pal.setColor(QPalette.Highlight, QColor(theme.colors['selection']))
    pal.setColor(QPalette.HighlightedText, QColor(theme.colors['selection_fg']))
    app.setPalette(pal)
    app._lightdb_theme_name = theme.name
    return theme