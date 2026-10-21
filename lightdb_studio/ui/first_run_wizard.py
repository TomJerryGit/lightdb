# -*- coding: utf-8 -*-
"""首启引导（P3）：欢迎 / 本地 demo 说明 / 完成或跳过。

只在 `lightdb_studio.app.run()` 里调用 `maybe_show_first_run()`；
**不放进 MainWindow 构造**（避免破坏既有 GUI 测试与嵌入场景）。

持久化：`settings.first_run_done = True`（完成或跳过后都保存）；
环境变量 `LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN=1` 可跳过（测试 / 打包冒烟）。
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

ENV_SUPPRESS = 'LIGHTDB_STUDIO_SUPPRESS_FIRST_RUN'
DEMO_PORT = 8899
DEMO_HOST = '127.0.0.1'
DEMO_COMMAND = 'powershell -ExecutionPolicy Bypass -File .\\run_studio.ps1'


def _is_truthy(value) -> bool:
    return str(value or '').strip().lower() not in ('', '0', 'false', 'no', 'off')


def suppress_first_run() -> bool:
    return _is_truthy(os.environ.get(ENV_SUPPRESS))


class FirstRunDialog(QDialog):
    """三步首启引导：欢迎 → 本地 demo → 完成。"""

    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle('欢迎使用 LightDB Studio')
        self.setAccessibleName('首启引导 / First run guide')
        self.resize(720, 500)
        self._build_ui()
        self._update_buttons()

    # ---------------- UI ----------------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.title_label = QLabel('欢迎使用 LightDB Studio')
        self.title_label.setFont(QFont('Microsoft YaHei', 16, QFont.Bold))
        self.title_label.setStyleSheet('color: #4ec9b0;')
        layout.addWidget(self.title_label)

        self.stack = QStackedWidget()
        self.stack.setAccessibleName('首启引导步骤 / First run steps')
        self.stack.addWidget(self._page_welcome())
        self.stack.addWidget(self._page_demo())
        self.stack.addWidget(self._page_done())
        layout.addWidget(self.stack, 1)

        buttons = QHBoxLayout()
        self.skip_button = QPushButton('跳过')
        self.skip_button.setAccessibleName('跳过首启引导')
        self.skip_button.clicked.connect(self.skip)
        buttons.addWidget(self.skip_button)
        buttons.addStretch()
        self.back_button = QPushButton('上一步')
        self.back_button.setAccessibleName('上一步')
        self.back_button.clicked.connect(self._go_back)
        buttons.addWidget(self.back_button)
        self.next_button = QPushButton('下一步')
        self.next_button.setAccessibleName('下一步')
        self.next_button.clicked.connect(self._go_next)
        buttons.addWidget(self.next_button)
        self.finish_button = QPushButton('完成')
        self.finish_button.setAccessibleName('完成首启引导')
        self.finish_button.clicked.connect(self.finish)
        buttons.addWidget(self.finish_button)
        layout.addLayout(buttons)

    def _page_welcome(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        text = QLabel(
            '<h3>LightDB Studio</h3>'
            '<p>LightDB 的图形化管理工具：SQL 编辑执行、对象浏览、'
            '数据编辑、报表/图表、Schema 对比、备份恢复等。</p>'
            '<p>这是首次启动，用一个简短的引导帮你完成初始配置。</p>'
            '<p style="color:#969696">点击「下一步」查看如何在本地快速体验。</p>')
        text.setWordWrap(True)
        text.setTextFormat(Qt.RichText)
        layout.addWidget(text)
        layout.addStretch()
        return page

    def _page_demo(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.demo_text = QLabel(
            '<h3>本地 Demo（一键体验）</h3>'
            '<ul>'
            '<li>在仓库根目录运行 <code>run_studio.ps1</code> 可一键启动'
            '本地 demo 服务端 + Studio。</li>'
            f'<li>默认端口 <b>{DEMO_PORT}</b>，用户名 <b>root</b>，'
            '空口令 <b>仅限本地 demo</b>。</li>'
            '<li>生产环境请务必设置强口令，不要使用空口令/默认账号。</li>'
            '</ul>'
            '<p>也可以手动启动 demo 服务端后，在 Studio 里新建连接到 '
            f'<code>{DEMO_HOST}:{DEMO_PORT}</code>。</p>')
        self.demo_text.setWordWrap(True)
        self.demo_text.setTextFormat(Qt.RichText)
        layout.addWidget(self.demo_text)

        row = QHBoxLayout()
        self.demo_command_edit = QLineEdit(DEMO_COMMAND)
        self.demo_command_edit.setReadOnly(True)
        self.demo_command_edit.setAccessibleName('本地 demo 启动命令')
        row.addWidget(self.demo_command_edit)
        self.copy_button = QPushButton('复制命令')
        self.copy_button.setAccessibleName('复制 demo 启动命令')
        self.copy_button.clicked.connect(self.copy_demo_command)
        row.addWidget(self.copy_button)
        layout.addLayout(row)
        layout.addStretch()
        return page

    def _page_done(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.done_text = QLabel(
            '<h3>准备就绪</h3>'
            '<p>下一步：点击工具栏「连接」新建数据库连接，'
            f'或直接连接本地 demo（{DEMO_HOST}:{DEMO_PORT}，root / 空口令仅本地）。</p>'
            '<p>之后可从「帮助」或设置里重新查看文档；本次引导不会再自动弹出。</p>')
        self.done_text.setWordWrap(True)
        self.done_text.setTextFormat(Qt.RichText)
        layout.addWidget(self.done_text)
        layout.addStretch()
        return page

    # ---------------- 流程 ----------------

    def _update_buttons(self):
        idx = self.stack.currentIndex()
        self.back_button.setEnabled(idx > 0)
        self.next_button.setVisible(idx < self.stack.count() - 1)
        self.finish_button.setVisible(idx == self.stack.count() - 1)

    def _go_next(self):
        if self.stack.currentIndex() < self.stack.count() - 1:
            self.stack.setCurrentIndex(self.stack.currentIndex() + 1)
            self._update_buttons()

    def _go_back(self):
        if self.stack.currentIndex() > 0:
            self.stack.setCurrentIndex(self.stack.currentIndex() - 1)
            self._update_buttons()

    def copy_demo_command(self):
        try:
            cb = QApplication.clipboard()
            if cb is not None:
                cb.setText(DEMO_COMMAND)
                return True
        except Exception:
            pass
        return False

    # ---------------- 持久化 ----------------

    def mark_done(self, skipped: bool = False):
        settings = getattr(self.config, 'settings', self.config)
        if settings is not None:
            try:
                settings.first_run_done = True
                if hasattr(settings, 'save'):
                    settings.save()
            except Exception as exc:                  # noqa: BLE001
                print(f'[first-run] 保存 first_run_done 失败: {exc}')
        self._skipped = bool(skipped)

    def finish(self):
        self.mark_done(skipped=False)
        self.accept()

    def skip(self):
        self.mark_done(skipped=True)
        self.accept()

    def reject(self):
        # 关闭窗口视作跳过：不让用户下次再被弹一次
        self.mark_done(skipped=True)
        super().reject()


def maybe_show_first_run(parent, config, interactive: bool = True) -> bool:
    """未完成且未被抑制时展示首启引导；返回是否展示（或被抑制/已完成）。"""
    if suppress_first_run():
        return False
    settings = getattr(config, 'settings', config)
    if settings is None or getattr(settings, 'first_run_done', False):
        return False
    if not interactive:
        return True
    try:
        dlg = FirstRunDialog(config, parent)
        dlg.exec()
        return True
    except Exception as exc:                          # noqa: BLE001
        print(f'[first-run] 展示首启引导失败（跳过）: {exc}')
        return False
