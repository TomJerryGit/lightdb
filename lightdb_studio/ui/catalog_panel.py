"""数据目录面板"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QComboBox, QLabel, QTextEdit,
    QSplitter, QMessageBox, QDialog, QFormLayout, QLineEdit,
    QDialogButtonBox, QCheckBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont

from ..core.catalog import (
    CatalogEngine, SENSITIVITY_LEVELS, TableMeta)


class TableMetaEditorDialog(QDialog):
    """表元数据编辑"""

    def __init__(self, meta: TableMeta, parent=None):
        super().__init__(parent)
        self.meta = meta
        self.setWindowTitle(f"编辑元数据 - {meta.name}")
        self.resize(560, 480)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit(self.meta.business_name)
        self.name_edit.setPlaceholderText("业务名称，如：用户基本信息表")
        form.addRow("业务名称:", self.name_edit)

        self.desc_edit = QTextEdit(self.meta.comment)
        self.desc_edit.setMaximumHeight(80)
        self.desc_edit.setPlaceholderText("表用途说明...")
        form.addRow("描述:", self.desc_edit)

        self.owner_edit = QLineEdit(self.meta.owner)
        self.owner_edit.setPlaceholderText("数据负责人，如：张三")
        form.addRow("负责人:", self.owner_edit)

        self.domain_combo = QComboBox()
        self.domain_combo.setEditable(True)
        self.domain_combo.addItems([
            '未分类', '用户中心', '交易中心', '商品中心',
            '财务中心', '风控中心', '数据仓库', '系统配置',
        ])
        self.domain_combo.setCurrentText(self.meta.domain)
        form.addRow("业务域:", self.domain_combo)

        self.sens_combo = QComboBox()
        for key, info in SENSITIVITY_LEVELS.items():
            self.sens_combo.addItem(
                f"{info['label']} ({key})", key)
        idx = list(SENSITIVITY_LEVELS.keys()).index(
            self.meta.sensitivity)
        self.sens_combo.setCurrentIndex(idx)
        form.addRow("敏感度:", self.sens_combo)

        self.tags_edit = QLineEdit(', '.join(self.meta.tags))
        self.tags_edit.setPlaceholderText("逗号分隔，如: 核心, 高频, 报表")
        form.addRow("标签:", self.tags_edit)

        layout.addLayout(form)
        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_data(self):
        return {
            'business_name': self.name_edit.text().strip(),
            'comment': self.desc_edit.toPlainText().strip(),
            'owner': self.owner_edit.text().strip(),
            'domain': self.domain_combo.currentText().strip() or '未分类',
            'sensitivity': self.sens_combo.currentData(),
            'tags': [t.strip() for t in self.tags_edit.text().split(',')
                     if t.strip()],
        }


class CatalogPanel(QWidget):
    """数据目录面板"""

    table_selected = Signal(str, str)   # db, table

    def __init__(self, catalog: CatalogEngine, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # 搜索栏
        search_row = QHBoxLayout()

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "搜索表名、列名、描述、标签...")
        self.search_edit.textChanged.connect(self._on_search)
        search_row.addWidget(self.search_edit, 1)

        self.refresh_btn = QPushButton("🔄")
        self.refresh_btn.setMaximumWidth(32)
        self.refresh_btn.setToolTip("重新扫描元数据")
        self.refresh_btn.clicked.connect(self.rescan)
        search_row.addWidget(self.refresh_btn)
        layout.addLayout(search_row)

        # 过滤栏
        filter_row = QHBoxLayout()

        filter_row.addWidget(QLabel("敏感度:"))
        self.sens_combo = QComboBox()
        self.sens_combo.addItem('全部', '')
        for key, info in SENSITIVITY_LEVELS.items():
            self.sens_combo.addItem(info['label'], key)
        self.sens_combo.currentIndexChanged.connect(self._on_search)
        filter_row.addWidget(self.sens_combo)

        filter_row.addWidget(QLabel("业务域:"))
        self.domain_combo = QComboBox()
        self.domain_combo.addItem('全部', '')
        self.domain_combo.currentIndexChanged.connect(self._on_search)
        filter_row.addWidget(self.domain_combo)

        filter_row.addWidget(QLabel("标签:"))
        self.tag_combo = QComboBox()
        self.tag_combo.addItem('全部', '')
        self.tag_combo.currentIndexChanged.connect(self._on_search)
        filter_row.addWidget(self.tag_combo)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        # 主体
        splitter = QSplitter(Qt.Horizontal)

        # 左：结果列表
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        self.result_list = QListWidget()
        self.result_list.setFont(QFont("Microsoft YaHei", 10))
        self.result_list.itemSelectionChanged.connect(self._on_select)
        ll.addWidget(self.result_list)
        splitter.addWidget(left)

        # 右：详情
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        self.detail_tabs = QTabWidget()

        # 概览
        overview_w = QWidget()
        ol = QVBoxLayout(overview_w)
        self.overview = QTextEdit()
        self.overview.setReadOnly(True)
        self.overview.setFont(QFont("Microsoft YaHei", 10))
        ol.addWidget(self.overview)
        self.detail_tabs.addTab(overview_w, "概览")

        # 列信息
        cols_w = QWidget()
        cl = QVBoxLayout(cols_w)
        self.cols_table = QTableWidget()
        self.cols_table.setColumnCount(6)
        self.cols_table.setHorizontalHeaderLabels(
            ['列名', '类型', '业务名', '敏感度', '标签', '描述'])
        self.cols_table.horizontalHeader().setStretchLastSection(True)
        self.cols_table.setColumnWidth(0, 120)
        self.cols_table.setColumnWidth(1, 100)
        self.cols_table.setColumnWidth(2, 120)
        self.cols_table.setColumnWidth(3, 80)
        self.cols_table.setColumnWidth(4, 120)
        cl.addWidget(self.cols_table)
        self.detail_tabs.addTab(cols_w, "列信息")

        # Top 查询
        queries_w = QWidget()
        ql = QVBoxLayout(queries_w)
        self.queries_list = QListWidget()
        self.queries_list.setFont(QFont("Consolas", 9))
        ql.addWidget(self.queries_list)
        self.detail_tabs.addTab(queries_w, "常用查询")

        rl.addWidget(self.detail_tabs)

        # 操作
        btn_row = QHBoxLayout()

        edit_btn = QPushButton("✏ 编辑元数据")
        edit_btn.clicked.connect(self._edit_meta)
        btn_row.addWidget(edit_btn)

        jump_btn = QPushButton("↗ 跳转到表")
        jump_btn.clicked.connect(self._jump_to_table)
        btn_row.addWidget(jump_btn)

        impact_btn = QPushButton("📊 影响分析")
        impact_btn.clicked.connect(self._show_impact)
        btn_row.addWidget(impact_btn)
        btn_row.addStretch()
        rl.addLayout(btn_row)

        splitter.addWidget(right)
        splitter.setSizes([320, 520])
        layout.addWidget(splitter)

        self._refresh_filters()
        self._current_meta = None

    # ==================== 刷新 ====================

    def refresh(self):
        self._refresh_filters()
        self._on_search()

    def _refresh_filters(self):
        # 业务域
        current_domain = self.domain_combo.currentText()
        self.domain_combo.blockSignals(True)
        self.domain_combo.clear()
        self.domain_combo.addItem('全部', '')
        for d in self.catalog.list_domains():
            self.domain_combo.addItem(d, d)
        idx = self.domain_combo.findText(current_domain)
        if idx >= 0:
            self.domain_combo.setCurrentIndex(idx)
        self.domain_combo.blockSignals(False)

        # 标签
        current_tag = self.tag_combo.currentText()
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem('全部', '')
        for t in self.catalog.list_tags():
            self.tag_combo.addItem(t, t)
        idx = self.tag_combo.findText(current_tag)
        if idx >= 0:
            self.tag_combo.setCurrentIndex(idx)
        self.tag_combo.blockSignals(False)

    def _on_search(self):
        query = self.search_edit.text()
        filters = {
            'sensitivity': self.sens_combo.currentData() or None,
            'domain': self.domain_combo.currentData() or None,
            'tag': self.tag_combo.currentData() or None,
        }
        results = self.catalog.search(query, filters, limit=200)

        self.result_list.clear()
        for r in results:
            t = r['table']
            sens = SENSITIVITY_LEVELS.get(t.sensitivity, {})
            label = sens.get('label', '')
            icon = {
                'public': '🟢', 'internal': '🔵',
                'confidential': '🟡', 'restricted': '🟠', 'pii': '🔴',
            }.get(t.sensitivity, '⚪')

            text = f"{icon} {t.database}.{t.name}"
            if t.business_name:
                text += f"  ({t.business_name})"

            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, r['key'])
            item.setToolTip(
                f"域: {t.domain} | 行数: {t.row_count:,} | "
                f"查询: {t.query_count} 次\n{t.comment[:200]}")

            # 颜色
            if t.sensitivity == 'pii':
                item.setForeground(QColor('#f48771'))
            elif t.sensitivity == 'restricted':
                item.setForeground(QColor('#ce9178'))
            elif t.sensitivity == 'confidential':
                item.setForeground(QColor('#dcdcaa'))

            self.result_list.addItem(item)

    def _on_select(self):
        item = self.result_list.currentItem()
        if not item:
            return
        key = item.data(Qt.UserRole)
        meta = self.catalog.tables.get(key)
        if not meta:
            return
        self._current_meta = meta
        self._show_detail(meta)

    def _show_detail(self, meta: TableMeta):
        # 概览
        sens = SENSITIVITY_LEVELS.get(meta.sensitivity, {})
        lines = [
            f"表名: {meta.database}.{meta.name}",
            f"业务名称: {meta.business_name or '(未设置)'}",
            f"描述: {meta.comment or '(无)'}",
            f"负责人: {meta.owner or '(未设置)'}",
            f"业务域: {meta.domain}",
            f"敏感度: {sens.get('label', '')} ({meta.sensitivity})",
            f"标签: {', '.join(meta.tags) or '(无)'}",
            f"行数: {meta.row_count:,}",
            f"列数: {len(meta.columns)}",
            f"查询次数: {meta.query_count}",
            f"最近查询: "
            f"{self._fmt_time(meta.last_queried) if meta.last_queried else '(从未)'}",
        ]
        self.overview.setPlainText('\n'.join(lines))

        # 列信息
        self.cols_table.setRowCount(len(meta.columns))
        for i, c in enumerate(meta.columns):
            self.cols_table.setItem(i, 0, QTableWidgetItem(c.name))
            self.cols_table.setItem(i, 1, QTableWidgetItem(c.type))

            biz = c.business_name or ''
            self.cols_table.setItem(i, 2, QTableWidgetItem(biz))

            sens_info = SENSITIVITY_LEVELS.get(c.sensitivity, {})
            si = QTableWidgetItem(sens_info.get('label', ''))
            si.setForeground(QColor(sens_info.get('color', '#d4d4d4')))
            self.cols_table.setItem(i, 3, si)

            self.cols_table.setItem(
                i, 4, QTableWidgetItem(', '.join(c.tags)))

            self.cols_table.setItem(
                i, 5, QTableWidgetItem(c.comment or ''))

        # Top 查询
        self.queries_list.clear()
        for sql in meta.top_queries:
            item = QListWidgetItem(sql)
            item.setToolTip(sql)
            self.queries_list.addItem(item)

    def _fmt_time(self, ts):
        from datetime import datetime
        if not ts:
            return ''
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')

    # ==================== 操作 ====================

    def _edit_meta(self):
        if not self._current_meta:
            return
        meta = self._current_meta
        dlg = TableMetaEditorDialog(meta, self)
        if dlg.exec() == TableMetaEditorDialog.Accepted:
            data = dlg.get_data()
            self.catalog.update_table_meta(
                meta.database, meta.name, **data)
            self.refresh()
            QMessageBox.information(self, "完成", "元数据已保存")

    def _jump_to_table(self):
        if not self._current_meta:
            return
        meta = self._current_meta
        self.table_selected.emit(meta.database, meta.name)

    def _show_impact(self):
        if not self._current_meta:
            return
        meta = self._current_meta
        # 需要 LineageStore，从主窗口获取
        win = self.window()
        if hasattr(win, 'lineage_enhanced'):
            from .impact_dialog import ImpactDialog
            dlg = ImpactDialog(
                win.lineage_enhanced,
                meta.name, None, win)
            dlg.exec()

    # ==================== 重新扫描 ====================

    def rescan(self):
        """重新扫描当前连接的元数据"""
        win = self.window()
        conn = None
        if hasattr(win, '_get_current_conn'):
            conn = win._get_current_conn()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择连接")
            return

        try:
            db = conn.config.database or 'default'
            tables = conn.get_tables()
        except Exception as e:
            QMessageBox.critical(self, "扫描失败", str(e))
            return

        n = 0
        for t in tables:
            try:
                cols = conn.get_columns(t)
                # 表注释（如果有）
                comment = ''
                try:
                    _, r = conn.query(
                        f"SELECT COUNT(*) FROM `{t}`")
                    rows = r[0][0] if r else 0
                except Exception:
                    rows = 0

                self.catalog.index_table(
                    db, t, conn.config.name, cols,
                    comment=comment, row_count=rows)
                n += 1
            except Exception:
                pass

        self.refresh()
        QMessageBox.information(
            self, "完成", f"已扫描 {n} 张表")