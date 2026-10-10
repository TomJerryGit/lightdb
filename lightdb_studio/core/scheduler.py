"""
任务调度器
- 定时执行 SQL / 报表
- 后台线程每秒检查
- 失败重试
- 执行历史
"""
import json
import threading
import time
from pathlib import Path
from collections import deque
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from typing import List, Callable, Optional

SCHEDULE_FILE = Path.home() / '.lightdb_studio' / 'schedules.json'


@dataclass
class ScheduledJob:
    name: str = ""
    # 任务类型: 'sql' | 'report'
    job_type: str = "sql"
    connection: str = ""
    database: str = ""
    sql: str = ""                  # job_type=sql 时
    report_template: str = ""      # job_type=report 时
    export_format: str = "csv"     # csv / excel / html
    export_dir: str = ""
    # 触发: 'once' | 'interval' | 'daily' | 'weekly' | 'monthly'
    trigger: str = "daily"
    # once: 时间戳
    run_at: float = 0
    # interval: 秒
    interval_seconds: int = 3600
    # daily/weekly/monthly: "HH:MM"
    time_of_day: str = "09:00"
    # weekly: 星期几 (0=Mon)
    day_of_week: int = 0
    # monthly: 几号
    day_of_month: int = 1
    # 状态
    enabled: bool = True
    last_run: float = 0
    last_status: str = ""
    next_run: float = 0
    run_count: int = 0
    fail_count: int = 0
    # 重试
    max_retries: int = 0
    # 邮件通知
    notify_email: str = ""
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class JobRun:
    job_name: str
    start_time: float
    end_time: float
    status: str                    # 'success' | 'failed'
    rows: int = 0
    error: str = ""
    output: str = ""


class Scheduler:
    def __init__(self, connection_provider: Callable = None,
                 report_engine=None):
        """
        connection_provider: 函数，接受连接名返回 DBConnection
        """
        self.file = SCHEDULE_FILE
        self.jobs: List[ScheduledJob] = []
        self.history = deque(maxlen=500)
        self.connection_provider = connection_provider
        self.report_engine = report_engine

        self._running = False
        self._thread = None
        self._lock = threading.RLock()
        self._callbacks = []

        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for j in data.get('jobs', []):
                self.jobs.append(ScheduledJob(**j))
            for h in data.get('history', []):
                self.history.append(JobRun(**h))
        except Exception:
            pass

    def save(self):
        data = {
            'jobs': [asdict(j) for j in self.jobs],
            'history': [asdict(h) for h in list(self.history)[-200:]],
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def add_callback(self, fn):
        """添加执行回调（用于 UI 刷新）"""
        self._callbacks.append(fn)

    # ==================== 生命周期 ====================

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='Scheduler')
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self):
        while self._running:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(1.0)

    def _tick(self):
        now = time.time()
        with self._lock:
            for j in self.jobs:
                if not j.enabled:
                    continue
                if j.next_run == 0:
                    j.next_run = self._calc_next_run(j, now)
                    continue
                if j.next_run <= now:
                    self._execute(j)
                    j.next_run = self._calc_next_run(j, now + 1)

    def _calc_next_run(self, job: ScheduledJob, now: float) -> float:
        if job.trigger == 'once':
            return job.run_at if job.run_at > now else 0
        if job.trigger == 'interval':
            return now + job.interval_seconds
        if job.trigger == 'daily':
            h, m = self._parse_time(job.time_of_day)
            target = datetime.fromtimestamp(now).replace(
                hour=h, minute=m, second=0, microsecond=0)
            if target.timestamp() <= now:
                target += timedelta(days=1)
            return target.timestamp()
        if job.trigger == 'weekly':
            h, m = self._parse_time(job.time_of_day)
            target = datetime.fromtimestamp(now).replace(
                hour=h, minute=m, second=0, microsecond=0)
            days_ahead = (job.day_of_week - target.weekday()) % 7
            if days_ahead == 0 and target.timestamp() <= now:
                days_ahead = 7
            target += timedelta(days=days_ahead)
            return target.timestamp()
        if job.trigger == 'monthly':
            h, m = self._parse_time(job.time_of_day)
            target = datetime.fromtimestamp(now).replace(
                day=min(job.day_of_month, 28),
                hour=h, minute=m, second=0, microsecond=0)
            if target.timestamp() <= now:
                # 下个月
                if target.month == 12:
                    target = target.replace(year=target.year + 1, month=1)
                else:
                    target = target.replace(month=target.month + 1)
            return target.timestamp()
        return 0

    def _parse_time(self, s: str):
        try:
            h, m = s.split(':')
            return int(h), int(m)
        except (ValueError, AttributeError):
            return 9, 0

    # ==================== 执行 ====================

    def _execute(self, job: ScheduledJob, attempt: int = 0):
        """执行任务"""
        start = time.time()
        status = 'success'
        error = ''
        rows = 0
        output = ''

        try:
            conn = self._get_conn(job.connection)
            if conn is None:
                raise RuntimeError(f"连接 '{job.connection}' 不可用")

            if job.database:
                try:
                    conn.switch_database(job.database)
                except Exception:
                    pass

            if job.job_type == 'sql':
                cols, data = conn.query(job.sql)
                rows = len(data)
                if job.export_format and job.export_dir:
                    output = self._export(cols, data, job)
            elif job.job_type == 'report':
                output = self._run_report(conn, job)
                rows = -1
            else:
                raise RuntimeError(f"未知任务类型: {job.job_type}")

        except Exception as e:
            status = 'failed'
            error = str(e)
            job.fail_count += 1

        end = time.time()
        job.last_run = start
        job.last_status = status
        job.run_count += 1

        # 记录历史
        run = JobRun(
            job_name=job.name,
            start_time=start,
            end_time=end,
            status=status,
            rows=rows,
            error=error,
            output=output,
        )
        with self._lock:
            self.history.append(run)
        self.save()

        # 失败重试
        if status == 'failed' and attempt < job.max_retries:
            delay = 2 ** attempt  # 指数退避
            threading.Timer(delay, lambda: self._execute(job, attempt + 1)).start()

        # 通知 UI
        for cb in self._callbacks:
            try:
                cb(job, run)
            except Exception:
                pass

    def _get_conn(self, name: str):
        if self.connection_provider is None:
            return None
        try:
            return self.connection_provider(name)
        except Exception:
            return None

    def _export(self, cols, rows, job: ScheduledJob) -> str:
        import os
        out_dir = job.export_dir or str(Path.home() / '.lightdb_studio' /
                                         'reports' / 'outputs')
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        ext = {'csv': 'csv', 'excel': 'xlsx', 'html': 'html'}.get(
            job.export_format, 'csv')
        filename = f"{job.name}_{ts}.{ext}"
        path = os.path.join(out_dir, filename)

        if job.export_format == 'csv':
            self.report_engine.export_csv(cols, rows, path)
        elif job.export_format == 'excel':
            self.report_engine.export_excel(cols, rows, path,
                                            title=job.name)
        elif job.export_format == 'html':
            self.report_engine.export_html(cols, rows, path, title=job.name)
        return path

    def _run_report(self, conn, job: ScheduledJob) -> str:
        tmpl = self.report_engine.get(job.report_template)
        if tmpl is None:
            raise RuntimeError(f"报表 '{job.report_template}' 不存在")
        cols, rows = conn.query(tmpl.sql)
        return self._export(cols, rows, job)

    def execute_now(self, name: str) -> Optional[JobRun]:
        """手动立即执行"""
        for j in self.jobs:
            if j.name == name:
                self._execute(j)
                return list(self.history)[-1] if self.history else None
        return None

    def get_history(self, job_name: str = None, limit: int = 100):
        h = list(self.history)
        if job_name:
            h = [r for r in h if r.job_name == job_name]
        return h[-limit:][::-1]