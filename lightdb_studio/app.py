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
# 实现放在**中立模块** `core/fonts.py`：它原先定义在本入口模块里，而
# `ui/main_window.py` 为兜底写了 `from ..app import ensure_font_dir` —— 那会让
# 本模块被**第二次执行**（`__main__` 之外再建一个 `lightdb_studio.app` 模块
# 对象，模块级代码与启动计时标记全部重跑一遍），并构成
# `app -> ui.main_window -> app` 的循环导入。移到 core 后入口模块不再被反向
# 导入（见 core/fonts.py 的模块 docstring）。
from .core.fonts import ensure_font_dir             # noqa: E402,F401


_FONT_DIR = ensure_font_dir()
_mark('字体目录设置完成（QT_QPA_FONTDIR=%s）' % (_FONT_DIR or '未找到，跳过'))

from .ui.main_window import MainWindow              # noqa: E402
from .config import AppConfig                       # noqa: E402

_mark('业务模块导入完成（config + main_window）')


def run(argv=None) -> int:
    """启动 Studio。

    - 默认：正常 GUI 启动（会展示首启引导，除非已完成/被环境变量抑制）；
    - ``--smoke``：构造窗口后 800ms 自动退出、退出码 0（PyInstaller 打包冒烟）；
    """
    argv = list(sys.argv if argv is None else argv)
    smoke = '--smoke' in argv
    argv = [a for a in argv if a != '--smoke']

    # 尽早安装崩溃日志 / 异常钩子（QApplication 创建之前；失败不影响启动）
    try:
        from .utils.crashlog import (
            setup_logging, install_excepthooks, install_qt_message_handler)
        _LOG_FILE = setup_logging()
        install_excepthooks()
        _mark('崩溃日志/异常钩子安装完成（%s）' % (_LOG_FILE or '日志不可用'))
    except Exception as exc:                          # noqa: BLE001
        print(f'[app] 崩溃日志初始化失败（继续启动）: {exc}', file=sys.stderr)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    _mark('HighDpi 策略设置完成')

    app = QApplication(argv)
    app.setApplicationName("LightDB Studio")
    app.setOrganizationName("LightDB")
    _mark('QApplication 创建完成')

    try:
        install_qt_message_handler()
        _mark('Qt 消息处理器安装完成')
    except Exception as exc:                          # noqa: BLE001
        print(f'[app] Qt 消息处理器安装失败（继续启动）: {exc}', file=sys.stderr)

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

    if smoke:
        # 打包产物验证：不弹首启引导、不主动拨号，窗口构造成功即 800ms 后退出
        try:
            config.settings.auto_connect = False
        except Exception:
            pass
        QTimer.singleShot(800, app.quit)
        _mark('smoke 模式：800ms 后自动退出')
    else:
        # 首启引导只在这里展示（不在 MainWindow 构造里，避免破坏既有测试）
        try:
            from .ui.first_run_wizard import maybe_show_first_run
            maybe_show_first_run(window, config)
        except Exception as exc:                      # noqa: BLE001
            print(f'[app] 首启引导失败（跳过，不影响启动）: {exc}',
                  file=sys.stderr)

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
