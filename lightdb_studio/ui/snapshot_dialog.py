"""快照管理对话框"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QDialogButtonBox, QMessageBox, QTabWidget, QWidget,
    QLabel, QLineEdit, QFormLayout, QListWidget, QListWidgetItem,
    QProgressDialog, QComboBox, QCheckBox, QGroupBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from datetime import datetime

from ..core.snapshot import SnapshotManager


class SnapshotDialog(QDialog):
    def __init__(self, conn_getter, parent=None):
        super().__init__(parent)
        self.mgr = SnapshotManager()
        self.conn_getter = conn_getter
        self.setWindowTitle("快照 / 克隆")
        self.resize(960, 640)
        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        tabs = QTabWidget()

        # Tab1: 快照
        snap_w = QWidget()
        sl = QVBoxLayout(snap_w)

        sl.addWidget(QLabel(
            "创建数据库快照（导出为 SQL 文件），用于备份或迁移。"))

        self.list_widget = QTableWidget()
        self.list_widget.setColumnCount(6)
        self.list_widget.setHorizontalHeaderLabels(
            ['名称', '连接', '库', '表数', '大小', '创建时间'])
        self.list_widget.setColumnWidth(0, 200)
        self.list_widget.setColumnWidth(1, 120)
        self.list_widget.setColumnWidth(2, 120)
        self.list_widget.setColumnWidth(3, 80)
        self.list_widget.setColumnWidth(4, 100)
        self.list_widget.horizontalHeader().setStretchLastSection(True)
        self.list_widget.itemSelectionChanged.connect(self._on_select)
        sl.addWidget(self.list_widget)

        row = QHBoxLayout()

        create_btn = QPushButton("➕ 创建快照")
        create_btn.clicked.connect(self._create)
        row.addWidget(create_btn)

        restore_btn = QPushButton("↩️ 恢复快照")
        restore_btn.clicked.connect(self._restore)
        row.addWidget(restore_btn)

        delete_btn = QPushButton("🗑 删除")
        delete_btn.clicked.connect(self._delete)
        row.addWidget(delete_btn)

        row.addStretch()
        sl.addLayout(row)

        tabs.addTab(snap_w, "快照")

        # Tab2: 克隆
        clone_w = QWidget()
        cl = QVBoxLayout(clone_w)

        cl.addWidget(QLabel(
            "将源数据库直接克隆到另一个连接（表结构 + 数据）。"))

        form = QFormLayout()
        self.src_edit = QLineEdit()
        form.addRow("源连接:", self.src_edit)
        self.dst_edit = QLineEdit()
        form.addRow("目标连接:", self.dst_edit)

        cl.addLayout(form)

        self.drop_check = QCheckBox("目标库已存在的同名表先删除")
        self.drop_check.setChecked(True)
        cl.addWidget(self.drop_check)

        clone_btn = QPushButton("▶ 开始克隆")
        clone_btn.clicked.connect(self._clone)
        cl.addWidget(clone_btn)

        self.clone_log = QListWidget()
        self.clone_log.setFont(QFont("Consolas", 9))
        cl.addWidget(self.clone_log)

        tabs.addTab(clone_w, "克隆")

        layout.addWidget(tabs)

        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.reject)
        layout.addWidget(close_btn)

    def _refresh_list(self):
        snaps = self.mgr.list_all()
        self.list_widget.setRowCount(len(snaps))
        for r, s in enumerate(snaps):
            self.list_widget.setItem(r, 0, QTableWidgetItem(s.name))
            self.list_widget.setItem(r, 1, QTableWidgetItem(s.connection))
            self.list_widget.setItem(r, 2, QTableWidgetItem(s.database))

            n_tables = len(s.tables)
            total_rows = sum(s.row_counts.values()) if s.row_counts else 0
            self.list_widget.setItem(
                r, 3, QTableWidgetItem(f"{n_tables} / {total_rows}"))

            size_kb = s.size / 1024
            self.list_widget.setItem(
                r, 4, QTableWidgetItem(f"{size_kb:.1f} KB"))

            ts = datetime.fromtimestamp(s.created_at).strftime(
                '%Y-%m-%d %H:%M:%S')
            self.list_widget.setItem(r, 5, QTableWidgetItem(ts))

    def _on_select(self):
        pass

    def _get_selected_snapshot(self):
        r = self.list_widget.currentRow()
        if r < 0:
            return None
        name = self.list_widget.item(r, 0).text()
        return self.mgr.get(name)

    def _create(self):
        conn = self.conn_getter()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择连接")
            return

        # 询问名称
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "创建快照", "快照名称:",
            text=f"snap_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        if not ok or not name.strip():
            return

        progress = QProgressDialog("创建快照...", "取消", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)

        def cb(i, total, tname):
            progress.setValue(int(i / total * 100))
            progress.setLabelText(f"处理表: {tname}")

        try:
            result = self.mgr.create_snapshot(
                conn, name.strip(), progress_cb=cb)
            progress.close()
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "失败", str(e))
            return

        # create_snapshot 现在返回 SnapshotResult：失败时绝不弹「完成」，
        # 而是把失败清单（表名 / 语句 / 错误原文）完整展示出来。
        if not getattr(result, 'ok', True):
            QMessageBox.critical(
                self, "快照失败",
                f"快照未创建（未落盘、未进索引）：\n{result.error}\n\n"
                + (result.summary() if hasattr(result, 'summary') else ''))
            return

        snap = result.snapshot
        QMessageBox.information(
            self, "完成",
            f"快照已创建:\n{snap.file}\n"
            f"大小: {snap.size / 1024:.1f} KB\n"
            f"表数: {len(snap.tables)}")
        self._refresh_list()

    def _restore(self):
        snap = self._get_selected_snapshot()
        if not snap:
            return

        conn = self.conn_getter()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择连接")
            return

        ret = QMessageBox.question(
            self, "确认",
            f"将快照 '{snap.name}' 恢复到当前连接？\n"
            "⚠️ 如果表已存在会失败，请确认后操作。")
        if ret != QMessageBox.Yes:
            return

        progress = QProgressDialog("恢复中...", "取消", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)

        def cb(i, total, preview):
            progress.setValue(int(i / total * 100))
            progress.setLabelText(f"执行: {preview[:60]}")

        try:
            result = self.mgr.restore_snapshot(
                conn, snap, progress_cb=cb)
            progress.close()
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "失败", str(e))
            return

        msg = (f"执行 {result['executed']} 条 SQL\n"
               f"失败 {result['failed']} 条")
        if result.get('errors'):
            msg += '\n\n错误清单:\n' + '\n'.join(
                f"  - {e}" for e in result['errors'][:20])
        if result.get('ok'):
            QMessageBox.information(self, "完成", msg)
        else:
            QMessageBox.critical(self, "恢复未全部成功", msg)

    def _delete(self):
        snap = self._get_selected_snapshot()
        if not snap:
            return
        ret = QMessageBox.question(
            self, "确认", f"删除快照 '{snap.name}'？")
        if ret == QMessageBox.Yes:
            self.mgr.remove(snap.name)
            self._refresh_list()

    def _clone(self):
        src_name = self.src_edit.text().strip()
        dst_name = self.dst_edit.text().strip()
        if not src_name or not dst_name:
            QMessageBox.warning(self, "提示", "请填写源/目标连接名")
            return

        # 用主窗口的连接表
        parent = self.parent()
        if not hasattr(parent, 'object_tree'):
            return
        src = parent.object_tree.connections.get(src_name)
        dst = parent.object_tree.connections.get(dst_name)
        if src is None or dst is None:
            QMessageBox.warning(self, "提示", "连接不存在")
            return

        ret = QMessageBox.question(
            self, "确认",
            f"将 '{src_name}' 的表克隆到 '{dst_name}'？\n"
            "目标库同名表可能被覆盖！")
        if ret != QMessageBox.Yes:
            return

        self.clone_log.clear()

        progress = QProgressDialog("克隆中...", "取消", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)

        def cb(i, total, tname):
            progress.setValue(int(i / total * 100))
            progress.setLabelText(f"克隆表: {tname}")
            item = QListWidgetItem(f"→ {tname}")
            item.setForeground(QColor("#89d185"))
            self.clone_log.addItem(item)

        try:
            result = self.mgr.clone_database(
                src, dst, drop_existing=self.drop_check.isChecked(),
                progress_cb=cb)
            progress.close()
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "失败", str(e))
            return

        msg = f"克隆 {result['cloned']} 表，失败 {result['failed']}"
        if result.get('errors'):
            msg += '\n\n错误清单:\n' + '\n'.join(
                f"  - {e}" for e in result['errors'][:20])
        if result.get('ok'):
            QMessageBox.information(self, "完成", msg)
        else:
            QMessageBox.critical(self, "克隆未全部成功", msg)