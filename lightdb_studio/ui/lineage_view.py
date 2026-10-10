"""数据血缘可视化"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsTextItem,
    QGraphicsLineItem, QDialogButtonBox, QComboBox, QMessageBox,
    QTabWidget, QWidget, QListWidget, QListWidgetItem, QFileDialog,
)
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QColor, QPen, QBrush, QFont, QPainter, QImage, QPainterPath,
    QLinearGradient,
)

from ..core.lineage import LineageStore


class LineageDialog(QDialog):
    """血缘图对话框"""

    def __init__(self, store: LineageStore, initial_table: str = '',
                 parent=None):
        super().__init__(parent)
        self.store = store
        self.current_table = ''
        self.upstream = []
        self.downstream = []
        self.setWindowTitle("数据血缘")
        self.resize(1080, 720)
        self._build_ui()
        if initial_table:
            self.table_edit.setText(initial_table)
            self._analyze()

    # ==================== UI ====================

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 顶部工具条
        top = QHBoxLayout()
        top.addWidget(QLabel("表名:"))
        self.table_edit = QLineEdit()
        self.table_edit.setPlaceholderText("输入表名，如: users")
        self.table_edit.returnPressed.connect(self._analyze)
        top.addWidget(self.table_edit)

        top.addWidget(QLabel("方向:"))
        self.direction_combo = QComboBox()
        self.direction_combo.addItems(['双向', '只看上游', '只看下游'])
        self.direction_combo.currentIndexChanged.connect(self._on_filter_changed)
        top.addWidget(self.direction_combo)

        top.addWidget(QLabel("层级:"))
        self.depth_combo = QComboBox()
        self.depth_combo.addItems(['1', '2', '3', '5'])
        self.depth_combo.setCurrentIndex(2)
        self.depth_combo.currentIndexChanged.connect(self._on_filter_changed)
        top.addWidget(self.depth_combo)

        analyze_btn = QPushButton("🔍 分析")
        analyze_btn.clicked.connect(self._analyze)
        top.addWidget(analyze_btn)

        export_btn = QPushButton("📤 导出 PNG")
        export_btn.clicked.connect(self._export_png)
        top.addWidget(export_btn)
        top.addStretch()
        layout.addLayout(top)

        # 主体
        body = QHBoxLayout()

        self.view = QGraphicsView()
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setBackgroundBrush(QBrush(QColor("#1e1e1e")))
        self.scene = QGraphicsScene()
        self.view.setScene(self.scene)
        body.addWidget(self.view, 3)

        detail_w = QWidget()
        dl = QVBoxLayout(detail_w)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(QLabel("相关表:"))
        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._on_double_click)
        dl.addWidget(self.list_widget)
        body.addWidget(detail_w, 1)
        layout.addLayout(body)

        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.reject)
        layout.addWidget(close_btn)

    # ==================== 数据加载 ====================

    def _analyze(self):
        """完整分析：拉数据 + 绘制 + 刷新列表"""
        table = self.table_edit.text().strip()
        if not table:
            return
        self.current_table = table

        depth = int(self.depth_combo.currentText())
        direction = self.direction_combo.currentText()

        self.upstream = []
        self.downstream = []

        try:
            if direction in ('双向', '只看上游'):
                self.upstream = self.store.upstream(table, depth)
            if direction in ('双向', '只看下游'):
                self.downstream = self.store.downstream(table, depth)
        except Exception as e:
            QMessageBox.warning(self, "查询失败", str(e))
            return

        self._draw_graph()
        self._refresh_list()

    def _on_filter_changed(self):
        """方向/层级变化时重新拉数据"""
        if self.current_table:
            self._analyze()

    # ==================== 绘制 ====================

    def _draw_graph(self):
        """只负责绘制，不做数据拉取"""
        self.scene.clear()
        if not self.current_table:
            return

        # 中心节点
        cx, cy = 500, 300

        # 收集所有节点：name -> (x, y, color)
        nodes = {self.current_table: (cx, cy, '#569cd6')}

        # 上游层级
        up_by_depth = {}
        for u in self.upstream:
            up_by_depth.setdefault(u['depth'], []).append(u['table'])

        for depth, tables in up_by_depth.items():
            x = cx - depth * 220
            n = len(tables)
            for i, t in enumerate(tables):
                y = cy + (i - (n - 1) / 2) * 90
                if t not in nodes:
                    nodes[t] = (x, y, '#ce9178')

        # 下游层级
        down_by_depth = {}
        for d in self.downstream:
            down_by_depth.setdefault(d['depth'], []).append(d['table'])

        for depth, tables in down_by_depth.items():
            x = cx + depth * 220
            n = len(tables)
            for i, t in enumerate(tables):
                y = cy + (i - (n - 1) / 2) * 90
                if t not in nodes:
                    nodes[t] = (x, y, '#89d185')

        # 绘制边
        edges = []
        for u in self.upstream:
            if u['table'] in nodes and u.get('via') in nodes:
                edges.append((u['table'], u['via'], '#ce9178'))
        for d in self.downstream:
            if d.get('via') in nodes and d['table'] in nodes:
                edges.append((d['via'], d['table'], '#89d185'))

        for f, t, color in edges:
            if f not in nodes or t not in nodes:
                continue
            x1, y1, _ = nodes[f]
            x2, y2, _ = nodes[t]
            line = QGraphicsLineItem(x1 + 160, y1 + 20, x2, y2 + 20)
            line.setPen(QPen(QColor(color), 2))
            self.scene.addItem(line)

            # 箭头小圆点
            dot = QGraphicsRectItem(x2 - 3, y2 + 17, 6, 6)
            dot.setBrush(QBrush(QColor(color)))
            dot.setPen(QPen(Qt.NoPen))
            self.scene.addItem(dot)

        # 绘制节点
        for t, (x, y, color) in nodes.items():
            r = QRectF(x, y, 160, 40)
            rect = QGraphicsRectItem(r)
            rect.setBrush(QBrush(QColor("#252526")))
            rect.setPen(QPen(QColor(color), 2))
            self.scene.addItem(rect)

            text = QGraphicsTextItem(t[:24])
            text.setDefaultTextColor(QColor(color))
            text.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
            text.setPos(x + 8, y + 10)
            self.scene.addItem(text)

        # 自动 fit
        self.view.fitInView(
            self.scene.itemsBoundingRect().adjusted(-40, -40, 40, 40),
            Qt.KeepAspectRatio)

    def _refresh_list(self):
        self.list_widget.clear()
        for u in self.upstream:
            item = QListWidgetItem(
                f"⬆ {u['table']}  (第 {u['depth']} 层)")
            item.setData(Qt.UserRole, u['table'])
            item.setForeground(QColor("#ce9178"))
            self.list_widget.addItem(item)
        for d in self.downstream:
            item = QListWidgetItem(
                f"⬇ {d['table']}  (第 {d['depth']} 层)")
            item.setData(Qt.UserRole, d['table'])
            item.setForeground(QColor("#89d185"))
            self.list_widget.addItem(item)

    # ==================== 交互 ====================

    def _on_double_click(self, item):
        table = item.data(Qt.UserRole)
        if table:
            self.table_edit.setText(table)
            self._analyze()

    def _export_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出", "lineage.png", "PNG (*.png)")
        if not path:
            return
        try:
            rect = self.scene.itemsBoundingRect()
            rect.adjust(-20, -20, 20, 20)
            img = QImage(max(1, int(rect.width())),
                         max(1, int(rect.height())),
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


class LineageGraphView(QWidget):
    """内嵌版本（用于 MainWindow Tab）"""

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.current_table = ''
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.addWidget(QLabel("表名:"))
        self.table_edit = QLineEdit()
        self.table_edit.setPlaceholderText("输入表名...")
        self.table_edit.returnPressed.connect(self.analyze)
        top.addWidget(self.table_edit)

        btn = QPushButton("🔍 分析")
        btn.clicked.connect(self.analyze)
        top.addWidget(btn)
        top.addStretch()

        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: #969696;")
        top.addWidget(self.info_label)
        layout.addLayout(top)

        self.view = QGraphicsView()
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setBackgroundBrush(QBrush(QColor("#1e1e1e")))
        self.scene = QGraphicsScene()
        self.view.setScene(self.scene)
        layout.addWidget(self.view)

    def analyze(self):
        table = self.table_edit.text().strip()
        if not table:
            return
        self.current_table = table

        self.scene.clear()
        try:
            up = self.store.upstream(table, 3)
            down = self.store.downstream(table, 3)
        except Exception as e:
            self.info_label.setText(f"错误: {e}")
            return

        cx, cy = 500, 300
        nodes = {table: (cx, cy, '#569cd6')}

        up_by_depth = {}
        for u in up:
            up_by_depth.setdefault(u['depth'], []).append(u['table'])
        down_by_depth = {}
        for d in down:
            down_by_depth.setdefault(d['depth'], []).append(d['table'])

        for depth, tables in up_by_depth.items():
            x = cx - depth * 200
            n = len(tables)
            for i, t in enumerate(tables):
                y = cy + (i - (n - 1) / 2) * 80
                if t not in nodes:
                    nodes[t] = (x, y, '#ce9178')

        for depth, tables in down_by_depth.items():
            x = cx + depth * 200
            n = len(tables)
            for i, t in enumerate(tables):
                y = cy + (i - (n - 1) / 2) * 80
                if t not in nodes:
                    nodes[t] = (x, y, '#89d185')

        # 边
        edges = []
        for u in up:
            if u['table'] in nodes and u.get('via') in nodes:
                edges.append((u['table'], u['via'], '#ce9178'))
        for d in down:
            if d.get('via') in nodes and d['table'] in nodes:
                edges.append((d['via'], d['table'], '#89d185'))

        for f, t, color in edges:
            if f not in nodes or t not in nodes:
                continue
            x1, y1, _ = nodes[f]
            x2, y2, _ = nodes[t]
            line = QGraphicsLineItem(x1 + 80, y1 + 20, x2, y2 + 20)
            line.setPen(QPen(QColor(color), 2))
            self.scene.addItem(line)

        # 节点
        for t, (x, y, color) in nodes.items():
            r = QRectF(x, y, 160, 40)
            rect = QGraphicsRectItem(r)
            rect.setBrush(QBrush(QColor("#252526")))
            rect.setPen(QPen(QColor(color), 2))
            self.scene.addItem(rect)

            text = QGraphicsTextItem(t[:24])
            text.setDefaultTextColor(QColor(color))
            text.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
            text.setPos(x + 8, y + 10)
            self.scene.addItem(text)

        self.view.fitInView(
            self.scene.itemsBoundingRect().adjusted(-20, -20, 20, 20),
            Qt.KeepAspectRatio)

        self.info_label.setText(
            f"上游 {len(up)} 个，下游 {len(down)} 个，共 {len(nodes)} 个节点")