"""实时性能监控面板"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QTableWidget, QTableWidgetItem, QPushButton, QTabWidget,
    QListWidget, QListWidgetItem, QComboBox, QSpinBox
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QBrush
from collections import deque

from ..core.performance_monitor import PerformanceMonitor


class SparkLine(QWidget):
    """迷你曲线"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = deque(maxlen=60)
        self.max_value = 1
        self.color = QColor('#4ec9b0')
        self.bg = QColor('#1e1e1e')
        self.setMinimumHeight(60)

    def push(self, v):
        self.values.append(v)
        self.max_value = max(list(self.values) + [1])
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self.bg)

        if len(self.values) < 2:
            return

        w = self.width() - 2
        h = self.height() - 2
        n = len(self.values)
        vals = list(self.values)

        # 线
        painter.setPen(QPen(self.color, 2))
        points = []
        for i, v in enumerate(vals):
            x = 1 + w * i / (n - 1)
            y = 1 + h * (1 - v / self.max_value)
            points.append((x, y))

        for i in range(len(points) - 1):
            painter.drawLine(
                int(points[i][0]), int(points[i][1]),
                int(points[i+1][0]), int(points[i+1][1]))

        # 填充
        if points:
            from PySide6.QtGui import QPolygonF
            from PySide6.QtCore import QPointF
            poly = QPolygonF()
            poly.append(QPointF(points[0][0], h))
            for x, y in points:
                poly.append(QPointF(x, y))
            poly.append(QPointF(points[-1][0], h))
            c = QColor(self.color)
            c.setAlpha(60)
            painter.setBrush(QBrush(c))
            painter.setPen(Qt.NoPen)
            painter.drawPolygon(poly)


class PerfMonitorPanel(QWidget):
    """性能监控面板"""

    def __init__(self, conn_getter, parent=None):
        super().__init__(parent)
        self.monitor = PerformanceMonitor(
            conn_getter, interval=2.0, history_size=180)
        self.monitor.on_sample(self._on_sample)
        self._build_ui()
        self.monitor.start()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        # 顶部指标
        stats_group = QGroupBox("实时指标")
        sl = QHBoxLayout(stats_group)

        self.qps_label = self._make_stat("QPS", "0")
        self.conn_label = self._make_stat("连接", "0")
        self.threads_label = self._make_stat("运行线程", "0")
        self.alerts_label = self._make_stat("告警", "0")

        for w in [self.qps_label, self.conn_label,
                  self.threads_label, self.alerts_label]:
            sl.addWidget(w)
        sl.addStretch()
        layout.addWidget(stats_group)

        # 曲线
        chart_group = QGroupBox("QPS 趋势")
        cl = QVBoxLayout(chart_group)
        self.qps_chart = SparkLine()
        cl.addWidget(self.qps_chart)
        layout.addWidget(chart_group)

        # Tab
        self.tabs = QTabWidget()

        # 告警
        alert_w = QWidget()
        al = QVBoxLayout(alert_w)
        self.alert_list = QListWidget()
        al.addWidget(self.alert_list)
        clear_alerts_btn = QPushButton("清空告警")
        clear_alerts_btn.clicked.connect(self._clear_alerts)
        al.addWidget(clear_alerts_btn)
        self.tabs.addTab(alert_w, "告警")

        # 进程列表
        proc_w = QWidget()
        pl = QVBoxLayout(proc_w)
        proc_row = QHBoxLayout()
        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self._refresh_processes)
        proc_row.addWidget(refresh_btn)
        kill_btn = QPushButton("⚡ 终止选中")
        kill_btn.clicked.connect(self._kill_selected)
        proc_row.addWidget(kill_btn)
        proc_row.addStretch()
        pl.addLayout(proc_row)
        self.proc_table = QTableWidget()
        pl.addWidget(self.proc_table)
        self.tabs.addTab(proc_w, "进程")

        # 表统计
        stat_w = QWidget()
        stl = QVBoxLayout(stat_w)
        load_btn = QPushButton("🔄 加载表统计")
        load_btn.clicked.connect(self._load_stats)
        stl.addWidget(load_btn)
        self.stats_table = QTableWidget()
        stl.addWidget(self.stats_table)
        self.tabs.addTab(stat_w, "表统计")

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
            "color: #4ec9b0; font-size: 20px; font-weight: bold;")
        val.setAlignment(Qt.AlignCenter)
        l.addWidget(title)
        l.addWidget(val)
        w._val_label = val
        return w

    def _on_sample(self, sample):
        self.qps_label._val_label.setText(f"{sample.qps:.0f}")
        self.conn_label._val_label.setText(str(sample.connections))
        self.threads_label._val_label.setText(str(sample.threads_running))
        self.alerts_label._val_label.setText(str(len(self.monitor.alerts)))

        self.qps_chart.push(sample.qps)

        # 刷新告警
        self._refresh_alerts()

    def _refresh_alerts(self):
        self.alert_list.clear()
        from datetime import datetime
        for a in list(self.monitor.alerts)[-30:][::-1]:
            ts = datetime.fromtimestamp(a['ts']).strftime('%H:%M:%S')
            item = QListWidgetItem(
                f"[{ts}] {a['title']}: {a['message']}")
            item.setForeground(QColor("#f48771"))
            self.alert_list.addItem(item)

    def _clear_alerts(self):
        self.monitor.alerts.clear()
        self._refresh_alerts()

    def _refresh_processes(self):
        # 通过主窗口连接
        win = self.window()
        conn = None
        if hasattr(win, '_get_current_conn'):
            conn = win._get_current_conn()
        if conn is None:
            return
        try:
            cols, rows = conn.query("SHOW PROCESSLIST")
        except Exception:
            return
        self.proc_table.setRowCount(len(rows))
        self.proc_table.setColumnCount(len(cols))
        self.proc_table.setHorizontalHeaderLabels(cols)
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                self.proc_table.setItem(r, c, QTableWidgetItem(
                    'NULL' if v is None else str(v)))
        self.proc_table.resizeColumnsToContents()

    def _kill_selected(self):
        win = self.window()
        conn = getattr(win, '_get_current_conn', lambda: None)()
        if conn is None:
            return
        rows = sorted({i.row() for i in self.proc_table.selectedIndexes()})
        for r in rows:
            item = self.proc_table.item(r, 0)
            if item:
                try:
                    conn.execute(f"KILL {item.text()}")
                except Exception:
                    pass
        self._refresh_processes()

    def _load_stats(self):
        win = self.window()
        conn = getattr(win, '_get_current_conn', lambda: None)()
        if conn is None:
            return
        try:
            cols, rows = conn.query("SHOW STATS")
        except Exception:
            return
        self.stats_table.setRowCount(len(rows))
        self.stats_table.setColumnCount(len(cols))
        self.stats_table.setHorizontalHeaderLabels(cols)
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                self.stats_table.setItem(r, c, QTableWidgetItem(
                    'NULL' if v is None else str(v)))
        self.stats_table.resizeColumnsToContents()