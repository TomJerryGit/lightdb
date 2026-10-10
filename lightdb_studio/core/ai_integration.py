"""
AI 集成 - 对接 OpenAI / Claude / 本地 Ollama
"""
import json
import os
import urllib.request
import urllib.error
import threading
from dataclasses import dataclass
from typing import List, Dict, Callable, Optional

SETTINGS_KEY = 'lightdb_studio/ai_settings.json'


@dataclass
class AIConfig:
    provider: str = "openai"       # openai / claude / ollama / disabled
    api_key: str = ""
    base_url: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: int = 2000
    timeout: int = 30


class AIClient:
    """LLM 客户端（同步调用 + 异步回调）"""

    def __init__(self, config: AIConfig = None):
        self.config = config or AIConfig()
        self._cancel = False

    def is_enabled(self) -> bool:
        return (self.config.provider != 'disabled'
                and (self.config.provider == 'ollama'
                     or bool(self.config.api_key)))

    # ==================== Prompt ====================

    def _build_system_prompt(self, schema: Dict) -> str:
        tables = schema.get('tables', [])
        cols = schema.get('columns', {})

        lines = ["你是 LightDB Studio 的 SQL 助手。",
                 "请根据用户的问题生成或解释 SQL。",
                 "使用 MySQL 兼容语法。",
                 "只回答 SQL 或解释，不要闲聊。", ""]
        if tables:
            lines.append("## 当前数据库结构")
            for t in tables[:30]:
                cs = cols.get(t, [])
                lines.append(f"- {t}: {', '.join(cs[:20])}")
        return '\n'.join(lines)

    # ==================== 调用 ====================

    def chat(self, messages: List[Dict],
             schema: Dict = None,
             on_done: Callable = None,
             on_error: Callable = None,
             on_chunk: Callable = None) -> threading.Thread:
        """异步对话"""

        def _run():
            try:
                response = self._chat_sync(messages, schema)
                if on_done:
                    on_done(response)
            except Exception as e:
                if on_error:
                    on_error(str(e))

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    def _chat_sync(self, messages: List[Dict], schema: Dict = None) -> str:
        provider = self.config.provider
        if provider == 'openai':
            return self._call_openai(messages, schema)
        if provider == 'claude':
            return self._call_claude(messages, schema)
        if provider == 'ollama':
            return self._call_ollama(messages, schema)
        raise RuntimeError(f"未知 provider: {provider}")

    def _call_openai(self, messages, schema):
        base = self.config.base_url or 'https://api.openai.com/v1'
        url = base.rstrip('/') + '/chat/completions'

        sys_prompt = self._build_system_prompt(schema) if schema else None
        msgs = []
        if sys_prompt:
            msgs.append({'role': 'system', 'content': sys_prompt})
        msgs.extend(messages)

        payload = {
            'model': self.config.model,
            'messages': msgs,
            'temperature': self.config.temperature,
            'max_tokens': self.config.max_tokens,
        }
        data = json.dumps(payload).encode('utf-8')

        req = urllib.request.Request(
            url, data=data, method='POST',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.config.api_key}',
            })
        try:
            with urllib.request.urlopen(
                    req, timeout=self.config.timeout) as resp:
                result = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f"OpenAI API 错误 {e.code}: {body[:200]}")

        try:
            return result['choices'][0]['message']['content'].strip()
        except (KeyError, IndexError):
            raise RuntimeError(f"响应格式异常: {str(result)[:200]}")

    def _call_claude(self, messages, schema):
        base = self.config.base_url or 'https://api.anthropic.com'
        url = base.rstrip('/') + '/v1/messages'

        sys_prompt = self._build_system_prompt(schema) if schema else ''

        # Claude 格式: messages 只含 user/assistant
        claude_msgs = []
        for m in messages:
            if m['role'] in ('user', 'assistant'):
                claude_msgs.append(m)

        payload = {
            'model': self.config.model or 'claude-3-haiku-20240307',
            'max_tokens': self.config.max_tokens,
            'temperature': self.config.temperature,
            'system': sys_prompt,
            'messages': claude_msgs,
        }
        data = json.dumps(payload).encode('utf-8')

        req = urllib.request.Request(
            url, data=data, method='POST',
            headers={
                'Content-Type': 'application/json',
                'x-api-key': self.config.api_key,
                'anthropic-version': '2023-06-01',
            })
        try:
            with urllib.request.urlopen(
                    req, timeout=self.config.timeout) as resp:
                result = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f"Claude API 错误 {e.code}: {body[:200]}")

        try:
            parts = result.get('content', [])
            texts = [p['text'] for p in parts if p.get('type') == 'text']
            return '\n'.join(texts).strip()
        except (KeyError, TypeError):
            raise RuntimeError(f"响应格式异常: {str(result)[:200]}")

    def _call_ollama(self, messages, schema):
        base = self.config.base_url or 'http://localhost:11434'
        url = base.rstrip('/') + '/api/chat'

        sys_prompt = self._build_system_prompt(schema) if schema else None
        msgs = []
        if sys_prompt:
            msgs.append({'role': 'system', 'content': sys_prompt})
        msgs.extend(messages)

        payload = {
            'model': self.config.model or 'llama3',
            'messages': msgs,
            'stream': False,
            'options': {
                'temperature': self.config.temperature,
            },
        }
        data = json.dumps(payload).encode('utf-8')

        req = urllib.request.Request(
            url, data=data, method='POST',
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(
                    req, timeout=self.config.timeout) as resp:
                result = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f"Ollama 错误 {e.code}: {body[:200]}")

        return result.get('message', {}).get('content', '').strip()

    # ==================== 高级功能 ====================

    def explain_sql(self, sql: str, on_done=None, on_error=None):
        """解释 SQL"""
        messages = [{
            'role': 'user',
            'content': f"请用中文解释这条 SQL 的作用和执行流程，"
                       f"列出涉及的表和关键条件：\n\n{sql}"
        }]
        return self.chat(messages, None, on_done, on_error)

    def optimize_sql(self, sql: str, schema: Dict = None,
                     on_done=None, on_error=None):
        """优化 SQL"""
        messages = [{
            'role': 'user',
            'content': f"分析这条 SQL 的性能问题，"
                       f"给出优化建议和改写后的 SQL：\n\n{sql}"
        }]
        return self.chat(messages, schema, on_done, on_error)

    def nl2sql(self, text: str, schema: Dict,
               on_done=None, on_error=None):
        """自然语言转 SQL"""
        messages = [{
            'role': 'user',
            'content': f"根据以下需求生成 SQL：\n\n{text}\n\n"
                       f"只返回 SQL 语句，不要解释。"
        }]
        return self.chat(messages, schema, on_done, on_error)

    def recommend_index(self, sql: str, schema: Dict = None,
                        on_done=None, on_error=None):
        """推荐索引"""
        messages = [{
            'role': 'user',
            'content': f"分析这条 SQL，推荐应该创建的索引（如果有）：\n\n"
                       f"{sql}\n\n"
                       f"输出格式：\n"
                       f"- 索引名\n- 列\n- 原因\n- CREATE INDEX 语句"
        }]
        return self.chat(messages, schema, on_done, on_error)


# ==================== 配置持久化 ====================

class AIConfigStore:
    def __init__(self):
        from pathlib import Path
        self.file = Path.home() / '.lightdb_studio' / 'ai_config.json'

    def load(self) -> AIConfig:
        if not self.file.exists():
            return AIConfig()
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return AIConfig(**data)
        except Exception:
            return AIConfig()

    def save(self, config: AIConfig):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file, 'w', encoding='utf-8') as f:
            json.dump(vars(config), f, ensure_ascii=False, indent=2)