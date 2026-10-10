"""
工作流 - 多步骤 SQL 编排
- 步骤串行执行
- 步骤间依赖（前面的结果可作为后面的参数）
- 条件分支
- 失败策略（继续 / 停止 / 回滚）
"""
import json
import time
import re
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional   # ★ 修复：补 Dict

WORKFLOW_FILE = Path.home() / '.lightdb_studio' / 'workflows.json'


@dataclass
class WorkflowStep:
    name: str = ""
    sql: str = ""
    continue_on_error: bool = False
    save_as: str = ""
    condition: str = ""
    description: str = ""


@dataclass
class Workflow:
    name: str = ""
    description: str = ""
    connection: str = ""
    database: str = ""
    steps: List[WorkflowStep] = field(default_factory=list)
    use_transaction: bool = False
    last_run: float = 0
    last_status: str = ""
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class WorkflowRun:
    workflow: str
    start_time: float
    end_time: float
    status: str
    step_results: List[Dict] = field(default_factory=list)
    error: str = ""


class WorkflowEngine:
    def __init__(self, connection_provider=None):
        self.file = WORKFLOW_FILE
        self.workflows: List[Workflow] = []
        self.history: List[WorkflowRun] = []
        self.connection_provider = connection_provider
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for w in data.get('workflows', []):
                w = dict(w)
                steps = [WorkflowStep(**s) for s in w.pop('steps', [])]
                wf = Workflow(**w)
                wf.steps = steps
                self.workflows.append(wf)
        except Exception:
            pass

    def save(self):
        data = {
            'workflows': [
                {**asdict(w), 'steps': [asdict(s) for s in w.steps]}
                for w in self.workflows
            ]
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def add(self, wf: Workflow):
        self.workflows.append(wf)
        self.save()

    def update(self, idx: int, wf: Workflow):
        if 0 <= idx < len(self.workflows):
            self.workflows[idx] = wf
            self.save()

    def remove(self, idx: int):
        if 0 <= idx < len(self.workflows):
            del self.workflows[idx]
            self.save()

    def get(self, name: str) -> Optional[Workflow]:
        for w in self.workflows:
            if w.name == name:
                return w
        return None

    # ==================== 执行 ====================

    def run(self, workflow: Workflow,
            progress_cb=None) -> WorkflowRun:
        start = time.time()
        run = WorkflowRun(
            workflow=workflow.name,
            start_time=start,
            end_time=start,
            status='running',
        )

        conn = None
        if self.connection_provider:
            conn = self.connection_provider(workflow.connection)

        if conn is None:
            run.status = 'failed'
            run.error = '连接不可用'
            run.end_time = time.time()
            self.history.append(run)
            return run

        if workflow.database:
            try:
                conn.switch_database(workflow.database)
            except Exception:
                pass

        variables: Dict[str, list] = {}   # ★ 修复：Dict 已 import

        try:
            if workflow.use_transaction:
                conn.execute("BEGIN")

            for i, step in enumerate(workflow.steps):
                step_result = {
                    'name': step.name,
                    'index': i,
                    'status': 'pending',
                    'rows': 0,
                    'error': '',
                    'elapsed': 0,
                }

                if step.condition and not self._eval_condition(
                        step.condition, variables):
                    step_result['status'] = 'skipped'
                    run.step_results.append(step_result)
                    if progress_cb:
                        progress_cb(i, step, step_result)
                    continue

                sql = self._substitute(step.sql, variables)

                t0 = time.time()
                try:
                    up = sql.strip().upper().rstrip(';')
                    if up.startswith(('SELECT', 'SHOW', 'DESC',
                                      'EXPLAIN', 'WITH')):
                        cols, rows = conn.query(sql)
                        step_result['rows'] = len(rows)
                        step_result['columns'] = cols
                        step_result['data'] = rows[:100]
                        if step.save_as:
                            variables[step.save_as] = rows
                    else:
                        rows_affected, err = conn.execute(sql)
                        if err:
                            raise RuntimeError(err)
                        step_result['rows'] = rows_affected

                    step_result['status'] = 'success'

                except Exception as e:
                    step_result['status'] = 'failed'
                    step_result['error'] = str(e)
                    if not step.continue_on_error:
                        raise

                step_result['elapsed'] = time.time() - t0
                run.step_results.append(step_result)

                if progress_cb:
                    progress_cb(i, step, step_result)

            if workflow.use_transaction:
                conn.execute("COMMIT")

            run.status = 'success'

        except Exception as e:
            run.status = 'failed'
            run.error = str(e)
            if workflow.use_transaction:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass

        run.end_time = time.time()
        workflow.last_run = start
        workflow.last_status = run.status
        self.history.append(run)
        if len(self.history) > 200:
            self.history = self.history[-200:]
        self.save()

        return run

    def _substitute(self, sql: str, variables: Dict) -> str:
        def repl(m):
            expr = m.group(1).strip()
            parts = expr.split('.', 1)
            name = parts[0]
            if name not in variables:
                return m.group(0)
            val = variables[name]
            if len(parts) > 1 and isinstance(val, list) and val:
                field_name = parts[1]
                first = val[0]
                if isinstance(first, (list, tuple)):
                    for v in first:
                        if v is not None:
                            return self._format(v)
                return self._format(first)
            return self._format(val)
        return re.sub(r'\{\{(.+?)\}\}', repl, sql)

    def _format(self, v):
        if v is None:
            return 'NULL'
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, bool):
            return '1' if v else '0'
        return "'" + str(v).replace("'", "''") + "'"

    def _eval_condition(self, cond: str, variables: Dict) -> bool:
        cond = cond.strip()
        if cond in variables:
            v = variables[cond]
            if isinstance(v, list):
                return len(v) > 0
            return bool(v)

        m = re.match(
            r'(\w+)\s*(==|!=|>=|<=|>|<)\s*(.+)', cond)
        if m:
            name, op, target = m.group(1), m.group(2), m.group(3).strip()
            if name not in variables:
                return False
            v = variables[name]
            if isinstance(v, list):
                if not v:
                    return op in ('==', '<=')
                v = v[0][0] if v[0] else None
            target = target.strip("'\"")
            try:
                if isinstance(v, (int, float)):
                    target = float(target)
                elif isinstance(v, bool):
                    target = target.lower() in ('true', '1', 'yes')
            except (ValueError, TypeError):
                pass
            if op == '==': return v == target
            if op == '!=': return v != target
            if op == '>': return v > target
            if op == '<': return v < target
            if op == '>=': return v >= target
            if op == '<=': return v <= target
        return False