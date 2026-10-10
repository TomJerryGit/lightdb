"""增强图表 - 10 种图表类型"""
import math
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QFontMetrics,
    QLinearGradient, QRadialGradient, QPolygonF, QPainterPath,
)


class EnhancedChartWidget(QWidget):
    """支持更多图表类型"""

    # 已有类型
    CHART_BAR = 'bar'
    CHART_LINE = 'line'
    CHART_PIE = 'pie'
    CHART_SCATTER = 'scatter'
    # 新增类型
    CHART_RADAR = 'radar'
    CHART_HEATMAP = 'heatmap'
    CHART_AREA = 'area'
    CHART_DONUT = 'donut'
    CHART_STACKED_BAR = 'stacked_bar'
    CHART_BUBBLE = 'bubble'

    COLORS = [
        '#4ec9b0', '#569cd6', '#dcdcaa', '#ce9178', '#c586c0',
        '#9cdcfe', '#f48771', '#d16969', '#89d185', '#b5cea8',
        '#d7ba7d', '#4fc1ff', '#c586c0', '#dcdcaa', '#ce9178',
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.labels = []
        self.values = []
        self.matrix = None       # 热力图用
        self.series = None       # 多系列（堆叠）
        self.chart_type = self.CHART_BAR
        self.title = ""
        self.custom_colors = []
        self.background = QColor("#1e1e1e")
        self.foreground = QColor("#d4d4d4")
        self.grid_color = QColor("#3c3c3c")
        self.setMinimumSize(400, 300)

    def set_data(self, labels, values, title="", chart_type='bar',
                 colors=None, matrix=None, series=None):
        self.labels = labels or []
        self.values = values or []
        self.title = title
        self.chart_type = chart_type
        self.matrix = matrix
        self.series = series
        if colors:
            self.custom_colors = colors
        self.update()

    def set_theme(self, bg, fg, grid):
        self.background = QColor(bg)
        self.foreground = QColor(fg)
        self.grid_color = QColor(grid)
        self.update()

    def _color(self, i):
        if self.custom_colors and i < len(self.custom_colors):
            return QColor(self.custom_colors[i])
        return QColor(self.COLORS[i % len(self.COLORS)])

    # ==================== 主绘制 ====================

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self.background)

        if self.chart_type != self.CHART_HEATMAP and not self.values:
            painter.setPen(self.foreground)
            painter.drawText(self.rect(), Qt.AlignCenter, "无数据")
            return

        margin_left, margin_right = 70, 30
        margin_top, margin_bottom = 50, 80

        if self.title:
            painter.setPen(self.foreground)
            painter.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
            painter.drawText(
                QRectF(0, 10, self.width(), 30),
                Qt.AlignCenter, self.title)

        plot = QRectF(
            margin_left, margin_top,
            self.width() - margin_left - margin_right,
            self.height() - margin_top - margin_bottom)

        t = self.chart_type
        try:
            if t == self.CHART_PIE:
                self._draw_pie(painter, plot, donut=False)
            elif t == self.CHART_DONUT:
                self._draw_pie(painter, plot, donut=True)
            elif t == self.CHART_LINE:
                self._draw_line(painter, plot, area=False)
            elif t == self.CHART_AREA:
                self._draw_line(painter, plot, area=True)
            elif t == self.CHART_RADAR:
                self._draw_radar(painter, plot)
            elif t == self.CHART_HEATMAP:
                self._draw_heatmap(painter, plot)
            elif t == self.CHART_STACKED_BAR:
                self._draw_stacked_bar(painter, plot)
            elif t == self.CHART_BUBBLE:
                self._draw_bubble(painter, plot)
            elif t == self.CHART_SCATTER:
                self._draw_scatter(painter, plot)
            else:
                self._draw_bar(painter, plot)
        finally:
            painter.end()

    # ==================== 通用 ====================

    def _draw_axes(self, painter, rect):
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

    def _fmt(self, v):
        if isinstance(v, float):
            return f"{v:.2f}"
        if isinstance(v, int):
            return str(v)
        return str(v)[:12]

    # ==================== 柱状 ====================

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
            grad = QLinearGradient(x, y, x, rect.bottom())
            c = self._color(i)
            grad.setColorAt(0, c)
            grad.setColorAt(1, c.darker(150))
            painter.setBrush(QBrush(grad))
            painter.setPen(QPen(c.darker(120), 1))
            painter.drawRect(QRectF(x, y, bar_w, h))

            painter.setPen(self.foreground)
            text = self._fmt(v)
            tw = fm.horizontalAdvance(text)
            painter.drawText(QPointF(x + bar_w / 2 - tw / 2, y - 4), text)

            label = str(self.labels[i])[:8] if i < len(self.labels) else ''
            tw = fm.horizontalAdvance(label)
            painter.drawText(
                QRectF(x - 10, rect.bottom() + 4, bar_w + 20, 60),
                Qt.AlignHCenter | Qt.TextWordWrap, label)

    # ==================== 折线/面积 ====================

    def _draw_line(self, painter, rect, area=False):
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

        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())

        # 面积
        if area:
            path = QPainterPath()
            path.moveTo(points[0].x(), rect.bottom())
            for p in points:
                path.lineTo(p.x(), p.y())
            path.lineTo(points[-1].x(), rect.bottom())
            path.closeSubpath()
            grad = QLinearGradient(0, rect.top(), 0, rect.bottom())
            c = self._color(0)
            grad.setColorAt(0, QColor(c.red(), c.green(), c.blue(), 180))
            grad.setColorAt(1, QColor(c.red(), c.green(), c.blue(), 20))
            painter.setBrush(QBrush(grad))
            painter.setPen(Qt.NoPen)
            painter.drawPath(path)

        # 折线
        painter.setPen(QPen(self._color(0), 2))
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])

        # 数据点
        for i, p in enumerate(points):
            painter.setBrush(QBrush(self._color(0)))
            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.drawEllipse(p, 4, 4)

            if n <= 30:
                painter.setPen(self.foreground)
                text = self._fmt(self.values[i])
                tw = fm.horizontalAdvance(text)
                painter.drawText(
                    QPointF(p.x() - tw / 2, p.y() - 8), text)

        # X 轴标签
        painter.setPen(self.foreground)
        skip = max(1, n // 10)
        for i in range(0, n, skip):
            label = str(self.labels[i])[:8] if i < len(self.labels) else ''
            painter.drawText(
                QRectF(points[i].x() - 30, rect.bottom() + 4, 60, 60),
                Qt.AlignHCenter | Qt.TextWordWrap, label)

    # ==================== 饼/环 ====================

    def _draw_pie(self, painter, rect, donut=False):
        total = sum(abs(v) for v in self.values) or 1
        cx = rect.center().x()
        cy = rect.center().y()
        radius = min(rect.width(), rect.height()) * 0.4
        inner = radius * 0.5 if donut else 0

        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())

        start_angle = 0
        for i, v in enumerate(self.values):
            span = int(abs(v) / total * 360 * 16)
            c = self._color(i)
            painter.setBrush(QBrush(c))
            painter.setPen(QPen(self.background, 2))
            outer_rect = QRectF(cx - radius, cy - radius,
                                radius * 2, radius * 2)

            if donut:
                path = QPainterPath()
                path.arcMoveTo(outer_rect, start_angle / 16)
                path.arcTo(outer_rect, start_angle / 16, span / 16)
                inner_rect = QRectF(cx - inner, cy - inner,
                                    inner * 2, inner * 2)
                path.arcTo(inner_rect, (start_angle + span) / 16,
                           -span / 16)
                path.closeSubpath()
                painter.drawPath(path)
            else:
                painter.drawPie(outer_rect, start_angle, span)

            # 百分比
            if span > 0:
                mid = (start_angle + span / 2) / 16 * math.pi / 180
                r = (radius + inner) / 2
                tx = cx + r * math.cos(mid)
                ty = cy + r * math.sin(mid)
                pct = abs(v) / total * 100
                text = f"{pct:.1f}%"
                tw = fm.horizontalAdvance(text)
                painter.setPen(QColor("#ffffff"))
                painter.drawText(QPointF(tx - tw / 2, ty + 4), text)

            start_angle += span

        # 图例
        legend_x = rect.right() - 150
        legend_y = rect.top() + 10
        for i, (label, v) in enumerate(zip(self.labels, self.values)):
            if i > 15:
                break
            c = self._color(i)
            painter.setBrush(QBrush(c))
            painter.setPen(QPen(Qt.NoPen))
            painter.drawRect(QRectF(legend_x, legend_y + i * 18, 12, 12))
            painter.setPen(self.foreground)
            painter.drawText(
                QPointF(legend_x + 18, legend_y + i * 18 + 11),
                f"{str(label)[:12]}: {self._fmt(v)}")

    # ==================== 雷达图 ====================

    def _draw_radar(self, painter, rect):
        n = len(self.values)
        if n < 3:
            painter.setPen(self.foreground)
            painter.drawText(rect, Qt.AlignCenter,
                             "雷达图至少需要 3 个数据点")
            return

        cx = rect.center().x()
        cy = rect.center().y()
        radius = min(rect.width(), rect.height()) * 0.4
        max_v = max(abs(v) for v in self.values) or 1

        # 网格
        painter.setPen(QPen(self.grid_color, 1, Qt.DashLine))
        for level in range(1, 6):
            r = radius * level / 5
            points = []
            for i in range(n):
                angle = -math.pi / 2 + 2 * math.pi * i / n
                x = cx + r * math.cos(angle)
                y = cy + r * math.sin(angle)
                points.append(QPointF(x, y))
            for i in range(n):
                painter.drawLine(points[i], points[(i + 1) % n])

        # 轴
        for i in range(n):
            angle = -math.pi / 2 + 2 * math.pi * i / n
            x = cx + radius * math.cos(angle)
            y = cy + radius * math.sin(angle)
            painter.setPen(QPen(self.grid_color, 1))
            painter.drawLine(QPointF(cx, cy), QPointF(x, y))

        # 数据多边形
        path = QPainterPath()
        points = []
        for i, v in enumerate(self.values):
            angle = -math.pi / 2 + 2 * math.pi * i / n
            r = abs(v) / max_v * radius
            x = cx + r * math.cos(angle)
            y = cy + r * math.sin(angle)
            points.append(QPointF(x, y))

        path.moveTo(points[0])
        for p in points[1:]:
            path.lineTo(p)
        path.closeSubpath()

        c = self._color(0)
        fill = QColor(c)
        fill.setAlpha(100)
        painter.setBrush(QBrush(fill))
        painter.setPen(QPen(c, 2))
        painter.drawPath(path)

        # 顶点
        for p in points:
            painter.setBrush(QBrush(c))
            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.drawEllipse(p, 3, 3)

        # 标签
        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())
        painter.setPen(self.foreground)
        for i, label in enumerate(self.labels):
            angle = -math.pi / 2 + 2 * math.pi * i / n
            x = cx + (radius + 25) * math.cos(angle)
            y = cy + (radius + 25) * math.sin(angle)
            text = str(label)[:8]
            tw = fm.horizontalAdvance(text)
            painter.drawText(QPointF(x - tw / 2, y + 4), text)

    # ==================== 热力图 ====================

    def _draw_heatmap(self, painter, rect):
        if not self.matrix:
            painter.setPen(self.foreground)
            painter.drawText(rect, Qt.AlignCenter, "热力图需要矩阵数据")
            return

        rows = len(self.matrix)
        cols = len(self.matrix[0]) if self.matrix else 0
        if rows == 0 or cols == 0:
            return

        cell_w = rect.width() / cols
        cell_h = rect.height() / rows

        flat = [v for row in self.matrix for v in row
                if isinstance(v, (int, float))]
        max_v = max(flat) if flat else 1
        min_v = min(flat) if flat else 0
        span = max_v - min_v or 1

        painter.setFont(QFont("Consolas", 8))
        fm = QFontMetrics(painter.font())

        for r in range(rows):
            for c in range(cols):
                v = self.matrix[r][c] if c < len(self.matrix[r]) else 0
                if not isinstance(v, (int, float)):
                    v = 0
                ratio = (v - min_v) / span
                red = int(255 * ratio)
                blue = int(255 * (1 - ratio))
                color = QColor(red, 80, blue)
                painter.setBrush(QBrush(color))
                painter.setPen(QPen(self.background, 1))
                x = rect.left() + c * cell_w
                y = rect.top() + r * cell_h
                painter.drawRect(QRectF(x, y, cell_w, cell_h))

                if cell_w > 30 and cell_h > 20:
                    text = self._fmt(v)
                    tw = fm.horizontalAdvance(text)
                    painter.setPen(QColor("#ffffff"))
                    painter.drawText(
                        QPointF(x + cell_w / 2 - tw / 2,
                                y + cell_h / 2 + 4), text)

        # 行标签
        painter.setPen(self.foreground)
        for r in range(rows):
            if r < len(self.labels):
                y = rect.top() + r * cell_h + cell_h / 2 + 4
                painter.drawText(
                    QRectF(rect.left() - 65, y - 10, 60, 20),
                    Qt.AlignRight | Qt.AlignVCenter,
                    str(self.labels[r])[:8])

    # ==================== 堆叠柱状 ====================

    def _draw_stacked_bar(self, painter, rect):
        if not self.series:
            self._draw_bar(painter, rect)
            return

        self._draw_axes(painter, rect)

        n_groups = len(self.series[0]) if self.series else 0
        if n_groups == 0:
            return

        group_totals = []
        for g in range(n_groups):
            total = sum(s[g] for s in self.series if g < len(s))
            group_totals.append(total)
        max_v = max(group_totals) or 1

        bar_w = rect.width() / n_groups * 0.7
        gap = rect.width() / n_groups * 0.3

        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())

        for g in range(n_groups):
            x = rect.left() + g * (bar_w + gap) + gap / 2
            base_y = rect.bottom()
            for si, series in enumerate(self.series):
                if g >= len(series):
                    continue
                v = series[g]
                h = v / max_v * rect.height() * 0.9
                y = base_y - h
                c = self._color(si)
                painter.setBrush(QBrush(c))
                painter.setPen(QPen(c.darker(120), 1))
                painter.drawRect(QRectF(x, y, bar_w, h))
                base_y = y

            painter.setPen(self.foreground)
            text = self._fmt(group_totals[g])
            tw = fm.horizontalAdvance(text)
            painter.drawText(
                QPointF(x + bar_w / 2 - tw / 2, base_y - 4), text)

            label = str(self.labels[g])[:8] if g < len(self.labels) else ''
            painter.drawText(
                QRectF(x - 10, rect.bottom() + 4, bar_w + 20, 60),
                Qt.AlignHCenter | Qt.TextWordWrap, label)

    # ==================== 气泡图 ====================

    def _draw_bubble(self, painter, rect):
        self._draw_axes(painter, rect)
        if not self.values:
            return

        points = []
        for item in self.values:
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                points.append(item)
            else:
                points.append((len(points), item, 1))

        if not points:
            return

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        sizes = [p[2] for p in points]

        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        max_s = max(sizes) or 1
        span_x = max_x - min_x or 1
        span_y = max_y - min_y or 1

        for i, (x, y, s) in enumerate(points):
            px = rect.left() + (x - min_x) / span_x * rect.width()
            py = rect.bottom() - (y - min_y) / span_y * rect.height() * 0.9
            r = 5 + (s / max_s) * 30
            c = self._color(i % 10)
            fill = QColor(c)
            fill.setAlpha(150)
            painter.setBrush(QBrush(fill))
            painter.setPen(QPen(c, 2))
            painter.drawEllipse(QPointF(px, py), r, r)

    # ==================== 散点 ====================

    def _draw_scatter(self, painter, rect):
        self._draw_axes(painter, rect)
        n = len(self.values)
        if n == 0:
            return
        max_v = max(self.values) or 1
        max_l = max(range(n)) or 1
        painter.setFont(QFont("Microsoft YaHei", 8))

        for i, v in enumerate(self.values):
            x = rect.left() + rect.width() * i / max_l
            y = rect.bottom() - v / max_v * rect.height() * 0.9
            c = self._color(i % 10)
            painter.setBrush(QBrush(c))
            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.drawEllipse(QPointF(x, y), 5, 5)
            painter.setPen(self.foreground)
            painter.drawText(QPointF(x + 8, y - 4), self._fmt(v))