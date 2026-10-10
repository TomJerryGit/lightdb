"""
仪表盘 - 卡片式展示
- 添加指标卡（数值/图表）
- 拖拽排序
- 自动刷新
"""
import json
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QFrame,
    QLabel, QPushButton, QComboBox, QMessageBox, QDialog,
    QFormLayout, QLineEdit, QTextEdit, QDialogButtonBox,
    QMenu, QGridLayout
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QAction

from .chart_view_enhanced import EnhancedChartWidget

DASHBOARD_FILE = Path.home() / '.lightdb_studio' / 'dashboard.json'


class MetricCard(QFrame):
    """指标卡片"""
    removed = Signal(object)
    refreshed = Signal(object)

    def __init__(self, config: dict, conn_getter=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.conn_getter = conn_getter
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "MetricCard { background: #252526; "
            "border: 1px solid #3c3c3c; border-radius: 6px; }")
        self.setMinimumHeight(240)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 头部
        header = QHBoxLayout()
        title = QLabel(self.config.get('title', '未命名'))
        title.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))
        title.setStyleSheet("color: #007acc;")
        header.addWidget(title)
        header.addStretch()

        refresh_btn = QPushButton("🔄")
        refresh_btn.setFixedWidth(28)
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)

        layout.addLayout(header)

        # 内容区
        self.content_label = QLabel("--")
        self.content_label.setFont(QFont("Microsoft YaHei", 24, QFont.Bold))
        self.content_label.setAlignment(Qt.AlignCenter)
        self.content_label.setStyleSheet("color: #4ec9b0;")
        layout.addWidget(self.content_label, 1)

        # 副标题
        sub = QLabel(self.config.get('subtitle', ''))
        sub.setStyleSheet("color: #969696; font-size: 11px;")
        sub.setAlignment(Qt.AlignCenter)
        layout.addWidget(sub)

    def refresh(self):
        conn = self.conn_getter() if self.conn_getter else None
        if conn is None:
            self.content_label.setText("无连接")
            return
        sql = self.config.get('sql', '')
        if not sql:
            return
        try:
            cols, rows = conn.query(sql)
        except Exception as e:
            self.content_label.setText("错误")
            self.content_label.setStyleSheet("color: #f48771;")
            return

        # 显示逻辑
        display = self.config.get('display', 'value')
        if display == 'value' and rows and rows[0]:
            val = rows[0][0]
            if val is None:
                val = 0
            if isinstance(val, (int, float)):
                self.content_label.setText(f"{val:,}")
            else:
                self.content_label.setText(str(val))
        elif display == 'count':
            self.content_label.setText(f"{len(rows):,}")
        else:
            self.content_label.setText(f"{len(rows)} 行")

        self.content_label.setStyleSheet("color: #4ec9b0;")
        self.refreshed.emit(self)


class ChartCard(QFrame):
    """图表卡片"""
    removed = Signal(object)

    def __init__(self, config: dict, conn_getter=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.conn_getter = conn_getter
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "ChartCard { background: #252526; "
            "border: 1px solid #3c3c3c; border-radius: 6px; }")
        self.setMinimumHeight(280)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        title = QLabel(self.config.get('title', '图表'))
        title.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))
        title.setStyleSheet("color: #007acc;")
        header.addWidget(title)
        header.addStretch()

        refresh_btn = QPushButton("🔄")
        refresh_btn.setFixedWidth(28)
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)

        layout.addLayout(header)

        self.chart = EnhancedChartWidget()
        layout.addWidget(self.chart)

    def refresh(self):
        conn = self.conn_getter() if self.conn_getter else None
        if conn is None:
            return
        sql = self.config.get('sql', '')
        if not sql:
            return
        try:
            cols, rows = conn.query(sql)
        except Exception:
            return

        labels = []
        values = []
        for r in rows[:30]:
            if len(r) >= 2:
                labels.append(str(r[0]))
                try:
                    values.append(float(r[1]))
                except (ValueError, TypeError):
                    values.append(0)

        self.chart.set_data(
            labels, values,
            title='', chart_type=self.config.get('chart_type', 'bar'))


class MetricEditorDialog(QDialog):
    """指标卡编辑器"""
    def __init__(self, config: dict = None, parent=None):
        super().__init__(parent)
        self.config = config or {}
        self.setWindowTitle("编辑指标卡")
        self.resize(520, 400)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.title_edit = QLineEdit(self.config.get('title', ''))
        form.addRow("标题:", self.title_edit)

        self.sub_edit = QLineEdit(self.config.get('subtitle', ''))
        form.addRow("副标题:", self.sub_edit)

        self.display_combo = QComboBox()
        self.display_combo.addItems(
            ['显示第一行第一列', '显示行数', '显示列数'])
        idx = {'value': 0, 'count': 1}.get(
            self.config.get('display', 'value'), 0)
        self.display_combo.setCurrentIndex(idx)
        form.addRow("显示:", self.display_combo)

        layout.addLayout(form)

        layout.addWidget(QLabel("SQL:"))
        self.sql_edit = QTextEdit()
        self.sql_edit.setFont(QFont("Consolas", 10))
        self.sql_edit.setPlainText(self.config.get('sql', ''))
        layout.addWidget(self.sql_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_config(self):
        display_map = {0: 'value', 1: 'count', 2: 'columns'}
        return {
            'type': 'metric',
            'title': self.title_edit.text().strip() or '指标',
            'subtitle': self.sub_edit.text().strip(),
            'display': display_map[self.display_combo.currentIndex()],
            'sql': self.sql_edit.toPlainText().strip(),
        }


class DashboardWidget(QWidget):
    """仪表盘"""
    def __init__(self, conn_getter=None, parent=None):
        super().__init__(parent)
        self.conn_getter = conn_getter
        self.cards = []
        self.file = DASHBOARD_FILE
        self._build_ui()
        self._load()
        self._start_auto_refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 工具栏
        bar = QWidget()
        bar.setStyleSheet(
            "background: #2d2d30; border-bottom: 1px solid #3c3c3c;")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(6, 4, 6, 4)

        add_metric_btn = QPushButton("➕ 添加指标卡")
        add_metric_btn.clicked.connect(self._add_metric)
        bl.addWidget(add_metric_btn)

        add_chart_btn = QPushButton("📊 添加图表卡")
        add_chart_btn.clicked.connect(self._add_chart)
        bl.addWidget(add_chart_btn)

        bl.addStretch()

        refresh_all_btn = QPushButton("🔄 全部刷新")
        refresh_all_btn.clicked.connect(self.refresh_all)
        bl.addWidget(refresh_all_btn)

        auto_refresh_label = QLabel("自动刷新:")
        bl.addWidget(auto_refresh_label)

        self.interval_combo = QComboBox()
        self.interval_combo.addItems(
            ['关闭', '5 秒', '10 秒', '30 秒', '1 分钟', '5 分钟'])
        self.interval_combo.setCurrentIndex(3)
        self.interval_combo.currentIndexChanged.connect(
            self._on_interval_changed)
        bl.addWidget(self.interval_combo)

        layout.addWidget(bar)

        # 卡片区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        self.container = QWidget()
        self.grid = QGridLayout(self.container)
        self.grid.setSpacing(10)
        self.grid.setContentsMargins(10, 10, 10, 10)
        scroll.setWidget(self.container)

        layout.addWidget(scroll)

    def _add_metric(self):
        dlg = MetricEditorDialog(parent=self)
        if dlg.exec() == MetricEditorDialog.Accepted:
            self._add_card(dlg.get_config())

    def _add_chart(self):
        dlg = ChartCardEditorDialog(parent=self)
        if dlg.exec() == ChartCardEditorDialog.Accepted:
            self._add_card(dlg.get_config())

    def _add_card(self, config):
        if config['type'] == 'metric':
            card = MetricCard(config, self.conn_getter)
        else:
            card = ChartCard(config, self.conn_getter)

        card.removed.connect(self._remove_card)
        self.cards.append(card)
        self._reflow()
        card.refresh()
        self._save()

    def _remove_card(self, card):
        if card in self.cards:
            self.cards.remove(card)
            self.grid.removeWidget(card)
            card.deleteLater()
            self._reflow()
            self._save()

    def _reflow(self):
        # 清空网格
        while self.grid.count():
            item = self.grid.takeAt(0)
            # 不删除 widget，只是移除布局

        cols = 3
        for i, card in enumerate(self.cards):
            r, c = divmod(i, cols)
            self.grid.addWidget(card, r, c)
            card.setContextMenuPolicy(Qt.CustomContextMenu)
            try:
                card.customContextMenuRequested.disconnect()
            except Exception:
                pass
            card.customContextMenuRequested.connect(
                lambda pos, cd=card: self._card_menu(cd, pos))

    def _card_menu(self, card, pos):
        menu = QMenu(self)
        a_refresh = menu.addAction("🔄 刷新")
        a_remove = menu.addAction("🗑 移除")
        action = menu.exec(card.mapToGlobal(pos))
        if action == a_refresh:
            card.refresh()
        elif action == a_remove:
            self._remove_card(card)

    def refresh_all(self):
        for c in self.cards:
            c.refresh()

    def _start_auto_refresh(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all)
        self._on_interval_changed(self.interval_combo.currentIndex())

    def _on_interval_changed(self, idx):
        intervals = [0, 5000, 10000, 30000, 60000, 300000]
        ms = intervals[idx]
        if ms > 0:
            self.timer.start(ms)
        else:
            self.timer.stop()

    def _save(self):
        configs = [c.config for c in self.cards]
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(configs, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                configs = json.load(f)
            for cfg in configs:
                if cfg.get('type') == 'metric':
                    card = MetricCard(cfg, self.conn_getter)
                else:
                    card = ChartCard(cfg, self.conn_getter)
                card.removed.connect(self._remove_card)
                self.cards.append(card)
            self._reflow()
        except Exception:
            pass


class ChartCardEditorDialog(QDialog):
    def __init__(self, config: dict = None, parent=None):
        super().__init__(parent)
        self.config = config or {}
        self.setWindowTitle("编辑图表卡")
        self.resize(520, 460)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.title_edit = QLineEdit(self.config.get('title', ''))
        form.addRow("标题:", self.title_edit)

        self.chart_combo = QComboBox()
        self.chart_combo.addItems([
            '柱状图', '折线图', '饼图', '环形图', '面积图', '散点图'
        ])
        form.addRow("图表类型:", self.chart_combo)
        layout.addLayout(form)

        layout.addWidget(QLabel("SQL（第1列为标签，第2列为值）:"))
        self.sql_edit = QTextEdit()
        self.sql_edit.setFont(QFont("Consolas", 10))
        self.sql_edit.setPlainText(self.config.get('sql', ''))
        layout.addWidget(self.sql_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_config(self):
        chart_map = ['bar', 'line', 'pie', 'donut', 'area', 'scatter']
        return {
            'type': 'chart',
            'title': self.title_edit.text().strip() or '图表',
            'chart_type': chart_map[self.chart_combo.currentIndex()],
            'sql': self.sql_edit.toPlainText().strip(),
        }