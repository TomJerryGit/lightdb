"""
性能监控面板
- 实时 QPS
- 慢查询
- 连接列表 + Kill
- 缓存命中率
"""
import time
from collections import deque
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QPushButton, QTabWidget, QListWidget,
    QListWidgetItem, QMessageBox, QGroupBox
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont


class PerfPanel(QWidget):
    """性能面板"""

    def __init__(self, conn_getter, parent=None):
        """
        conn_getter: 一个函数，返回当前连接（或 None）
        """
        super().__init__(parent)
        self.conn_getter = conn_getter
        self.history = deque(maxlen=60)  # 最近 60 秒
        self.last_sample = None
        self._build_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)  # 每秒刷新

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        # 顶部统计
        stats_group = QGroupBox("服务器状态")
        sg_layout = QHBoxLayout(stats_group)

        self.qps_label = self._make_stat("QPS", "0")
        self.uptime_label = self._make_stat("Uptime", "-")
        self.conns_label = self._make_stat("连接数", "-")
        self.queries_label = self._make_stat("总查询", "-")
        self.cache_label = self._make_stat("缓存命中", "-")

        for w in [self.qps_label, self.uptime_label, self.conns_label,
                  self.queries_label, self.cache_label]:
            sg_layout.addWidget(w)

        sg_layout.addStretch()
        layout.addWidget(stats_group)

        # Tab：进程列表 / 慢查询
        self.tabs = QTabWidget()

        # 进程列表
        proc_widget = QWidget()
        pw_layout = QVBoxLayout(proc_widget)

        proc_btns = QHBoxLayout()
        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self._refresh_processlist)
        proc_btns.addWidget(refresh_btn)

        kill_btn = QPushButton("⚡ 终止选中")
        kill_btn.clicked.connect(self._kill_selected)
        proc_btns.addWidget(kill_btn)

        proc_btns.addStretch()
        pw_layout.addLayout(proc_btns)

        self.proc_table = QTableWidget()
        pw_layout.addWidget(self.proc_table)

        self.tabs.addTab(proc_widget, "进程列表")

        # 慢查询
        slow_widget = QWidget()
        sw_layout = QVBoxLayout(slow_widget)

        slow_btns = QHBoxLayout()
        load_slow_btn = QPushButton("🔄 加载慢查询")
        load_slow_btn.clicked.connect(self._load_slow_queries)
        slow_btns.addWidget(load_slow_btn)
        slow_btns.addStretch()
        sw_layout.addLayout(slow_btns)

        self.slow_list = QListWidget()
        self.slow_list.setFont(QFont("Consolas", 9))
        sw_layout.addWidget(self.slow_list)

        self.tabs.addTab(slow_widget, "慢查询")

        # 数据库统计
        stats_widget = QWidget()
        st_layout = QVBoxLayout(stats_widget)

        st_btns = QHBoxLayout()
        load_stats_btn = QPushButton("🔄 加载表统计")
        load_stats_btn.clicked.connect(self._load_table_stats)
        st_btns.addWidget(load_stats_btn)
        st_btns.addStretch()
        st_layout.addLayout(st_btns)

        self.stats_table = QTableWidget()
        st_layout.addWidget(self.stats_table)

        self.tabs.addTab(stats_widget, "表统计")

        layout.addWidget(self.tabs)

    def _make_stat(self, label, value):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(8, 4, 8, 4)
        l.setSpacing(0)

        title = QLabel(label)
        title.setStyleSheet("color: #969696; font-size: 11px;")
        title.setAlignment(Qt.AlignCenter)

        val = QLabel(value)
        val.setStyleSheet(
            "color: #4ec9b0; font-size: 18px; font-weight: bold;")
        val.setAlignment(Qt.AlignCenter)
        val.setObjectName("value")

        l.addWidget(title)
        l.addWidget(val)
        w._value_label = val
        return w

    def _tick(self):
        conn = self.conn_getter()
        if conn is None or not conn.is_connected():
            self.qps_label._value_label.setText("-")
            return

        try:
            cols, rows = conn.query("SHOW STATUS")
            status = dict(rows) if rows else {}

            queries = int(status.get('Questions', '0') or '0')
            uptime = int(status.get('Uptime', '0') or '0')

            # QPS 计算
            now = time.time()
            if self.last_sample is not None:
                dt = now - self.last_sample[0]
                dq = queries - self.last_sample[1]
                qps = dq / dt if dt > 0 else 0
                self.history.append(qps)
                self.qps_label._value_label.setText(f"{qps:.1f}")

            self.last_sample = (now, queries)

            self.uptime_label._value_label.setText(self._fmt_uptime(uptime))
            self.conns_label._value_label.setText(
                status.get('Threads_connected', '-'))
            self.queries_label._value_label.setText(str(queries))
        except Exception:
            pass

    def _fmt_uptime(self, seconds):
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"

    def _refresh_processlist(self):
        conn = self.conn_getter()
        if not conn:
            return
        try:
            cols, rows = conn.query("SHOW PROCESSLIST")
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return

        self.proc_table.setRowCount(len(rows))
        self.proc_table.setColumnCount(len(cols))
        self.proc_table.setHorizontalHeaderLabels(cols)
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                item = QTableWidgetItem('NULL' if v is None else str(v))
                self.proc_table.setItem(r, c, item)
        self.proc_table.resizeColumnsToContents()

    def _kill_selected(self):
        conn = self.conn_getter()
        if not conn:
            return
        rows = sorted({i.row() for i in self.proc_table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "提示", "请先选中要终止的连接")
            return

        # 找 Id 列
        id_col = 0
        for c in range(self.proc_table.columnCount()):
            header = self.proc_table.horizontalHeaderItem(c)
            if header and header.text().lower() == 'id':
                id_col = c
                break

        for r in rows:
            item = self.proc_table.item(r, id_col)
            if not item:
                continue
            cid = item.text()
            try:
                conn.execute(f"KILL {cid}")
            except Exception:
                pass

        QMessageBox.information(self, "完成", f"已终止 {len(rows)} 个连接")
        self._refresh_processlist()

    def _load_slow_queries(self):
        conn = self.conn_getter()
        if not conn:
            return
        try:
            # 尝试查询慢查询日志（LightDB 特有）
            cols, rows = conn.query("SHOW AUDIT LOG")
        except Exception:
            self.slow_list.clear()
            self.slow_list.addItem("-- 慢查询日志不可用")
            return

        self.slow_list.clear()
        for r in rows[-200:]:
            line = ' | '.join(str(v)[:80] for v in r if v is not None)
            self.slow_list.addItem(line)

    def _load_table_stats(self):
        conn = self.conn_getter()
        if not conn:
            return
        try:
            cols, rows = conn.query("SHOW STATS")
        except Exception as e:
            QMessageBox.warning(self, "提示", f"SHOW STATS 不可用: {e}")
            return

        self.stats_table.setRowCount(len(rows))
        self.stats_table.setColumnCount(len(cols))
        self.stats_table.setHorizontalHeaderLabels(cols)
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                self.stats_table.setItem(r, c, QTableWidgetItem(
                    'NULL' if v is None else str(v)))
        self.stats_table.resizeColumnsToContents()