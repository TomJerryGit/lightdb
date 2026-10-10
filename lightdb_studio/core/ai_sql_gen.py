"""
AI SQL 生成器 2.0
- Few-shot 学习用户习惯
- 多轮迭代修正
- 自动生成测试数据
- 反馈学习
"""
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Callable, Optional

from .ai_integration import AIClient, AIConfig

LEARNING_FILE = Path.home() / '.lightdb_studio' / 'ai_learning.json'


@dataclass
class Example:
    """Few-shot 示例"""
    question: str
    sql: str
    feedback: int = 0       # -1 差 / 0 中性 / 1 好
    used_count: int = 0
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class Generation:
    """生成记录"""
    question: str
    sql: str = ""
    explanation: str = ""
    schema: Dict = field(default_factory=dict)
    examples_used: List[str] = field(default_factory=list)
    iterations: int = 0
    final_score: int = 0
    feedback: int = 0
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


class AISQLGenerator:
    """AI SQL 生成器"""

    def __init__(self, ai_config: AIConfig = None, schema_provider=None):
        self.client = AIClient(ai_config) if ai_config else None
        self.schema_provider = schema_provider
        self.file = LEARNING_FILE
        self.examples: List[Example] = []
        self.history: List[Generation] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def set_ai_config(self, config: AIConfig):
        self.client = AIClient(config)

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.examples = [Example(**e)
                             for e in data.get('examples', [])]
            self.history = [Generation(**g)
                            for g in data.get('history', [])][-200:]
        except Exception:
            pass

    def save(self):
        data = {
            'examples': [asdict(e) for e in self.examples],
            'history': [asdict(g) for g in self.history[-200:]],
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 示例管理 ====================

    def add_example(self, question: str, sql: str, feedback: int = 1):
        """添加学习示例"""
        # 去重
        for e in self.examples:
            if e.question.strip() == question.strip():
                e.sql = sql
                e.feedback = feedback
                self.save()
                return
        self.examples.append(Example(
            question=question.strip(),
            sql=sql.strip(),
            feedback=feedback,
        ))
        self.save()

    def rate_last(self, feedback: int):
        """评价最近一次生成"""
        if not self.history:
            return
        last = self.history[-1]
        last.feedback = feedback
        # 好/坏都加入学习
        if feedback != 0:
            self.add_example(last.question, last.sql, feedback)
        self.save()

    def find_similar_examples(self, question: str,
                              top_n: int = 3) -> List[Example]:
        """找相似示例（基于关键词）"""
        # 中文分词简化：按字 + 关键词
        def tokens(s):
            s = s.lower()
            # 提取中文单字 + 英文单词（按字拆分，避免整段中文与其他问题无法匹配）
            cn = re.findall(r'[\u4e00-\u9fff]', s)
            en = re.findall(r'[a-z_]+', s)
            return set(cn + en)

        q_tokens = tokens(question)
        scored = []
        for e in self.examples:
            if e.feedback < 0:
                continue
            e_tokens = tokens(e.question)
            if not e_tokens:
                continue
            # Jaccard 相似度
            inter = q_tokens & e_tokens
            union = q_tokens | e_tokens
            score = len(inter) / len(union) if union else 0
            # 好评加权
            if e.feedback > 0:
                score *= 1.2
            scored.append((score, e))

        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[:top_n] if _ > 0.05]

    # ==================== 生成 ====================

    def generate(self, question: str,
                 schema: Dict = None,
                 max_iterations: int = 3,
                 on_progress: Callable = None,
                 on_done: Callable = None,
                 on_error: Callable = None):
        """
        多轮迭代生成 SQL
        on_progress(iter, message) 每轮更新
        """
        if not self.client or not self.client.is_enabled():
            if on_error:
                on_error("AI 未配置")
            return

        if schema is None and self.schema_provider:
            try:
                tables, cols = self.schema_provider()
                schema = {'tables': tables, 'columns': cols}
            except Exception:
                schema = {}

        # 找相似示例
        similar = self.find_similar_examples(question, 3)

        gen = Generation(
            question=question,
            schema=schema or {},
            examples_used=[e.question for e in similar],
        )

        self._iterate_generate(
            gen, schema or {}, similar, 1, max_iterations,
            on_progress, on_done, on_error)

    def _iterate_generate(self, gen: Generation, schema: Dict,
                          examples: List[Example], iteration: int,
                          max_iter: int,
                          on_progress, on_done, on_error):
        """递归迭代"""
        if on_progress:
            on_progress(iteration, f"第 {iteration} 轮生成中...")

        prompt = self._build_prompt(gen.question, schema, examples,
                                     gen.sql, iteration)

        messages = [{'role': 'user', 'content': prompt}]

        def _on_done(response):
            sql = self._extract_sql(response)
            explanation = self._extract_explanation(response)

            gen.sql = sql
            gen.explanation = explanation
            gen.iterations = iteration

            # 自我批评
            if iteration < max_iter and self._should_refine(sql):
                if on_progress:
                    on_progress(iteration,
                                "发现问题，正在优化...")
                self._iterate_generate(
                    gen, schema, examples, iteration + 1, max_iter,
                    on_progress, on_done, on_error)
                return

            # 完成
            self.history.append(gen)
            self.save()

            if on_done:
                on_done({
                    'sql': sql,
                    'explanation': explanation,
                    'iterations': iteration,
                })

        self.client.chat(messages, schema, _on_done, on_error)

    def _build_prompt(self, question: str, schema: Dict,
                      examples: List[Example], prev_sql: str,
                      iteration: int) -> str:
        parts = []

        # 系统提示
        parts.append("你是 SQL 专家。请根据用户需求生成准确、高效的 SQL 语句。")
        parts.append("")

        # Schema
        if schema.get('tables'):
            parts.append("## 数据库结构")
            for t in schema['tables'][:20]:
                cols = schema.get('columns', {}).get(t, [])
                parts.append(f"- {t}({', '.join(cols[:15])})")
            parts.append("")

        # Few-shot 示例
        if examples:
            parts.append("## 参考示例（学自历史）")
            for e in examples:
                parts.append(f"Q: {e.question}")
                parts.append(f"SQL: {e.sql}")
                parts.append("")
            parts.append("")

        # 用户需求
        if iteration == 1:
            parts.append(f"## 需求\n{question}")
            parts.append("")
            parts.append("## 输出格式")
            parts.append("```sql")
            parts.append("-- 你的 SQL 语句")
            parts.append("```")
            parts.append("")
            parts.append("**说明**: 简要说明你的思路")
        else:
            parts.append(f"## 原始需求\n{question}")
            parts.append("")
            parts.append(f"## 上一版 SQL（需要优化）")
            parts.append("```sql")
            parts.append(prev_sql)
            parts.append("```")
            parts.append("")
            parts.append("## 请优化这份 SQL，修正潜在问题：")
            parts.append("- 性能（索引利用、扫描量）")
            parts.append("- 正确性（NULL 处理、边界）")
            parts.append("- 可读性")
            parts.append("")
            parts.append("输出改进后的 SQL。")

        return '\n'.join(parts)

    def _extract_sql(self, text: str) -> str:
        """从响应中提取 SQL"""
        m = re.search(r'```(?:sql)?\s*(.+?)```',
                      text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
        # 找 SQL 起始
        lines = []
        started = False
        for line in text.split('\n'):
            if not started and re.match(
                    r'\s*(SELECT|INSERT|UPDATE|DELETE|WITH|CREATE)',
                    line, re.IGNORECASE):
                started = True
            if started:
                lines.append(line)
        return '\n'.join(lines).strip() or text.strip()

    def _extract_explanation(self, text: str) -> str:
        """提取说明"""
        m = re.search(r'\*\*说明\*\*[:：]\s*(.+)',
                      text, re.DOTALL)
        if m:
            return m.group(1).strip()[:500]
        # 提取 SQL 之后的文本
        parts = re.split(r'```.*?```', text, flags=re.DOTALL)
        if len(parts) > 1:
            tail = parts[-1].strip()
            if tail:
                return tail[:500]
        return ""

    def _should_refine(self, sql: str) -> bool:
        """判断是否需要继续优化"""
        if not sql:
            return True
        up = sql.upper()
        # 明显问题
        if re.search(r'SELECT\s+\*', up) and 'COUNT' not in up:
            return True
        if 'WHERE' not in up and ('UPDATE' in up or 'DELETE' in up):
            return True
        if 'LIMIT' not in up and up.startswith('SELECT') \
                and 'GROUP BY' not in up:
            # 可能未加 LIMIT，但这是常见情况，不强制
            pass
        return False

    # ==================== 测试数据生成 ====================

    def generate_test_data(self, table: str, columns: List[Dict],
                           count: int = 10,
                           on_done=None, on_error=None):
        """生成测试数据 INSERT 语句"""
        if not self.client or not self.client.is_enabled():
            if on_error:
                on_error("AI 未配置")
            return

        col_defs = []
        for c in columns:
            col_defs.append(f"{c['name']} {c.get('type', '')}")

        prompt = f"""为表 {table} 生成 {count} 条测试数据。
列定义: {', '.join(col_defs)}

要求:
1. 数据看起来真实、有意义（不是 foo/bar）
2. 主键列使用递增数字
3. 符合各列类型
4. 只输出 INSERT 语句，用 ```sql 包裹
"""

        messages = [{'role': 'user', 'content': prompt}]

        def _on_done(response):
            sql = self._extract_sql(response)
            if on_done:
                on_done(sql)

        self.client.chat(messages, None, _on_done, on_error)

    # ==================== 统计 ====================

    def stats(self) -> dict:
        good = sum(1 for e in self.examples if e.feedback > 0)
        bad = sum(1 for e in self.examples if e.feedback < 0)
        return {
            'total_examples': len(self.examples),
            'good_examples': good,
            'bad_examples': bad,
            'total_generations': len(self.history),
            'avg_iterations': (
                sum(g.iterations for g in self.history) /
                len(self.history) if self.history else 0
            ),
        }