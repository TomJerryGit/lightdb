"""环境切换条 - 顶部工具栏"""
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QMenu,
    QMessageBox, QDialog, QVBoxLayout, QFormLayout, QLineEdit,
    QComboBox, QCheckBox, QDialogButtonBox, QColorDialog
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QAction, QPixmap, QPainter


class EnvironmentBar(QWidget):
    """环境切换条"""

    environment_changed = Signal(object)   # Environment

    def __init__(self, env_mgr, parent=None):
        super().__init__(parent)
        self.env_mgr = env_mgr
        self.env_mgr.on_change(self._on_env_changed)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(8)

        # 当前环境标签
        self.current_label = QLabel()
        self.current_label.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        layout.addWidget(self.current_label)

        # 环境切换按钮
        self.switch_btn = QPushButton("▼ 切换环境")
        self.switch_btn.clicked.connect(self._show_menu)
        layout.addWidget(self.switch_btn)

        layout.addStretch()

        # 提示
        self.info_label = QLabel()
        self.info_label.setStyleSheet("color: #969696; font-size: 11px;")
        layout.addWidget(self.info_label)

        self.setStyleSheet(
            "EnvironmentBar { background: #252526; "
            "border-bottom: 1px solid #3c3c3c; }")

    def _refresh(self):
        env = self.env_mgr.get_current()
        if env is None:
            self.current_label.setText("🌐 未选择环境")
            self.current_label.setStyleSheet("color: #969696;")
            self.info_label.setText("")
            self.setStyleSheet(
                "EnvironmentBar { background: #252526; "
                "border-bottom: 1px solid #3c3c3c; }")
            return

        self.current_label.setText(f"{env.icon} {env.label}")
        self.current_label.setStyleSheet(
            f"color: {env.color}; padding: 2px 6px;")

        info = []
        if env.connection:
            info.append(f"连接: {env.connection}")
        if env.database:
            info.append(f"库: {env.database}")
        if env.readonly:
            info.append("🔒 只读")
        elif env.dangerous:
            info.append("⚠️ 危险")
        self.info_label.setText(' | '.join(info))

        # 根据危险程度改背景
        if env.dangerous:
            self.setStyleSheet(
                f"EnvironmentBar {{ background: {env.color}22; "
                f"border-bottom: 1px solid {env.color}; }}")
        else:
            self.setStyleSheet(
                f"EnvironmentBar {{ background: #252526; "
                f"border-bottom: 1px solid {env.color}; }}")

    def _show_menu(self):
        menu = QMenu(self)

        for env in self.env_mgr.environments:
            text = f"{env.icon} {env.label}"
            if env.dangerous:
                text += "  ⚠️"
            if env.readonly:
                text += "  🔒"
            action = menu.addAction(text)
            action.setData(env.name)
            if env.name == self.env_mgr.current:
                action.setText(f"✓ {text}")

        menu.addSeparator()
        action_manage = menu.addAction("⚙️ 管理环境...")
        action_manage.setData("__manage__")

        chosen = menu.exec(self.switch_btn.mapToGlobal(
            self.switch_btn.rect().bottomLeft()))
        if not chosen:
            return

        data = chosen.data()
        if data == "__manage__":
            self._open_manage_dialog()
        else:
            self.env_mgr.set_current(data)

    def _open_manage_dialog(self):
        dlg = EnvironmentManageDialog(self.env_mgr, self)
        dlg.exec()

    def _on_env_changed(self, env):
        self._refresh()
        self.environment_changed.emit(env)


class EnvironmentManageDialog(QDialog):
    """环境管理对话框"""

    def __init__(self, env_mgr, parent=None):
        super().__init__(parent)
        self.env_mgr = env_mgr
        self.setWindowTitle("环境管理")
        self.resize(680, 520)
        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        from PySide6.QtWidgets import QListWidget, QListWidgetItem, QSplitter
        layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Horizontal)

        # 左：列表
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("环境列表:"))
        self.list_widget = QListWidget()
        self.list_widget.itemSelectionChanged.connect(self._on_select)
        ll.addWidget(self.list_widget)

        row = QHBoxLayout()
        add_btn = QPushButton("➕ 新建")
        add_btn.clicked.connect(self._add)
        row.addWidget(add_btn)
        del_btn = QPushButton("🗑 删除")
        del_btn.clicked.connect(self._delete)
        row.addWidget(del_btn)
        ll.addLayout(row)

        splitter.addWidget(left)

        # 右：编辑
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("名称:", self.name_edit)

        self.label_edit = QLineEdit()
        form.addRow("显示名:", self.label_edit)

        self.conn_edit = QLineEdit()
        form.addRow("连接:", self.conn_edit)

        self.db_edit = QLineEdit()
        form.addRow("数据库:", self.db_edit)

        self.icon_edit = QLineEdit()
        self.icon_edit.setPlaceholderText("如 🛠 🧪 ⚙️ 🚨")
        form.addRow("图标:", self.icon_edit)

        self.color_edit = QLineEdit()
        self.color_edit.setPlaceholderText("#89d185")
        form.addRow("颜色:", self.color_edit)

        self.preset_combo = QComboBox()
        self.preset_combo.addItems(['(自定义)', 'dev', 'test',
                                    'staging', 'prod', 'other'])
        self.preset_combo.currentTextChanged.connect(self._apply_preset)
        form.addRow("预设:", self.preset_combo)

        self.dangerous_check = QCheckBox("危险环境（写操作二次确认）")
        form.addRow("", self.dangerous_check)

        self.readonly_check = QCheckBox("只读（禁止写操作）")
        form.addRow("", self.readonly_check)

        self.desc_edit = QLineEdit()
        form.addRow("描述:", self.desc_edit)

        rl.addLayout(form)

        save_btn = QPushButton("💾 保存修改")
        save_btn.clicked.connect(self._save_current)
        rl.addWidget(save_btn)
        rl.addStretch()

        splitter.addWidget(right)
        splitter.setSizes([280, 400])

        layout.addWidget(splitter)

        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.accept)
        layout.addWidget(close_btn)

    def _refresh_list(self):
        from PySide6.QtWidgets import QListWidgetItem
        self.list_widget.clear()
        for env in self.env_mgr.environments:
            item = QListWidgetItem(f"{env.icon} {env.label}")
            item.setData(Qt.UserRole, env.name)
            self.list_widget.addItem(item)

    def _on_select(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        env = self.env_mgr.get(item.data(Qt.UserRole))
        if not env:
            return
        self.name_edit.setText(env.name)
        self.label_edit.setText(env.label)
        self.conn_edit.setText(env.connection)
        self.db_edit.setText(env.database)
        self.icon_edit.setText(env.icon)
        self.color_edit.setText(env.color)
        self.dangerous_check.setChecked(env.dangerous)
        self.readonly_check.setChecked(env.readonly)
        self.desc_edit.setText(env.description)

    def _add(self):
        from ..core.environments import Environment
        env = Environment(name=f"env_{len(self.env_mgr.environments)}",
                          label="新环境")
        self.env_mgr.add(env)
        self._refresh_list()

    def _delete(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        ret = QMessageBox.question(self, "确认", f"删除环境 '{name}'？")
        if ret == QMessageBox.Yes:
            self.env_mgr.remove(name)
            self._refresh_list()

    def _apply_preset(self, preset):
        if preset == '(自定义)':
            return
        from ..core.environments import ENV_PRESETS
        p = ENV_PRESETS.get(preset)
        if not p:
            return
        self.color_edit.setText(p.get('color', ''))
        self.icon_edit.setText(p.get('icon', ''))
        self.dangerous_check.setChecked(p.get('dangerous', False))
        self.readonly_check.setChecked(p.get('require_confirm', False))

    def _save_current(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        env = self.env_mgr.get(item.data(Qt.UserRole))
        if not env:
            return

        env.label = self.label_edit.text().strip() or env.name
        env.connection = self.conn_edit.text().strip()
        env.database = self.db_edit.text().strip()
        env.icon = self.icon_edit.text().strip() or '🛠'
        env.color = self.color_edit.text().strip() or '#89d185'
        env.dangerous = self.dangerous_check.isChecked()
        env.readonly = self.readonly_check.isChecked()
        env.description = self.desc_edit.text().strip()

        self.env_mgr.update(env.name, env)
        self._refresh_list()
        QMessageBox.information(self, "完成", "已保存")