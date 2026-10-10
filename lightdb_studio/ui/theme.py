"""主题管理 - 深色 / 浅色 / 高对比度"""
from PySide6.QtGui import QColor


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
        return base + self.extra_qss


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
})


THEMES = {
    'dark': DARK_THEME,
    'light': LIGHT_THEME,
    'highcontrast': HC_THEME,
}


def apply_theme(app, theme_name: str):
    theme = THEMES.get(theme_name, DARK_THEME)
    app.setStyleSheet(theme.qss())
    # 保留高亮风格
    from PySide6.QtGui import QPalette
    pal = app.palette()
    pal.setColor(QPalette.Base, QColor(theme.colors['bg']))
    pal.setColor(QPalette.Text, QColor(theme.colors['fg']))
    pal.setColor(QPalette.Highlight, QColor(theme.colors['selection']))
    pal.setColor(QPalette.HighlightedText, QColor(theme.colors['selection_fg']))
    app.setPalette(pal)
    return theme