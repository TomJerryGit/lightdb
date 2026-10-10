"""
SQL 审查工作流
- 提交 SQL 请求
- 审核人批准/拒绝
- 批准后可执行
- 记录审查历史
"""
import json
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Optional

REVIEW_FILE = Path.home() / '.lightdb_studio' / 'reviews.json'


@dataclass
class ReviewRequest:
    id: str = ""
    title: str = ""
    sql: str = ""
    connection: str = ""
    database: str = ""
    submitter: str = "me"
    reason: str = ""
    # 状态: pending / approved / rejected / executed / cancelled
    status: str = "pending"
    reviewer: str = ""
    review_comment: str = ""
    submitted_at: float = 0
    reviewed_at: float = 0
    executed_at: float = 0
    execution_status: str = ""
    execution_error: str = ""

    def __post_init__(self):
        if not self.submitted_at:
            self.submitted_at = time.time()
        if not self.id:
            self.id = f"REQ-{int(self.submitted_at * 1000) % 1000000}"


class ReviewWorkflow:
    def __init__(self):
        self.file = REVIEW_FILE
        self.requests: List[ReviewRequest] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for r in data.get('requests', []):
                self.requests.append(ReviewRequest(**r))
        except Exception:
            pass

    def save(self):
        data = {'requests': [asdict(r) for r in self.requests]}
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== CRUD ====================

    def submit(self, title: str, sql: str, connection: str = '',
               database: str = '', submitter: str = 'me',
               reason: str = '') -> ReviewRequest:
        req = ReviewRequest(
            title=title,
            sql=sql,
            connection=connection,
            database=database,
            submitter=submitter,
            reason=reason,
        )
        self.requests.append(req)
        self.save()
        return req

    def approve(self, req_id: str, reviewer: str = 'admin',
                comment: str = ''):
        req = self.get(req_id)
        if req and req.status == 'pending':
            req.status = 'approved'
            req.reviewer = reviewer
            req.review_comment = comment
            req.reviewed_at = time.time()
            self.save()
            return True
        return False

    def reject(self, req_id: str, reviewer: str = 'admin',
               comment: str = ''):
        req = self.get(req_id)
        if req and req.status == 'pending':
            req.status = 'rejected'
            req.reviewer = reviewer
            req.review_comment = comment
            req.reviewed_at = time.time()
            self.save()
            return True
        return False

    def mark_executed(self, req_id: str, success: bool, error: str = ''):
        req = self.get(req_id)
        if req and req.status == 'approved':
            req.status = 'executed'
            req.executed_at = time.time()
            req.execution_status = 'success' if success else 'failed'
            req.execution_error = error
            self.save()
            return True
        return False

    def cancel(self, req_id: str):
        req = self.get(req_id)
        if req and req.status == 'pending':
            req.status = 'cancelled'
            self.save()
            return True
        return False

    def get(self, req_id: str) -> Optional[ReviewRequest]:
        for r in self.requests:
            if r.id == req_id:
                return r
        return None

    def list_all(self, status: str = None) -> List[ReviewRequest]:
        reqs = list(reversed(self.requests))
        if status:
            reqs = [r for r in reqs if r.status == status]
        return reqs

    def stats(self) -> dict:
        counts = {
            'pending': 0, 'approved': 0, 'rejected': 0,
            'executed': 0, 'cancelled': 0,
        }
        for r in self.requests:
            counts[r.status] = counts.get(r.status, 0) + 1
        return {
            'total': len(self.requests),
            **counts,
        }