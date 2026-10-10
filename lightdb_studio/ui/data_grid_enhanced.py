"""数据网格增强工具"""
import csv
import io
from PySide6.QtWidgets import QTableWidget, QApplication, QMenu, QMessageBox
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction

from .cell_viewer import CellViewerDialog


class EnhancedDataGridMixin:
    """
    给 QTableWidget 增加：
    - 双击查看大内容
    - Ctrl+C 复制（Excel 格式）
    - Ctrl+V 粘贴
    - 右键菜单
    """

    @staticmethod
    def install(grid: QTableWidget):
        """安装所有增强功能"""
        grid._cell_viewer_cb = None
        grid._enhanced_installed = True

        # 双击查看
        def on_double(idx):
            item = grid.item(idx.row(), idx.column())
            if item is None:
                return
            val = item.data(Qt.UserRole)
            if val is None:
                val = item.text()
            col_name = grid.horizontalHeaderItem(idx.column()).text() \
                if grid.horizontalHeaderItem(idx.column()) else ''
            # 仅大文本或二进制才打开查看器
            if isinstance(val, bytes) or (isinstance(val, str) and len(val) > 200):
                dlg = CellViewerDialog(val, col_name, '', grid)
                dlg.exec()
            elif isinstance(val, str) and ('\n' in val):
                dlg = CellViewerDialog(val, col_name, '', grid)
                dlg.exec()

        grid.cellDoubleClicked.connect(on_double)

        # 右键菜单
        grid.setContextMenuPolicy(Qt.CustomContextMenu)

        def on_menu(pos):
            idx = grid.indexAt(pos)
            menu = QMenu(grid)

            if idx.isValid():
                item = grid.item(idx.row(), idx.column())
                val = item.data(Qt.UserRole) if item else None

                a_view = menu.addAction("👁 查看内容...")
                a_copy = menu.addAction("📋 复制单元格")
                a_copy_row = menu.addAction("📋 复制整行")
                menu.addSeparator()
                a_export = menu.addAction("📤 导出选中为 CSV")

                action = menu.exec(grid.viewport().mapToGlobal(pos))

                if action == a_view:
                    col_name = grid.horizontalHeaderItem(idx.column()).text()
                    dlg = CellViewerDialog(val if val is not None else item.text(),
                                           col_name, '', grid)
                    dlg.exec()
                elif action == a_copy:
                    text = item.text() if item else ''
                    QApplication.clipboard().setText(text)
                elif action == a_copy_row:
                    EnhancedDataGridMixin._copy_selected_rows(grid)
                elif action == a_export:
                    EnhancedDataGridMixin._export_selected(grid)
            else:
                a_paste = menu.addAction("📥 从剪贴板粘贴（Excel）")
                action = menu.exec(grid.viewport().mapToGlobal(pos))
                if action == a_paste:
                    EnhancedDataGridMixin._paste_from_clipboard(grid)

        grid.customContextMenuRequested.connect(on_menu)

    @staticmethod
    def _copy_selected_rows(grid: QTableWidget):
        """复制选中行（Excel 格式，制表符分隔）"""
        rows = sorted({i.row() for i in grid.selectedIndexes()})
        if not rows:
            return
        cols = grid.columnCount()
        lines = []
        for r in rows:
            line = []
            for c in range(cols):
                item = grid.item(r, c)
                line.append(item.text() if item else '')
            lines.append('\t'.join(line))
        QApplication.clipboard().setText('\n'.join(lines))

    @staticmethod
    def _paste_from_clipboard(grid: QTableWidget):
        """从剪贴板粘贴（Excel 格式）"""
        text = QApplication.clipboard().text()
        if not text:
            return

        # 解析：制表符/换行
        lines = text.splitlines()
        data = [line.split('\t') for line in lines]

        start = grid.currentRow()
        if start < 0:
            start = 0

        for i, row in enumerate(data):
            r = start + i
            if r >= grid.rowCount():
                grid.insertRow(r)
            for j, val in enumerate(row):
                c = grid.currentColumn() + j if grid.currentColumn() >= 0 else j
                if c >= grid.columnCount():
                    continue
                item = grid.item(r, c)
                if item is None:
                    from PySide6.QtWidgets import QTableWidgetItem
                    item = QTableWidgetItem()
                    grid.setItem(r, c, item)
                item.setText(val)

    @staticmethod
    def _export_selected(grid: QTableWidget):
        """导出选中为 CSV 文本"""
        rows = sorted({i.row() for i in grid.selectedIndexes()})
        if not rows:
            QMessageBox.information(grid, "提示", "请先选中要导出的行")
            return
        lines = []
        for r in rows:
            line = []
            for c in range(grid.columnCount()):
                item = grid.item(r, c)
                line.append(item.text() if item else '')
            lines.append('\t'.join(line))
        QApplication.clipboard().setText('\n'.join(lines))
        QMessageBox.information(
            grid, "已复制", f"已将 {len(rows)} 行复制到剪贴板")

    @staticmethod
    def column_filter(grid: QTableWidget, col: int, text: str):
        """按列过滤"""
        text = text.strip().lower()
        for r in range(grid.rowCount()):
            item = grid.item(r, col)
            show = (not text) or (
                item and text in item.text().lower())
            grid.setRowHidden(r, not show)