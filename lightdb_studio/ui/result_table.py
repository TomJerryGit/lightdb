"""查询结果表格"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QLabel, QHBoxLayout, QHeaderView, QAbstractItemView
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont


class ResultTable(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 状态栏
        info_bar = QWidget()
        info_bar.setStyleSheet("background: #252526; border-top: 1px solid #3c3c3c;")
        info_layout = QHBoxLayout(info_bar)
        info_layout.setContentsMargins(8, 3, 8, 3)
        self.info_label = QLabel("就绪")
        self.info_label.setStyleSheet("color: #969696;")
        info_layout.addWidget(self.info_label)
        info_layout.addStretch()
        self.time_label = QLabel("")
        self.time_label.setStyleSheet("color: #969696;")
        info_layout.addWidget(self.time_label)
        layout.addWidget(info_bar)

        # 表格
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionsMovable(True)

        layout.addWidget(self.table)

    def show_result(self, columns, rows, elapsed_ms=0.0):
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setRowCount(len(rows))
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)

        for ri, row in enumerate(rows):
            for ci, val in enumerate(row):
                item = QTableWidgetItem()
                if val is None:
                    item.setText("NULL")
                    item.setForeground(QColor("#808080"))
                    f = item.font()
                    f.setItalic(True)
                    item.setFont(f)
                elif isinstance(val, bool):
                    item.setText('1' if val else '0')
                    item.setTextAlignment(Qt.AlignCenter)
                elif isinstance(val, (int, float)):
                    item.setText(str(val))
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                elif isinstance(val, bytes):
                    item.setText(f"<binary {len(val)} bytes>")
                else:
                    s = str(val)
                    if len(s) > 500:
                        s = s[:500] + "..."
                    item.setText(s)
                    if '\n' in s:
                        item.setText(s.replace('\n', ' '))
                self.table.setItem(ri, ci, item)

        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()

        # 限制列宽
        for ci in range(self.table.columnCount()):
            w = self.table.columnWidth(ci)
            if w > 400:
                self.table.setColumnWidth(ci, 400)
            if w < 60:
                self.table.setColumnWidth(ci, 60)

        self.info_label.setText(f"返回 {len(rows)} 行")
        self.time_label.setText(f"{elapsed_ms:.2f} ms")

    def show_message(self, message: str):
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.table.setSortingEnabled(True)
        self.info_label.setText(message)

    def show_error(self, error: str):
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.table.setSortingEnabled(True)
        self.info_label.setText("错误")
        self.info_label.setStyleSheet("color: #f48771;")
        self.time_label.setText("")

    def clear(self):
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.info_label.setText("就绪")
        self.info_label.setStyleSheet("color: #969696;")
        self.time_label.setText("")