"""快照管理对话框"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QDialogButtonBox, QMessageBox, QTabWidget, QWidget,
    QLabel, QLineEdit, QFormLayout, QListWidget, QListWidgetItem,
    QProgressDialog, QComboBox, QCheckBox, QGroupBox, QSpinBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from datetime import datetime

from ..core.snapshot import SnapshotManager


class SnapshotDialog(QDialog):
    def __init__(self, conn_getter, parent=None, manager=None):
        super().__init__(parent)
        self.mgr = manager or SnapshotManager()
        self.conn_getter = conn_getter
        self.setWindowTitle("快照 / 克隆")
        self.resize(1000, 720)
        self._build_ui()
        self._refresh_list()
        self.load_strategy_settings()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        tabs = QTabWidget()

        # Tab1: 快照
        snap_w = QWidget()
        sl = QVBoxLayout(snap_w)

        sl.addWidget(QLabel(
            "创建数据库快照（导出为 SQL 文件），用于备份或迁移。"))

        self.list_widget = QTableWidget()
        self.list_widget.setColumnCount(6)
        self.list_widget.setHorizontalHeaderLabels(
            ['名称', '连接', '库', '表数', '大小', '创建时间'])
        self.list_widget.setColumnWidth(0, 200)
        self.list_widget.setColumnWidth(1, 120)
        self.list_widget.setColumnWidth(2, 120)
        self.list_widget.setColumnWidth(3, 80)
        self.list_widget.setColumnWidth(4, 100)
        self.list_widget.horizontalHeader().setStretchLastSection(True)
        self.list_widget.itemSelectionChanged.connect(self._on_select)
        sl.addWidget(self.list_widget)

        # 旧索引迁移/删除失败等告警（P0-3）：不静默，显示在列表上方。
        self.index_warn_label = QLabel("")
        self.index_warn_label.setWordWrap(True)
        self.index_warn_label.setStyleSheet("color: #d7ba7d;")
        self.index_warn_label.setVisible(False)
        sl.insertWidget(sl.indexOf(self.list_widget), self.index_warn_label)

        row = QHBoxLayout()

        create_btn = QPushButton("➕ 创建快照")
        create_btn.clicked.connect(self._create)
        row.addWidget(create_btn)

        restore_btn = QPushButton("↩️ 恢复快照")
        restore_btn.clicked.connect(self._restore)
        row.addWidget(restore_btn)

        self.restore_preview_btn = QPushButton("👁 恢复预览")
        self.restore_preview_btn.clicked.connect(self._on_preview_restore)
        row.addWidget(self.restore_preview_btn)

        delete_btn = QPushButton("🗑 删除")
        delete_btn.clicked.connect(self._delete)
        row.addWidget(delete_btn)

        row.addStretch()
        sl.addLayout(row)

        # P2-5：保留策略 / 定时快照设置（不做后台线程，由外部定时器/按钮触发）
        strategy = QGroupBox("保留策略 / 定时快照（无后台常驻线程）")
        sfl = QFormLayout(strategy)
        self.keep_last_spin = QSpinBox()
        self.keep_last_spin.setRange(0, 10000)
        self.keep_last_spin.setValue(10)
        self.keep_last_spin.setSpecialValueText("0（不按数量）")
        sfl.addRow("保留最新 N 个:", self.keep_last_spin)
        self.max_age_spin = QSpinBox()
        self.max_age_spin.setRange(0, 3650)
        self.max_age_spin.setSuffix(" 天")
        self.max_age_spin.setSpecialValueText("0（不按年龄）")
        sfl.addRow("删除超过:", self.max_age_spin)
        self.schedule_enabled = QCheckBox("启用定时快照")
        sfl.addRow("定时:", self.schedule_enabled)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 1440)
        self.interval_spin.setValue(60)
        self.interval_spin.setSuffix(" 分钟")
        sfl.addRow("间隔:", self.interval_spin)
        self.prefix_edit = QLineEdit("auto")
        sfl.addRow("名称前缀:", self.prefix_edit)

        btns = QHBoxLayout()
        self.save_schedule_btn = QPushButton("💾 保存设置")
        self.save_schedule_btn.clicked.connect(self._on_save_strategy)
        btns.addWidget(self.save_schedule_btn)
        self.prune_preview_btn = QPushButton("🔍 预览清理")
        self.prune_preview_btn.clicked.connect(self._on_prune_preview)
        btns.addWidget(self.prune_preview_btn)
        self.prune_apply_btn = QPushButton("🧹 执行清理")
        self.prune_apply_btn.clicked.connect(self._on_prune_apply)
        btns.addWidget(self.prune_apply_btn)
        self.run_due_btn = QPushButton("⏱ 运行到期检查")
        self.run_due_btn.clicked.connect(self._on_run_due)
        btns.addWidget(self.run_due_btn)
        btns.addStretch()
        sfl.addRow("", btns)
        self.strategy_status = QLabel("")
        self.strategy_status.setWordWrap(True)
        self.strategy_status.setStyleSheet("color: #9cdcfe;")
        sfl.addRow("状态:", self.strategy_status)
        sl.addWidget(strategy)

        tabs.addTab(snap_w, "快照")

        # Tab2: 克隆
        clone_w = QWidget()
        cl = QVBoxLayout(clone_w)

        cl.addWidget(QLabel(
            "将源数据库直接克隆到另一个连接（表结构 + 数据）。"))

        form = QFormLayout()
        self.src_edit = QLineEdit()
        form.addRow("源连接:", self.src_edit)
        self.dst_edit = QLineEdit()
        form.addRow("目标连接:", self.dst_edit)

        cl.addLayout(form)

        self.drop_check = QCheckBox("目标库已存在的同名表先删除")
        self.drop_check.setChecked(True)
        cl.addWidget(self.drop_check)

        clone_btn = QPushButton("▶ 开始克隆")
        clone_btn.clicked.connect(self._clone)
        cl.addWidget(clone_btn)

        self.clone_log = QListWidget()
        self.clone_log.setFont(QFont("Consolas", 9))
        cl.addWidget(self.clone_log)

        tabs.addTab(clone_w, "克隆")

        layout.addWidget(tabs)

        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.reject)
        layout.addWidget(close_btn)

    def _refresh_list(self):
        snaps = self.mgr.list_all()
        self.list_widget.setRowCount(len(snaps))
        # P0-3：旧索引迁移/删除失败等告警显示在列表上方，不静默。
        warnings = list(getattr(self.mgr, 'index_warnings', []))[-3:]
        if warnings:
            self.index_warn_label.setText(
                "⚠ 快照索引告警：" + "；".join(warnings))
            self.index_warn_label.setVisible(True)
        else:
            self.index_warn_label.setVisible(False)
        for r, s in enumerate(snaps):
            self.list_widget.setItem(r, 0, QTableWidgetItem(s.name))
            self.list_widget.setItem(r, 1, QTableWidgetItem(s.connection))
            self.list_widget.setItem(r, 2, QTableWidgetItem(s.database))

            n_tables = len(s.tables)
            total_rows = sum(s.row_counts.values()) if s.row_counts else 0
            self.list_widget.setItem(
                r, 3, QTableWidgetItem(f"{n_tables} / {total_rows}"))

            size_kb = s.size / 1024
            self.list_widget.setItem(
                r, 4, QTableWidgetItem(f"{size_kb:.1f} KB"))

            ts = datetime.fromtimestamp(s.created_at).strftime(
                '%Y-%m-%d %H:%M:%S')
            self.list_widget.setItem(r, 5, QTableWidgetItem(ts))

    def _on_select(self):
        pass

    def _get_selected_snapshot(self):
        r = self.list_widget.currentRow()
        if r < 0:
            return None
        name = self.list_widget.item(r, 0).text()
        return self.mgr.get(name)

    # ==================== P2-5 策略 / 定时 / 恢复预览 ====================

    def load_strategy_settings(self) -> dict:
        cfg = self.mgr.load_schedule()
        self.keep_last_spin.setValue(int(cfg.get('keep_last') or 0))
        self.max_age_spin.setValue(int(cfg.get('max_age_days') or 0))
        self.schedule_enabled.setChecked(bool(cfg.get('enabled')))
        self.interval_spin.setValue(int(cfg.get('interval_minutes') or 60))
        self.prefix_edit.setText(str(cfg.get('name_prefix') or 'auto'))
        self._refresh_strategy_status(cfg)
        return cfg

    def current_strategy_config(self) -> dict:
        return {
            'enabled': self.schedule_enabled.isChecked(),
            'interval_minutes': self.interval_spin.value(),
            'keep_last': self.keep_last_spin.value(),
            'max_age_days': self.max_age_spin.value(),
            'name_prefix': self.prefix_edit.text().strip() or 'auto',
        }

    def _refresh_strategy_status(self, cfg=None):
        cfg = cfg or self.mgr.load_schedule()
        try:
            due = self.mgr.due()
            last = cfg.get('last_run') or 0
            last_text = (datetime.fromtimestamp(last).strftime('%Y-%m-%d %H:%M:%S')
                         if last else '从未')
            text = (f"定时: {'启用' if cfg.get('enabled') else '关闭'} | "
                    f"到期: {'是' if due else '否'} | 上次: {last_text} | "
                    f"间隔: {cfg.get('interval_minutes')} 分钟 | "
                    f"保留: {cfg.get('keep_last')} / "
                    f"{cfg.get('max_age_days')} 天")
            if cfg.get('last_error'):
                text += f" | ⚠ 上次错误: {cfg['last_error']}"
        except Exception as exc:                      # noqa: BLE001
            text = f'定时状态读取失败: {exc}'
        self.strategy_status.setText(text)

    def save_strategy_settings(self) -> dict:
        cfg = self.mgr.save_schedule(self.current_strategy_config())
        self._refresh_strategy_status(cfg)
        return cfg

    def _on_save_strategy(self):
        cfg = self.save_strategy_settings()
        QMessageBox.information(
            self, '已保存',
            f"保留策略/定时设置已保存到:\n{self.mgr._schedule_path()}\n\n"
            f"定时: {'启用' if cfg['enabled'] else '关闭'}，"
            f"每 {cfg['interval_minutes']} 分钟，"
            f"保留 {cfg['keep_last']} 个 / {cfg['max_age_days']} 天")

    def prune_dry_run(self) -> dict:
        report = self.mgr.prune(
            keep_last=self.keep_last_spin.value(),
            max_age_days=self.max_age_spin.value() or None,
            dry_run=True)
        lines = [report.get('message', '')]
        for c in report.get('candidates') or []:
            lines.append(f"  - {c['name']}（{c['age_days']} 天，"
                         f"{c['size'] / 1024:.1f} KB）")
        if report.get('errors'):
            lines.append('错误: ' + '; '.join(report['errors']))
        self.strategy_status.setText('\n'.join(lines))
        return report

    def prune_apply(self) -> dict:
        report = self.mgr.prune(
            keep_last=self.keep_last_spin.value(),
            max_age_days=self.max_age_spin.value() or None,
            dry_run=False)
        self._refresh_list()
        self._refresh_strategy_status()
        return report

    def _on_prune_preview(self):
        report = self.prune_dry_run()
        QMessageBox.information(self, '清理预览', self.strategy_status.text())
        return report

    def _on_prune_apply(self):
        ret = QMessageBox.question(
            self, '确认清理',
            '将按保留策略删除快照文件与索引，是否继续？')
        if ret != QMessageBox.Yes:
            return
        report = self.prune_apply()
        msg = report.get('message', '')
        if report.get('errors'):
            msg += '\n\n错误（未静默）:\n' + '\n'.join(report['errors'][:10])
        if report.get('ok'):
            QMessageBox.information(self, '清理完成', msg)
        else:
            QMessageBox.warning(self, '清理未完全成功', msg)

    def run_due_now(self) -> dict:
        conn = self.conn_getter() if callable(self.conn_getter) else None
        if conn is None:
            QMessageBox.warning(self, '提示', '请先选择连接')
            return {'ok': False, 'created': False, 'error': '无可用连接'}
        report = self.mgr.run_due(conn)
        self._refresh_list()
        self.load_strategy_settings()
        return report

    def _on_run_due(self):
        report = self.run_due_now()
        if report.get('created'):
            msg = (f"已创建定时快照: {report.get('snapshot')}\n"
                   f"文件: {report.get('file')}")
            if report.get('prune'):
                msg += '\n\n清理: ' + report['prune'].get('message', '')
            QMessageBox.information(self, '定时快照完成', msg)
        elif report.get('ok'):
            QMessageBox.information(
                self, '未到期', report.get('reason') or '未到执行时间')
        else:
            QMessageBox.critical(
                self, '定时快照失败', report.get('error') or report.get('reason') or '')

    def preview_selected_restore(self) -> dict:
        snap = self._get_selected_snapshot()
        if not snap:
            QMessageBox.warning(self, '提示', '请先选择一个快照')
            return {'ok': False, 'error': '未选择快照'}
        preview = self.mgr.preview_restore(snap)
        self.strategy_status.setText(preview.get('summary') or preview.get('error'))
        return preview

    def _on_preview_restore(self):
        preview = self.preview_selected_restore()
        if not preview.get('ok'):
            QMessageBox.warning(self, '预览失败',
                                preview.get('error') or '无法读取快照')
            return
        QMessageBox.information(
            self, '恢复预览',
            preview['summary'] + '\n\n真实恢复前会自动创建紧急快照 '
            'pre_restore_<ts>；恢复失败会尝试回滚并显式报告。')

    def _create(self):
        conn = self.conn_getter()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择连接")
            return

        # 询问名称
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "创建快照", "快照名称:",
            text=f"snap_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        if not ok or not name.strip():
            return
        requested_name = name.strip()

        progress = QProgressDialog("创建快照...", "取消", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)

        def cb(i, total, tname):
            progress.setValue(int(i / total * 100))
            progress.setLabelText(f"处理表: {tname}")

        try:
            result = self.mgr.create_snapshot(
                conn, requested_name, progress_cb=cb)
            progress.close()
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "失败", str(e))
            return

        # create_snapshot 现在返回 SnapshotResult：失败时绝不弹「完成」，
        # 而是把失败清单（表名 / 语句 / 错误原文）完整展示出来。
        if not getattr(result, 'ok', True):
            QMessageBox.critical(
                self, "快照失败",
                f"快照未创建（未落盘、未进索引）：\n{result.error}\n\n"
                + (result.summary() if hasattr(result, 'summary') else ''))
            return

        snap = result.snapshot
        # P0-3：同名时 core 会自动加 _2/_3 后缀，这里把**实际名字**告诉用户
        actual_name = snap.name
        prefix = ""
        if actual_name != requested_name:
            prefix = (f"同名快照「{requested_name}」已存在，"
                      f"已自动命名为「{actual_name}」。\n\n")
        QMessageBox.information(
            self, "完成",
            prefix + f"快照已创建:\n{snap.file}\n"
            f"名称: {actual_name}\n"
            f"大小: {snap.size / 1024:.1f} KB\n"
            f"表数: {len(snap.tables)}")
        self._refresh_list()

    def _restore(self):
        snap = self._get_selected_snapshot()
        if not snap:
            return

        conn = self.conn_getter()
        if conn is None:
            QMessageBox.warning(self, "提示", "请先选择连接")
            return

        # P2-5：先预览（语句/表/风险），再确认；真实恢复自带紧急快照与回滚
        preview = self.mgr.preview_restore(snap)
        if not preview.get('ok'):
            QMessageBox.critical(
                self, "恢复预览失败", preview.get('error') or "快照不可读")
            return

        ret = QMessageBox.question(
            self, "确认恢复",
            preview['summary']
            + "\n\n恢复前会自动为当前库创建紧急快照 pre_restore_<ts>；"
              "恢复失败会尝试用该快照回滚（不静默半恢复）。是否继续？")
        if ret != QMessageBox.Yes:
            return

        progress = QProgressDialog("恢复中...", "取消", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)

        def cb(i, total, preview_stmt):
            progress.setValue(int(i / total * 100))
            progress.setLabelText(f"执行: {preview_stmt[:60]}")

        try:
            result = self.mgr.restore_snapshot(
                conn, snap, progress_cb=cb)
            progress.close()
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "失败", str(e))
            return

        msg = (f"执行 {result.get('executed', 0)} 条 SQL\n"
               f"失败 {result.get('failed', 0)} 条")
        if result.get('emergency_snapshot'):
            msg += f"\n\n恢复前紧急快照: {result['emergency_snapshot']}"
        if result.get('rolled_back'):
            msg += "\n恢复失败，已用紧急快照回滚到恢复前状态。"
        elif result.get('rollback_error'):
            msg += f"\n回滚失败: {result['rollback_error']}"
        if result.get('errors'):
            msg += '\n\n错误清单:\n' + '\n'.join(
                f"  - {e}" for e in result['errors'][:20])
        if result.get('ok'):
            QMessageBox.information(self, "完成", msg)
        else:
            QMessageBox.critical(self, "恢复未全部成功", msg)
        self._refresh_list()

    def _delete(self):
        snap = self._get_selected_snapshot()
        if not snap:
            return
        ret = QMessageBox.question(
            self, "确认",
            f"删除快照 '{snap.name}'？\n（同名重复条目会被一并删除）")
        if ret == QMessageBox.Yes:
            removed = self.mgr.remove(snap.name)
            if not removed:
                # remove 返回 False 可能是"没有该条目"，也可能是文件/索引写回失败；
                # 具体原因在 mgr.index_warnings 里（界面尽量不静默）。
                QMessageBox.warning(
                    self, "删除未完全成功",
                    "快照条目未全部删除或文件删除/索引写回失败，"
                    "详情见程序 stderr 告警。\n"
                    + "\n".join(self.mgr.index_warnings[-3:]))
            self._refresh_list()

    def _clone(self):
        src_name = self.src_edit.text().strip()
        dst_name = self.dst_edit.text().strip()
        if not src_name or not dst_name:
            QMessageBox.warning(self, "提示", "请填写源/目标连接名")
            return

        # 用主窗口的连接表
        parent = self.parent()
        if not hasattr(parent, 'object_tree'):
            return
        src = parent.object_tree.connections.get(src_name)
        dst = parent.object_tree.connections.get(dst_name)
        if src is None or dst is None:
            QMessageBox.warning(self, "提示", "连接不存在")
            return

        ret = QMessageBox.question(
            self, "确认",
            f"将 '{src_name}' 的表克隆到 '{dst_name}'？\n"
            "目标库同名表可能被覆盖！")
        if ret != QMessageBox.Yes:
            return

        self.clone_log.clear()

        progress = QProgressDialog("克隆中...", "取消", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)

        def cb(i, total, tname):
            progress.setValue(int(i / total * 100))
            progress.setLabelText(f"克隆表: {tname}")
            item = QListWidgetItem(f"→ {tname}")
            item.setForeground(QColor("#89d185"))
            self.clone_log.addItem(item)

        try:
            result = self.mgr.clone_database(
                src, dst, drop_existing=self.drop_check.isChecked(),
                progress_cb=cb)
            progress.close()
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "失败", str(e))
            return

        msg = f"克隆 {result['cloned']} 表，失败 {result['failed']}"
        if result.get('errors'):
            msg += '\n\n错误清单:\n' + '\n'.join(
                f"  - {e}" for e in result['errors'][:20])
        if result.get('ok'):
            QMessageBox.information(self, "完成", msg)
        else:
            QMessageBox.critical(self, "克隆未全部成功", msg)