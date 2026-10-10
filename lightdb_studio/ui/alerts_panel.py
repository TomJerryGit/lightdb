"""智能告警面板"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QTabWidget, QWidget, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QFormLayout, QLineEdit,
    QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox, QDialogButtonBox
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from datetime import datetime

from ..core.alerts import AlertEngine, AlertRule


class AlertRuleEditorDialog(QDialog):
    def __init__(self, rule: AlertRule = None, parent=None):
        super().__init__(parent)
        self.rule = rule or AlertRule()
        self.setWindowTitle("编辑告警规则" if rule else "新建告警规则")
        self.resize(520, 480)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit(self.rule.name)
        form.addRow("规则名:", self.name_edit)

        self.metric_edit = QLineEdit(self.rule.metric)
        self.metric_edit.setPlaceholderText("qps / connections / slow_queries")
        form.addRow("指标:", self.metric_edit)

        self.type_combo = QComboBox()
        self.type_combo.addItems(
            ['threshold (阈值)', 'trend (趋势)', 'anomaly (异常)'])
        idx = {'threshold': 0, 'trend': 1, 'anomaly': 2}.get(
            self.rule.rule_type, 0)
        self.type_combo.setCurrentIndex(idx)
        form.addRow("类型:", self.type_combo)

        self.op_combo = QComboBox()
        self.op_combo.addItems(['>', '<', '>=', '<='])
        self.op_combo.setCurrentText(self.rule.operator)
        form.addRow("运算符:", self.op_combo)

        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(-1e9, 1e9)
        self.threshold_spin.setValue(self.rule.threshold)
        form.addRow("阈值:", self.threshold_spin)

        self.change_spin = QDoubleSpinBox()
        self.change_spin.setRange(-1000, 1000)
        self.change_spin.setValue(self.rule.change_pct)
        self.change_spin.setSuffix(" %")
        form.addRow("变化率:", self.change_spin)

        self.window_spin = QSpinBox()
        self.window_spin.setRange(2, 60)
        self.window_spin.setValue(self.rule.window)
        form.addRow("观察窗口:", self.window_spin)

        self.method_combo = QComboBox()
        self.method_combo.addItems(['zscore', 'iqr'])
        self.method_combo.setCurrentText(self.rule.method)
        form.addRow("异常方法:", self.method_combo)

        self.sens_spin = QDoubleSpinBox()
        self.sens_spin.setRange(1.0, 10.0)
        self.sens_spin.setSingleStep(0.5)
        self.sens_spin.setValue(self.rule.sensitivity)
        form.addRow("敏感度:", self.sens_spin)

        self.sev_combo = QComboBox()
        self.sev_combo.addItems(['info', 'warning', 'critical'])
        self.sev_combo.setCurrentText(self.rule.severity)
        form.addRow("严重度:", self.sev_combo)

        self.cooldown_spin = QSpinBox()
        self.cooldown_spin.setRange(0, 86400)
        self.cooldown_spin.setValue(self.rule.cooldown)
        self.cooldown_spin.setSuffix(" 秒")
        form.addRow("冷却时间:", self.cooldown_spin)

        layout.addLayout(form)
        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_rule(self) -> AlertRule:
        type_map = {0: 'threshold', 1: 'trend', 2: 'anomaly'}
        return AlertRule(
            name=self.name_edit.text().strip(),
            metric=self.metric_edit.text().strip(),
            rule_type=type_map[self.type_combo.currentIndex()],
            operator=self.op_combo.currentText(),
            threshold=self.threshold_spin.value(),
            change_pct=self.change_spin.value(),
            window=self.window_spin.value(),
            method=self.method_combo.currentText(),
            sensitivity=self.sens_spin.value(),
            severity=self.sev_combo.currentText(),
            cooldown=self.cooldown_spin.value(),
        )


class AlertsPanel(QDialog):
    """告警面板"""

    def __init__(self, alert_engine: AlertEngine, parent=None):
        super().__init__(parent)
        self.engine = alert_engine
        self.setWindowTitle("智能告警")
        self.resize(960, 620)
        self._build_ui()

        # 定时刷新
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_active)
        self.timer.start(3000)

        self._refresh_all()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        tabs = QTabWidget()

        # 活跃告警
        active_w = QWidget()
        al = QVBoxLayout(active_w)

        active_top = QHBoxLayout()
        active_top.addWidget(QLabel("当前活跃告警："))
        active_top.addStretch()

        resolve_btn = QPushButton("✅ 标记解决")
        resolve_btn.clicked.connect(self._resolve)
        active_top.addWidget(resolve_btn)

        clear_btn = QPushButton("🗑 清理已解决")
        clear_btn.clicked.connect(self._clear_resolved)
        active_top.addWidget(clear_btn)
        al.addLayout(active_top)

        self.active_table = QTableWidget()
        self.active_table.setColumnCount(5)
        self.active_table.setHorizontalHeaderLabels(
            ['严重度', '规则', '消息', '值', '时间'])
        self.active_table.setColumnWidth(0, 80)
        self.active_table.setColumnWidth(1, 160)
        self.active_table.setColumnWidth(2, 380)
        self.active_table.setColumnWidth(3, 100)
        self.active_table.horizontalHeader().setStretchLastSection(True)
        al.addWidget(self.active_table)
        tabs.addTab(active_w, "活跃告警")

        # 历史
        hist_w = QWidget()
        hl = QVBoxLayout(hist_w)
        self.hist_list = QListWidget()
        self.hist_list.setFont(QFont("Consolas", 9))
        hl.addWidget(self.hist_list)
        tabs.addTab(hist_w, "历史")

        # 规则
        rules_w = QWidget()
        rl = QVBoxLayout(rules_w)

        rules_top = QHBoxLayout()
        add_btn = QPushButton("➕ 新建规则")
        add_btn.clicked.connect(self._add_rule)
        rules_top.addWidget(add_btn)

        default_btn = QPushButton("⭐ 添加默认规则")
        default_btn.clicked.connect(self._add_defaults)
        rules_top.addWidget(default_btn)

        del_btn = QPushButton("🗑 删除")
        del_btn.clicked.connect(self._del_rule)
        rules_top.addWidget(del_btn)
        rules_top.addStretch()
        rl.addLayout(rules_top)

        self.rules_table = QTableWidget()
        self.rules_table.setColumnCount(7)
        self.rules_table.setHorizontalHeaderLabels(
            ['名称', '指标', '类型', '条件', '严重度', '触发次数', '启用'])
        self.rules_table.setColumnWidth(0, 160)
        self.rules_table.setColumnWidth(1, 120)
        self.rules_table.setColumnWidth(2, 100)
        self.rules_table.setColumnWidth(3, 160)
        self.rules_table.setColumnWidth(4, 80)
        self.rules_table.setColumnWidth(5, 80)
        self.rules_table.horizontalHeader().setStretchLastSection(True)
        rl.addWidget(self.rules_table)
        tabs.addTab(rules_w, "规则")

        layout.addWidget(tabs)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)

    def _refresh_all(self):
        self._refresh_active()
        self._refresh_history()
        self._refresh_rules()

    def _refresh_active(self):
        alerts = self.engine.get_active()
        self.active_table.setRowCount(len(alerts))
        for r, a in enumerate(alerts):
            icon = {'info': '🔵', 'warning': '🟡',
                    'critical': '🔴'}.get(a.severity, '⚪')
            self.active_table.setItem(r, 0, QTableWidgetItem(icon))
            self.active_table.setItem(r, 1, QTableWidgetItem(a.rule_name))
            self.active_table.setItem(r, 2, QTableWidgetItem(a.message))
            self.active_table.setItem(r, 3, QTableWidgetItem(
                f"{a.value:.2f}"))
            ts = datetime.fromtimestamp(a.fired_at).strftime('%H:%M:%S')
            self.active_table.setItem(r, 4, QTableWidgetItem(ts))

            if a.severity == 'critical':
                for c in range(5):
                    item = self.active_table.item(r, c)
                    if item:
                        item.setForeground(QColor('#f48771'))

    def _refresh_history(self):
        self.hist_list.clear()
        for a in self.engine.get_history(100):
            ts = datetime.fromtimestamp(a.fired_at).strftime(
                '%Y-%m-%d %H:%M:%S')
            icon = {'info': '🔵', 'warning': '🟡',
                    'critical': '🔴'}.get(a.severity, '⚪')
            text = f"[{ts}] {icon} {a.rule_name}: {a.message}"
            item = QListWidgetItem(text)
            if a.severity == 'critical':
                item.setForeground(QColor('#f48771'))
            self.hist_list.addItem(item)

    def _refresh_rules(self):
        self.rules_table.setRowCount(len(self.engine.rules))
        for r, rule in enumerate(self.engine.rules):
            self.rules_table.setItem(r, 0, QTableWidgetItem(rule.name))
            self.rules_table.setItem(r, 1, QTableWidgetItem(rule.metric))
            self.rules_table.setItem(r, 2, QTableWidgetItem(rule.rule_type))
            if rule.rule_type == 'threshold':
                cond = f"{rule.operator} {rule.threshold}"
            elif rule.rule_type == 'trend':
                cond = f"变化 {rule.change_pct}%"
            else:
                cond = f"{rule.method} > {rule.sensitivity}σ"
            self.rules_table.setItem(r, 3, QTableWidgetItem(cond))
            self.rules_table.setItem(r, 4, QTableWidgetItem(rule.severity))
            self.rules_table.setItem(r, 5, QTableWidgetItem(
                str(rule.fire_count)))
            self.rules_table.setItem(
                r, 6, QTableWidgetItem('✓' if rule.enabled else '✗'))

    def _resolve(self):
        r = self.active_table.currentRow()
        if r < 0:
            return
        active = self.engine.get_active()
        if r < len(active):
            # 找到在 active_alerts 中的索引
            target = active[r]
            for i, a in enumerate(self.engine.active_alerts):
                if a is target:
                    self.engine.resolve(i)
                    break
            self._refresh_active()

    def _clear_resolved(self):
        self.engine.clear_resolved()
        self._refresh_active()

    def _add_rule(self):
        dlg = AlertRuleEditorDialog(parent=self)
        if dlg.exec() == AlertRuleEditorDialog.Accepted:
            rule = dlg.get_rule()
            if rule.name and rule.metric:
                self.engine.add_rule(rule)
                self._refresh_rules()

    def _add_defaults(self):
        self.engine.add_default_rules()
        self._refresh_rules()
        QMessageBox.information(self, "完成", "已添加默认规则")

    def _del_rule(self):
        r = self.rules_table.currentRow()
        if r < 0:
            return
        rule = self.engine.rules[r]
        ret = QMessageBox.question(
            self, "确认", f"删除规则 '{rule.name}'？")
        if ret == QMessageBox.Yes:
            self.engine.remove_rule(rule.name)
            self._refresh_rules()