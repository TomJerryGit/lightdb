"""LightDB Studio 应用引导

**启动计时开关**：设置环境变量 `LIGHTDB_STUDIO_STARTUP_TIMING=1` 后，会在
stderr 打印每个启动阶段的耗时，用于定位"GUI 启动很慢"到底慢在哪一段：

    $env:LIGHTDB_STUDIO_STARTUP_TIMING = '1'      # PowerShell
    python -m lightdb_studio.app

不设置该变量时**零输出、零额外开销**（只多一次环境变量读取与一个
`perf_counter()`），行为与以往完全一致。

计时基准是**本模块开始导入的时刻**（不含 Python 解释器自身启动）；
`入口开始` 那一行的耗时因此接近 0，看的是随后各阶段的增量。
"""
import os
import sys
import time

_T0 = time.perf_counter()
_TIMING = os.environ.get('LIGHTDB_STUDIO_STARTUP_TIMING', '').strip().lower() \
    not in ('', '0', 'false', 'no', 'off')


def _mark(label: str) -> None:
    """打印一个启动阶段标记（仅在开关打开时）"""
    if _TIMING:
        print('[startup] %7.3fs  %s'
              % (time.perf_counter() - _T0, label),
              file=sys.stderr, flush=True)


_mark('入口开始（不含解释器启动）')

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtGui import QFont                     # noqa: E402
from PySide6.QtCore import Qt, QTimer               # noqa: E402

_mark('PySide6 导入完成（Qt 库加载）')

# ---------- 字体目录（消除 QFontDatabase 告警 + 让离屏也有字体） ----------
# Qt 6 起不再自带字体，`PySide6/lib/fonts` 是不存在的目录，于是启动时报
#   QFontDatabase: Cannot find font directory .../PySide6/lib/fonts
# 只设一个**存在**的目录（Windows 优先用 %WINDIR%\Fonts，其它平台试几个
# 常见目录），不存在的目录一律不设 —— 设了反而更容易触发同一告警。
# 必须在 QApplication 构造之前设置（Qt 在建 QApplication 时读这个变量）。
def ensure_font_dir() -> str:
    """幂等地把 QT_QPA_FONTDIR 指到一个**存在**的字体目录（返回该目录）。

    必须在 `QApplication` 构造之前调用（Qt 在建 QApplication 时读该变量）。
    已经显式设置过就不覆盖。"""
    if os.environ.get('QT_QPA_FONTDIR'):
        return os.environ['QT_QPA_FONTDIR']          # 用户显式指定，不覆盖
    candidates = []
    windir = os.environ.get('WINDIR') or os.environ.get('SystemRoot')
    if windir:
        candidates.append(os.path.join(windir, 'Fonts'))
    candidates += ['/usr/share/fonts', '/usr/local/share/fonts']
    for path in candidates:
        if os.path.isdir(path):
            os.environ['QT_QPA_FONTDIR'] = path
            return path
    return ''


_FONT_DIR = ensure_font_dir()
_mark('字体目录设置完成（QT_QPA_FONTDIR=%s）' % (_FONT_DIR or '未找到，跳过'))

from .ui.main_window import MainWindow              # noqa: E402
from .config import AppConfig                       # noqa: E402

_mark('业务模块导入完成（config + main_window）')


def run() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    _mark('HighDpi 策略设置完成')

    app = QApplication(sys.argv)
    app.setApplicationName("LightDB Studio")
    app.setOrganizationName("LightDB")
    _mark('QApplication 创建完成')

    font = QFont("Microsoft YaHei", 9)
    app.setFont(font)

    # 加载配置 + 应用主题
    config = AppConfig()
    _mark('AppConfig 加载完成（含 ~/.lightdb_studio 读写）')

    from .ui.theme import apply_theme
    theme_name = getattr(config.settings, 'theme', 'dark')
    apply_theme(app, theme_name)
    _mark('主题应用完成（%s）' % theme_name)

    window = MainWindow(config)
    _mark('MainWindow 构造完成（30+ 管理器 / 插件扫描）')

    window.show()
    _mark('window.show() 返回')

    if _TIMING:
        # 事件循环真正跑起来后的第一个事件：此时窗口应已显示，
        # 这一行是"用户能看见窗口"的近似时刻。
        QTimer.singleShot(
            0, lambda: _mark('事件循环首个事件（窗口可见=%s）'
                             % window.isVisible()))
        QTimer.singleShot(
            1000, lambda: _mark('启动后 1s（首屏绘制已完成）'))

    return app.exec()


if __name__ == '__main__':
    sys.exit(run())
