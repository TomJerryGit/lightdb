"""设置中心"""
from PySide6.QtWidgets import (
    QDialog, QTabWidget, QWidget, QVBoxLayout, QFormLayout,
    QComboBox, QSpinBox, QCheckBox, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QLineEdit, QDialogButtonBox,
    QKeySequenceEdit, QHBoxLayout, QMessageBox, QGroupBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.resize(680, 520)
        self._build_ui()
        self._load_values()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()

        self._build_general_tab()
        self._build_editor_tab()
        self._build_shortcuts_tab()
        self._build_advanced_tab()

        layout.addWidget(self.tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Apply).setText("应用")
        buttons.accepted.connect(self._apply_and_close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._apply)
        layout.addWidget(buttons)

    def _build_general_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(['深色 (Dark)', '浅色 (Light)',
                                  '高对比度 (High Contrast)'])
        form.addRow("主题:", self.theme_combo)

        self.lang_combo = QComboBox()
        self.lang_combo.addItems(['简体中文', 'English'])
        form.addRow("语言:", self.lang_combo)

        self.page_size_spin = QSpinBox()
        self.page_size_spin.setRange(50, 5000)
        self.page_size_spin.setSingleStep(50)
        form.addRow("默认每页行数:", self.page_size_spin)

        self.auto_connect_check = QCheckBox("启动时自动连接")
        self.auto_connect_check.setChecked(True)
        form.addRow("", self.auto_connect_check)

        self.restore_tabs_check = QCheckBox("恢复上次打开的标签")
        form.addRow("", self.restore_tabs_check)

        self.tabs.addTab(w, "常规")

    def _build_editor_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.font_family_combo = QComboBox()
        self.font_family_combo.setEditable(True)
        self.font_family_combo.addItems(
            ['Consolas', 'Courier New', 'Microsoft YaHei Mono',
             'Cascadia Code', 'JetBrains Mono', 'Monaco'])
        form.addRow("编辑器字体:", self.font_family_combo)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 32)
        form.addRow("字号:", self.font_size_spin)

        self.tab_size_spin = QSpinBox()
        self.tab_size_spin.setRange(2, 12)
        form.addRow("Tab 缩进:", self.tab_size_spin)

        self.auto_complete_check = QCheckBox("启用 SQL 自动补全")
        form.addRow("", self.auto_complete_check)

        self.auto_pair_check = QCheckBox("自动配对括号/引号")
        form.addRow("", self.auto_pair_check)

        self.highlight_current_check = QCheckBox("高亮当前行")
        form.addRow("", self.highlight_current_check)

        self.tabs.addTab(w, "编辑器")

    def _build_shortcuts_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        layout.addWidget(QLabel("双击快捷键可修改:"))

        self.shortcut_table = QTableWidget()
        self.shortcut_table.setColumnCount(2)
        self.shortcut_table.setHorizontalHeaderLabels(['功能', '快捷键'])
        self.shortcut_table.setColumnWidth(0, 220)
        self.shortcut_table.setColumnWidth(1, 180)
        layout.addWidget(self.shortcut_table)

        # 默认快捷键
        self._shortcut_defs = [
            ('新建查询', 'Ctrl+N'),
            ('执行', 'Ctrl+Return'),
            ('执行选中', 'Ctrl+Shift+Return'),
            ('EXPLAIN', 'Ctrl+E'),
            ('打开 SQL', 'Ctrl+O'),
            ('保存 SQL', 'Ctrl+S'),
            ('关闭标签', 'Ctrl+W'),
            ('刷新', 'F9'),
            ('性能面板', 'Ctrl+P'),
            ('查询历史', 'Ctrl+H'),
            ('数据图表', 'Ctrl+G'),
            ('设置', 'Ctrl+,'),
        ]
        self.shortcut_table.setRowCount(len(self._shortcut_defs))
        for i, (name, key) in enumerate(self._shortcut_defs):
            self.shortcut_table.setItem(i, 0, QTableWidgetItem(name))
            self.shortcut_table.setItem(i, 1, QTableWidgetItem(key))

        btn_row = QHBoxLayout()
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._reset_shortcuts)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.tabs.addTab(w, "快捷键")

    def _build_advanced_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        # 日志
        log_group = QGroupBox("日志")
        lg = QFormLayout(log_group)
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(['DEBUG', 'INFO', 'WARNING', 'ERROR'])
        lg.addRow("日志级别:", self.log_level_combo)
        layout.addWidget(log_group)

        # 性能
        perf_group = QGroupBox("性能")
        pg = QFormLayout(perf_group)
        self.query_timeout_spin = QSpinBox()
        self.query_timeout_spin.setRange(0, 3600)
        self.query_timeout_spin.setSuffix(" 秒 (0=无限制)")
        self.query_timeout_spin.setMaximumWidth(200)
        pg.addRow("查询超时:", self.query_timeout_spin)

        self.max_rows_spin = QSpinBox()
        self.max_rows_spin.setRange(100, 1000000)
        self.max_rows_spin.setSingleStep(1000)
        pg.addRow("结果集最大行数:", self.max_rows_spin)
        layout.addWidget(perf_group)

        # 缓存
        cache_group = QGroupBox("缓存")
        cg = QFormLayout(cache_group)
        self.clear_cache_btn = QPushButton("清除查询历史缓存")
        self.clear_cache_btn.clicked.connect(self._clear_history)
        cg.addRow("", self.clear_cache_btn)

        self.reset_btn = QPushButton("重置所有设置")
        self.reset_btn.clicked.connect(self._reset_all)
        cg.addRow("", self.reset_btn)
        layout.addWidget(cache_group)

        layout.addStretch()
        self.tabs.addTab(w, "高级")

    # ==================== 加载 / 保存 ====================

    def _load_values(self):
        s = self.settings

        theme_map = {'dark': 0, 'light': 1, 'highcontrast': 2}
        self.theme_combo.setCurrentIndex(theme_map.get(s.theme, 0))

        lang_map = {'zh_CN': 0, 'en_US': 1}
        self.lang_combo.setCurrentIndex(lang_map.get(s.language, 0))

        self.page_size_spin.setValue(s.page_size)
        self.auto_connect_check.setChecked(s.auto_connect)
        self.restore_tabs_check.setChecked(s.restore_tabs)

        self.font_family_combo.setCurrentText(s.editor_font_family)
        self.font_size_spin.setValue(s.editor_font_size)
        self.tab_size_spin.setValue(s.editor_tab_size)
        self.auto_complete_check.setChecked(s.editor_auto_complete)
        self.auto_pair_check.setChecked(s.editor_auto_pair)
        self.highlight_current_check.setChecked(s.editor_highlight_line)

        self.log_level_combo.setCurrentText(s.log_level)
        self.query_timeout_spin.setValue(s.query_timeout)
        self.max_rows_spin.setValue(s.max_result_rows)

    def _collect(self):
        s = self.settings

        theme_idx = self.theme_combo.currentIndex()
        s.theme = ['dark', 'light', 'highcontrast'][theme_idx]

        lang_idx = self.lang_combo.currentIndex()
        s.language = ['zh_CN', 'en_US'][lang_idx]

        s.page_size = self.page_size_spin.value()
        s.auto_connect = self.auto_connect_check.isChecked()
        s.restore_tabs = self.restore_tabs_check.isChecked()

        s.editor_font_family = self.font_family_combo.currentText()
        s.editor_font_size = self.font_size_spin.value()
        s.editor_tab_size = self.tab_size_spin.value()
        s.editor_auto_complete = self.auto_complete_check.isChecked()
        s.editor_auto_pair = self.auto_pair_check.isChecked()
        s.editor_highlight_line = self.highlight_current_check.isChecked()

        s.log_level = self.log_level_combo.currentText()
        s.query_timeout = self.query_timeout_spin.value()
        s.max_result_rows = self.max_rows_spin.value()

        # 快捷键
        s.shortcuts = {}
        for i in range(self.shortcut_table.rowCount()):
            name = self.shortcut_table.item(i, 0).text()
            key = self.shortcut_table.item(i, 1).text()
            s.shortcuts[name] = key

    def _apply(self):
        self._collect()
        self.settings.save()
        # 通知应用（通过父窗口的 refresh 信号）

    def _apply_and_close(self):
        self._apply()
        self.accept()

    def _reset_shortcuts(self):
        for i, (name, key) in enumerate(self._shortcut_defs):
            self.shortcut_table.setItem(i, 1, QTableWidgetItem(key))

    def _clear_history(self):
        ret = QMessageBox.question(self, "确认", "清除所有查询历史？")
        if ret != QMessageBox.Yes:
            return
        from ..utils.history import QueryHistory
        h = QueryHistory()
        h.clear()
        QMessageBox.information(self, "完成", "已清除")

    def _reset_all(self):
        ret = QMessageBox.question(self, "确认", "重置所有设置为默认值？")
        if ret != QMessageBox.Yes:
            return
        from ..config import Settings
        self.settings.__dict__.update(Settings().__dict__)
        self.settings.save()
        self._load_values()
        QMessageBox.information(self, "完成", "已重置")