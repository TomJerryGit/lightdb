"""脱敏配置对话框"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QDialogButtonBox, QComboBox, QLineEdit, QCheckBox,
    QMessageBox, QLabel, QWidget, QHeaderView, QAbstractItemView,
    QGroupBox, QSpinBox
)
from PySide6.QtCore import Qt

from ..core.masking import MaskingEngine, MaskingRule


STRATEGIES = [
    ('full', '全部遮蔽 (***)'),
    ('partial', '部分遮蔽 (保留首尾)'),
    ('phone', '手机号 (138****8888)'),
    ('email', '邮箱 (a***@x.com)'),
    ('id_card', '身份证 (保留前6后4)'),
    ('hash', '哈希 (SHA256 前16位)'),
    ('replace', '替换为固定值'),
    ('generalize', '数值泛化 (数量级)'),
    ('date', '日期截断 (年-月)'),
    ('none', '不脱敏'),
]


class MaskingDialog(QDialog):
    def __init__(self, engine: MaskingEngine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("数据脱敏规则")
        self.resize(860, 580)
        self._build_ui()
        self._load_rules()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            "定义按列名匹配的脱敏规则。导出的数据会自动应用这些规则。\n"
            "列名支持通配符：`*phone*` 匹配所有含 phone 的列。")
        info.setStyleSheet("color: #969696;")
        layout.addWidget(info)

        # 表格
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ['启用', '列名模式', '策略', '保留前', '保留后', '替换值'])
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 200)
        self.table.setColumnWidth(3, 70)
        self.table.setColumnWidth(4, 70)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        # 按钮行
        btn_row = QHBoxLayout()

        add_btn = QPushButton("➕ 添加规则")
        add_btn.clicked.connect(self._add_rule)
        btn_row.addWidget(add_btn)

        del_btn = QPushButton("➖ 删除选中")
        del_btn.clicked.connect(self._del_rules)
        btn_row.addWidget(del_btn)

        auto_btn = QPushButton("🔍 自动识别常见敏感列")
        auto_btn.clicked.connect(self._auto_detect)
        btn_row.addWidget(auto_btn)

        btn_row.addStretch()

        clear_btn = QPushButton("清空全部")
        clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(clear_btn)

        layout.addLayout(btn_row)

        # 测试区
        test_group = QGroupBox("测试")
        tg = QHBoxLayout(test_group)
        tg.addWidget(QLabel("列名:"))
        self.test_col = QLineEdit("user_phone")
        tg.addWidget(self.test_col)
        tg.addWidget(QLabel("值:"))
        self.test_val = QLineEdit("13812345678")
        tg.addWidget(self.test_val)
        test_btn = QPushButton("测试")
        test_btn.clicked.connect(self._test)
        tg.addWidget(test_btn)
        tg.addWidget(QLabel("结果:"))
        self.test_result = QLineEdit()
        self.test_result.setReadOnly(True)
        self.test_result.setStyleSheet(
            "background: #252526; color: #89d185;")
        tg.addWidget(self.test_result, 1)
        layout.addWidget(test_group)

        # 确定/取消
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_rules(self):
        self.table.setRowCount(0)
        for r in self.engine.rules:
            self._add_row(r)

    def _add_row(self, rule: MaskingRule):
        r = self.table.rowCount()
        self.table.insertRow(r)

        # 启用
        chk = QCheckBox()
        chk.setChecked(rule.enabled)
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setAlignment(Qt.AlignCenter)
        l.addWidget(chk)
        self.table.setCellWidget(r, 0, w)

        # 列名
        self.table.setItem(r, 1, QTableWidgetItem(rule.column_pattern))

        # 策略
        combo = QComboBox()
        for key, label in STRATEGIES:
            combo.addItem(label, key)
        # 选中
        for i, (key, _) in enumerate(STRATEGIES):
            if key == rule.strategy:
                combo.setCurrentIndex(i)
                break
        self.table.setCellWidget(r, 2, combo)

        # 保留前/后
        self.table.setItem(r, 3, QTableWidgetItem(str(rule.keep_prefix)))
        self.table.setItem(r, 4, QTableWidgetItem(str(rule.keep_suffix)))

        # 替换值
        self.table.setItem(r, 5, QTableWidgetItem(rule.replacement))

    def _add_rule(self):
        self._add_row(MaskingRule(
            column_pattern="*new_col*",
            strategy="full",
            replacement="***",
            keep_prefix=0, keep_suffix=0))

    def _del_rules(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _auto_detect(self):
        n = self.engine.auto_detect_rules()
        self._load_rules()
        QMessageBox.information(
            self, "完成",
            f"已根据内置敏感模式生成 {n} 条规则（含之前规则）")

    def _clear(self):
        ret = QMessageBox.question(self, "确认", "清空所有脱敏规则？")
        if ret == QMessageBox.Yes:
            self.table.setRowCount(0)

    def _collect(self):
        rules = []
        for r in range(self.table.rowCount()):
            # 启用
            w = self.table.cellWidget(r, 0)
            enabled = w.findChild(QCheckBox).isChecked() if w else True

            # 列名
            name_item = self.table.item(r, 1)
            pattern = name_item.text().strip() if name_item else ''
            if not pattern:
                continue

            # 策略
            combo = self.table.cellWidget(r, 2)
            strategy = combo.currentData() if combo else 'full'

            # 保留前后
            try:
                kp = int(self.table.item(r, 3).text() or 0)
            except (ValueError, AttributeError):
                kp = 0
            try:
                ks = int(self.table.item(r, 4).text() or 0)
            except (ValueError, AttributeError):
                ks = 0

            repl_item = self.table.item(r, 5)
            repl = repl_item.text() if repl_item else '***'

            rules.append(MaskingRule(
                column_pattern=pattern,
                strategy=strategy,
                replacement=repl,
                keep_prefix=kp,
                keep_suffix=ks,
                enabled=enabled,
            ))
        return rules

    def _test(self):
        col = self.test_col.text().strip()
        val = self.test_val.text()

        # 临时构造规则集
        old_rules = self.engine.rules
        try:
            self.engine.rules = self._collect()
            rule = self.engine.get_strategy(col)
            if rule:
                result = self.engine.mask_value(val, rule)
                self.test_result.setText(
                    f"{result}  (策略: {rule.strategy})")
            else:
                self.test_result.setText(
                    f"{val}  (无匹配规则)")
        finally:
            self.engine.rules = old_rules

    def _save(self):
        self.engine.rules = self._collect()
        self.engine.save()
        self.accept()