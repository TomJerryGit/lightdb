# -*- coding: utf-8 -*-
"""AI SQL 助手（task-43）：自然语言 → CREATE TABLE / CREATE PROCEDURE。

边界（与产品要求一致）：
- 只做**生成 + 预览 + 语法校验**；写库由 UI 二次确认后走现有执行入口；
- AI 未配置/请求失败 → 显式异常，绝不返回假 SQL；
- 所有 SQL 用引擎自己的 `lightdb.sql.parser.Parser` 校验，非法显式返回；
- 存储过程以本引擎真实能力为准：CALL **不返回结果集**，过程体含 SELECT
  结果集会被标为引擎不兼容，不提供直接执行（不生成“看起来能跑”的假 SQL）；
- 纯逻辑无 Qt 依赖，可单测；UI 在 `ui/ai_sql_dialog.py`。
"""
import json
import re
from dataclasses import dataclass, field

from lightdb.sql.parser import Parser

from .ai_integration import AIClient, AIConfigStore

_PROMPT_TABLE = """你是 LightDB（MySQL 兼容）SQL 生成器。
请根据用户自然语言生成一条可执行的 CREATE TABLE 语句。
要求：
- 只使用 MySQL 兼容语法；标识符用反引号；
- 主键/自增/NOT NULL/UNIQUE/DEFAULT 等按描述落实；
- 不要生成 DROP/ALTER 等危险语句；
- 输出 JSON：{{"sql": "...", "explanation": "一句话说明"}}，不要输出其它内容。
当前库结构（避免重名，可参考已有列类型）：
{schema}
"""

_PROMPT_PROCEDURE = """你是 LightDB（MySQL 兼容）存储过程生成器。
本引擎存储过程支持范围（真实探测）：
{capability}
请根据用户自然语言生成一条 CREATE PROCEDURE 语句。
硬性要求：
- 过程体只能使用上面标注为支持的语法；
- 本引擎 CALL 不返回结果集：过程体内**不得包含会产生结果集的 SELECT**；
- 输出 JSON：{{"sql": "...", "explanation": "一句话说明"}}。
当前库结构：
{schema}
"""


class AISQLUnavailableError(RuntimeError):
    """AI 未配置或不可用（显式失败，不伪造生成结果）。"""


class AISQLGenerationError(RuntimeError):
    """AI 请求/响应失败。"""


class AISQLParseError(ValueError):
    """AI 响应中无法提取 SQL。"""


@dataclass
class SQLGenerationResult:
    sql: str = ''
    explanation: str = ''
    raw: str = ''
    format: str = ''
    valid: bool = False
    error: str = ''
    engine_compatible: bool = True
    engine_issues: list = field(default_factory=list)
    capability: dict = field(default_factory=dict)
    schema_used: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            'sql': self.sql, 'explanation': self.explanation,
            'raw': self.raw, 'format': self.format, 'valid': self.valid,
            'error': self.error,
            'engine_compatible': self.engine_compatible,
            'engine_issues': list(self.engine_issues),
            'capability': dict(self.capability),
        }


# ==================== 引擎能力探测 ====================

PROCEDURE_CAPABILITY = {
    'create_procedure': True,
    'call': True,
    'in_params': True,
    'out_inout_params': True,
    'declare_variables': True,
    'if': True,
    'while': True,
    'nested_call': True,
    'set_variables': True,
    'call_returns_result_set': False,
    'notes': [
        '支持 CREATE PROCEDURE name(IN/OUT/INOUT 参数) BEGIN ... END',
        '支持 DECLARE / SET / IF / WHILE / 嵌套 CALL',
        'CALL 不返回多结果集：过程体内禁止产生结果集的 SELECT',
        '不支持的内容会由引擎显式拒绝（本助手不会生成并宣称可执行）',
    ],
}


def probe_procedure_capabilities(conn=None):
    """探测/返回本引擎存储过程支持范围（parser 实测 + 已知执行器能力）。"""
    capability = dict(PROCEDURE_CAPABILITY)
    capability['notes'] = list(PROCEDURE_CAPABILITY['notes'])
    sample = ('CREATE PROCEDURE __probe_proc(IN n INT) BEGIN '
              'DECLARE v INT; SET v = n + 1; END')
    try:
        Parser().parse(sample)
        capability['parser_accepts_sample'] = True
    except Exception as exc:                          # noqa: BLE001
        capability['parser_accepts_sample'] = False
        capability['parser_error'] = str(exc)
    capability['probed_with_connection'] = bool(conn is not None)
    return capability


_RESULT_SELECT_RE = re.compile(
    r'(?is)\bSELECT\b(?![\s\S]{0,40}?\bINTO\b)')


def procedure_compatibility(sql):
    """检查过程体是否属于本引擎可执行子集；返回 issues 列表。"""
    issues = []
    text = str(sql or '')
    m = re.search(r'(?is)\bBEGIN\b(.*)\bEND\b', text)
    body = m.group(1) if m else text
    if re.search(r'(?i)\bSELECT\b', body):
        issues.append('过程体包含 SELECT：本引擎 CALL 不返回多结果集，'
                      '执行器会拒绝；请改用 SET/@变量或不生成结果集')
    for forbidden in ('CREATE FUNCTION', 'CREATE TRIGGER', 'CREATE VIEW'):
        if forbidden.lower() in body.lower():
            issues.append(f'过程体不支持嵌套 {forbidden}')
    return issues


# ==================== 响应解析 / 语法校验 ====================

_FENCE_RE = re.compile(r'```(?:sql|mysql)?\s*(.*?)```', re.IGNORECASE | re.DOTALL)
_SQL_START_RE = re.compile(
    r'(?is)(CREATE\s+(?:TABLE|PROCEDURE)|SELECT|WITH|INSERT|UPDATE|DELETE)')


def parse_ai_sql_response(text):
    """从 AI 返回中提取 SQL：JSON / ```sql 代码块 / 纯 SQL。"""
    raw = str(text or '').strip()
    if not raw:
        raise AISQLParseError('AI 返回为空，无法提取 SQL')
    explanation = ''
    # 1) JSON（可能被包在代码块/说明里）
    candidates = [raw]
    for fence in _FENCE_RE.findall(raw):
        candidates.append(fence.strip())
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get('sql'), str):
            sql = data['sql'].strip()
            explanation = str(data.get('explanation') or '')
            if not sql:
                raise AISQLParseError('AI JSON 的 sql 字段为空')
            return {'sql': sql, 'explanation': explanation, 'raw': raw,
                    'format': 'json'}
    # 2) 代码块
    fences = _FENCE_RE.findall(raw)
    if fences:
        for fence in fences:
            sql = fence.strip()
            if _SQL_START_RE.search(sql):
                return {'sql': sql, 'explanation': explanation,
                        'raw': raw, 'format': 'code_block'}
        raise AISQLParseError('代码块中没有 SQL 语句')
    # 3) 纯 SQL（允许前后少量说明，从 CREATE/SELECT 开始截到末尾）
    matches = list(_SQL_START_RE.finditer(raw))
    if not matches:
        raise AISQLParseError('AI 返回中没有可识别的 SQL')
    start = matches[0].start()
    sql = raw[start:].strip()
    # 去掉结尾的 markdown/说明行（只保留到最后一个分号或语句末尾）
    if ';' in sql:
        sql = sql[:sql.rfind(';') + 1]
    return {'sql': sql, 'explanation': explanation, 'raw': raw,
            'format': 'plain_sql'}


def validate_sql(sql):
    """用引擎 parser 校验；返回 {'ok','error','parsed'}。"""
    text = str(sql or '').strip()
    if not text:
        return {'ok': False, 'error': 'SQL 为空', 'parsed': None}
    try:
        parsed = Parser().parse(text)
        return {'ok': True, 'error': '', 'parsed': parsed}
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                'parsed': None}


# ==================== 上下文 / 助手 ====================

def build_schema_context(tables=None, columns=None, limit=30):
    tables = list(tables or [])
    columns = dict(columns or {})
    lines = []
    for table in tables[:limit]:
        cols = columns.get(table) or []
        cols_text = ', '.join(str(c) for c in cols[:20])
        lines.append(f'- {table}: {cols_text}')
    return '\n'.join(lines) or '（当前库没有可用表结构）'


class AISQLAssistant:
    """AI SQL 生成器（同步、无 Qt）；调用方负责预览/确认/执行。"""

    def __init__(self, client=None, config=None, config_store=None):
        self.store = config_store or AIConfigStore()
        self.config = config if config is not None else self.store.load()
        self.client = client if client is not None else AIClient(self.config)
        self.last_prompt = ''

    def is_configured(self):
        client = self.client
        if client is None:
            return False
        try:
            return bool(client.is_enabled())
        except Exception:
            return False

    def _chat(self, messages, schema):
        if not self.is_configured():
            raise AISQLUnavailableError(
                'AI 服务未配置：请在设置中配置 AI provider/api_key（或本地 '
                'Ollama），示例库仍可离线使用')
        try:
            if hasattr(self.client, '_chat_sync'):
                return self.client._chat_sync(messages, schema=schema)
            return self.client.chat(messages, schema=schema)
        except AISQLUnavailableError:
            raise
        except Exception as exc:                      # noqa: BLE001
            raise AISQLGenerationError(f'AI 请求失败: {exc}') from exc

    def _result_from_response(self, kind, response, capability=None,
                              schema_used=None):
        parsed = parse_ai_sql_response(response)
        validation = validate_sql(parsed['sql'])
        result = SQLGenerationResult(
            sql=parsed['sql'], explanation=parsed['explanation'],
            raw=parsed['raw'], format=parsed['format'],
            valid=validation['ok'], error=validation['error'],
            capability=dict(capability or {}),
            schema_used=dict(schema_used or {}))
        if kind == 'procedure':
            result.engine_issues = procedure_compatibility(result.sql)
            result.engine_compatible = not result.engine_issues
        return result

    def generate_table(self, description, schema_tables=None,
                       schema_columns=None):
        schema = {'tables': list(schema_tables or []),
                  'columns': dict(schema_columns or {})}
        context = build_schema_context(schema['tables'], schema['columns'])
        self.last_prompt = _PROMPT_TABLE.format(schema=context)
        messages = [{'role': 'user',
                     'content': f'需求：{description}\n\n请生成 CREATE TABLE。'}]
        response = self._chat(messages, schema)
        return self._result_from_response('table', response,
                                          schema_used=schema)

    def generate_procedure(self, description, schema_tables=None,
                           schema_columns=None, conn=None):
        schema = {'tables': list(schema_tables or []),
                  'columns': dict(schema_columns or {})}
        capability = probe_procedure_capabilities(conn=conn)
        context = build_schema_context(schema['tables'], schema['columns'])
        capability_text = '\n'.join('- ' + n for n in capability['notes'])
        self.last_prompt = _PROMPT_PROCEDURE.format(
            capability=capability_text, schema=context)
        messages = [{'role': 'user',
                     'content': f'需求：{description}\n\n请生成 CREATE PROCEDURE。'}]
        response = self._chat(messages, schema)
        return self._result_from_response('procedure', response,
                                          capability=capability,
                                          schema_used=schema)
