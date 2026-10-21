# -*- coding: utf-8 -*-
"""数据图表可视化（P2-2：可配置化 + 多 Y 系列聚合 + PNG/SVG 导出 + 配置持久化）

组件：
- `ChartConfig` / `parse_chart_config`：图表配置（x / 多个 y / 聚合 / 类型 / 标题 /
  行数上限），可从旧配置（单 y 字符串、中文类型名）容错解析；
- `build_chart_data`：纯函数，按 X 分组 + 聚合（SUM/AVG/COUNT/MIN/MAX）生成
  多系列数据；无有效数值列/无数据时抛 `ChartConfigError`（显式提示）；
- `ChartWidget`：多系列绘制（bar/line/pie/scatter），保留旧 `set_data()` API；
- `export_chart_png/svg`：导出并**校验文件可解析**（PNG 用 QImage，SVG 检查 <svg）；
- `ChartDialog`：X 轴/多 Y/聚合/类型/标题实时重绘，PNG/SVG 导出，
  配置 save/load（JSON）；`config()` 可直接写入报表模板 `chart` 字段。
"""
import json
import math
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QComboBox, QPushButton,
    QLabel, QFileDialog, QMessageBox, QSpinBox, QLineEdit,
    QListWidget, QListWidgetItem, QGroupBox, QWidget,
)
from PySide6.QtCore import Qt, QRectF, QPoint, QPointF, QSize, QRect, Signal
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QFontMetrics, QImage,
)

CHART_TYPES = ('bar', 'line', 'pie', 'scatter')
AGGREGATIONS = ('SUM', 'AVG', 'COUNT', 'MIN', 'MAX')
_CHART_TYPE_LABELS = {'柱状图': 'bar', '折线图': 'line', '饼图': 'pie',
                      '散点图': 'scatter'}
_AGG_LABELS = {'求和': 'SUM', '平均': 'AVG', '计数': 'COUNT',
               '最小值': 'MIN', '最大值': 'MAX'}


class ChartError(ValueError):
    """图表/导出异常基类。"""


class ChartConfigError(ChartError):
    """图表配置无效（无数据 / 无数值列 / 列不存在等）。"""


# ==================== 数值识别 / 聚合（纯函数，可单测） ====================

def is_numeric_value(value) -> bool:
    """int/float（排除 bool），或可转成 float 的字符串。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return False
        try:
            float(s)
            return True
        except ValueError:
            return False
    return False


def to_number(value):
    """转成 float；NULL/不可转 → None（bool 不当数值）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def numeric_columns(columns, rows, sample=50) -> list:
    """返回数值列下标列表（任意样本值可转数值且非 NULL）。"""
    indexes = []
    for ci in range(len(columns or [])):
        for r in (rows or [])[:sample]:
            if ci < len(r) and is_numeric_value(r[ci]):
                indexes.append(ci)
                break
    return indexes


def parse_chart_config(raw=None) -> dict:
    """把任意（含旧格式）配置规范化为 dict：x/y/agg/type/title/limit。"""
    if raw is None:
        raw = {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    x = raw.get('x') or ''
    y = raw.get('y')
    if y is None:
        y = raw.get('y_columns') or raw.get('values')
    if isinstance(y, str):
        y = [y]
    elif y is None:
        y = []
    else:
        try:
            y = [str(v) for v in list(y)]
        except TypeError:
            y = []
    agg = str(raw.get('agg') or raw.get('aggregate') or 'SUM').upper()
    agg = _AGG_LABELS.get(raw.get('agg'), agg)
    if agg not in AGGREGATIONS:
        agg = 'SUM'
    ctype = raw.get('type') or raw.get('chart_type') or 'bar'
    ctype = _CHART_TYPE_LABELS.get(ctype, ctype)
    if ctype not in CHART_TYPES:
        ctype = 'bar'
    try:
        limit = int(raw.get('limit') or raw.get('rows') or 0)
    except (TypeError, ValueError):
        limit = 0
    return {
        'x': str(x or ''),
        'y': [str(v) for v in y if str(v)],
        'agg': agg,
        'type': ctype,
        'title': str(raw.get('title') or ''),
        'limit': max(0, limit),
    }


class ChartConfig:
    """图表配置（可 save/load JSON，可直接塞进报表模板 chart 字段）。"""

    def __init__(self, x='', y=None, agg='SUM', chart_type='bar',
                 title='', limit=0):
        self.x = str(x or '')
        self.y = [str(v) for v in (y or [])]
        self.agg = str(agg or 'SUM').upper()
        self.chart_type = str(chart_type or 'bar')
        self.title = str(title or '')
        self.limit = int(limit or 0)

    def to_dict(self) -> dict:
        return parse_chart_config({
            'x': self.x, 'y': list(self.y), 'agg': self.agg,
            'type': self.chart_type, 'title': self.title,
            'limit': self.limit})

    @classmethod
    def from_dict(cls, raw) -> 'ChartConfig':
        cfg = parse_chart_config(raw)
        return cls(x=cfg['x'], y=cfg['y'], agg=cfg['agg'],
                   chart_type=cfg['type'], title=cfg['title'],
                   limit=cfg['limit'])

    def save(self, path):
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding='utf-8')
        return str(path)

    @classmethod
    def load(cls, path) -> 'ChartConfig':
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
        return cls.from_dict(raw)

    def __repr__(self):
        return (f"ChartConfig(x={self.x!r}, y={self.y!r}, agg={self.agg!r}, "
                f"type={self.chart_type!r})")


def aggregate_group(values, agg) -> float:
    """对一组（可能含 None/NULL）数值做聚合；无有效值返回 None。"""
    nums = [v for v in values if v is not None]
    if agg == 'COUNT':
        return float(len(nums))
    if not nums:
        return None
    if agg == 'SUM':
        return float(sum(nums))
    if agg == 'AVG':
        return float(sum(nums) / len(nums))
    if agg == 'MIN':
        return float(min(nums))
    if agg == 'MAX':
        return float(max(nums))
    return float(sum(nums))


def build_chart_data(columns, rows, config=None) -> dict:
    """按配置生成图表数据（labels + 多 series）。

    返回 {'labels': [...], 'series': [{'name','values'}...], 'config': {...}}
    抛出 `ChartConfigError`：无数据 / X 列不存在 / 没有有效数值列 / Y 列不存在。
    """
    columns = list(columns or [])
    rows = list(rows or [])
    cfg = parse_chart_config(config)
    if not rows:
        raise ChartConfigError('没有数据可绘图')
    if not columns:
        raise ChartConfigError('没有列信息')
    num_idx = numeric_columns(columns, rows)
    if not num_idx:
        raise ChartConfigError('没有可用的数值列（Y 轴需要数值）')

    # X 轴：配置指定的列，否则第一列非数值列
    x_name = cfg['x']
    if x_name and x_name in columns:
        x_idx = columns.index(x_name)
    else:
        x_idx = 0
        if x_idx in num_idx and len(columns) > 1:
            x_idx = next((i for i in range(len(columns))
                          if i not in num_idx), 0)
    x_name = columns[x_idx]

    # Y 轴：未指定则默认第一个数值列；指定了但不存在则报错
    y_names = [n for n in cfg['y'] if n in columns]
    if cfg['y'] and len(y_names) != len(cfg['y']):
        bad = [n for n in cfg['y'] if n not in columns]
        raise ChartConfigError('Y 轴列不存在: ' + ', '.join(bad))
    if not y_names:
        y_names = [columns[num_idx[0]]]
    for name in y_names:
        if name not in columns:
            raise ChartConfigError(f'Y 轴列不存在: {name}')
        if columns.index(name) not in num_idx:
            raise ChartConfigError(f'Y 轴必须是数值列: {name}')

    limit = cfg['limit']
    data = rows[:limit] if limit > 0 else rows
    labels = []
    seen = {}
    series_values = {name: [] for name in y_names}
    for row in data:
        xv = row[x_idx] if x_idx < len(row) else None
        label = '(NULL)' if xv is None else str(xv)
        if label not in seen:
            seen[label] = len(labels)
            labels.append(label)
            for name in y_names:
                series_values[name].append([])
        pos = seen[label]
        for name in y_names:
            ci = columns.index(name)
            val = to_number(row[ci]) if ci < len(row) else None
            if val is not None:
                series_values[name][pos].append(val)

    agg = cfg['agg']
    series = []
    for name in y_names:
        agg_values = [aggregate_group(vals, agg)
                      for vals in series_values[name]]
        series.append({'name': name, 'values': agg_values})
    cfg['x'] = x_name
    cfg['y'] = y_names
    if not labels:
        raise ChartConfigError('没有可绘制的数据点')
    return {'labels': labels, 'series': series, 'config': cfg}


# ==================== 导出（PNG / SVG，带文件校验） ====================

def export_chart_png(widget, filepath) -> str:
    """渲染 widget 为 PNG 并校验文件可被 QImage 解析。"""
    path = Path(filepath)
    pixmap = widget.grab()
    if pixmap.isNull() or pixmap.width() <= 0:
        raise ChartError('图表渲染失败（空图像）')
    if not pixmap.save(str(path), 'PNG'):
        raise ChartError(f'PNG 保存失败: {path}')
    if not path.exists() or path.stat().st_size <= 0:
        raise ChartError(f'PNG 文件为空: {path}')
    img = QImage(str(path))
    if img.isNull() or img.width() <= 0 or img.height() <= 0:
        raise ChartError(f'PNG 文件无法解析: {path}')
    return str(path)


def export_chart_svg(widget, filepath, width=1200, height=720) -> str:
    """用 QSvgGenerator 渲染 SVG，并校验文件包含 <svg 且非空。"""
    path = Path(filepath)
    from PySide6.QtSvg import QSvgGenerator
    old_size = widget.size()
    target = QSize(max(int(width or 800), 200), max(int(height or 480), 150))
    gen = QSvgGenerator()
    gen.setFileName(str(path))
    gen.setSize(target)
    gen.setViewBox(QRect(0, 0, target.width(), target.height()))
    gen.setTitle('LightDB Chart')
    painter = QPainter()
    try:
        if not painter.begin(gen):
            raise ChartError('SVG 渲染器初始化失败')
        try:
            widget.resize(target)
            widget.render(painter, QPoint(0, 0))
        finally:
            painter.end()
    finally:
        try:
            widget.resize(old_size)
        except Exception:
            pass
    if not path.exists() or path.stat().st_size <= 0:
        raise ChartError(f'SVG 文件为空: {path}')
    text = path.read_text(encoding='utf-8', errors='replace')
    if '<svg' not in text.lower() or len(text) < 200:
        raise ChartError(f'SVG 文件无法解析: {path}')
    return str(path)


# ==================== 绘制组件 ====================

class ChartWidget(QWidget):
    """多系列图表绘制（保留旧 set_data 单系列 API）。"""

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
        self.series = []          # [{'name': str, 'values': [float|None]}]
        self.values = []          # 兼容旧属性（单系列）
        self.chart_type = self.CHART_BAR
        self.title = ""
        self.background = QColor("#1e1e1e")
        self.foreground = QColor("#d4d4d4")
        self.grid_color = QColor("#3c3c3c")
        self.setMinimumSize(400, 300)

    # ---------- 数据接口 ----------

    def set_data(self, labels, values, title="", chart_type='bar'):
        """旧 API：单系列。"""
        self.set_series(labels, [{'name': title or 'values',
                                  'values': list(values or [])}],
                        title, chart_type)

    def set_series(self, labels, series, title="", chart_type='bar'):
        self.labels = list(labels or [])
        self.series = []
        for idx, s in enumerate(series or []):
            if isinstance(s, dict):
                name = str(s.get('name') or f'series{idx + 1}')
                vals = list(s.get('values') or [])
            else:
                name = f'series{idx + 1}'
                vals = list(s or [])
            self.series.append({'name': name,
                                'values': [to_number(v) for v in vals]})
        self.values = list(self.series[0]['values']) if self.series else []
        self.title = title or ''
        self.chart_type = chart_type or 'bar'
        self.update()

    def set_theme(self, bg, fg, grid):
        self.background = QColor(bg)
        self.foreground = QColor(fg)
        self.grid_color = QColor(grid)
        self.update()

    def has_data(self) -> bool:
        return bool(self.labels and self.series and any(
            v is not None for s in self.series for v in s['values']))

    # ---------- 绘制 ----------

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.fillRect(self.rect(), self.background)
            if not self.has_data():
                painter.setPen(self.foreground)
                painter.drawText(self.rect(), Qt.AlignCenter,
                                 getattr(self, 'empty_text', '无数据'))
                return
            margin_left, margin_right = 70, 30
            margin_top, margin_bottom = 50, 80
            if self.title:
                painter.setPen(self.foreground)
                painter.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
                painter.drawText(QRectF(0, 10, self.width(), 30),
                                 Qt.AlignCenter, self.title)
            plot_rect = QRectF(
                margin_left, margin_top,
                max(10, self.width() - margin_left - margin_right),
                max(10, self.height() - margin_top - margin_bottom))
            if self.chart_type == self.CHART_PIE:
                self._draw_pie(painter, plot_rect)
            elif self.chart_type == self.CHART_LINE:
                self._draw_line(painter, plot_rect)
            elif self.chart_type == self.CHART_SCATTER:
                self._draw_scatter(painter, plot_rect)
            else:
                self._draw_bar(painter, plot_rect)
            if len(self.series) > 1 or self.chart_type == self.CHART_PIE:
                self._draw_legend(painter)
        finally:
            painter.end()

    def _all_values(self):
        vals = []
        for s in self.series:
            vals.extend(v for v in s['values'] if v is not None)
        return vals

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

    def _draw_bar(self, painter, rect):
        self._draw_axes(painter, rect)
        n = len(self.labels)
        if n == 0:
            return
        all_vals = self._all_values()
        max_v = max([abs(v) for v in all_vals] + [1])
        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())
        group_w = rect.width() / n
        ns = max(1, len(self.series))
        bar_w = group_w * 0.7 / ns
        for si, series in enumerate(self.series):
            color = QColor(self.COLORS[si % len(self.COLORS)])
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(Qt.NoPen))
            for i, v in enumerate(series['values']):
                if i >= n or v is None:
                    continue
                h = abs(v) / max_v * rect.height() * 0.9
                x = rect.left() + i * group_w + group_w * 0.15 + si * bar_w
                y = rect.bottom() - h
                painter.drawRect(QRectF(x, y, bar_w * 0.9, h))
        # X 标签
        painter.setPen(self.foreground)
        for i, label in enumerate(self.labels):
            text = str(label)
            if len(text) > 8:
                text = text[:7] + '…'
            tw = fm.horizontalAdvance(text)
            painter.drawText(
                QRectF(rect.left() + i * group_w, rect.bottom() + 4,
                       group_w, 60),
                Qt.AlignHCenter | Qt.TextWordWrap, text)

    def _draw_line(self, painter, rect):
        self._draw_axes(painter, rect)
        n = len(self.labels)
        if n == 0:
            return
        all_vals = self._all_values()
        if not all_vals:
            return
        max_v = max(all_vals)
        min_v = min(all_vals)
        span = (max_v - min_v) or 1
        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())
        for si, series in enumerate(self.series):
            color = QColor(self.COLORS[si % len(self.COLORS)])
            painter.setPen(QPen(color, 2))
            points = []
            for i, v in enumerate(series['values']):
                if i >= n or v is None:
                    points.append(None)
                    continue
                x = rect.left() + (rect.width() * i / (n - 1)
                                   if n > 1 else rect.width() / 2)
                y = rect.bottom() - (v - min_v) / span * rect.height() * 0.9
                points.append(QPointF(x, y))
            last = None
            for p in points:
                if p is None:
                    continue
                if last is not None:
                    painter.drawLine(last, p)
                last = p
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(QColor('#ffffff'), 1))
            for p in points:
                if p is not None:
                    painter.drawEllipse(p, 3, 3)
        painter.setPen(self.foreground)
        for i, label in enumerate(self.labels):
            text = str(label)
            if len(text) > 8:
                text = text[:7] + '…'
            x = rect.left() + (rect.width() * i / (n - 1)
                               if n > 1 else rect.width() / 2)
            painter.drawText(
                QRectF(x - 40, rect.bottom() + 4, 80, 60),
                Qt.AlignHCenter | Qt.TextWordWrap, text)

    def _draw_pie(self, painter, rect):
        # 多系列时按 label 求和合并（饼图需要单一占比）
        combined = []
        for i in range(len(self.labels)):
            total = 0.0
            for s in self.series:
                v = s['values'][i] if i < len(s['values']) else None
                if v is not None:
                    total += abs(v)
            combined.append(total)
        total = sum(combined) or 1
        cx = rect.center().x()
        cy = rect.center().y()
        radius = min(rect.width(), rect.height()) * 0.4
        start_angle = 0
        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())
        for i, v in enumerate(combined):
            span = int(v / total * 360 * 16)
            color = QColor(self.COLORS[i % len(self.COLORS)])
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(self.background, 2))
            painter.drawPie(
                QRectF(cx - radius, cy - radius, radius * 2, radius * 2),
                start_angle, span)
            mid = (start_angle + span / 2) / 16 * math.pi / 180
            tx = cx + radius * 0.7 * math.cos(mid)
            ty = cy + radius * 0.7 * math.sin(mid)
            painter.setPen(QColor('#ffffff'))
            pct = v / total * 100
            text = f'{pct:.1f}%'
            painter.drawText(
                QPointF(tx - fm.horizontalAdvance(text) / 2, ty + 4), text)
            start_angle += span

    def _draw_scatter(self, painter, rect):
        self._draw_axes(painter, rect)
        n = len(self.labels)
        if n == 0:
            return
        all_vals = self._all_values()
        max_v = max([abs(v) for v in all_vals] + [1])
        painter.setFont(QFont("Microsoft YaHei", 8))
        for si, series in enumerate(self.series):
            color = QColor(self.COLORS[si % len(self.COLORS)])
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(QColor('#ffffff'), 1))
            for i, v in enumerate(series['values']):
                if i >= n or v is None:
                    continue
                x = rect.left() + rect.width() * i / max(1, n - 1)
                y = rect.bottom() - abs(v) / max_v * rect.height() * 0.9
                painter.drawEllipse(QPointF(x, y), 5, 5)
        painter.setPen(self.foreground)
        fm = QFontMetrics(painter.font())
        for i, label in enumerate(self.labels):
            text = str(label)
            if len(text) > 8:
                text = text[:7] + '…'
            x = rect.left() + rect.width() * i / max(1, n - 1)
            painter.drawText(
                QRectF(x - 40, rect.bottom() + 4, 80, 60),
                Qt.AlignHCenter | Qt.TextWordWrap, text)

    def _draw_legend(self, painter):
        painter.setFont(QFont("Microsoft YaHei", 8))
        fm = QFontMetrics(painter.font())
        x = 12
        y = self.height() - 24
        if self.chart_type == self.CHART_PIE:
            entries = list(zip(self.labels, self.COLORS))
            for i, (label, color_name) in enumerate(entries[:8]):
                if i > 0:
                    x += 130
                color = QColor(color_name)
                painter.setBrush(QBrush(color))
                painter.setPen(QPen(Qt.NoPen))
                painter.drawRect(QRectF(x, y - 9, 10, 10))
                painter.setPen(self.foreground)
                painter.drawText(QPointF(x + 14, y), str(label)[:10])
            return
        for i, series in enumerate(self.series[:8]):
            color = QColor(self.COLORS[i % len(self.COLORS)])
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(Qt.NoPen))
            painter.drawRect(QRectF(x, y - 9, 10, 10))
            painter.setPen(self.foreground)
            painter.drawText(QPointF(x + 14, y), str(series['name'])[:14])
            x += 130

    def _fmt(self, v):
        if isinstance(v, float):
            return f'{v:.2f}'
        return str(v)[:12]


# ==================== 图表对话框（P2-2） ====================

class ChartDialog(QDialog):
    """可配置图表：X / 多 Y / 聚合 / 类型 / 标题 + PNG/SVG 导出 + 配置持久化。"""

    config_changed = Signal(dict)

    def __init__(self, columns, rows, parent=None):
        super().__init__(parent)
        self.columns = list(columns or [])
        self.rows = list(rows or [])
        self._last_error = ''
        self._last_data = None
        self.setWindowTitle('数据图表')
        self.resize(980, 700)
        self._build_ui()
        self.refresh()

    # ---------- UI ----------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel('图表类型:'))
        self.type_combo = QComboBox()
        for label, code in (('柱状图', 'bar'), ('折线图', 'line'),
                            ('饼图', 'pie'), ('散点图', 'scatter')):
            self.type_combo.addItem(label, code)
        self.type_combo.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.type_combo)

        top.addWidget(QLabel('X 轴:'))
        self.x_combo = QComboBox()
        self.x_combo.addItems(self.columns)
        self.x_combo.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.x_combo)

        top.addWidget(QLabel('聚合:'))
        self.agg_combo = QComboBox()
        for agg in AGGREGATIONS:
            self.agg_combo.addItem(agg, agg)
        self.agg_combo.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.agg_combo)

        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(0, 5000)
        self.limit_spin.setValue(20)
        self.limit_spin.setPrefix('前 ')
        self.limit_spin.setSuffix(' 行')
        self.limit_spin.valueChanged.connect(self.refresh)
        top.addWidget(self.limit_spin)
        top.addStretch()
        layout.addLayout(top)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel('标题:'))
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText('图表标题（可选）')
        self.title_edit.textChanged.connect(self.refresh)
        title_row.addWidget(self.title_edit)
        layout.addLayout(title_row)

        y_group = QGroupBox('Y 轴（数值列，可多选）')
        y_layout = QHBoxLayout(y_group)
        self.y_list = QListWidget()
        self.y_list.setMaximumHeight(92)
        self.y_list.itemChanged.connect(self.refresh)
        y_layout.addWidget(self.y_list)
        layout.addWidget(y_group)
        self._populate_columns()

        self.chart = ChartWidget()
        layout.addWidget(self.chart, 1)

        bottom = QHBoxLayout()
        self.info_label = QLabel('')
        self.info_label.setStyleSheet(
            'color: #969696; padding: 4px; background: #252526;')
        bottom.addWidget(self.info_label, 1)
        self.png_btn = QPushButton('🖼 导出 PNG')
        self.png_btn.clicked.connect(self._on_export_png)
        bottom.addWidget(self.png_btn)
        self.svg_btn = QPushButton('🖊 导出 SVG')
        self.svg_btn.clicked.connect(self._on_export_svg)
        bottom.addWidget(self.svg_btn)
        self.save_cfg_btn = QPushButton('💾 保存配置')
        self.save_cfg_btn.clicked.connect(self._on_save_config)
        bottom.addWidget(self.save_cfg_btn)
        self.load_cfg_btn = QPushButton('📂 加载配置')
        self.load_cfg_btn.clicked.connect(self._on_load_config)
        bottom.addWidget(self.load_cfg_btn)
        layout.addLayout(bottom)

    def _populate_columns(self):
        self.y_list.blockSignals(True)
        self.y_list.clear()
        nums = set(numeric_columns(self.columns, self.rows))
        first_checked = False
        for ci, name in enumerate(self.columns):
            item = QListWidgetItem(str(name))
            item.setData(Qt.UserRole, str(name))
            if ci in nums:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                check = not first_checked
                item.setCheckState(Qt.Checked if check else Qt.Unchecked)
                if check:
                    first_checked = True
            else:
                item.setFlags(Qt.NoItemFlags)
                item.setToolTip('非数值列，不能作为 Y 轴')
            self.y_list.addItem(item)
        self.y_list.blockSignals(False)

    # ---------- 配置 ----------

    def config(self) -> dict:
        y = []
        for i in range(self.y_list.count()):
            item = self.y_list.item(i)
            if item.checkState() == Qt.Checked:
                y.append(item.data(Qt.UserRole))
        return parse_chart_config({
            'x': self.x_combo.currentText(),
            'y': y,
            'agg': self.agg_combo.currentData() or 'SUM',
            'type': self.type_combo.currentData() or 'bar',
            'title': self.title_edit.text(),
            'limit': self.limit_spin.value(),
        })

    def apply_config(self, raw) -> dict:
        """应用配置（兼容旧单 y / 中文类型名）；返回规范化后的配置。"""
        cfg = parse_chart_config(raw)
        self.blockSignals(True)
        try:
            idx = self.type_combo.findData(cfg['type'])
            if idx >= 0:
                self.type_combo.setCurrentIndex(idx)
            idx = self.agg_combo.findData(cfg['agg'])
            if idx >= 0:
                self.agg_combo.setCurrentIndex(idx)
            if cfg['x'] in self.columns:
                self.x_combo.setCurrentText(cfg['x'])
            self.title_edit.setText(cfg['title'])
            if cfg['limit']:
                self.limit_spin.setValue(cfg['limit'])
            wanted = set(cfg['y'])
            for i in range(self.y_list.count()):
                item = self.y_list.item(i)
                if not (item.flags() & Qt.ItemIsUserCheckable):
                    continue
                item.setCheckState(
                    Qt.Checked if item.data(Qt.UserRole) in wanted
                    else Qt.Unchecked)
        finally:
            self.blockSignals(False)
        self.refresh()
        return self.config()

    def set_data(self, columns, rows):
        self.columns = list(columns or [])
        self.rows = list(rows or [])
        self.blockSignals(True)
        try:
            self.x_combo.clear()
            self.x_combo.addItems(self.columns)
            self._populate_columns()
        finally:
            self.blockSignals(False)
        self.refresh()

    # ---------- 重绘 ----------

    def refresh(self) -> dict:
        """按当前控件重新计算并重绘；无有效数值列时显式提示。"""
        cfg = self.config()
        if not cfg['title']:
            cfg['title'] = f"{'+'.join(cfg['y']) or ''} by {cfg['x']}"
        try:
            data = build_chart_data(self.columns, self.rows, cfg)
        except ChartConfigError as exc:
            self._last_error = str(exc)
            self._last_data = None
            self.chart.empty_text = str(exc)
            self.chart.set_series([], [], cfg['title'], cfg['type'])
            self.info_label.setText(f'⚠ {exc}')
            self.info_label.setStyleSheet(
                'color: #f48771; padding: 4px; background: #252526;')
            self.png_btn.setEnabled(False)
            self.svg_btn.setEnabled(False)
            self._emit_config(cfg)
            return {'ok': False, 'error': str(exc), 'config': cfg}
        self._last_error = ''
        self._last_data = data
        self.chart.empty_text = '无数据'
        self.chart.set_series(data['labels'],
                              [{'name': s['name'], 'values': s['values']}
                               for s in data['series']],
                              cfg['title'], cfg['type'])
        total_points = sum(1 for s in data['series'] for v in s['values']
                           if v is not None)
        self.info_label.setStyleSheet(
            'color: #969696; padding: 4px; background: #252526;')
        self.info_label.setText(
            f"X={cfg['x']} | Y={', '.join(cfg['y'])} | 聚合={cfg['agg']} | "
            f"{len(data['labels'])} 个分类 / {total_points} 个数据点")
        self.png_btn.setEnabled(True)
        self.svg_btn.setEnabled(True)
        self._emit_config(cfg)
        return {'ok': True, 'error': '', 'config': cfg, 'data': data}

    def _emit_config(self, cfg):
        try:
            self.config_changed.emit(dict(cfg))
        except Exception:
            pass

    def last_error(self) -> str:
        return self._last_error

    # ---------- 导出 ----------

    def export_png(self, path=None, quiet=False) -> str:
        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self, '保存图表', 'chart.png', 'PNG 图片 (*.png)')
        if not path:
            return ''
        try:
            out = export_chart_png(self.chart, path)
        except ChartError as exc:
            if not quiet:
                QMessageBox.critical(self, '导出失败', str(exc))
            raise
        if not quiet:
            QMessageBox.information(self, '成功', f'已保存 PNG:\n{out}')
        return out

    def export_svg(self, path=None, quiet=False) -> str:
        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self, '保存图表', 'chart.svg', 'SVG 图片 (*.svg)')
        if not path:
            return ''
        try:
            out = export_chart_svg(self.chart, path)
        except ChartError as exc:
            if not quiet:
                QMessageBox.critical(self, '导出失败', str(exc))
            raise
        if not quiet:
            QMessageBox.information(self, '成功', f'已保存 SVG:\n{out}')
        return out

    def _on_export_png(self):
        try:
            self.export_png()
        except ChartError:
            pass

    def _on_export_svg(self):
        try:
            self.export_svg()
        except ChartError:
            pass

    # ---------- 配置持久化 ----------

    def save_config(self, path=None) -> str:
        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self, '保存图表配置', 'chart_config.json',
                'JSON (*.json)')
        if not path:
            return ''
        ChartConfig.from_dict(self.config()).save(path)
        return str(path)

    def load_config(self, path=None) -> dict:
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, '加载图表配置', '', 'JSON (*.json)')
        if not path:
            return {}
        cfg = ChartConfig.load(path).to_dict()
        return self.apply_config(cfg)

    def _on_save_config(self):
        path = self.save_config()
        if path:
            QMessageBox.information(self, '成功', f'配置已保存:\n{path}')

    def _on_load_config(self):
        cfg = self.load_config()
        if cfg:
            QMessageBox.information(
                self, '成功',
                f"已加载配置: X={cfg.get('x')} Y={cfg.get('y')}")
