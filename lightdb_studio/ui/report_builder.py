# -*- coding: utf-8 -*-
"""报表设计器（P2-3 产品化）

- 参数化查询：`:name` / `?` 占位符，执行前弹参数输入；缺参/多参显式报错；
  参数走 `conn.query(sql, params)`，绝不手拼 SQL；
- 模板管理：新建/编辑/重命名/复制/删除，保存校验（名称/SQL 非空、占位符合法）；
- 预览：前 50/100/500 行可配，内嵌结果表格；
- 导出 HTML/CSV/XLSX（落盘后真实文件校验由调用方/测试完成）；
- 模板持久化 round-trip（ReportEngine 负责 JSON，含 params / chart /
  preview_limit），旧模板（无这些字段）兼容；
- 图表：从当前预览打开 `ChartDialog`，配置写入模板 `chart` 字段。
"""
import json
import os
import time

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QComboBox, QPushButton, QFormLayout, QGroupBox, QTableWidget,
    QTableWidgetItem, QDialogButtonBox, QMessageBox, QTabWidget,
    QWidget, QFileDialog, QListWidget, QListWidgetItem, QSplitter,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from ..core.reports import (
    ReportTemplate, ReportEngine, ReportParameterError,
    ReportValidationError, extract_sql_params,
)
from .chart_view import ChartDialog

PREVIEW_LIMITS = (50, 100, 500)


class ParamInputDialog(QDialog):
    """按 SQL 占位符生成参数输入框（无参数时调用方不应弹窗）。"""

    def __init__(self, sql, defaults=None, title='输入查询参数', parent=None):
        super().__init__(parent)
        self.sql = sql or ''
        self.req = extract_sql_params(self.sql)
        self.defaults = dict(defaults or {})
        self.edits = {}      # name -> QLineEdit（named）或 index -> QLineEdit
        self.setWindowTitle(title)
        self.resize(460, 220)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        if self.req['style'] == 'named':
            for name in self.req['names']:
                edit = QLineEdit()
                edit.setPlaceholderText(f':{name} 的值')
                if name in self.defaults:
                    edit.setText(str(self.defaults[name]))
                self.edits[name] = edit
                form.addRow(name + ':', edit)
        elif self.req['style'] == 'qmark':
            for i in range(self.req['qmark_count']):
                edit = QLineEdit()
                edit.setPlaceholderText(f'第 {i + 1} 个 ? 参数')
                self.edits[i] = edit
                form.addRow(f'参数 {i + 1}:', edit)
        else:
            form.addRow(QLabel('该 SQL 没有参数'))
        layout.addLayout(form)
        layout.addStretch()
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        if self.req['style'] == 'named':
            return {name: edit.text() for name, edit in self.edits.items()}
        if self.req['style'] == 'qmark':
            return [self.edits[i].text() for i in range(len(self.edits))]
        return {}

    def validate(self):
        """返回 (ok, error)；空文本框视为未提供该参数。"""
        if self.req['style'] == 'mixed':
            return False, 'SQL 不能混用 :name 与 ? 占位符'
        if self.req['style'] == 'none':
            return True, ''
        if self.req['style'] == 'named':
            missing = [name for name, edit in self.edits.items()
                       if not edit.text().strip()]
            if missing:
                return False, '缺少参数: ' + ', '.join(missing)
        else:
            missing = [str(i + 1) for i, edit in self.edits.items()
                       if not edit.text().strip()]
            if missing:
                return False, '缺少参数: 第 ' + ', '.join(missing) + ' 个'
        return True, ''

    def _accept(self):
        ok, err = self.validate()
        if not ok:
            QMessageBox.warning(self, '参数错误', err)
            return
        self.accept()


class ReportBuilder(QDialog):
    def __init__(self, engine: ReportEngine, conn_getter=None,
                 current_sql: str = '', parent=None):
        super().__init__(parent)
        self.engine = engine
        self.conn_getter = conn_getter
        self.last_preview = None
        self.last_error = ''
        self.last_export = None
        self._last_params = None
        self._chart_config = {}
        self._params_defaults = {}
        self.setWindowTitle('报表管理')
        self.resize(1060, 760)
        self._build_ui()
        self._refresh_list()
        self._refresh_outputs()
        self.sql_edit.setPlainText(current_sql)
        self._update_param_hint()

    # ==================== UI ====================

    def _build_ui(self):
        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)

        # 左：模板列表
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel('📋 报表模板:'))
        self.list_widget = QListWidget()
        self.list_widget.itemSelectionChanged.connect(self._on_select)
        ll.addWidget(self.list_widget)
        row = QHBoxLayout()
        new_btn = QPushButton('➕ 新建')
        new_btn.clicked.connect(self._new_template)
        row.addWidget(new_btn)
        del_btn = QPushButton('🗑 删除')
        del_btn.clicked.connect(self._delete_template)
        row.addWidget(del_btn)
        ll.addLayout(row)
        rename_btn = QPushButton('✏ 重命名')
        rename_btn.clicked.connect(self._rename_template)
        ll.addWidget(rename_btn)
        copy_btn = QPushButton('📋 复制')
        copy_btn.clicked.connect(self._duplicate_template)
        ll.addWidget(copy_btn)
        splitter.addWidget(left)

        # 右：编辑器
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow('报表名称:', self.name_edit)
        self.desc_edit = QLineEdit()
        form.addRow('描述:', self.desc_edit)
        self.conn_edit = QLineEdit()
        form.addRow('连接:', self.conn_edit)
        self.db_edit = QLineEdit()
        form.addRow('数据库:', self.db_edit)
        rl.addLayout(form)
        rl.addWidget(QLabel('SQL 查询（支持 :name 或 ? 参数占位符）:'))
        self.sql_edit = QTextEdit()
        self.sql_edit.setFont(QFont('Consolas', 10))
        self.sql_edit.setMinimumHeight(140)
        self.sql_edit.textChanged.connect(self._update_param_hint)
        rl.addWidget(self.sql_edit)
        self.param_label = QLabel('')
        self.param_label.setStyleSheet('color: #9cdcfe;')
        rl.addWidget(self.param_label)

        btn_row = QHBoxLayout()
        save_btn = QPushButton('💾 保存')
        save_btn.clicked.connect(self._save_current)
        btn_row.addWidget(save_btn)
        preview_btn = QPushButton('👁 预览')
        preview_btn.clicked.connect(self._preview)
        btn_row.addWidget(preview_btn)
        chart_btn = QPushButton('📈 图表配置...')
        chart_btn.clicked.connect(self._open_chart_config)
        btn_row.addWidget(chart_btn)
        btn_row.addWidget(QLabel('预览行数:'))
        self.limit_combo = QComboBox()
        for n in PREVIEW_LIMITS:
            self.limit_combo.addItem(str(n), n)
        self.limit_combo.setCurrentIndex(1)     # 默认 100
        self.limit_combo.currentIndexChanged.connect(self._update_param_hint)
        btn_row.addWidget(self.limit_combo)
        btn_row.addStretch()
        rl.addLayout(btn_row)

        self.chart_label = QLabel('图表: 未配置')
        self.chart_label.setStyleSheet('color: #969696;')
        rl.addWidget(self.chart_label)

        # 预览表格（内嵌）
        preview_group = QGroupBox('预览结果')
        pg = QVBoxLayout(preview_group)
        self.preview_info = QLabel('尚未预览')
        pg.addWidget(self.preview_info)
        self.preview_table = QTableWidget()
        self.preview_table.setAlternatingRowColors(True)
        pg.addWidget(self.preview_table)
        rl.addWidget(preview_group, 1)

        # 导出区
        exp_group = QGroupBox('导出')
        eg = QHBoxLayout(exp_group)
        eg.addWidget(QLabel('格式:'))
        self.format_combo = QComboBox()
        self.format_combo.addItems(['CSV', 'Excel', 'HTML'])
        eg.addWidget(self.format_combo)
        exp_btn = QPushButton('📤 立即导出')
        exp_btn.clicked.connect(self._export)
        eg.addWidget(exp_btn)
        eg.addStretch()
        rl.addWidget(exp_group)

        # 历史输出
        hist_group = QGroupBox('历史输出')
        hg = QVBoxLayout(hist_group)
        self.output_list = QListWidget()
        hg.addWidget(self.output_list)
        hg_row = QHBoxLayout()
        refresh_out_btn = QPushButton('🔄 刷新')
        refresh_out_btn.clicked.connect(self._refresh_outputs)
        hg_row.addWidget(refresh_out_btn)
        open_btn = QPushButton('📂 打开目录')
        open_btn.clicked.connect(self._open_output_dir)
        hg_row.addWidget(open_btn)
        hg_row.addStretch()
        hg.addLayout(hg_row)
        rl.addWidget(hist_group)

        splitter.addWidget(right)
        splitter.setSizes([300, 760])
        layout.addWidget(splitter)
        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.reject)
        layout.addWidget(close_btn)

    # ==================== 模板列表 / 表单 ====================

    def _refresh_list(self):
        self.list_widget.clear()
        for t in self.engine.templates:
            item = QListWidgetItem(f'📊 {t.name}' +
                                   ('  ⏰' if getattr(t, 'schedule', '') else ''))
            item.setData(Qt.UserRole, t.name)
            self.list_widget.addItem(item)

    def _refresh_outputs(self):
        self.output_list.clear()
        try:
            for o in self.engine.list_outputs()[:50]:
                ts = time.strftime('%Y-%m-%d %H:%M:%S',
                                   time.localtime(o['mtime']))
                item = QListWidgetItem(
                    f"[{ts}] {o['name']} ({o['size'] / 1024:.1f} KB)")
                item.setData(Qt.UserRole, o['path'])
                self.output_list.addItem(item)
        except Exception:
            pass

    def _on_select(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        t = self.engine.get(item.data(Qt.UserRole))
        if not t:
            return
        self._apply_template_to_form(t)

    def _apply_template_to_form(self, t: ReportTemplate):
        self.name_edit.setText(t.name)
        self.desc_edit.setText(t.description)
        self.conn_edit.setText(t.connection)
        self.db_edit.setText(t.database)
        self.sql_edit.setPlainText(t.sql)
        try:
            idx = self.limit_combo.findData(int(t.preview_limit or 100))
            if idx >= 0:
                self.limit_combo.setCurrentIndex(idx)
        except Exception:
            pass
        self._chart_config = dict(t.chart or {})
        self._params_defaults = dict(t.params or {})
        self._update_chart_label()

    def _update_chart_label(self):
        if self._chart_config:
            self.chart_label.setText(
                f"图表: {self._chart_config.get('type', 'bar')} | "
                f"X={self._chart_config.get('x', '')} | "
                f"Y={','.join(self._chart_config.get('y') or [])} | "
                f"聚合={self._chart_config.get('agg', 'SUM')}")
        else:
            self.chart_label.setText('图表: 未配置')

    def _current_limit(self) -> int:
        try:
            return int(self.limit_combo.currentData() or 100)
        except Exception:
            return 100

    def _new_template(self):
        self.list_widget.clearSelection()
        self.name_edit.clear()
        self.desc_edit.clear()
        if self.conn_getter:
            conn = self.conn_getter()
            if conn:
                self.conn_edit.setText(conn.config.name)
                self.db_edit.setText(conn.config.database or '')
        self.sql_edit.clear()
        self._chart_config = {}
        self._params_defaults = {}
        self.last_preview = None
        self.preview_table.clear()
        self.preview_info.setText('尚未预览')
        self._update_param_hint()

    def _update_param_hint(self):
        sql = self.sql_edit.toPlainText()
        req = extract_sql_params(sql)
        if req['style'] == 'mixed':
            self.param_label.setText('⚠ SQL 不能混用 :name 与 ? 占位符')
            return
        if req['style'] == 'named':
            self.param_label.setText(
                '参数: ' + ', '.join(':' + n for n in req['names']))
        elif req['style'] == 'qmark':
            self.param_label.setText(f"参数: {req['qmark_count']} 个 ?")
        else:
            self.param_label.setText('无参数')
        self.param_label.setText(
            self.param_label.text() + f' | 预览行数: {self._current_limit()}')

    # ==================== 参数输入 ====================

    def _ask_params_for(self, sql):
        """UI：弹出参数输入；无参数返回 {}；用户取消返回 None。"""
        req = extract_sql_params(sql)
        if req['style'] == 'none':
            return {}
        if req['style'] == 'mixed':
            QMessageBox.warning(self, '参数错误',
                                'SQL 不能混用 :name 与 ? 占位符')
            return None
        dlg = ParamInputDialog(sql, defaults=self._params_defaults,
                               parent=self)
        if dlg.exec() == QDialog.Accepted:
            values = dlg.values()
            if req['style'] == 'named':
                self._params_defaults = dict(values)
            return values
        return None

    # ==================== 预览 ====================

    def run_preview(self, params=None, limit=None) -> dict:
        """执行预览；params=None 时走参数输入弹窗（无参数不弹）。"""
        sql = self.sql_edit.toPlainText().strip()
        self.last_error = ''
        if not sql:
            self.last_error = 'SQL 不能为空'
            self._warn('提示', self.last_error)
            return {'ok': False, 'error': self.last_error}
        conn = self._get_conn_for(self.conn_edit.text().strip())
        if conn is None:
            self.last_error = '无可用连接'
            self._warn('提示', self.last_error)
            return {'ok': False, 'error': self.last_error}
        if params is None:
            req0 = extract_sql_params(sql)
            params = ({} if req0['style'] == 'none'
                      else self._ask_params_for(sql))
            if params is None:
                return {'ok': False, 'cancelled': True,
                        'error': '已取消参数输入'}
        try:
            result = self.engine.preview_query(
                conn, sql, params,
                limit if limit is not None else self._current_limit())
        except ReportParameterError as exc:
            self.last_error = str(exc)
            self._warn('参数错误', self.last_error)
            return {'ok': False, 'error': self.last_error}
        except Exception as exc:                      # noqa: BLE001
            self.last_error = str(exc)
            self._critical('错误', self.last_error)
            return {'ok': False, 'error': self.last_error}
        self.last_preview = result
        self._last_params = params
        req = extract_sql_params(sql)
        if req['style'] == 'named' and isinstance(params, dict):
            self._params_defaults = dict(params)
        self._show_preview_table(result)
        return {'ok': True, 'error': '', **result}

    def _show_preview_table(self, result):
        cols = result.get('columns') or []
        rows = result.get('rows') or []
        self.preview_table.clear()
        self.preview_table.setColumnCount(len(cols))
        self.preview_table.setRowCount(len(rows))
        self.preview_table.setHorizontalHeaderLabels([str(c) for c in cols])
        for ri, row in enumerate(rows):
            for ci, val in enumerate(row):
                text = 'NULL' if val is None else str(val)
                self.preview_table.setItem(ri, ci, QTableWidgetItem(text))
        self.preview_table.resizeColumnsToContents()
        self.preview_info.setText(
            f"共 {result.get('total', 0)} 行，预览前 "
            f"{result.get('returned', 0)} 行（上限 {result.get('limit', 0)}）"
            + (f" | 参数: {result.get('param_names')}"
               if result.get('param_names') else ''))

    def _preview(self):
        self.run_preview()

    # ==================== 保存 / 重命名 / 复制 / 删除 ====================

    def build_template_from_form(self) -> ReportTemplate:
        name = self.name_edit.text().strip()
        sql = self.sql_edit.toPlainText().strip()
        tmpl = ReportTemplate(
            name=name,
            description=self.desc_edit.text().strip(),
            sql=sql,
            connection=self.conn_edit.text().strip(),
            database=self.db_edit.text().strip(),
            chart=dict(self._chart_config),
            params=dict(self._params_defaults),
            preview_limit=self._current_limit(),
        )
        tmpl.validate()          # 名称/SQL 非空 + 占位符合法（失败抛异常）
        return tmpl

    def save_current_template(self) -> ReportTemplate:
        """校验并保存模板（UI 与测试共用；失败抛 ReportValidationError）。"""
        tmpl = self.build_template_from_form()
        self.engine.upsert(tmpl)
        self._refresh_list()
        return tmpl

    def _save_current(self):
        try:
            self.save_current_template()
        except ReportValidationError as exc:
            self._warn('提示', str(exc))
            return
        except Exception as exc:                      # noqa: BLE001
            self._critical('保存失败', str(exc))
            return
        QMessageBox.information(self, '完成', '报表已保存')

    def _select_name(self, name):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.UserRole) == name:
                self.list_widget.setCurrentRow(i)
                return True
        return False

    def _selected_name(self):
        item = self.list_widget.currentItem()
        return item.data(Qt.UserRole) if item else ''

    def rename_current(self, new_name: str):
        name = self._selected_name()
        if not name:
            raise ReportValidationError('请先选择一个报表模板')
        idx = self.engine.templates.index(self.engine.get(name))
        tmpl = self.engine.rename(idx, new_name)
        self.name_edit.setText(tmpl.name)
        self._refresh_list()
        self._select_name(tmpl.name)
        return tmpl

    def duplicate_current(self, new_name: str = ''):
        name = self._selected_name()
        if not name:
            raise ReportValidationError('请先选择一个报表模板')
        idx = self.engine.templates.index(self.engine.get(name))
        tmpl = self.engine.duplicate(idx, new_name)
        self._refresh_list()
        self._select_name(tmpl.name)
        return tmpl

    def delete_current(self, ask=False) -> bool:
        name = self._selected_name() or self.name_edit.text().strip()
        if not name:
            return False
        if ask:
            ret = QMessageBox.question(self, '确认', f"删除报表 '{name}'？")
            if ret != QMessageBox.Yes:
                return False
        ok = self.engine.remove_by_name(name)
        self._refresh_list()
        return ok

    def _rename_template(self):
        from PySide6.QtWidgets import QInputDialog
        new_name, ok = QInputDialog.getText(self, '重命名', '新名称:')
        if not ok or not new_name.strip():
            return
        try:
            self.rename_current(new_name.strip())
        except Exception as exc:                      # noqa: BLE001
            self._critical('重命名失败', str(exc))

    def _duplicate_template(self):
        try:
            self.duplicate_current()
        except Exception as exc:                      # noqa: BLE001
            self._critical('复制失败', str(exc))

    def _delete_template(self):
        self.delete_current(ask=True)

    # ==================== 图表 ====================

    def set_chart_config(self, cfg):
        self._chart_config = dict(cfg or {})
        self._update_chart_label()

    def build_chart_dialog(self):
        """用当前预览结果构造 ChartDialog（不 exec，便于测试/嵌入）。"""
        if not self.last_preview:
            return None
        dlg = ChartDialog(self.last_preview.get('columns') or [],
                          self.last_preview.get('rows') or [], self)
        if self._chart_config:
            dlg.apply_config(self._chart_config)
        return dlg

    def _open_chart_config(self):
        if not self.last_preview:
            self._warn('提示', '请先预览结果，再配置图表')
            return
        dlg = self.build_chart_dialog()
        if dlg is None:
            return
        dlg.exec()
        self.set_chart_config(dlg.config())
        QMessageBox.information(self, '完成', '图表配置已应用到模板')

    # ==================== 导出 ====================

    def _get_conn_for(self, name):
        if self.conn_getter is None:
            return None
        conn = self.conn_getter()
        if conn and (not name or getattr(conn.config, 'name', '') == name):
            return conn
        return conn

    def export_current(self, fmt='CSV', path=None, params=None) -> dict:
        """查询全量并通过 ReportEngine 导出 CSV/XLSX/HTML。"""
        sql = self.sql_edit.toPlainText().strip()
        self.last_error = ''
        if not sql:
            self.last_error = 'SQL 不能为空'
            return {'ok': False, 'error': self.last_error}
        conn = self._get_conn_for(self.conn_edit.text().strip())
        if conn is None:
            self.last_error = '无可用连接'
            return {'ok': False, 'error': self.last_error}
        if params is None:
            req0 = extract_sql_params(sql)
            params = ({} if req0['style'] == 'none'
                      else self._ask_params_for(sql))
            if params is None:
                return {'ok': False, 'cancelled': True,
                        'error': '已取消参数输入'}
        try:
            cols, rows = self.engine.run_query(conn, sql, params)
        except ReportParameterError as exc:
            self.last_error = str(exc)
            return {'ok': False, 'error': self.last_error}
        except Exception as exc:                      # noqa: BLE001
            self.last_error = str(exc)
            return {'ok': False, 'error': self.last_error}
        fmt_norm = {'CSV': 'csv', 'EXCEL': 'xlsx', 'XLSX': 'xlsx',
                    'HTML': 'html'}.get(str(fmt).upper(), 'csv')
        title = self.name_edit.text().strip() or 'report'
        if not path:
            ext = {'csv': 'csv', 'xlsx': 'xlsx', 'html': 'html'}[fmt_norm]
            ts = time.strftime('%Y%m%d_%H%M%S')
            path, _ = QFileDialog.getSaveFileName(
                self, '保存报表',
                str(self.engine.out_dir / f'{title}_{ts}.{ext}'),
                f'{fmt} (*.{ext})')
        if not path:
            return {'ok': False, 'cancelled': True, 'error': '未选择保存路径'}
        try:
            self.engine.export_data(cols, rows, path, fmt=fmt_norm,
                                    title=title)
        except Exception as exc:                      # noqa: BLE001
            self.last_error = str(exc)
            self._critical('导出失败', self.last_error)
            return {'ok': False, 'error': self.last_error}
        self.last_export = {'format': fmt_norm, 'path': str(path),
                            'rows': len(rows), 'columns': list(cols)}
        self._refresh_outputs()
        return {'ok': True, **self.last_export}

    def _export(self):
        result = self.export_current(
            fmt=self.format_combo.currentText(), path=None)
        if result.get('ok'):
            QMessageBox.information(
                self, '成功', f"已导出:\n{result.get('path')}")

    def _open_output_dir(self):
        import subprocess
        path = str(self.engine.out_dir)
        try:
            if os.name == 'nt':
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception:
            pass

    # ---------- 消息封装（测试可 monkeypatch） ----------

    def _warn(self, title, text):
        try:
            QMessageBox.warning(self, title, str(text))
        except Exception:
            pass

    def _critical(self, title, text):
        try:
            QMessageBox.critical(self, title, str(text))
        except Exception:
            pass
