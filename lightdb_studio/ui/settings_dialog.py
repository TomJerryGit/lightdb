"""设置中心（P2：语言热切换 / 高 DPI 说明 / 可访问性）

- 语言下拉 zh_CN/en_US：应用后立即 `i18n.set_lang()`（主窗口监听并热重译），
  `settings.language` 持久化到 settings.json；
- 高 DPI：Qt6 默认自动高 DPI，本版本不提供应用内覆盖；界面明确显示“自动
  （由系统 / Qt6 High-DPI 控制）”并展示实际 devicePixelRatio，避免静默无效；
- 关键控件设置 accessibleName。
"""
from PySide6.QtWidgets import (
    QDialog, QTabWidget, QWidget, QVBoxLayout, QFormLayout,
    QComboBox, QSpinBox, QCheckBox, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QLineEdit, QDialogButtonBox,
    QKeySequenceEdit, QHBoxLayout, QMessageBox, QGroupBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from ..utils.i18n import i18n


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(i18n.t('settings.title'))
        self.setAccessibleName(i18n.t('a11y.settings'))
        self.resize(720, 600)
        self._build_ui()
        self._load_values()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName(i18n.t('a11y.settings'))
        self._build_general_tab()
        self._build_editor_tab()
        self._build_shortcuts_tab()
        self._build_advanced_tab()
        layout.addWidget(self.tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
            | QDialogButtonBox.Apply)
        buttons.button(QDialogButtonBox.Ok).setText(i18n.t('settings.ok'))
        buttons.button(QDialogButtonBox.Cancel).setText(
            i18n.t('settings.cancel'))
        buttons.button(QDialogButtonBox.Apply).setText(
            i18n.t('settings.apply'))
        self._ok_btn = buttons.button(QDialogButtonBox.Ok)
        self._cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        self._apply_btn = buttons.button(QDialogButtonBox.Apply)
        buttons.accepted.connect(self._apply_and_close)
        buttons.rejected.connect(self.reject)
        self._apply_btn.clicked.connect(self._apply)
        layout.addWidget(buttons)

    def _a11y(self, widget, name):
        try:
            widget.setAccessibleName(name)
            widget.setToolTip(name)
        except Exception:
            pass
        return widget

    def _build_general_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.theme_combo = self._a11y(QComboBox(), '主题 / Theme')
        self.theme_combo.addItems(['深色 (Dark)', '浅色 (Light)',
                                   '高对比度 (High Contrast)'])
        self.theme_combo.setToolTip(
            'N1 主题：深色/浅色 Navicat 风格；切换立即预览，Apply 后持久化')
        self.theme_combo.currentIndexChanged.connect(self._preview_theme)
        form.addRow(i18n.t('settings.theme'), self.theme_combo)

        self.lang_combo = self._a11y(QComboBox(), '语言 / Language')
        self.lang_combo.addItems(['简体中文', 'English'])
        self.lang_combo.setToolTip(
            '切换界面语言（zh_CN / en_US），立即生效')
        form.addRow(i18n.t('settings.language'), self.lang_combo)

        self.page_size_spin = self._a11y(QSpinBox(), '默认每页行数 / Page size')
        self.page_size_spin.setRange(50, 5000)
        self.page_size_spin.setSingleStep(50)
        form.addRow(i18n.t('settings.page_size'), self.page_size_spin)

        self.auto_connect_check = self._a11y(
            QCheckBox(i18n.t('settings.auto_connect')), '自动连接 / Auto connect')
        self.auto_connect_check.setChecked(True)
        form.addRow('', self.auto_connect_check)

        self.restore_tabs_check = self._a11y(
            QCheckBox(i18n.t('settings.restore_tabs')), '恢复标签 / Restore tabs')
        form.addRow('', self.restore_tabs_check)

        # 高 DPI（Qt6 默认自动）：明确展示，不做假设置
        self.dpi_label = QLabel(i18n.t('settings.high_dpi_auto'))
        self.dpi_label.setWordWrap(True)
        self.dpi_label.setStyleSheet('color: #9cdcfe;')
        form.addRow(i18n.t('settings.high_dpi'), self.dpi_label)
        self.high_dpi_note = QLabel(i18n.t('settings.high_dpi_note'))
        self.high_dpi_note.setWordWrap(True)
        self.high_dpi_note.setStyleSheet('color: #969696;')
        self.high_dpi_note.setAccessibleName('High DPI 说明 / High DPI note')
        form.addRow('', self.high_dpi_note)

        self.tabs.addTab(w, i18n.t('settings.tab.general'))

    def _build_editor_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.font_family_combo = self._a11y(QComboBox(), '编辑器字体 / Font')
        self.font_family_combo.setEditable(True)
        self.font_family_combo.addItems(
            ['Consolas', 'Courier New', 'Microsoft YaHei Mono',
             'Cascadia Code', 'JetBrains Mono', 'Monaco'])
        form.addRow(i18n.t('settings.editor_font'), self.font_family_combo)

        self.font_size_spin = self._a11y(QSpinBox(), '字号 / Font size')
        self.font_size_spin.setRange(8, 32)
        form.addRow(i18n.t('settings.font_size'), self.font_size_spin)

        self.tab_size_spin = self._a11y(QSpinBox(), 'Tab 缩进 / Tab size')
        self.tab_size_spin.setRange(2, 12)
        form.addRow(i18n.t('settings.tab_size'), self.tab_size_spin)

        self.auto_complete_check = self._a11y(
            QCheckBox(i18n.t('settings.auto_complete')), '自动补全 / Auto complete')
        form.addRow('', self.auto_complete_check)

        self.auto_pair_check = self._a11y(
            QCheckBox(i18n.t('settings.auto_pair')), '自动配对 / Auto pair')
        form.addRow('', self.auto_pair_check)

        self.highlight_current_check = self._a11y(
            QCheckBox(i18n.t('settings.highlight_line')), '高亮当前行 / Highlight line')
        form.addRow('', self.highlight_current_check)

        self.tabs.addTab(w, i18n.t('settings.tab.editor'))

    def _build_shortcuts_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        layout.addWidget(QLabel(i18n.t('settings.shortcuts_hint')))
        self.shortcut_table = QTableWidget()
        self.shortcut_table.setAccessibleName('快捷键表 / Shortcut table')
        self.shortcut_table.setColumnCount(2)
        self.shortcut_table.setHorizontalHeaderLabels(
            [i18n.t('settings.shortcut_feature'), i18n.t('settings.shortcut_key')])
        self.shortcut_table.setColumnWidth(0, 220)
        self.shortcut_table.setColumnWidth(1, 180)
        layout.addWidget(self.shortcut_table)

        self._shortcut_defs = []
        self._shortcut_ids = []
        from .shortcuts import shortcuts_for_settings_page
        page = shortcuts_for_settings_page()
        self.shortcut_table.setRowCount(len(page))
        for i, (action_id, label, key, editor_only) in enumerate(page):
            self._shortcut_ids.append(action_id)
            self._shortcut_defs.append((label, key))
            self.shortcut_table.setItem(i, 0, QTableWidgetItem(label))
            self.shortcut_table.setItem(i, 1, QTableWidgetItem(key))

        btn_row = QHBoxLayout()
        reset_btn = QPushButton(i18n.t('settings.reset_shortcuts'))
        reset_btn.setAccessibleName(i18n.t('settings.reset_shortcuts'))
        reset_btn.clicked.connect(self._reset_shortcuts)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        self.tabs.addTab(w, i18n.t('settings.tab.shortcuts'))

    def _build_advanced_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        log_group = QGroupBox(i18n.t('settings.log'))
        lg = QFormLayout(log_group)
        self.log_level_combo = self._a11y(QComboBox(), '日志级别 / Log level')
        self.log_level_combo.addItems(['DEBUG', 'INFO', 'WARNING', 'ERROR'])
        lg.addRow(i18n.t('settings.log_level'), self.log_level_combo)
        layout.addWidget(log_group)

        perf_group = QGroupBox(i18n.t('settings.performance'))
        pg = QFormLayout(perf_group)
        self.query_timeout_spin = self._a11y(
            QSpinBox(), '查询超时 / Query timeout')
        self.query_timeout_spin.setRange(0, 3600)
        self.query_timeout_spin.setSuffix(i18n.t('settings.timeout_suffix'))
        self.query_timeout_spin.setMaximumWidth(220)
        pg.addRow(i18n.t('settings.query_timeout'), self.query_timeout_spin)
        self.max_rows_spin = self._a11y(
            QSpinBox(), '结果最大行数 / Max rows')
        self.max_rows_spin.setRange(100, 1000000)
        self.max_rows_spin.setSingleStep(1000)
        pg.addRow(i18n.t('settings.max_rows'), self.max_rows_spin)
        layout.addWidget(perf_group)

        cache_group = QGroupBox(i18n.t('settings.cache'))
        cg = QFormLayout(cache_group)
        self.clear_cache_btn = QPushButton(i18n.t('settings.clear_history'))
        self.clear_cache_btn.setAccessibleName(i18n.t('settings.clear_history'))
        self.clear_cache_btn.clicked.connect(self._clear_history)
        cg.addRow('', self.clear_cache_btn)
        self.reset_btn = QPushButton(i18n.t('settings.reset_all'))
        self.reset_btn.setAccessibleName(i18n.t('settings.reset_all'))
        self.reset_btn.clicked.connect(self._reset_all)
        cg.addRow('', self.reset_btn)
        layout.addWidget(cache_group)
        layout.addStretch()
        self.tabs.addTab(w, i18n.t('settings.tab.advanced'))

    # ==================== 加载 / 保存 ====================

    def _preview_theme(self, *_args):
        """N1：主题切换立即预览（Apply 时才保存到 settings）。"""
        try:
            from PySide6.QtWidgets import QApplication
            from .theme import apply_theme
            names = ['dark', 'light', 'highcontrast']
            apply_theme(QApplication.instance(),
                        names[self.theme_combo.currentIndex()])
        except Exception:
            pass

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
        from .shortcuts import load_shortcuts
        saved_shortcuts = load_shortcuts(s.shortcuts)
        for i, action_id in enumerate(getattr(self, '_shortcut_ids', [])):
            if i < self.shortcut_table.rowCount():
                item = self.shortcut_table.item(i, 1)
                if item is None:
                    item = QTableWidgetItem('')
                    self.shortcut_table.setItem(i, 1, item)
                item.setText(saved_shortcuts.get(action_id, ''))

    def _collect(self):
        s = self.settings
        s.theme = ['dark', 'light', 'highcontrast'][
            self.theme_combo.currentIndex()]
        s.language = ['zh_CN', 'en_US'][self.lang_combo.currentIndex()]
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
        s.shortcuts = {}
        for i, action_id in enumerate(getattr(self, '_shortcut_ids', [])):
            item = self.shortcut_table.item(i, 1)
            key = item.text().strip() if item is not None else ''
            s.shortcuts[action_id] = key
        return s

    def apply_settings(self) -> dict:
        """保存设置并立即切换 i18n 语言（主窗口监听后热重译）。"""
        s = self._collect()
        from .shortcuts import validate_shortcuts
        validation = validate_shortcuts(s.shortcuts)
        if not validation['ok']:
            lines = []
            for c in validation['conflicts']:
                lines.append(f"{c['key']}: " + ' / '.join(c['labels']))
            lines.extend(validation['errors'])
            QMessageBox.warning(
                self, '快捷键冲突', '存在重复/非法快捷键，未保存：\n'
                + '\n'.join(lines[:10]))
            return {'ok': False, 'language': s.language,
                    'conflicts': validation['conflicts'],
                    'errors': validation['errors']}
        s.save()
        changed = i18n.set_lang(s.language)
        # 自身文案也刷新（保持打开时不再显示旧语言）
        self.setWindowTitle(i18n.t('settings.title'))
        self._ok_btn.setText(i18n.t('settings.ok'))
        self._cancel_btn.setText(i18n.t('settings.cancel'))
        self._apply_btn.setText(i18n.t('settings.apply'))
        for idx, key in enumerate(('settings.tab.general',
                                   'settings.tab.editor',
                                   'settings.tab.shortcuts',
                                   'settings.tab.advanced')):
            if idx < self.tabs.count():
                self.tabs.setTabText(idx, i18n.t(key))
        return {'ok': changed, 'language': s.language,
                'high_dpi_auto': True,
                'dpi_note': self.high_dpi_note.text()}

    def _apply(self):
        return self.apply_settings()

    def _apply_and_close(self):
        self.apply_settings()
        self.accept()

    def _reset_shortcuts(self):
        for i, (_name, key) in enumerate(self._shortcut_defs):
            self.shortcut_table.setItem(i, 1, QTableWidgetItem(key))

    def _clear_history(self):
        ret = QMessageBox.question(
            self, i18n.t('dialog.confirm'),
            i18n.t('settings.clear_confirm'))
        if ret != QMessageBox.Yes:
            return
        from ..utils.history import QueryHistory
        h = QueryHistory()
        h.clear()
        QMessageBox.information(self, i18n.t('dialog.success'),
                                i18n.t('settings.clear_done'))

    def _reset_all(self):
        ret = QMessageBox.question(
            self, i18n.t('dialog.confirm'),
            i18n.t('settings.reset_confirm'))
        if ret != QMessageBox.Yes:
            return
        from ..config import Settings
        self.settings.__dict__.update(Settings().__dict__)
        self.settings.save()
        self._load_values()
        i18n.set_lang(self.settings.language)
        QMessageBox.information(self, i18n.t('dialog.success'),
                                i18n.t('settings.reset_done'))
