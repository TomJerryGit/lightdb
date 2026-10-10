"""数据图表可视化"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QComboBox, QPushButton,
    QLabel, QFileDialog, QMessageBox, QCheckBox, QSpinBox
)
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QFontMetrics, QImage
)
from PySide6.QtWidgets import QWidget


class ChartWidget(QWidget):
    """图表绘制"""

    CHART_BAR = 'bar'
    CHART_LINE = 'line'
    CHART_PIE = 'pie'
    CHART_SCATTER = 'scatter'

    COLORS = [
        '#4ec9b0', '#569cd6', '#dcdcaa', '#ce9178', '#c586c0',
        '#9cdcfe', '#f48771', '#d16969', '#89d185', '#b5cea8',
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.labels = []
        self.values = []
        self.chart_type = self.CHART_BAR
        self.title = ""
        self.background = QColor("#1e1e1e")
        self.foreground = QColor("#d4d4d4")
        self.grid_color = QColor("#3c3c3c")
        self.setMinimumSize(400, 300)

    def set_data(self, labels, values, title="", chart_type='bar'):
        self.labels = labels
        self.values = values
        self.title = title
        self.chart_type = chart_type
        self.update()

    def set_theme(self, bg, fg, grid):
        self.background = QColor(bg)
        self.foreground = QColor(fg)
        self.grid_color = QColor(grid)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self.background)

        if not self.values:
            painter.setPen(self.foreground)
            painter.drawText(self.rect(), Qt.AlignCenter, "无数据")
            return

        margin_left = 70
        margin_right = 30
        margin_top = 50
        margin_bottom = 80

        # 标题
        if self.title:
            painter.setPen(self.foreground)
            f = QFont("Microsoft YaHei", 12, QFont.Bold)
            painter.setFont(f)
            painter.drawText(QRectF(0, 10, self.width(), 30),
                             Qt.AlignCenter, self.title)

        plot_rect = QRectF(
            margin_left, margin_top,
            self.width() - margin_left - margin_right,
            self.height() - margin_top - margin_bottom)

        if self.chart_type == self.CHART_PIE:
            self._draw_pie(painter, plot_rect)
        elif self.chart_type == self.CHART_LINE:
            self._draw_line(painter, plot_rect)
        elif self.chart_type == self.CHART_SCATTER:
            self._draw_scatter(painter, plot_rect)
        else:
            self._draw_bar(painter, plot_rect)

    def _draw_axes(self, painter, rect):
        """绘制坐标轴 + 网格"""
        painter.setPen(QPen(self.grid_color, 1, Qt.DashLine))
        for i in range(6):
            y = rect.top() + rect.height() * i / 5
            painter.drawLine(QPointF(rect.left(), y),
                             QPointF(rect.right(), y))

        painter.setPen(QPen(self.foreground, 1))
        painter.drawLine(QPointF(rect.left(), rect.bottom()),
                         QPointF(rect.right(), rect.bottom()))
        painter.drawLine(QPointF(rect.left(), rect.top()),
                         QPointF(rect.left(), rect.bottom()))

    def _draw_bar(self, painter, rect):
        self._draw_axes(painter, rect)

        n = len(self.values)
        if n == 0:
            return
        max_v = max(abs(v) for v in self.values) or 1
        bar_w = rect.width() / n * 0.7
        gap = rect.width() / n * 0.3

        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())

        for i, v in enumerate(self.values):
            h = abs(v) / max_v * rect.height() * 0.9
            x = rect.left() + i * (bar_w + gap) + gap / 2
            y = rect.bottom() - h
            color = QColor(self.COLORS[i % len(self.COLORS)])
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(Qt.NoPen))
            painter.drawRect(QRectF(x, y, bar_w, h))

            # 值
            painter.setPen(self.foreground)
            val_text = self._fmt(v)
            tw = fm.horizontalAdvance(val_text)
            painter.drawText(QPointF(x + bar_w / 2 - tw / 2, y - 4),
                             val_text)

            # 标签
            label = str(self.labels[i])
            if len(label) > 8:
                label = label[:7] + '…'
            tw = fm.horizontalAdvance(label)
            painter.drawText(
                QRectF(x - 10, rect.bottom() + 4, bar_w + 20, 60),
                Qt.AlignHCenter | Qt.TextWordWrap, label)

    def _draw_line(self, painter, rect):
        self._draw_axes(painter, rect)

        n = len(self.values)
        if n < 2:
            if n == 1:
                self._draw_bar(painter, rect)
            return

        max_v = max(self.values) or 1
        min_v = min(self.values)
        span = max_v - min_v or 1

        points = []
        for i, v in enumerate(self.values):
            x = rect.left() + rect.width() * i / (n - 1)
            y = rect.bottom() - (v - min_v) / span * rect.height() * 0.9
            points.append(QPointF(x, y))

        # 网格线和坐标
        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())

        # 折线
        pen = QPen(QColor(self.COLORS[0]), 2)
        painter.setPen(pen)
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])

        # 数据点
        painter.setBrush(QBrush(QColor(self.COLORS[0])))
        painter.setPen(QPen(QColor("#ffffff"), 1))
        for i, p in enumerate(points):
            painter.drawEllipse(p, 4, 4)
            # 值
            painter.setPen(self.foreground)
            val_text = self._fmt(self.values[i])
            tw = fm.horizontalAdvance(val_text)
            painter.drawText(QPointF(p.x() - tw / 2, p.y() - 8), val_text)

            # 标签
            label = str(self.labels[i])[:7]
            tw = fm.horizontalAdvance(label)
            painter.drawText(
                QRectF(p.x() - 30, rect.bottom() + 4, 60, 60),
                Qt.AlignHCenter | Qt.TextWordWrap, label)

    def _draw_pie(self, painter, rect):
        total = sum(abs(v) for v in self.values) or 1
        cx = rect.center().x()
        cy = rect.center().y()
        radius = min(rect.width(), rect.height()) * 0.4

        start_angle = 0
        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())

        for i, v in enumerate(self.values):
            span = int(abs(v) / total * 360 * 16)
            color = QColor(self.COLORS[i % len(self.COLORS)])
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(self.background, 2))
            painter.drawPie(
                QRectF(cx - radius, cy - radius, radius * 2, radius * 2),
                start_angle, span)

            # 百分比标注
            mid_angle = (start_angle + span / 2) / 16 * 3.14159 / 180
            import math
            tx = cx + radius * 0.7 * math.cos(mid_angle)
            ty = cy + radius * 0.7 * math.sin(mid_angle)
            pct = abs(v) / total * 100
            text = f"{pct:.1f}%"
            tw = fm.horizontalAdvance(text)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(QPointF(tx - tw / 2, ty + 4), text)

            start_angle += span

        # 图例
        legend_x = rect.right() - 120
        legend_y = rect.top() + 10
        for i, (label, v) in enumerate(zip(self.labels, self.values)):
            if i > 15:
                break
            color = QColor(self.COLORS[i % len(self.COLORS)])
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(Qt.NoPen))
            painter.drawRect(QRectF(legend_x, legend_y + i * 18, 12, 12))
            painter.setPen(self.foreground)
            label_str = str(label)[:12]
            painter.drawText(QPointF(legend_x + 18, legend_y + i * 18 + 11),
                             f"{label_str}: {self._fmt(v)}")

    def _draw_scatter(self, painter, rect):
        self._draw_axes(painter, rect)

        n = len(self.values)
        if n == 0:
            return

        max_v = max(self.values) or 1
        max_l = max(range(n)) or 1

        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())

        for i, v in enumerate(self.values):
            x = rect.left() + rect.width() * i / max_l
            y = rect.bottom() - v / max_v * rect.height() * 0.9
            color = QColor(self.COLORS[i % len(self.COLORS)])
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.drawEllipse(QPointF(x, y), 5, 5)

            painter.setPen(self.foreground)
            val_text = self._fmt(v)
            painter.drawText(QPointF(x + 8, y - 4), val_text)

    def _fmt(self, v):
        if isinstance(v, float):
            return f"{v:.2f}"
        if isinstance(v, int):
            return str(v)
        return str(v)[:12]


class ChartDialog(QDialog):
    """图表对话框"""

    def __init__(self, columns, rows, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.rows = rows
        self.setWindowTitle("数据图表")
        self.resize(960, 640)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 顶部控制
        top = QHBoxLayout()
        top.addWidget(QLabel("图表类型:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(['柱状图', '折线图', '饼图', '散点图'])
        self.type_combo.currentTextChanged.connect(self._redraw)
        top.addWidget(self.type_combo)

        top.addWidget(QLabel("X 轴 / 标签:"))
        self.x_combo = QComboBox()
        self.x_combo.addItems(self.columns)
        self.x_combo.currentTextChanged.connect(self._redraw)
        top.addWidget(self.x_combo)

        top.addWidget(QLabel("Y 轴 / 值:"))
        self.y_combo = QComboBox()
        self.y_combo.addItems(self.columns)
        # 默认选一个数值列
        for i, c in enumerate(self.columns):
            if self._is_numeric_col(i):
                self.y_combo.setCurrentIndex(i)
                break
        self.y_combo.currentTextChanged.connect(self._redraw)
        top.addWidget(self.y_combo)

        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(2, 500)
        self.limit_spin.setValue(20)
        self.limit_spin.setPrefix("前 ")
        self.limit_spin.setSuffix(" 行")
        self.limit_spin.valueChanged.connect(self._redraw)
        top.addWidget(self.limit_spin)

        top.addStretch()

        export_btn = QPushButton("📤 导出 PNG")
        export_btn.clicked.connect(self._export_png)
        top.addWidget(export_btn)

        layout.addLayout(top)

        # 图表
        self.chart = ChartWidget()
        layout.addWidget(self.chart)

        # 底部
        self.info_label = QLabel("")
        self.info_label.setStyleSheet(
            "color: #969696; padding: 4px; background: #252526;")
        layout.addWidget(self.info_label)

        self._redraw()

    def _is_numeric_col(self, ci):
        if not self.rows:
            return False
        for r in self.rows[:20]:
            if ci < len(r) and r[ci] is not None:
                return isinstance(r[ci], (int, float)) and \
                    not isinstance(r[ci], bool)
        return False

    def _redraw(self):
        if not self.rows or not self.columns:
            return

        x_idx = self.x_combo.currentIndex()
        y_idx = self.y_combo.currentIndex()
        if x_idx < 0 or y_idx < 0:
            return

        chart_type = {
            '柱状图': ChartWidget.CHART_BAR,
            '折线图': ChartWidget.CHART_LINE,
            '饼图': ChartWidget.CHART_PIE,
            '散点图': ChartWidget.CHART_SCATTER,
        }.get(self.type_combo.currentText(), ChartWidget.CHART_BAR)

        limit = self.limit_spin.value()
        data = self.rows[:limit]

        labels = []
        values = []
        for r in data:
            xv = r[x_idx] if x_idx < len(r) else ''
            yv = r[y_idx] if y_idx < len(r) else 0
            if yv is None:
                yv = 0
            if not isinstance(yv, (int, float)):
                try:
                    yv = float(yv)
                except (ValueError, TypeError):
                    yv = 0
            labels.append(str(xv) if xv is not None else '')
            values.append(yv)

        title = f"{self.y_combo.currentText()} by {self.x_combo.currentText()}"
        self.chart.set_data(labels, values, title, chart_type)
        self.info_label.setText(
            f"显示 {len(values)} 条数据 | 最大值: {max(values) if values else 0:.2f} | "
            f"最小值: {min(values) if values else 0:.2f} | "
            f"平均: {sum(values)/len(values) if values else 0:.2f}")

    def _export_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存图表", "chart.png", "PNG 图片 (*.png)")
        if not path:
            return
        try:
            pixmap = self.chart.grab()
            pixmap.save(path)
            QMessageBox.information(self, "成功", f"已保存:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))