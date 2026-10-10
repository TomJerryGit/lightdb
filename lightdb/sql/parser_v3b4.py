"""
v3.0 Batch 4 解析器扩展
- SET @var = expr
- SET @@var = value
- SELECT @var
- 支持 MySQL 的 INTERVAL 语法
"""
import re
from typing import Optional, Dict


class ParserV3B4:
    """扩展解析器"""

    @staticmethod
    def parse_set(sql: str) -> Optional[Dict]:
        """
        解析 SET 语句
        - SET @var = expr
        - SET @@session.var = value
        - SET @@var = value
        - SET NAMES charset
        - SET SESSION var = value
        """
        s = sql.strip()
        upper = s.upper()

        # SET NAMES
        m = re.match(r'SET\s+NAMES\s+(\w+)', s, re.IGNORECASE)
        if m:
            return {
                'command': 'SET NAMES',
                'charset': m.group(1),
            }

        # SET @@session.var = value
        m = re.match(
            r'SET\s+(?:@@(?:session|global|local)\.)?(\w+)\s*=\s*(.+)$',
            s, re.IGNORECASE | re.DOTALL)
        if m:
            var = m.group(1)
            val = m.group(2).strip().strip("'\"")
            return {
                'command': 'SET SYSTEM VAR',
                'name': var,
                'value': val,
            }

        # SET @user_var = expr
        m = re.match(r'SET\s+@(\w+)\s*=\s*(.+)$',
                      s, re.IGNORECASE | re.DOTALL)
        if m:
            return {
                'command': 'SET USER VAR',
                'name': '@' + m.group(1),
                'value': m.group(2).strip(),
            }

        # SET TRANSACTION ISOLATION LEVEL
        m = re.search(
            r'ISOLATION\s+LEVEL\s+'
            r'(READ\s+COMMITTED|REPEATABLE\s+READ|SERIALIZABLE|READ\s+UNCOMMITTED)',
            s, re.IGNORECASE)
        if m:
            return {
                'command': 'SET ISOLATION',
                'level': m.group(1).upper().replace('  ', ' '),
            }

        return None

    @staticmethod
    def parse_select_user_var(sql: str) -> Optional[Dict]:
        """解析 SELECT @var, @@var"""
        m = re.match(
            r'SELECT\s+(@@?\w+(?:\.\w+)?(?:\s*,\s*@@?\w+(?:\.\w+)?)*)\s*$',
            sql, re.IGNORECASE)
        if m:
            vars = [v.strip() for v in m.group(1).split(',')]
            return {
                'command': 'SELECT VARIABLES',
                'variables': vars,
            }
        return None

    @staticmethod
    def parse_interval(expr: str) -> Optional[tuple]:
        """
        解析 INTERVAL 表达式
        "INTERVAL 1 DAY" → (1, 'DAY')
        """
        m = re.match(
            r'INTERVAL\s+(\d+)\s+(\w+)', expr.strip(), re.IGNORECASE)
        if m:
            return (int(m.group(1)), m.group(2).upper())
        return None


# ==================== 集成执行器 ====================

class VariableExecutor:
    """变量执行器"""

    def __init__(self, vm=None):
        from .variables import get_vm
        self.vm = vm or get_vm()

    def execute_set_system_var(self, parsed, session_id):
        session = self.vm.get_session(session_id)
        name = parsed['name']
        if session.is_readonly(name):
            raise ValueError(f"变量 {name} 为只读")
        session.set_session(name, self._coerce(parsed['value']))
        return {'type': 'ok', 'affected': 0,
                'message': f"SET {name}"}

    def execute_set_user_var(self, parsed, session_id):
        session = self.vm.get_session(session_id)
        session.set_user(parsed['name'], self._coerce(parsed['value']))
        return {'type': 'ok', 'affected': 0,
                'message': f"SET {parsed['name']}"}

    def execute_select_variables(self, parsed, session_id):
        session = self.vm.get_session(session_id)
        cols = []
        values = []
        for v in parsed['variables']:
            cols.append(v)
            if v.startswith('@@'):
                # 会话变量
                name = v[2:]
                # 支持 @@session.var
                if '.' in name:
                    _, name = name.split('.', 1)
                value = session.get_session(name)
                if value is None:
                    value = session.get_special(name)
                values.append(value)
            elif v.startswith('@'):
                name = v[1:]
                values.append(session.get_user(name))
            else:
                values.append(None)

        return {
            'type': 'resultset',
            'columns': cols,
            'rows': [tuple(values)],
        }

    def _coerce(self, val):
        """值类型转换"""
        if not isinstance(val, str):
            return val
        v = val.strip()
        # 数字
        try:
            if '.' in v:
                return float(v)
            return int(v)
        except ValueError:
            pass
        # 字符串
        if v.startswith("'") and v.endswith("'"):
            return v[1:-1]
        # NULL
        if v.upper() == 'NULL':
            return None
        return v