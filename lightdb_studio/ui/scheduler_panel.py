"""任务调度面板"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QDialogButtonBox, QFormLayout, QLineEdit, QComboBox,
    QSpinBox, QCheckBox, QMessageBox, QTabWidget, QWidget, QLabel,
    QTextEdit, QListWidget, QListWidgetItem, QGroupBox, QTimeEdit
)
from PySide6.QtCore import Qt, QTime
from PySide6.QtGui import QColor, QFont
from datetime import datetime

from ..core.scheduler import Scheduler, ScheduledJob


class JobEditorDialog(QDialog):
    def __init__(self, job: ScheduledJob = None, conn_names=None,
                 report_names=None, parent=None):
        super().__init__(parent)
        self.job = job or ScheduledJob()
        self.conn_names = conn_names or []
        self.report_names = report_names or []
        self.setWindowTitle("编辑任务" if job else "新建任务")
        self.resize(640, 620)
        self._build_ui()
        self._fill()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("任务名称:", self.name_edit)

        self.type_combo = QComboBox()
        self.type_combo.addItems(['执行 SQL', '生成报表'])
        self.type_combo.currentIndexChanged.connect(self._on_type_change)
        form.addRow("任务类型:", self.type_combo)

        self.conn_combo = QComboBox()
        self.conn_combo.setEditable(True)
        self.conn_combo.addItems(self.conn_names)
        form.addRow("连接:", self.conn_combo)

        self.db_edit = QLineEdit()
        form.addRow("数据库:", self.db_edit)

        layout.addLayout(form)

        # SQL / 报表
        self.sql_label = QLabel("SQL:")
        layout.addWidget(self.sql_label)
        self.sql_edit = QTextEdit()
        self.sql_edit.setFont(QFont("Consolas", 10))
        self.sql_edit.setMaximumHeight(120)
        layout.addWidget(self.sql_edit)

        self.report_label = QLabel("选择报表:")
        layout.addWidget(self.report_label)
        self.report_combo = QComboBox()
        self.report_combo.addItems(self.report_names)
        layout.addWidget(self.report_combo)

        # 触发
        trigger_group = QGroupBox("触发条件")
        tg = QFormLayout(trigger_group)

        self.trigger_combo = QComboBox()
        self.trigger_combo.addItems(
            ['立即执行一次', '间隔执行', '每天', '每周', '每月'])
        self.trigger_combo.currentIndexChanged.connect(
            self._on_trigger_change)
        tg.addRow("频率:", self.trigger_combo)

        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setTime(QTime(9, 0))
        tg.addRow("时间:", self.time_edit)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(10, 86400 * 30)
        self.interval_spin.setValue(3600)
        self.interval_spin.setSuffix(" 秒")
        tg.addRow("间隔:", self.interval_spin)

        self.day_combo = QComboBox()
        self.day_combo.addItems(['周一', '周二', '周三', '周四',
                                 '周五', '周六', '周日'])
        tg.addRow("星期:", self.day_combo)

        self.month_spin = QSpinBox()
        self.month_spin.setRange(1, 28)
        self.month_spin.setValue(1)
        tg.addRow("每月第几天:", self.month_spin)

        layout.addWidget(trigger_group)

        # 导出
        exp_group = QGroupBox("导出")
        eg = QFormLayout(exp_group)
        self.exp_format = QComboBox()
        self.exp_format.addItems(['不导出', 'CSV', 'Excel', 'HTML'])
        eg.addRow("格式:", self.exp_format)
        layout.addWidget(exp_group)

        # 高级
        adv_group = QGroupBox("高级")
        ag = QFormLayout(adv_group)
        self.enabled_check = QCheckBox("启用")
        self.enabled_check.setChecked(True)
        ag.addRow("", self.enabled_check)

        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 5)
        ag.addRow("失败重试:", self.retries_spin)
        layout.addWidget(adv_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._on_type_change()

    def _fill(self):
        j = self.job
        self.name_edit.setText(j.name)
        idx = 0 if j.job_type == 'sql' else 1
        self.type_combo.setCurrentIndex(idx)
        self.conn_combo.setCurrentText(j.connection)
        self.db_edit.setText(j.database)
        self.sql_edit.setPlainText(j.sql)
        if j.report_template:
            ri = self.report_combo.findText(j.report_template)
            if ri >= 0:
                self.report_combo.setCurrentIndex(ri)

        # 触发
        trigger_map = {'once': 0, 'interval': 1,
                       'daily': 2, 'weekly': 3, 'monthly': 4}
        self.trigger_combo.setCurrentIndex(
            trigger_map.get(j.trigger, 2))

        if j.time_of_day:
            try:
                h, m = j.time_of_day.split(':')
                self.time_edit.setTime(QTime(int(h), int(m)))
            except Exception:
                pass
        if j.interval_seconds:
            self.interval_spin.setValue(j.interval_seconds)
        self.day_combo.setCurrentIndex(j.day_of_week)
        self.month_spin.setValue(j.day_of_month or 1)

        fmt_map = {'': 0, 'csv': 1, 'excel': 2, 'html': 3}
        self.exp_format.setCurrentIndex(
            fmt_map.get(j.export_format, 0))

        self.enabled_check.setChecked(j.enabled)
        self.retries_spin.setValue(j.max_retries)

        self._on_trigger_change()

    def _on_type_change(self):
        is_sql = self.type_combo.currentIndex() == 0
        self.sql_label.setVisible(is_sql)
        self.sql_edit.setVisible(is_sql)
        self.report_label.setVisible(not is_sql)
        self.report_combo.setVisible(not is_sql)

    def _on_trigger_change(self):
        idx = self.trigger_combo.currentIndex()
        self.time_edit.setVisible(idx in (2, 3, 4))
        self.interval_spin.setVisible(idx == 1)
        self.day_combo.setVisible(idx == 3)
        self.month_spin.setVisible(idx == 4)

    def get_job(self) -> ScheduledJob:
        j = self.job

        j.name = self.name_edit.text().strip()
        j.job_type = 'sql' if self.type_combo.currentIndex() == 0 else 'report'
        j.connection = self.conn_combo.currentText().strip()
        j.database = self.db_edit.text().strip()
        j.sql = self.sql_edit.toPlainText().strip()
        j.report_template = self.report_combo.currentText().strip()

        trigger_map = ['once', 'interval', 'daily', 'weekly', 'monthly']
        j.trigger = trigger_map[self.trigger_combo.currentIndex()]

        t = self.time_edit.time()
        j.time_of_day = f"{t.hour():02d}:{t.minute():02d}"
        j.interval_seconds = self.interval_spin.value()
        j.day_of_week = self.day_combo.currentIndex()
        j.day_of_month = self.month_spin.value()

        fmt_map = ['', 'csv', 'excel', 'html']
        j.export_format = fmt_map[self.exp_format.currentIndex()]

        j.enabled = self.enabled_check.isChecked()
        j.max_retries = self.retries_spin.value()

        if j.trigger == 'once':
            import time
            j.run_at = time.time() + 5  # 5 秒后执行

        return j


class SchedulerPanel(QDialog):
    def __init__(self, scheduler: Scheduler, conn_names=None,
                 report_names=None, parent=None):
        super().__init__(parent)
        self.scheduler = scheduler
        self.conn_names = conn_names or []
        self.report_names = report_names or []
        self.setWindowTitle("任务调度")
        self.resize(940, 620)
        self._build_ui()
        self._refresh()
        self.scheduler.add_callback(self._on_job_executed)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        tabs = QTabWidget()

        # Tab1: 任务列表
        jobs_widget = QWidget()
        jl = QVBoxLayout(jobs_widget)

        self.jobs_table = QTableWidget()
        self.jobs_table.setColumnCount(7)
        self.jobs_table.setHorizontalHeaderLabels(
            ['状态', '名称', '类型', '频率', '下次执行', '上次结果', '运行次数'])
        self.jobs_table.setColumnWidth(0, 60)
        self.jobs_table.setColumnWidth(1, 180)
        self.jobs_table.setColumnWidth(2, 80)
        self.jobs_table.setColumnWidth(3, 130)
        self.jobs_table.setColumnWidth(4, 160)
        self.jobs_table.setColumnWidth(5, 100)
        self.jobs_table.horizontalHeader().setStretchLastSection(True)
        self.jobs_table.doubleClicked.connect(self._edit_job)
        jl.addWidget(self.jobs_table)

        row = QHBoxLayout()
        add_btn = QPushButton("➕ 新建任务")
        add_btn.clicked.connect(self._add_job)
        row.addWidget(add_btn)

        edit_btn = QPushButton("✏ 编辑")
        edit_btn.clicked.connect(self._edit_job)
        row.addWidget(edit_btn)

        del_btn = QPushButton("🗑 删除")
        del_btn.clicked.connect(self._del_job)
        row.addWidget(del_btn)

        toggle_btn = QPushButton("⏯ 启用/禁用")
        toggle_btn.clicked.connect(self._toggle_job)
        row.addWidget(toggle_btn)

        run_btn = QPushButton("▶ 立即执行")
        run_btn.clicked.connect(self._run_now)
        row.addWidget(run_btn)
        row.addStretch()

        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self._refresh)
        row.addWidget(refresh_btn)
        jl.addLayout(row)

        tabs.addTab(jobs_widget, "任务列表")

        # Tab2: 执行历史
        hist_widget = QWidget()
        hl = QVBoxLayout(hist_widget)

        self.hist_list = QListWidget()
        self.hist_list.setFont(QFont("Consolas", 9))
        hl.addWidget(self.hist_list)

        hist_row = QHBoxLayout()
        ref_hist_btn = QPushButton("🔄 刷新")
        ref_hist_btn.clicked.connect(self._refresh_history)
        hist_row.addWidget(ref_hist_btn)
        clear_hist_btn = QPushButton("清空历史")
        clear_hist_btn.clicked.connect(self._clear_history)
        hist_row.addWidget(clear_hist_btn)
        hist_row.addStretch()
        hl.addLayout(hist_row)

        tabs.addTab(hist_widget, "执行历史")

        layout.addWidget(tabs)

        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.reject)
        layout.addWidget(close_btn)

    def _refresh(self):
        jobs = self.scheduler.jobs
        self.jobs_table.setRowCount(len(jobs))
        for r, j in enumerate(jobs):
            status = '✅ 启用' if j.enabled else '⏸ 禁用'
            self.jobs_table.setItem(r, 0, QTableWidgetItem(status))

            self.jobs_table.setItem(r, 1, QTableWidgetItem(j.name))
            self.jobs_table.setItem(
                r, 2, QTableWidgetItem(
                    'SQL' if j.job_type == 'sql' else '报表'))

            # 频率描述
            freq = self._fmt_freq(j)
            self.jobs_table.setItem(r, 3, QTableWidgetItem(freq))

            # 下次执行
            next_str = self._fmt_time(j.next_run) if j.next_run > 0 else '-'
            self.jobs_table.setItem(r, 4, QTableWidgetItem(next_str))

            # 上次结果
            last = j.last_status or '-'
            item = QTableWidgetItem(last)
            if last == 'success':
                item.setForeground(QColor("#89d185"))
            elif last == 'failed':
                item.setForeground(QColor("#f48771"))
            self.jobs_table.setItem(r, 5, item)

            self.jobs_table.setItem(r, 6, QTableWidgetItem(str(j.run_count)))

    def _fmt_freq(self, j):
        if j.trigger == 'once':
            return '一次'
        if j.trigger == 'interval':
            return f"每 {j.interval_seconds} 秒"
        if j.trigger == 'daily':
            return f"每天 {j.time_of_day}"
        if j.trigger == 'weekly':
            days = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
            return f"每{days[j.day_of_week]} {j.time_of_day}"
        if j.trigger == 'monthly':
            return f"每月 {j.day_of_month} 号 {j.time_of_day}"
        return '-'

    def _fmt_time(self, ts):
        if not ts:
            return '-'
        try:
            return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return str(ts)

    def _add_job(self):
        dlg = JobEditorDialog(None, self.conn_names,
                              self.report_names, self)
        if dlg.exec() == JobEditorDialog.Accepted:
            job = dlg.get_job()
            if not job.name:
                QMessageBox.warning(self, "提示", "请填写任务名")
                return
            self.scheduler.jobs.append(job)
            self.scheduler.save()
            self._refresh()

    def _edit_job(self):
        r = self.jobs_table.currentRow()
        if r < 0 or r >= len(self.scheduler.jobs):
            return
        job = self.scheduler.jobs[r]
        dlg = JobEditorDialog(job, self.conn_names,
                              self.report_names, self)
        if dlg.exec() == JobEditorDialog.Accepted:
            self.scheduler.jobs[r] = dlg.get_job()
            self.scheduler.save()
            self._refresh()

    def _del_job(self):
        r = self.jobs_table.currentRow()
        if r < 0 or r >= len(self.scheduler.jobs):
            return
        job = self.scheduler.jobs[r]
        ret = QMessageBox.question(self, "确认",
                                    f"删除任务 '{job.name}'？")
        if ret == QMessageBox.Yes:
            del self.scheduler.jobs[r]
            self.scheduler.save()
            self._refresh()

    def _toggle_job(self):
        r = self.jobs_table.currentRow()
        if r < 0 or r >= len(self.scheduler.jobs):
            return
        job = self.scheduler.jobs[r]
        job.enabled = not job.enabled
        if job.enabled and job.next_run == 0:
            job.next_run = time.time() + 5
        self.scheduler.save()
        self._refresh()

    def _run_now(self):
        r = self.jobs_table.currentRow()
        if r < 0 or r >= len(self.scheduler.jobs):
            return
        job = self.scheduler.jobs[r]
        ret = self.scheduler.execute_now(job.name)
        if ret:
            QMessageBox.information(
                self, "完成",
                f"状态: {ret.status}\n行数: {ret.rows}\n"
                + (f"错误: {ret.error}" if ret.error else ""))
        self._refresh()
        self._refresh_history()

    def _on_job_executed(self, job, run):
        """任务执行回调"""
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._refresh)
        QTimer.singleShot(0, self._refresh_history)

    def _refresh_history(self):
        self.hist_list.clear()
        for run in self.scheduler.get_history(limit=200):
            start = self._fmt_time(run.start_time)
            status = '✅' if run.status == 'success' else '❌'
            el = run.end_time - run.start_time
            text = (f"{start}  {status}  {run.job_name}  "
                    f"({el:.2f}s, {run.rows} 行)")
            item = QListWidgetItem(text)
            if run.status == 'failed':
                item.setForeground(QColor("#f48771"))
            if run.error:
                item.setToolTip(run.error)
            self.hist_list.addItem(item)

    def _clear_history(self):
        ret = QMessageBox.question(self, "确认", "清空执行历史？")
        if ret == QMessageBox.Yes:
            self.scheduler.history.clear()
            self.scheduler.save()
            self._refresh_history()