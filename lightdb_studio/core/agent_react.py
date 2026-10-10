"""
ReAct Agent 2.0
- 思考 → 行动 → 观察 → 再思考 循环
- 多工具调用
- 自主执行（带确认策略）
- 长期记忆
"""
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Callable, Any, Optional

from .ai_integration import AIClient, AIConfig

MEMORY_FILE = Path.home() / '.lightdb_studio' / 'agent_memory.json'


@dataclass
class ToolSpec:
    """工具定义"""
    name: str
    description: str
    params: Dict = field(default_factory=dict)
    # {'param_name': {'type': 'string', 'description': '...', 'required': True}}
    handler: Callable = None
    dangerous: bool = False     # 危险操作需确认


@dataclass
class AgentStep:
    """Agent 执行步骤"""
    step_type: str = ""         # thought / action / observation / answer
    content: str = ""
    tool: str = ""
    tool_params: Dict = field(default_factory=dict)
    tool_result: Any = None
    timestamp: float = 0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class AgentSession:
    """会话"""
    session_id: str = ""
    question: str = ""
    steps: List[AgentStep] = field(default_factory=list)
    final_answer: str = ""
    status: str = "running"     # running / success / failed / cancelled
    tool_calls: int = 0
    started_at: float = 0
    ended_at: float = 0

    def __post_init__(self):
        if not self.started_at:
            self.started_at = time.time()


class MemoryStore:
    """长期记忆"""

    def __init__(self):
        self.file = MEMORY_FILE
        self.memories: List[Dict] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.memories = data.get('memories', [])[-500:]
        except Exception:
            pass

    def save(self):
        data = {'memories': self.memories[-500:]}
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def add(self, question: str, answer: str,
            category: str = 'general'):
        self.memories.append({
            'question': question,
            'answer': answer,
            'category': category,
            'ts': time.time(),
        })
        self.save()

    def search(self, query: str, top_n: int = 3) -> List[Dict]:
        """简单关键词匹配"""
        words = set(re.findall(r'[\u4e00-\u9fff]+|[a-z]+',
                                query.lower()))
        scored = []
        for m in self.memories:
            m_words = set(re.findall(
                r'[\u4e00-\u9fff]+|[a-z]+',
                m.get('question', '').lower()))
            if not m_words:
                continue
            inter = words & m_words
            if inter:
                sim = len(inter) / (len(words) + len(m_words) - len(inter))
                scored.append((sim, m))
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:top_n]]


class ReActAgent:
    """ReAct 模式 Agent"""

    def __init__(self, ai_config: AIConfig = None,
                 conn_provider=None, schema_provider=None,
                 confirm_callback: Callable = None):
        """
        confirm_callback: 危险操作时调用，返回 True/False
        """
        self.client = AIClient(ai_config) if ai_config else None
        self.conn_provider = conn_provider
        self.schema_provider = schema_provider
        self.confirm_callback = confirm_callback
        self.memory = MemoryStore()
        self.tools: Dict[str, ToolSpec] = {}
        self._register_builtin_tools()

    def set_ai_config(self, config: AIConfig):
        self.client = AIClient(config)

    # ==================== 工具注册 ====================

    def _register_builtin_tools(self):
        """注册内置工具"""
        self.register_tool(ToolSpec(
            name='list_tables',
            description='列出数据库中的所有表',
            params={
                'database': {'type': 'string',
                              'description': '数据库名（可选）'},
            },
            handler=self._tool_list_tables,
        ))

        self.register_tool(ToolSpec(
            name='describe_table',
            description='查看表结构',
            params={
                'table': {'type': 'string',
                           'description': '表名',
                           'required': True},
            },
            handler=self._tool_describe_table,
        ))

        self.register_tool(ToolSpec(
            name='execute_query',
            description='执行 SELECT 查询并返回结果',
            params={
                'sql': {'type': 'string',
                         'description': 'SELECT 语句',
                         'required': True},
                'limit': {'type': 'integer',
                           'description': '返回行数限制',
                           'default': 50},
            },
            handler=self._tool_execute_query,
        ))

        self.register_tool(ToolSpec(
            name='explain_query',
            description='获取 SQL 执行计划',
            params={
                'sql': {'type': 'string',
                         'description': 'SQL 语句',
                         'required': True},
            },
            handler=self._tool_explain_query,
        ))

        self.register_tool(ToolSpec(
            name='profile_query',
            description='分析 SQL 性能（执行时间、扫描行数）',
            params={
                'sql': {'type': 'string',
                         'description': 'SQL 语句',
                         'required': True},
            },
            handler=self._tool_profile_query,
        ))

        self.register_tool(ToolSpec(
            name='get_table_stats',
            description='获取表统计信息（行数、大小）',
            params={
                'table': {'type': 'string',
                           'description': '表名',
                           'required': True},
            },
            handler=self._tool_table_stats,
        ))

        self.register_tool(ToolSpec(
            name='find_related_tables',
            description='查找表之间的血缘关系',
            params={
                'table': {'type': 'string',
                           'description': '表名',
                           'required': True},
            },
            handler=self._tool_related_tables,
        ))

        self.register_tool(ToolSpec(
            name='execute_write',
            description='执行写操作（INSERT/UPDATE/DELETE）— 需要确认',
            params={
                'sql': {'type': 'string',
                         'description': 'SQL 语句',
                         'required': True},
            },
            handler=self._tool_execute_write,
            dangerous=True,
        ))

        self.register_tool(ToolSpec(
            name='remember',
            description='记录重要信息到长期记忆',
            params={
                'content': {'type': 'string',
                             'description': '要记录的内容',
                             'required': True},
            },
            handler=self._tool_remember,
        ))

    def register_tool(self, tool: ToolSpec):
        self.tools[tool.name] = tool

    # ==================== 工具实现 ====================

    def _tool_list_tables(self, database: str = '') -> Dict:
        conn = self.conn_provider() if self.conn_provider else None
        if not conn:
            return {'error': '无连接'}
        try:
            if database:
                conn.switch_database(database)
            tables = conn.get_tables()
            return {'tables': tables, 'count': len(tables)}
        except Exception as e:
            return {'error': str(e)}

    def _tool_describe_table(self, table: str) -> Dict:
        conn = self.conn_provider() if self.conn_provider else None
        if not conn:
            return {'error': '无连接'}
        try:
            cols = conn.get_columns(table)
            return {
                'table': table,
                'columns': [
                    {'name': c['name'], 'type': c.get('type', ''),
                     'key': c.get('key', '')}
                    for c in cols
                ],
            }
        except Exception as e:
            return {'error': str(e)}

    def _tool_execute_query(self, sql: str, limit: int = 50) -> Dict:
        conn = self.conn_provider() if self.conn_provider else None
        if not conn:
            return {'error': '无连接'}

        # 安全检查
        if not sql.strip().upper().startswith(
                ('SELECT', 'SHOW', 'DESC', 'WITH', 'EXPLAIN')):
            return {'error': '仅支持 SELECT 类查询'}

        try:
            t0 = time.time()
            cols, rows = conn.query(sql)
            elapsed = (time.time() - t0) * 1000

            # 截断
            rows = rows[:limit]

            return {
                'columns': cols,
                'rows': [
                    [self._serialize(v) for v in r]
                    for r in rows
                ],
                'row_count': len(rows),
                'elapsed_ms': round(elapsed, 2),
            }
        except Exception as e:
            return {'error': str(e)}

    def _tool_explain_query(self, sql: str) -> Dict:
        conn = self.conn_provider() if self.conn_provider else None
        if not conn:
            return {'error': '无连接'}
        try:
            cols, rows = conn.query("EXPLAIN " + sql)
            return {
                'columns': cols,
                'plan': [
                    [self._serialize(v) for v in r] for r in rows
                ],
            }
        except Exception as e:
            return {'error': str(e)}

    def _tool_profile_query(self, sql: str) -> Dict:
        conn = self.conn_provider() if self.conn_provider else None
        if not conn:
            return {'error': '无连接'}
        try:
            from .sql_profiler import SQLProfiler
            profiler = SQLProfiler(conn)
            result = profiler.profile(sql, execute=True)
            return {
                'elapsed_ms': round(result.elapsed_ms, 2),
                'rows_returned': result.rows_returned,
                'rows_scanned': result.rows_scanned,
                'scan_type': result.scan_type,
                'index_used': result.index_used,
                'score': result.score,
                'warnings': result.warnings,
                'suggestions': result.suggestions,
            }
        except Exception as e:
            return {'error': str(e)}

    def _tool_table_stats(self, table: str) -> Dict:
        conn = self.conn_provider() if self.conn_provider else None
        if not conn:
            return {'error': '无连接'}
        try:
            _, r = conn.query(f"SELECT COUNT(*) FROM `{table}`")
            rows = r[0][0] if r else 0
            cols = conn.get_columns(table)
            return {
                'table': table,
                'row_count': rows,
                'column_count': len(cols),
            }
        except Exception as e:
            return {'error': str(e)}

    def _tool_related_tables(self, table: str) -> Dict:
        # 需要从主窗口获取 lineage
        import sys
        main_window = None
        for m in sys.modules.values():
            pass
        # 简化：从全局推荐引擎查找
        try:
            from .recommender import Recommender
            rec = Recommender()
            related = rec.related_tables(table, 10)
            return {'table': table, 'related': related}
        except Exception as e:
            return {'error': str(e)}

    def _tool_execute_write(self, sql: str) -> Dict:
        conn = self.conn_provider() if self.conn_provider else None
        if not conn:
            return {'error': '无连接'}

        # 危险操作，需要确认
        if self.confirm_callback:
            if not self.confirm_callback(
                    f"Agent 请求执行写操作：\n\n{sql}\n\n是否允许？"):
                return {'error': '用户拒绝执行'}

        try:
            rows, err = conn.execute(sql)
            if err:
                return {'error': err}
            return {'affected': rows}
        except Exception as e:
            return {'error': str(e)}

    def _tool_remember(self, content: str) -> Dict:
        self.memory.add('memory', content, 'note')
        return {'ok': True, 'saved': True}

    def _serialize(self, v):
        if v is None or isinstance(v, (int, float, str, bool)):
            return v
        if isinstance(v, bytes):
            return f"<binary {len(v)}B>"
        return str(v)

    # ==================== ReAct 循环 ====================

    def run(self, question: str,
            max_steps: int = 8,
            on_step: Callable = None,
            on_done: Callable = None,
            on_error: Callable = None) -> None:
        """
        执行 ReAct 循环
        on_step(step: AgentStep) 每步回调
        """
        if not self.client or not self.client.is_enabled():
            if on_error:
                on_error("AI 未配置")
            return

        session = AgentSession(question=question)

        import threading

        def _run():
            try:
                self._react_loop(session, max_steps, on_step)
                if on_done:
                    on_done(session)
            except Exception as e:
                session.status = 'failed'
                if on_error:
                    on_error(str(e))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def _react_loop(self, session: AgentSession, max_steps: int,
                    on_step: Callable):
        """ReAct 主循环（同步，在后台线程执行）"""
        # 1. 系统提示
        schema = None
        if self.schema_provider:
            try:
                tables, cols = self.schema_provider()
                schema = {'tables': tables, 'columns': cols}
            except Exception:
                schema = {}

        # 2. 检索记忆
        memory_hits = self.memory.search(session.question, 3)

        for step_idx in range(max_steps):
            # 3. 构建 prompt
            prompt = self._build_react_prompt(
                session, schema, memory_hits)

            # 4. 调用 LLM
            response = self._call_llm(prompt, schema)
            if not response:
                session.status = 'failed'
                return

            # 5. 解析响应
            parsed = self._parse_react_response(response)

            if parsed['type'] == 'thought':
                step = AgentStep(
                    step_type='thought',
                    content=parsed['content'],
                )
                session.steps.append(step)
                if on_step:
                    on_step(step)
                continue

            elif parsed['type'] == 'action':
                # 记录思考
                if parsed.get('thought'):
                    thought = AgentStep(
                        step_type='thought',
                        content=parsed['thought'],
                    )
                    session.steps.append(thought)
                    if on_step:
                        on_step(thought)

                # 执行工具
                action = AgentStep(
                    step_type='action',
                    tool=parsed['tool'],
                    tool_params=parsed.get('params', {}),
                    content=f"调用 {parsed['tool']}",
                )
                session.steps.append(action)
                if on_step:
                    on_step(action)

                # 执行
                result = self._execute_tool(
                    parsed['tool'], parsed.get('params', {}))

                session.tool_calls += 1

                # 观察
                obs = AgentStep(
                    step_type='observation',
                    content=json.dumps(result, ensure_ascii=False,
                                        default=str)[:2000],
                    tool_result=result,
                )
                session.steps.append(obs)
                if on_step:
                    on_step(obs)

            elif parsed['type'] == 'answer':
                session.final_answer = parsed['content']
                final_step = AgentStep(
                    step_type='answer',
                    content=parsed['content'],
                )
                session.steps.append(final_step)
                if on_step:
                    on_step(final_step)

                session.status = 'success'
                session.ended_at = time.time()

                # 记录到记忆
                self.memory.add(
                    session.question,
                    session.final_answer,
                    'answer')

                return

            else:
                # 无法解析，添加为 thought
                step = AgentStep(
                    step_type='thought',
                    content=response[:1000],
                )
                session.steps.append(step)
                if on_step:
                    on_step(step)

        # 超过最大步数
        session.status = 'failed'
        session.final_answer = "超过最大步数，未能得出结论"
        session.ended_at = time.time()

    def _build_react_prompt(self, session: AgentSession,
                            schema: Dict, memory_hits: List[Dict]) -> str:
        """构建 ReAct prompt"""
        lines = [
            "你是一个数据库 AI Agent。通过 ReAct（Reasoning + Acting）"
            "方式回答用户问题。",
            "",
            "## 可用工具",
        ]

        for tool in self.tools.values():
            params_desc = []
            for pname, pinfo in tool.params.items():
                desc = pinfo.get('description', '')
                required = pinfo.get('required', False)
                ptype = pinfo.get('type', 'string')
                marker = '*' if required else ''
                params_desc.append(f"{pname}{marker}({ptype}): {desc}")
            lines.append(f"- **{tool.name}**: {tool.description}")
            if params_desc:
                lines.append(f"  参数: {'; '.join(params_desc)}")

        lines.append("")
        lines.append("## 输出格式")
        lines.append("你需要严格按以下格式输出：")
        lines.append("")
        lines.append("**Thought**: [你的思考]")
        lines.append("")
        lines.append("**Action**: [工具名]")
        lines.append("**Parameters**:")
        lines.append("```json")
        lines.append('{"param1": "value1"}')
        lines.append("```")
        lines.append("")
        lines.append("或者当你准备好回答用户时：")
        lines.append("")
        lines.append("**Thought**: [最终思考]")
        lines.append("")
        lines.append("**Answer**: [最终答案]")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Schema
        if schema and schema.get('tables'):
            lines.append("## 数据库结构")
            for t in schema['tables'][:20]:
                cols = schema.get('columns', {}).get(t, [])
                lines.append(f"- {t}({', '.join(cols[:10])})")
            lines.append("")

        # 记忆
        if memory_hits:
            lines.append("## 相关记忆")
            for m in memory_hits:
                lines.append(f"Q: {m.get('question', '')}")
                lines.append(f"A: {m.get('answer', '')[:200]}")
                lines.append("")

        # 问题
        lines.append(f"## 用户问题")
        lines.append(session.question)
        lines.append("")

        # 历史步骤
        if session.steps:
            lines.append("## 已完成步骤")
            for s in session.steps[-6:]:   # 保留最近 6 步
                if s.step_type == 'thought':
                    lines.append(f"**Thought**: {s.content[:300]}")
                elif s.step_type == 'action':
                    lines.append(f"**Action**: {s.tool}")
                    lines.append(f"**Parameters**: "
                                  f"{json.dumps(s.tool_params, ensure_ascii=False)[:200]}")
                elif s.step_type == 'observation':
                    lines.append(f"**Observation**: {s.content[:500]}")
            lines.append("")
            lines.append("继续下一步。")

        return '\n'.join(lines)

    def _call_llm(self, prompt: str, schema: Dict) -> str:
        """同步调用 LLM"""
        result = [None]
        error = [None]
        event = __import__('threading').Event()

        def _on_done(r):
            result[0] = r
            event.set()

        def _on_error(e):
            error[0] = e
            event.set()

        self.client.chat(
            [{'role': 'user', 'content': prompt}],
            schema, _on_done, _on_error)

        event.wait(timeout=60)
        if error[0]:
            raise RuntimeError(error[0])
        return result[0] or ""

    def _parse_react_response(self, text: str) -> Dict:
        """解析 LLM 输出"""
        text = text.strip()

        # 提取 Thought
        thought_m = re.search(
            r'\*\*Thought\*\*[:：]\s*(.+?)(?=\*\*(?:Action|Answer)\*\*|$)',
            text, re.DOTALL | re.IGNORECASE)
        thought = thought_m.group(1).strip() if thought_m else ''

        # 检测 Answer
        answer_m = re.search(
            r'\*\*Answer\*\*[:：]\s*(.+)$',
            text, re.DOTALL | re.IGNORECASE)
        if answer_m:
            return {
                'type': 'answer',
                'content': answer_m.group(1).strip(),
                'thought': thought,
            }

        # 检测 Action
        action_m = re.search(
            r'\*\*Action\*\*[:：]\s*(\w+)',
            text, re.IGNORECASE)
        if action_m:
            tool = action_m.group(1).strip()

            # 解析参数
            params = {}
            # 从 JSON 块
            json_m = re.search(
                r'```(?:json)?\s*(\{.+?\})\s*```',
                text, re.DOTALL)
            if json_m:
                try:
                    params = json.loads(json_m.group(1))
                except json.JSONDecodeError:
                    pass
            else:
                # 从 Parameters: 后
                p_m = re.search(
                    r'\*\*Parameters\*\*[:：]\s*(.+?)(?=\*\*|$)',
                    text, re.DOTALL | re.IGNORECASE)
                if p_m:
                    try:
                        params = json.loads(p_m.group(1).strip())
                    except json.JSONDecodeError:
                        pass

            return {
                'type': 'action',
                'tool': tool,
                'params': params,
                'thought': thought,
            }

        # 只有 Thought
        return {'type': 'thought', 'content': text}

    def _execute_tool(self, tool_name: str,
                      params: Dict) -> Any:
        """执行工具"""
        tool = self.tools.get(tool_name)
        if not tool:
            return {'error': f"未知工具: {tool_name}"}
        if not tool.handler:
            return {'error': f"工具 {tool_name} 无处理器"}

        try:
            # 参数校验
            required = [k for k, v in tool.params.items()
                        if v.get('required')]
            missing = [k for k in required if k not in params]
            if missing:
                return {'error': f"缺少参数: {missing}"}

            return tool.handler(**params)
        except TypeError as e:
            return {'error': f"参数错误: {e}"}
        except Exception as e:
            return {'error': str(e)}

    def format_session(self, session: AgentSession) -> str:
        """格式化为文本"""
        lines = [f"# 问题: {session.question}", ""]
        for step in session.steps:
            if step.step_type == 'thought':
                lines.append(f"💭 {step.content}")
            elif step.step_type == 'action':
                lines.append(f"🔧 调用: {step.tool}")
                if step.tool_params:
                    lines.append(f"   参数: {json.dumps(step.tool_params, ensure_ascii=False)}")
            elif step.step_type == 'observation':
                lines.append(f"👁 观察: {step.content[:500]}")
            elif step.step_type == 'answer':
                lines.append(f"✅ 答案:\n{step.content}")
            lines.append("")

        return '\n'.join(lines)

    def stats(self) -> dict:
        return {
            'total_tools': len(self.tools),
            'dangerous_tools': sum(
                1 for t in self.tools.values() if t.dangerous),
            'total_memories': len(self.memory.memories),
        }