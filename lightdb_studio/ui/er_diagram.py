"""
ER 图 - 简单的自动布局
- 每个表一个矩形
- 自动检测外键关系（基于 column 命名 <table>_id 或 <table>id）
- 用线连起来
"""
import math
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QFileDialog, QMessageBox, QGraphicsView, QGraphicsScene,
    QGraphicsRectItem, QGraphicsTextItem, QGraphicsLineItem,
    QComboBox
)
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QPen, QBrush, QFont, QPainter


class ERDiagramDialog(QDialog):
    def __init__(self, conn, db_name, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.db_name = db_name
        self.setWindowTitle(f"ER 图 - {db_name}")
        self.resize(1200, 800)
        self._build_ui()
        self._load()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("布局:"))
        self.layout_combo = QComboBox()
        self.layout_combo.addItems(['圆形', '网格', '力导向'])
        self.layout_combo.currentTextChanged.connect(self._redraw)
        top.addWidget(self.layout_combo)
        top.addStretch()

        self.export_btn = QPushButton("📤 导出 PNG")
        self.export_btn.clicked.connect(self._export_png)
        top.addWidget(self.export_btn)

        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self._load)
        top.addWidget(refresh_btn)

        layout.addLayout(top)

        self.view = QGraphicsView()
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setBackgroundBrush(QBrush(QColor("#1e1e1e")))
        self.scene = QGraphicsScene()
        self.view.setScene(self.scene)
        layout.addWidget(self.view)

    def _load(self):
        self.scene.clear()
        self.tables = {}  # name -> {cols, rect}

        try:
            self.conn.switch_database(self.db_name)
            table_names = self.conn.get_tables()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return

        for t in table_names:
            try:
                cols = self.conn.get_columns(t)
                self.tables[t] = {'cols': cols, 'rect': None}
            except Exception:
                pass

        # 检测外键关系（简化：基于命名规则）
        self.relations = []
        table_names_set = set(self.tables.keys())
        for t, info in self.tables.items():
            for c in info['cols']:
                cname = c['name'].lower()
                # 匹配 <table>_id 或 <table>id
                for other in table_names_set:
                    if other == t:
                        continue
                    on = other.lower()
                    if cname in (f"{on}_id", f"{on}id") or \
                       cname == f"{on}_id":
                        # 找 other 的主键
                        pk = next((cc['name'] for cc in self.tables[other]['cols']
                                   if cc.get('key') == 'PRI'), None)
                        if pk:
                            self.relations.append({
                                'from_table': t,
                                'from_col': c['name'],
                                'to_table': other,
                                'to_col': pk,
                            })

        self._redraw()

    def _redraw(self):
        self.scene.clear()
        mode = self.layout_combo.currentText()
        if mode == '圆形':
            self._layout_circular()
        elif mode == '网格':
            self._layout_grid()
        else:
            self._layout_force()

        # 绘制关系线（先画线，再画表覆盖）
        for rel in self.relations:
            self._draw_relation(rel)

        # 绘制表
        for t, info in self.tables.items():
            self._draw_table(t, info)

    # ==================== 布局 ====================

    def _layout_circular(self):
        n = len(self.tables)
        if n == 0:
            return
        radius = max(200, n * 30)
        cx, cy = 600, 400
        for i, (t, info) in enumerate(self.tables.items()):
            angle = 2 * math.pi * i / n
            x = cx + radius * math.cos(angle)
            y = cy + radius * math.sin(angle)
            info['rect'] = QRectF(x, y, 200, 20 + 20 * len(info['cols']))

    def _layout_grid(self):
        cols = int(math.sqrt(len(self.tables))) + 1
        for i, (t, info) in enumerate(self.tables.items()):
            r, c = divmod(i, cols)
            x = c * 260 + 50
            y = r * 220 + 50
            info['rect'] = QRectF(x, y, 200, 20 + 20 * len(info['cols']))

    def _layout_force(self):
        """简化的力导向：用圆形布局 + 随机偏移"""
        import random
        n = len(self.tables)
        for i, (t, info) in enumerate(self.tables.items()):
            angle = 2 * math.pi * i / max(1, n)
            radius = 250 + random.randint(-50, 50)
            x = 600 + radius * math.cos(angle)
            y = 400 + radius * math.sin(angle)
            info['rect'] = QRectF(x, y, 200, 20 + 20 * len(info['cols']))

    # ==================== 绘制 ====================

    def _draw_table(self, name, info):
        r = info['rect']
        if r is None:
            return

        # 表格背景
        rect = QGraphicsRectItem(r)
        rect.setBrush(QBrush(QColor("#252526")))
        rect.setPen(QPen(QColor("#007acc"), 2))
        self.scene.addItem(rect)

        # 标题背景
        header_rect = QGraphicsRectItem(QRectF(r.x(), r.y(), r.width(), 26))
        header_rect.setBrush(QBrush(QColor("#0e639c")))
        header_rect.setPen(QPen(Qt.NoPen))
        self.scene.addItem(header_rect)

        # 标题文本
        title = QGraphicsTextItem(name)
        title.setDefaultTextColor(QColor("#ffffff"))
        f = QFont("Microsoft YaHei", 10, QFont.Bold)
        title.setFont(f)
        title.setPos(r.x() + 8, r.y() + 4)
        self.scene.addItem(title)

        # 列
        y = r.y() + 30
        for col in info['cols']:
            is_pk = col.get('key') == 'PRI'
            text = f"{'🔑' if is_pk else '  '} {col['name']}  {col['type']}"
            item = QGraphicsTextItem(text)
            item.setDefaultTextColor(
                QColor("#dcdcaa") if is_pk else QColor("#d4d4d4"))
            item.setFont(QFont("Consolas", 8))
            item.setPos(r.x() + 6, y)
            self.scene.addItem(item)
            y += 18

    def _draw_relation(self, rel):
        a = self.tables.get(rel['from_table'])
        b = self.tables.get(rel['to_table'])
        if not a or not b or a['rect'] is None or b['rect'] is None:
            return

        ra = a['rect']
        rb = b['rect']

        # 找最近的两个边中心
        p1 = QPointF(ra.center().x(), ra.bottom())
        p2 = QPointF(rb.center().x(), rb.top())

        # 判断方向
        if rb.center().y() < ra.center().y():
            p1 = QPointF(ra.center().x(), ra.top())
            p2 = QPointF(rb.center().x(), rb.bottom())

        pen = QPen(QColor("#569cd6"), 1.5, Qt.DashLine)
        line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
        line.setPen(pen)
        self.scene.addItem(line)

        # 小圆点
        dot = QGraphicsRectItem(p2.x() - 3, p2.y() - 3, 6, 6)
        dot.setBrush(QBrush(QColor("#569cd6")))
        dot.setPen(QPen(Qt.NoPen))
        self.scene.addItem(dot)

    def _export_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 PNG", f"{self.db_name}_er.png",
            "PNG 图片 (*.png)")
        if not path:
            return
        try:
            # 计算整个场景边界
            rect = self.scene.itemsBoundingRect()
            margin = 20
            rect.adjust(-margin, -margin, margin, margin)

            from PySide6.QtGui import QImage
            img = QImage(int(rect.width()), int(rect.height()),
                         QImage.Format_ARGB32)
            img.fill(QColor("#1e1e1e"))

            painter = QPainter(img)
            painter.setRenderHint(QPainter.Antialiasing)
            self.scene.render(painter, target=QRectF(img.rect()), source=rect)
            painter.end()

            img.save(path)
            QMessageBox.information(self, "成功", f"已导出:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))