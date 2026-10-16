"""把「连接失败」弹成**可复制、可行动**的对话框。

分层：结论与措辞全部在 `core/db_errors.py`（纯函数、好测），这里只做
Qt 侧的薄封装，保证两件事：

  1. 文本**可选中复制**（`TextSelectableByMouse|ByKeyboard`）——
     用户可以自己把启动命令或原始报文拷走；
  2. `_auto_restore()` 的失败用 `open()`（非阻塞）弹，绝不 `exec()`：
     启动期弹模态框会把首屏/事件循环按死，测试里更会直接挂住。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from ..core.db_errors import explain_connection_error


def build_connection_error_box(parent, exc, host=None, port=None,
                               title: str = '连接失败') -> QMessageBox:
    """构造（不显示）一个含完整可行动文本的错误框。文本可复制。"""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Critical)
    box.setWindowTitle(title)
    box.setText(explain_connection_error(exc, host=host, port=port))
    box.setTextInteractionFlags(Qt.TextSelectableByMouse
                                | Qt.TextSelectableByKeyboard)
    box.setStandardButtons(QMessageBox.Ok)
    return box


def show_connection_error(parent, exc, host=None, port=None,
                          title: str = '连接失败') -> str:
    """阻塞式弹出（用户主动点「新建连接 / 测试连接」时用）。返回展示文本。"""
    box = build_connection_error_box(parent, exc, host=host, port=port,
                                     title=title)
    box.exec()
    return box.text()


def show_connection_error_nonblocking(parent, exc, host=None, port=None,
                                      title: str = '连接失败') -> QMessageBox:
    """非阻塞弹出（启动期自动连接失败用）：`open()` 不挡事件循环。"""
    box = build_connection_error_box(parent, exc, host=host, port=port,
                                     title=title)
    box.setAttribute(Qt.WA_DeleteOnClose, True)
    box.open()
    return box
