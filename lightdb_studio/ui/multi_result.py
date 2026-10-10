"""
多结果集容器
- 存储过程可能返回多个结果集
- Tab 切换展示
"""
from PySide6.QtWidgets import QTabWidget, QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import Qt

from .result_table import ResultTable


class MultiResultView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self._add_placeholder()

    def _add_placeholder(self):
        w = QWidget()
        l = QVBoxLayout(w)
        lbl = QLabel("无结果")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("color: #666666; font-size: 14px;")
        l.addWidget(lbl)
        self.tabs.addTab(w, "结果")

    def clear(self):
        while self.tabs.count() > 0:
            self.tabs.removeTab(0)

    def add_result(self, cols, rows, label="结果", elapsed_ms=0.0):
        # 首次添加时清除占位符
        if self.tabs.count() == 1:
            w = self.tabs.widget(0)
            if isinstance(w, QWidget) and not isinstance(w, ResultTable):
                self.clear()

        rt = ResultTable()
        rt.show_result(cols, rows, elapsed_ms)
        idx = self.tabs.addTab(rt, label)
        self.tabs.setCurrentIndex(idx)
        return rt

    def add_message(self, message: str):
        if self.tabs.count() == 1:
            w = self.tabs.widget(0)
            if isinstance(w, QWidget) and not isinstance(w, ResultTable):
                self.clear()

        w = QWidget()
        l = QVBoxLayout(w)
        lbl = QLabel(message)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("color: #89d185; font-size: 13px;")
        l.addWidget(lbl)
        self.tabs.addTab(w, "消息")

    def add_error(self, error: str):
        if self.tabs.count() == 1:
            w = self.tabs.widget(0)
            if isinstance(w, QWidget) and not isinstance(w, ResultTable):
                self.clear()

        w = QWidget()
        l = QVBoxLayout(w)
        lbl = QLabel(error)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #f48771; font-size: 13px;")
        l.addWidget(lbl)
        self.tabs.addTab(w, "错误")

    def result_count(self) -> int:
        return self.tabs.count()