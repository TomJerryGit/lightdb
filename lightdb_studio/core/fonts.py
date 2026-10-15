"""字体目录工具（中立模块）

**为什么单独放一个模块**：`ensure_font_dir()` 原本定义在 `lightdb_studio/app.py`
（应用入口）里，而 `ui/main_window.py` 为了"测试/嵌入方直接建窗口"的兜底需要
调用它，于是写了 `from ..app import ensure_font_dir`。那会带来两个问题：

1. **入口模块被第二次执行**：`python -m lightdb_studio.app` 时本模块的名字是
   `__main__`；`main_window` 再 `import lightdb_studio.app` 会**另建一个**模块
   对象并把模块级代码（含启动计时标记）重跑一遍 —— 用
   `LIGHTDB_STUDIO_STARTUP_TIMING=1` 能直接看到"入口开始"打印两次。
2. **循环导入**：`app -> ui.main_window -> app`。

放到这个中立模块后，`app.py` 与 `ui/main_window.py` 都从这里导入，入口模块不再
被反向导入。本模块**没有模块级副作用**（只定义函数）。
"""
import os


def ensure_font_dir() -> str:
    """幂等地把 QT_QPA_FONTDIR 指到一个**存在**的字体目录（返回该目录）。

    背景：Qt 6 起不再自带字体，`PySide6/lib/fonts` 是不存在的目录，于是启动时报
      QFontDatabase: Cannot find font directory .../PySide6/lib/fonts
    只设一个**存在**的目录（Windows 优先用 %WINDIR%\\Fonts，其它平台试几个常见
    目录），不存在的目录一律不设 —— 设了反而更容易触发同一告警。

    必须在 `QApplication` 构造之前调用（Qt 在建 QApplication 时读该变量）。
    已经显式设置过就不覆盖。
    """
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
