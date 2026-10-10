"""AI 对话面板"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QLabel, QComboBox, QLineEdit, QListWidget, QListWidgetItem,
    QSplitter, QMessageBox, QToolButton, QMenu, QDialog,
    QDialogButtonBox, QFormLayout, QDoubleSpinBox, QSpinBox,
    QApplication
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor, QAction, QKeySequence

from ..core.ai_integration import AIClient, AIConfig, AIConfigStore


class AIChatPanel(QWidget):
    sql_generated = Signal(str)

    def __init__(self, schema_provider=None, conn_getter=None, parent=None):
        super().__init__(parent)
        self.schema_provider = schema_provider
        self.conn_getter = conn_getter
        self.store = AIConfigStore()
        self.config = self.store.load()
        self.client = AIClient(self.config)
        self.messages = []      # [{'role': 'user'/'assistant', 'content': str}]
        self._build_ui()
        self._check_status()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 顶部工具
        top = QHBoxLayout()
        top.setContentsMargins(4, 4, 4, 4)

        self.status_label = QLabel("AI: 未配置")
        self.status_label.setStyleSheet("color: #969696;")
        top.addWidget(self.status_label)
        top.addStretch()

        config_btn = QPushButton("⚙")
        config_btn.setFixedWidth(30)
        config_btn.setToolTip("AI 设置")
        config_btn.clicked.connect(self._open_config)
        top.addWidget(config_btn)

        clear_btn = QPushButton("🗑")
        clear_btn.setFixedWidth(30)
        clear_btn.setToolTip("清空对话")
        clear_btn.clicked.connect(self._clear)
        top.addWidget(clear_btn)
        layout.addLayout(top)

        # 快捷操作
        quick_row = QHBoxLayout()
        quick_row.setContentsMargins(4, 0, 4, 4)

        for text, slot in [
            ('📖 解释SQL', self._quick_explain),
            ('🚀 优化SQL', self._quick_optimize),
            ('🔍 索引建议', self._quick_index),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            quick_row.addWidget(btn)
        quick_row.addStretch()
        layout.addLayout(quick_row)

        # 对话列表
        self.chat_view = QTextEdit()
        self.chat_view.setReadOnly(True)
        self.chat_view.setFont(QFont("Microsoft YaHei", 10))
        self.chat_view.setStyleSheet(
            "QTextEdit { background: #1e1e1e; color: #d4d4d4; "
            "border: 1px solid #3c3c3c; padding: 8px; }")
        layout.addWidget(self.chat_view, 1)

        # 输入区
        input_row = QHBoxLayout()
        input_row.setContentsMargins(4, 4, 4, 4)

        self.input_edit = QTextEdit()
        self.input_edit.setPlaceholderText(
            "用自然语言描述需求，或直接粘贴 SQL...\n"
            "Ctrl+Enter 发送")
        self.input_edit.setMaximumHeight(80)
        self.input_edit.keyPressEvent = self._wrap_input_key(
            self.input_edit.keyPressEvent)
        input_row.addWidget(self.input_edit, 1)

        send_btn = QPushButton("发送")
        send_btn.setMinimumHeight(80)
        send_btn.clicked.connect(self._send)
        input_row.addWidget(send_btn)
        layout.addLayout(input_row)

        # 操作栏
        action_row = QHBoxLayout()
        action_row.setContentsMargins(4, 0, 4, 4)

        self.insert_btn = QPushButton("📝 插入到编辑器")
        self.insert_btn.setEnabled(False)
        self.insert_btn.clicked.connect(self._insert_last)
        action_row.addWidget(self.insert_btn)
        action_row.addStretch()
        layout.addLayout(action_row)

    def _wrap_input_key(self, original):
        def new(event):
            if (event.key() == Qt.Key_Return and
                    event.modifiers() & Qt.ControlModifier):
                self._send()
                return
            original(event)
        return new

    def _check_status(self):
        if self.client.is_enabled():
            self.status_label.setText(
                f"AI: {self.config.provider} / {self.config.model}")
            self.status_label.setStyleSheet("color: #89d185;")
        else:
            self.status_label.setText("AI: 未配置 (点击 ⚙ 配置)")
            self.status_label.setStyleSheet("color: #f48771;")

    def _open_config(self):
        dlg = AIConfigDialog(self.config, self)
        if dlg.exec() == AIConfigDialog.Accepted:
            self.config = dlg.get_config()
            self.store.save(self.config)
            self.client = AIClient(self.config)
            self._check_status()

    def _clear(self):
        self.messages = []
        self.chat_view.clear()
        self.insert_btn.setEnabled(False)

    # ==================== 消息显示 ====================

    def _append_message(self, role: str, content: str):
        color = '#569cd6' if role == 'user' else '#89d185'
        label = '你' if role == 'user' else 'AI'
        html = f"""
        <div style="margin: 8px 0;">
          <span style="color: {color}; font-weight: bold;">{label}:</span>
          <div style="margin-top: 4px; white-space: pre-wrap;">{self._escape(content)}</div>
        </div>
        """
        self.chat_view.append(html)
        # 滚动到底部
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _escape(self, s):
        return (s.replace('&', '&amp;')
                 .replace('<', '&lt;')
                 .replace('>', '&gt;'))

    # ==================== 发送 ====================

    def _send(self):
        text = self.input_edit.toPlainText().strip()
        if not text:
            return

        if not self.client.is_enabled():
            QMessageBox.warning(
                self, "提示",
                "AI 未配置。请点击右上角 ⚙ 配置 API。")
            return

        self.input_edit.clear()
        self.messages.append({'role': 'user', 'content': text})
        self._append_message('user', text)

        # 获取 schema
        schema = None
        if self.schema_provider:
            try:
                tables, cols = self.schema_provider()
                schema = {'tables': tables, 'columns': cols}
            except Exception:
                pass

        # 调用 AI
        self._append_message('assistant', '思考中...')

        def on_done(response):
            # 删除"思考中"（简单方式：重新绘制）
            self.messages.append({
                'role': 'assistant',
                'content': response,
            })
            self._rewrite_chat()
            self.insert_btn.setEnabled(True)

        def on_error(err):
            self._append_message('assistant', f'❌ 错误: {err}')

        self.client.chat(self.messages, schema, on_done, on_error)

    def _rewrite_chat(self):
        """重绘整个对话"""
        self.chat_view.clear()
        for m in self.messages:
            self._append_message(m['role'], m['content'])

    # ==================== 快捷操作 ====================

    def _get_editor_sql(self):
        """通过主窗口获取编辑器 SQL"""
        win = self.window()
        if hasattr(win, '_current_widgets'):
            editor, _ = win._current_widgets()
            if editor:
                return editor.toPlainText().strip()
        return ''

    def _quick_explain(self):
        sql = self._get_editor_sql()
        if not sql:
            QMessageBox.information(self, "提示", "编辑器无 SQL")
            return
        self.input_edit.setPlainText(
            f"请用中文详细解释以下 SQL：\n\n{sql}")
        self._send()

    def _quick_optimize(self):
        sql = self._get_editor_sql()
        if not sql:
            return
        self.input_edit.setPlainText(
            f"请分析并优化以下 SQL，给出优化后的版本和解释：\n\n{sql}")
        self._send()

    def _quick_index(self):
        sql = self._get_editor_sql()
        if not sql:
            return
        self.input_edit.setPlainText(
            f"请为以下 SQL 推荐必要的索引（含 CREATE INDEX 语句）：\n\n{sql}")
        self._send()

    def _insert_last(self):
        """插入最后一条 AI 生成的 SQL"""
        for m in reversed(self.messages):
            if m['role'] == 'assistant':
                sql = self._extract_sql(m['content'])
                if sql:
                    self.sql_generated.emit(sql)
                return

    def _extract_sql(self, text):
        """从 AI 回复中提取 SQL"""
        import re
        # 优先 ```sql ... ```
        m = re.search(r'```(?:sql)?\s*(.+?)```', text,
                      re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
        # 或者直接看是否像 SQL
        lines = []
        for line in text.split('\n'):
            if re.match(r'\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|'
                        r'ALTER|DROP|WITH)\s',
                        line, re.IGNORECASE):
                lines.append(line)
            elif lines:
                lines.append(line)
        if lines:
            return '\n'.join(lines).strip()
        return text.strip()


class AIConfigDialog(QDialog):
    def __init__(self, config: AIConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("AI 设置")
        self.resize(520, 460)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(
            ['openai', 'claude', 'ollama', 'disabled'])
        self.provider_combo.setCurrentText(self.config.provider)
        self.provider_combo.currentTextChanged.connect(self._on_provider)
        form.addRow("提供商:", self.provider_combo)

        self.api_key_edit = QLineEdit(self.config.api_key)
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("sk-...")
        form.addRow("API Key:", self.api_key_edit)

        self.base_url_edit = QLineEdit(self.config.base_url)
        self.base_url_edit.setPlaceholderText(
            "留空使用默认（OpenAI: api.openai.com）")
        form.addRow("Base URL:", self.base_url_edit)

        self.model_edit = QLineEdit(self.config.model)
        self.model_edit.setPlaceholderText(
            "gpt-4o-mini / claude-3-haiku-20240307 / llama3")
        form.addRow("模型:", self.model_edit)

        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setValue(self.config.temperature)
        form.addRow("Temperature:", self.temp_spin)

        self.tokens_spin = QSpinBox()
        self.tokens_spin.setRange(100, 32000)
        self.tokens_spin.setSingleStep(500)
        self.tokens_spin.setValue(self.config.max_tokens)
        form.addRow("Max Tokens:", self.tokens_spin)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 300)
        self.timeout_spin.setValue(self.config.timeout)
        form.addRow("超时(秒):", self.timeout_spin)

        layout.addLayout(form)

        info = QLabel(
            "<b>常用模型：</b><br>"
            "• OpenAI: gpt-4o-mini, gpt-4o, gpt-4-turbo<br>"
            "• Claude: claude-3-haiku-20240307, claude-3-5-sonnet-20241022<br>"
            "• Ollama: llama3, qwen2.5, deepseek-coder<br><br>"
            "<b>Ollama 无需 API Key，只需本地运行 ollama serve</b>")
        info.setStyleSheet("color: #969696; font-size: 11px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_provider(self, provider):
        defaults = {
            'openai': ('gpt-4o-mini', 'https://api.openai.com/v1'),
            'claude': ('claude-3-haiku-20240307',
                       'https://api.anthropic.com'),
            'ollama': ('llama3', 'http://localhost:11434'),
            'disabled': ('', ''),
        }
        if provider in defaults:
            model, url = defaults[provider]
            self.model_edit.setText(model)
            self.base_url_edit.setText(url)
            self.api_key_edit.setEnabled(provider != 'ollama'
                                          and provider != 'disabled')

    def get_config(self) -> AIConfig:
        return AIConfig(
            provider=self.provider_combo.currentText(),
            api_key=self.api_key_edit.text(),
            base_url=self.base_url_edit.text(),
            model=self.model_edit.text(),
            temperature=self.temp_spin.value(),
            max_tokens=self.tokens_spin.value(),
            timeout=self.timeout_spin.value(),
        )