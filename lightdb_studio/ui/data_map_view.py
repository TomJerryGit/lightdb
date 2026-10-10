"""数据地图视图"""
import math
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGraphicsView, QGraphicsScene,
    QGraphicsRectItem, QGraphicsTextItem, QGraphicsLineItem,
    QGraphicsEllipseItem, QComboBox, QPushButton, QLabel, QTabWidget,
    QTableWidget, QTableWidgetItem, QListWidget, QListWidgetItem,
    QFileDialog, QMessageBox, QSplitter
)
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QColor, QPen, QBrush, QFont, QPainter, QImage, QLinearGradient
)

from ..core.data_map import DataMap


class DataMapView(QWidget):
    """数据地图"""

    def __init__(self, catalog=None, lineage=None, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.lineage = lineage
        self.data_map = DataMap(catalog, lineage)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # 顶部工具栏
        top = QHBoxLayout()

        top.addWidget(QLabel("视图:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(
            ['业务域地图', '表级流向', '热度热力图'])
        self.mode_combo.currentIndexChanged.connect(self._redraw)
        top.addWidget(self.mode_combo)

        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self.refresh)
        top.addWidget(refresh_btn)

        export_btn = QPushButton("📤 导出 PNG")
        export_btn.clicked.connect(self._export_png)
        top.addWidget(export_btn)
        top.addStretch()
        layout.addLayout(top)

        # 图
        self.view = QGraphicsView()
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setBackgroundBrush(QBrush(QColor("#1e1e1e")))
        self.scene = QGraphicsScene()
        self.view.setScene(self.scene)
        layout.addWidget(self.view)

        # 信息栏
        self.info_label = QLabel("")
        self.info_label.setStyleSheet(
            "background: #252526; color: #969696; padding: 4px;")
        layout.addWidget(self.info_label)

    def refresh(self):
        self._redraw()

    def _redraw(self):
        mode = self.mode_combo.currentText()
        self.scene.clear()

        if mode == '业务域地图':
            self._draw_domain_map()
        elif mode == '表级流向':
            self._draw_flow()
        elif mode == '热度热力图':
            self._draw_heatmap()

    # ==================== 域地图 ====================

    def _draw_domain_map(self):
        data = self.data_map.build_domain_map()
        domains = data['domains']
        edges = data['edges']

        if not domains:
            self.info_label.setText("无数据")
            return

        # 环形布局
        n = len(domains)
        cx, cy = 500, 400
        radius = 250

        positions = {}
        for i, d in enumerate(domains):
            angle = 2 * math.pi * i / n - math.pi / 2
            x = cx + radius * math.cos(angle)
            y = cy + radius * math.sin(angle)
            positions[d['name']] = (x, y)

        # 绘制边
        for e in edges:
            if e['from_domain'] not in positions or \
               e['to_domain'] not in positions:
                continue
            x1, y1 = positions[e['from_domain']]
            x2, y2 = positions[e['to_domain']]

            line = QGraphicsLineItem(x1, y1, x2, y2)
            pen = QPen(QColor("#569cd6"), 1.5)
            pen.setStyle(Qt.DashLine)
            line.setPen(pen)
            self.scene.addItem(line)

        # 绘制节点
        for d in domains:
            x, y = positions[d['name']]
            w, h = 180, 90

            # 背景
            rect = QGraphicsRectItem(x - w/2, y - h/2, w, h)
            color = QColor(d['color'])
            fill = QColor(color)
            fill.setAlpha(60)
            rect.setBrush(QBrush(fill))
            rect.setPen(QPen(color, 2))
            self.scene.addItem(rect)

            # 名称
            text = QGraphicsTextItem(d['name'])
            text.setDefaultTextColor(color)
            text.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))
            text.setPos(x - 60, y - 30)
            self.scene.addItem(text)

            # 统计
            stats = f"表数 {d['table_count']}\n行数 {self._fmt(d['total_rows'])}\n查询 {d['query_count']}"
            stats_item = QGraphicsTextItem(stats)
            stats_item.setDefaultTextColor(QColor("#d4d4d4"))
            stats_item.setFont(QFont("Consolas", 9))
            stats_item.setPos(x - 60, y - 8)
            self.scene.addItem(stats_item)

        self.info_label.setText(
            f"共 {len(domains)} 个业务域，{len(edges)} 条域间流向")

    # ==================== 表级流向 ====================

    def _draw_flow(self):
        graph = self.data_map.build_flow_graph()
        nodes = graph['nodes']
        edges = graph['edges']

        if not nodes:
            self.info_label.setText("无血缘数据（先执行一些 SQL）")
            return

        # 分层布局（拓扑排序简化版）
        # 入度为 0 的表放左边
        from collections import defaultdict, deque
        in_degree = defaultdict(int)
        adj = defaultdict(list)
        for e in edges:
            in_degree[e['to']] += 1
            adj[e['from']].append(e['to'])

        # 拓扑 BFS
        levels = {}
        queue = deque()
        for n in nodes:
            if in_degree[n['name']] == 0:
                queue.append(n['name'])
                levels[n['name']] = 0
        while queue:
            n = queue.popleft()
            for nxt in adj[n]:
                if nxt not in levels:
                    levels[nxt] = levels[n] + 1
                    queue.append(nxt)

        # 未访问的放最后一层
        max_level = max(levels.values()) if levels else 0
        for n in nodes:
            if n['name'] not in levels:
                levels[n['name']] = max_level + 1

        # 按层分组
        by_level = defaultdict(list)
        for name, lvl in levels.items():
            by_level[lvl].append(name)

        # 布局
        positions = {}
        x_gap = 220
        y_gap = 90
        for lvl, names in by_level.items():
            x = 60 + lvl * x_gap
            for i, name in enumerate(names):
                y = 100 + i * y_gap
                positions[name] = (x, y)

        # 绘制边
        for e in edges:
            if e['from'] not in positions or e['to'] not in positions:
                continue
            x1, y1 = positions[e['from']]
            x2, y2 = positions[e['to']]

            # 从右边缘到左边缘
            line = QGraphicsLineItem(
                x1 + 160, y1 + 20, x2, y2 + 20)
            line.setPen(QPen(QColor("#569cd6"), 2))
            self.scene.addItem(line)

            # 箭头
            arrow = QGraphicsEllipseItem(x2 - 4, y2 + 16, 8, 8)
            arrow.setBrush(QBrush(QColor("#569cd6")))
            arrow.setPen(QPen(Qt.NoPen))
            self.scene.addItem(arrow)

        # 绘制节点
        for n in nodes:
            if n['name'] not in positions:
                continue
            x, y = positions[n['name']]
            w, h = 160, 40

            # 颜色按域
            domain_colors = {}
            color = QColor("#569cd6")

            rect = QGraphicsRectItem(x, y, w, h)
            rect.setBrush(QBrush(QColor("#252526")))
            rect.setPen(QPen(color, 2))
            self.scene.addItem(rect)

            # 名称
            text = QGraphicsTextItem(n['name'][:20])
            text.setDefaultTextColor(color)
            text.setFont(QFont("Consolas", 10, QFont.Bold))
            text.setPos(x + 8, y + 4)
            self.scene.addItem(text)

            # 行数
            if n['rows']:
                rows_text = QGraphicsTextItem(
                    f"{self._fmt(n['rows'])} 行")
                rows_text.setDefaultTextColor(QColor("#969696"))
                rows_text.setFont(QFont("Consolas", 8))
                rows_text.setPos(x + 8, y + 22)
                self.scene.addItem(rows_text)

        self.info_label.setText(
            f"共 {len(nodes)} 个节点，{len(edges)} 条血缘关系")

    # ==================== 热力图 ====================

    def _draw_heatmap(self):
        data = self.data_map.build_heatmap('query_count')
        items = data['data']
        max_value = data['max']

        if not items:
            self.info_label.setText("无数据")
            return

        # 网格布局
        cols = 10
        cell_w = 100
        cell_h = 50

        for i, item in enumerate(items[:100]):  # 最多显示 100 个
            r, c = divmod(i, cols)
            x = 60 + c * (cell_w + 5)
            y = 60 + r * (cell_h + 5)

            # 颜色强度
            ratio = item['value'] / max_value if max_value > 0 else 0
            red = int(255 * ratio)
            green = int(100 * ratio)
            blue = int(80 * (1 - ratio))

            rect = QGraphicsRectItem(x, y, cell_w, cell_h)
            rect.setBrush(QBrush(QColor(red, green, blue)))
            rect.setPen(QPen(QColor("#3c3c3c"), 1))
            self.scene.addItem(rect)

            # 表名
            text = QGraphicsTextItem(item['table'][:12])
            text.setDefaultTextColor(QColor("#ffffff"))
            text.setFont(QFont("Consolas", 9, QFont.Bold))
            text.setPos(x + 4, y + 4)
            self.scene.addItem(text)

            # 数值
            value_text = QGraphicsTextItem(str(item['value']))
            value_text.setDefaultTextColor(QColor("#ffffff"))
            value_text.setFont(QFont("Consolas", 10))
            value_text.setPos(x + 4, y + 24)
            self.scene.addItem(value_text)

        self.info_label.setText(
            f"热度图 Top {min(len(items), 100)}（按查询次数），"
            f"最高 {max_value}")

    def _fmt(self, n):
        if n >= 1_000_000_000:
            return f"{n/1e9:.1f}B"
        if n >= 1_000_000:
            return f"{n/1e6:.1f}M"
        if n >= 1_000:
            return f"{n/1e3:.1f}K"
        return str(n)

    def _export_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出", "data_map.png", "PNG (*.png)")
        if not path:
            return
        try:
            rect = self.scene.itemsBoundingRect()
            rect.adjust(-20, -20, 20, 20)
            img = QImage(int(rect.width()), int(rect.height()),
                         QImage.Format_ARGB32)
            img.fill(QColor("#1e1e1e"))
            painter = QPainter(img)
            painter.setRenderHint(QPainter.Antialiasing)
            self.scene.render(painter, target=QRectF(img.rect()),
                              source=rect)
            painter.end()
            img.save(path)
            QMessageBox.information(self, "完成", f"已导出:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))