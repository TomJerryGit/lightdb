"""
协作 - 会话共享 + 评论注解
"""
import json
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict

COLLAB_FILE = Path.home() / '.lightdb_studio' / 'collaboration.json'


@dataclass
class Comment:
    id: str = ""
    target_type: str = ""       # 'sql' / 'table' / 'dashboard'
    target_id: str = ""         # SQL hash 或表名
    author: str = "me"
    content: str = ""
    mentions: List[str] = field(default_factory=list)
    resolved: bool = False
    created_at: float = 0
    replies: List[Dict] = field(default_factory=list)

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:8]
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class SharedSession:
    id: str = ""
    name: str = ""
    connection: str = ""
    database: str = ""
    sql: str = ""
    owner: str = "me"
    collaborators: List[str] = field(default_factory=list)
    comments: List[Comment] = field(default_factory=list)
    readonly: bool = False
    created_at: float = 0
    updated_at: float = 0
    password: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:8]
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


class CollaborationStore:
    def __init__(self):
        self.file = COLLAB_FILE
        self.sessions: List[SharedSession] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for s in data.get('sessions', []):
                comments = [Comment(**c) for c in s.pop('comments', [])]
                sess = SharedSession(**s)
                sess.comments = comments
                self.sessions.append(sess)
        except Exception:
            pass

    def save(self):
        data = {
            'sessions': [
                {**asdict(s),
                 'comments': [asdict(c) for c in s.comments]}
                for s in self.sessions
            ]
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def create_session(self, name: str, connection: str,
                       database: str = '', sql: str = '',
                       owner: str = 'me',
                       readonly: bool = False) -> SharedSession:
        s = SharedSession(
            name=name, connection=connection, database=database,
            sql=sql, owner=owner, readonly=readonly)
        self.sessions.append(s)
        self.save()
        return s

    def get(self, sid: str) -> SharedSession:
        for s in self.sessions:
            if s.id == sid:
                return s
        return None

    def update_sql(self, sid: str, sql: str):
        s = self.get(sid)
        if s and not s.readonly:
            s.sql = sql
            s.updated_at = time.time()
            self.save()

    def add_comment(self, sid: str, target_id: str, content: str,
                    author: str = 'me', target_type: str = 'sql',
                    mentions: List[str] = None) -> Comment:
        s = self.get(sid)
        if not s:
            return None
        c = Comment(
            target_type=target_type,
            target_id=target_id,
            author=author,
            content=content,
            mentions=mentions or [],
        )
        s.comments.append(c)
        s.updated_at = time.time()
        self.save()
        return c

    def reply_comment(self, sid: str, cid: str, content: str,
                      author: str = 'me'):
        s = self.get(sid)
        if not s:
            return
        for c in s.comments:
            if c.id == cid:
                c.replies.append({
                    'author': author,
                    'content': content,
                    'ts': time.time(),
                })
                s.updated_at = time.time()
                self.save()
                return

    def resolve_comment(self, sid: str, cid: str):
        s = self.get(sid)
        if not s:
            return
        for c in s.comments:
            if c.id == cid:
                c.resolved = True
                s.updated_at = time.time()
                self.save()
                return

    def export_session(self, sid: str, filepath: str):
        """导出会话到文件"""
        s = self.get(sid)
        if not s:
            return False
        data = asdict(s)
        data['comments'] = [asdict(c) for c in s.comments]
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True

    def import_session(self, filepath: str) -> SharedSession:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        comments = [Comment(**c) for c in data.pop('comments', [])]
        s = SharedSession(**data)
        s.comments = comments
        # 生成新 id 避免冲突
        s.id = str(uuid.uuid4())[:8]
        self.sessions.append(s)
        self.save()
        return s

    def list_all(self) -> List[SharedSession]:
        return sorted(self.sessions, key=lambda x: -x.updated_at)

    def delete(self, sid: str):
        self.sessions = [s for s in self.sessions if s.id != sid]
        self.save()