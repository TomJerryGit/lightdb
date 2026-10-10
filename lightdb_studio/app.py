"""LightDB Studio 应用引导"""
import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt

from .ui.main_window import MainWindow
from .config import AppConfig


def run() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("LightDB Studio")
    app.setOrganizationName("LightDB")

    font = QFont("Microsoft YaHei", 9)
    app.setFont(font)

    # 加载配置 + 应用主题
    config = AppConfig()

    from .ui.theme import apply_theme
    theme_name = getattr(config.settings, 'theme', 'dark')
    apply_theme(app, theme_name)

    window = MainWindow(config)
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(run())