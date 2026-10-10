"""大文本 / 二进制单元格查看器"""
import base64
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QLabel,
    QPushButton, QDialogButtonBox, QApplication, QTabWidget,
    QWidget, QPlainTextEdit
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPixmap


class CellViewerDialog(QDialog):
    def __init__(self, value, col_name='', col_type='', parent=None):
        super().__init__(parent)
        self.value = value
        self.setWindowTitle(f"查看: {col_name}")
        self.resize(720, 520)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            f"<b>列:</b> {self.value}"
            if False else
            f"<b>类型:</b> {type(self.value).__name__}"
        )
        if isinstance(self.value, (str, bytes)):
            size = len(self.value)
            if isinstance(self.value, str):
                info.setText(
                    f"<b>字符串</b> | 长度: {size} 字符")
            else:
                info.setText(
                    f"<b>二进制</b> | 大小: {size} 字节")
        layout.addWidget(info)

        # Tab：文本 / 图片 / 十六进制
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self._build_text_tab()
        if isinstance(self.value, bytes):
            self._build_image_tab()
            self._build_hex_tab()

        # 按钮
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        copy_btn = QPushButton("复制")
        copy_btn.clicked.connect(self._copy)
        buttons.addButton(copy_btn, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_text_tab(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setFont(QFont("Consolas", 10))
        self.text_edit.setReadOnly(True)

        if isinstance(self.value, bytes):
            try:
                text = self.value.decode('utf-8')
            except UnicodeDecodeError:
                text = self.value.decode('latin-1')
        else:
            text = str(self.value) if self.value is not None else 'NULL'

        self.text_edit.setPlainText(text)
        l.addWidget(self.text_edit)
        self.tabs.addTab(w, "文本")

    def _build_image_tab(self):
        """尝试将二进制解释为图片"""
        w = QWidget()
        l = QVBoxLayout(w)
        label = QLabel()
        label.setAlignment(Qt.AlignCenter)

        pix = QPixmap()
        if pix.loadFromData(self.value):
            if pix.width() > 600 or pix.height() > 400:
                pix = pix.scaled(600, 400, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
            label.setPixmap(pix)
            l.addWidget(label)
            self.tabs.addTab(w, "图片")
        # 若不是图片，不添加

    def _build_hex_tab(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)

        view = QPlainTextEdit()
        view.setFont(QFont("Consolas", 9))
        view.setReadOnly(True)

        # 十六进制转储
        data = self.value[:8192]  # 最多显示 8KB
        lines = []
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            hex_part = ' '.join(f'{b:02x}' for b in chunk)
            hex_part = hex_part.ljust(16 * 3 - 1)
            ascii_part = ''.join(
                chr(b) if 32 <= b < 127 else '.' for b in chunk)
            lines.append(f'{i:08x}  {hex_part}  |{ascii_part}|')
        if len(self.value) > 8192:
            lines.append(f"... 剩余 {len(self.value) - 8192} 字节未显示")
        view.setPlainText('\n'.join(lines))
        l.addWidget(view)
        self.tabs.addTab(w, "十六进制")

    def _copy(self):
        if isinstance(self.value, bytes):
            try:
                text = self.value.decode('utf-8')
            except UnicodeDecodeError:
                text = base64.b64encode(self.value).decode('ascii')
        else:
            text = str(self.value) if self.value is not None else ''
        QApplication.clipboard().setText(text)