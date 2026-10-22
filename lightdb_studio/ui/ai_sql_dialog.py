# -*- coding: utf-8 -*-
"""AI SQL 助手对话框（task-43）：生成 → 预览 → 语法校验 → 填入/确认执行。

- AI 未配置/请求失败：显式红字提示，不伪造 SQL；示例库离线可用；
- 生成 SQL 用引擎 parser 校验，非法显式标红且禁止执行；
- 存储过程提示本引擎 CALL 兼容范围；过程体含结果集 SELECT 标为不兼容；
- DDL 执行前二次确认（QMessageBox），确认后由主窗口执行。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QVBoxLayout, QLineEdit, QGroupBox, QFormLayout,
)

from ..core.ai_sql_assistant import (
    AISQLAssistant, AISQLGenerationError, AISQLParseError,
    AISQLUnavailableError, validate_sql,
)
from . import ai_examples


class AISQLDialog(QDialog):
    insert_requested = Signal(str)
    execute_requested = Signal(str)

    def __init__(self, assistant=None, schema_provider=None, parent=None):
        super().__init__(parent)
        self.assistant = assistant or AISQLAssistant()
        self.schema_provider = schema_provider or (lambda: ([], {}))
        self.last_result = None
        self.last_error = ''
        self.setWindowTitle('AI SQL 助手（建表 / 存储过程 / 示例库）')
        self.resize(980, 760)
        self._build_ui()
        self._refresh_context()

    # ---------- UI ----------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.kind_combo = QComboBox()
        self.kind_combo.addItem('创建表（CREATE TABLE）', 'table')
        self.kind_combo.addItem('创建存储过程（CREATE PROCEDURE）',
                                'procedure')
        self.kind_combo.setAccessibleName('生成类型')
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        form.addRow('生成类型:', self.kind_combo)
        self.context_label = QLabel('')
        self.context_label.setWordWrap(True)
        self.context_label.setStyleSheet('color: #969696;')
        form.addRow('当前库上下文:', self.context_label)
        layout.addLayout(form)

        self.description = QPlainTextEdit()
        self.description.setPlaceholderText(
            '用自然语言描述，例如：创建用户表，id 主键自增，name 非空，'
            'email 唯一，created 时间')
        self.description.setMaximumHeight(90)
        layout.addWidget(QLabel('需求描述:'))
        layout.addWidget(self.description)

        row = QHBoxLayout()
        self.generate_btn = QPushButton('🤖 生成 SQL')
        self.generate_btn.setAccessibleName('生成 SQL')
        self.generate_btn.clicked.connect(self.generate)
        row.addWidget(self.generate_btn)
        self.example_combo = QComboBox()
        for example in ai_examples.all_examples():
            self.example_combo.addItem(
                f'[{ai_examples.CATEGORY_LABELS[example["category"]]}] '
                f'{example["title"]}', example['id'])
        self.example_combo.setAccessibleName('离线示例库')
        row.addWidget(self.example_combo, 1)
        self.example_btn = QPushButton('📚 插入示例')
        self.example_btn.clicked.connect(self.load_selected_example)
        row.addWidget(self.example_btn)
        row.addStretch()
        layout.addLayout(row)

        self.ai_hint = QLabel('')
        self.ai_hint.setWordWrap(True)
        try:
            configured = self.assistant.is_configured()
        except Exception:
            configured = False
        if configured:
            self.ai_hint.setText('AI 已配置：可生成 SQL（网络请求失败会显式提示）。')
            self.ai_hint.setStyleSheet('color: #89d185;')
        else:
            self.ai_hint.setText(
                'AI 未配置：生成不可用（不伪造输出）；离线示例库仍可用。'
                '请在 AI 对话面板/设置中配置 provider 与 api_key。')
            self.ai_hint.setStyleSheet('color: #d7ba7d;')
        layout.addWidget(self.ai_hint)

        layout.addWidget(QLabel('预览 / 示例 SQL:'))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont('Consolas', 10))
        self.preview.setStyleSheet('background: #1e1e1e; color: #dcdcaa;')
        layout.addWidget(self.preview, 1)
        self.explanation = QLabel('')
        self.explanation.setWordWrap(True)
        self.explanation.setStyleSheet('color: #969696;')
        layout.addWidget(self.explanation)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.procedure_hint = QLabel('')
        self.procedure_hint.setWordWrap(True)
        self.procedure_hint.setStyleSheet('color: #d7ba7d;')
        layout.addWidget(self.procedure_hint)

        bottom = QHBoxLayout()
        self.fill_btn = QPushButton('插入到编辑器')
        self.fill_btn.setAccessibleName('插入到编辑器')
        self.fill_btn.setEnabled(False)
        self.fill_btn.clicked.connect(self.fill_editor)
        bottom.addWidget(self.fill_btn)
        self.execute_btn = QPushButton('执行（需二次确认）')
        self.execute_btn.setAccessibleName('执行 SQL（二次确认）')
        self.execute_btn.setEnabled(False)
        self.execute_btn.clicked.connect(self.confirm_and_execute)
        bottom.addWidget(self.execute_btn)
        bottom.addStretch()
        close = QPushButton('关闭')
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        layout.addLayout(bottom)

    def _on_kind_changed(self, *_args):
        kind = self.current_kind()
        if kind == 'procedure':
            self.procedure_hint.setText(
                '存储过程按本引擎能力生成：支持 CREATE PROCEDURE / IN·OUT 参数 / '
                'DECLARE / SET / IF / WHILE / 嵌套 CALL；CALL 不返回结果集。')
        else:
            self.procedure_hint.setText('')

    def current_kind(self):
        return self.kind_combo.currentData() or 'table'

    def _refresh_context(self):
        try:
            tables, columns = self.schema_provider() or ([], {})
        except Exception:
            tables, columns = [], {}
        self.context_tables = list(tables or [])
        self.context_columns = dict(columns or {})
        self.context_label.setText(
            f"当前连接库：{len(self.context_tables)} 张表"
            + (('；参考: ' + ', '.join(map(str, self.context_tables[:8])))
               if self.context_tables else '（未获取到表结构，仍可生成）'))

    # ---------- 生成 / 示例 ----------

    def generate(self):
        description = self.description.toPlainText().strip()
        if not description:
            self._show_error('请输入需求描述')
            return None
        self.generate_btn.setEnabled(False)
        self.status.setText('生成中…')
        try:
            if self.current_kind() == 'procedure':
                result = self.assistant.generate_procedure(
                    description, self.context_tables, self.context_columns)
            else:
                result = self.assistant.generate_table(
                    description, self.context_tables, self.context_columns)
            self.show_result(result)
            return result
        except AISQLUnavailableError as exc:
            self._show_error(f'AI 不可用：{exc}')
        except AISQLGenerationError as exc:
            self._show_error(f'AI 生成失败：{exc}')
        except AISQLParseError as exc:
            self._show_error(f'AI 响应无法解析为 SQL：{exc}')
        except Exception as exc:                      # noqa: BLE001
            self._show_error(f'生成失败：{exc}')
        finally:
            self.generate_btn.setEnabled(True)
        return None

    def load_selected_example(self):
        example_id = self.example_combo.currentData()
        example = ai_examples.get_example(example_id)
        if example is None:
            self._show_error('示例不存在')
            return None
        self.last_result = None
        self.last_error = ''
        self.preview.setPlainText(example['sql'])
        self.explanation.setText(
            f"示例：{example['description']}\n注意：{example['warning']}")
        validation = validate_sql(example['sql'])
        self._update_execute_state(validation)
        self.status.setText('离线示例已载入（可插入编辑器或执行）。')
        self.status.setStyleSheet('color: #89d185;')
        return example

    def show_result(self, result):
        self.last_result = result
        self.last_error = ''
        self.preview.setPlainText(result.sql or '')
        self.explanation.setText(result.explanation or '')
        self.procedure_hint.setText('')
        if result.capability:
            notes = result.capability.get('notes') or []
            self.procedure_hint.setText('引擎能力提示：\n'
                                        + '\n'.join('• ' + n for n in notes))
        self._update_execute_state({
            'ok': result.valid, 'error': result.error})
        if not result.valid:
            self.status.setText('生成 SQL 未通过引擎 parser 校验（禁止执行）：'
                                + result.error)
            self.status.setStyleSheet('color: #f48771;')
            return
        if result.engine_issues:
            self.status.setText(
                'SQL 语法通过，但引擎不兼容（禁止执行）：'
                + '；'.join(result.engine_issues))
            self.status.setStyleSheet('color: #f48771;')
            return
        self.status.setText('生成 SQL 语法校验通过；请预览后决定插入/执行。')
        self.status.setStyleSheet('color: #89d185;')

    def _update_execute_state(self, validation):
        ok = bool(validation.get('ok'))
        self.fill_btn.setEnabled(bool(self.preview.toPlainText().strip()))
        engine_ok = True
        if self.last_result is not None:
            engine_ok = bool(self.last_result.engine_compatible)
        self.execute_btn.setEnabled(ok and engine_ok)

    def _show_error(self, message):
        self.last_error = str(message)
        self.last_result = None
        self.preview.setPlainText('')
        self.explanation.setText('')
        self.status.setText(str(message))
        self.status.setStyleSheet('color: #f48771;')
        self.fill_btn.setEnabled(False)
        self.execute_btn.setEnabled(False)

    # ---------- 插入 / 执行 ----------

    def fill_editor(self):
        sql = self.preview.toPlainText().strip()
        if not sql:
            return False
        self.insert_requested.emit(sql)
        self.status.setText('已发送到编辑器（未执行）。')
        self.status.setStyleSheet('color: #89d185;')
        return True

    def confirm_and_execute(self):
        sql = self.preview.toPlainText().strip()
        if not sql:
            return False
        validation = validate_sql(sql)
        if not validation['ok']:
            self._show_error('语法校验失败，禁止执行：' + validation['error'])
            return False
        if self.last_result is not None and not self.last_result.engine_compatible:
            self._show_error('引擎不兼容，禁止执行：'
                             + '；'.join(self.last_result.engine_issues))
            return False
        ret = QMessageBox.question(
            self, '确认执行 DDL',
            '即将执行生成的 SQL（可能修改数据库结构）。\n'
            '请确认已预览/备份。是否继续？\n\n' + sql[:800],
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            self.status.setText('用户取消执行（SQL 未执行）。')
            self.status.setStyleSheet('color: #d7ba7d;')
            return False
        self.execute_requested.emit(sql)
        self.status.setText('已发送执行请求（由主窗口执行）。')
        self.status.setStyleSheet('color: #89d185;')
        return True
