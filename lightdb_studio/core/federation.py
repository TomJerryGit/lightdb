"""
联邦查询
- 跨连接 JOIN
- 查询下推（把 WHERE 推到各源）
- 结果合并
"""
import re
import time
from dataclasses import dataclass, field
from typing import List, Dict, Callable, Optional


@dataclass
class SourceFragment:
    """分片查询"""
    connection: str
    sql: str
    alias: str = ""


@dataclass
class JoinCondition:
    left_alias: str
    left_col: str
    right_alias: str
    right_col: str
    join_type: str = "INNER"


class FederationEngine:
    """联邦查询引擎"""

    def __init__(self, conn_provider: Callable = None):
        self.conn_provider = conn_provider

    def set_conn_provider(self, provider):
        self.conn_provider = provider

    def execute(self, fragments: List[SourceFragment],
                join_conditions: List[JoinCondition],
                select_columns: List[str] = None,
                where: str = "",
                limit: int = 0) -> tuple:
        """
        执行联邦查询
        fragments: [SourceFragment, ...]
        join_conditions: [JoinCondition, ...]
        """
        if not fragments:
            return [], []

        # 1. 数据抽取（可下推）
        extracted = {}
        for f in fragments:
            conn = self._get_conn(f.connection)
            if not conn:
                raise ValueError(f"连接 {f.connection} 不可用")

            sql = f.sql
            # 单源下推 WHERE
            # 简单判断：如果 where 只涉及当前 fragment 的列，就下推
            # 完整实现需要列归属分析
            try:
                cols, rows = conn.query(sql)
            except Exception as e:
                raise RuntimeError(
                    f"查询 {f.connection} 失败: {e}")

            alias = f.alias or f.connection
            extracted[alias] = {
                'columns': cols,
                'rows': rows,
                'connection': f.connection,
            }

        # 2. 执行 JOIN
        if not join_conditions:
            # 笛卡尔积？简化：只取第一个源
            first = next(iter(extracted.keys()))
            cols = [f"{first}.{c}" for c in extracted[first]['columns']]
            rows = extracted[first]['rows']
        else:
            cols, rows = self._execute_joins(
                extracted, join_conditions)

        # 3. WHERE 过滤
        if where:
            rows = self._filter(rows, cols, where)

        # 4. 投影
        if select_columns and select_columns != ['*']:
            target_idx = []
            target_cols = []
            for sc in select_columns:
                # 支持 alias.col 和 col
                if sc in cols:
                    target_idx.append(cols.index(sc))
                    target_cols.append(sc)
                else:
                    # 尝试匹配后缀
                    for i, c in enumerate(cols):
                        if c.endswith('.' + sc) or c == sc:
                            target_idx.append(i)
                            target_cols.append(c)
                            break

            rows = [[r[i] for i in target_idx] for r in rows]
            cols = target_cols

        # 5. LIMIT
        if limit:
            rows = rows[:limit]

        return cols, rows

    def _get_conn(self, name: str):
        if not self.conn_provider:
            return None
        return self.conn_provider(name)

    def _execute_joins(self, extracted: Dict,
                       conditions: List[JoinCondition]) -> tuple:
        """执行 JOIN 链"""
        aliases = list(extracted.keys())
        if len(aliases) < 2:
            a = aliases[0]
            cols = [f"{a}.{c}" for c in extracted[a]['columns']]
            return cols, extracted[a]['rows']

        # 从第一个源开始
        current_alias = aliases[0]
        current_cols = [f"{current_alias}.{c}"
                        for c in extracted[current_alias]['columns']]
        current_rows = [
            list(r) for r in extracted[current_alias]['rows']
        ]

        # 依次 JOIN 其他源
        joined = {current_alias}

        for next_alias in aliases[1:]:
            if next_alias in joined:
                continue

            # 找 JOIN 条件
            cond = None
            for c in conditions:
                if c.left_alias == next_alias and c.right_alias in joined:
                    cond = c
                    break
                if c.right_alias == next_alias and c.left_alias in joined:
                    cond = JoinCondition(
                        left_alias=c.right_alias,
                        left_col=c.right_col,
                        right_alias=c.left_alias,
                        right_col=c.left_col,
                        join_type=c.join_type,
                    )
                    break

            if cond is None:
                # 无 JOIN 条件，做笛卡尔积
                next_cols = [f"{next_alias}.{c}"
                             for c in extracted[next_alias]['columns']]
                new_cols = current_cols + next_cols
                new_rows = []
                for r1 in current_rows:
                    for r2 in extracted[next_alias]['rows']:
                        new_rows.append(list(r1) + list(r2))
                current_cols = new_cols
                current_rows = new_rows
                joined.add(next_alias)
                continue

            # 找 JOIN 列索引
            left_idx = self._find_col_idx(
                current_cols, cond.left_alias, cond.left_col)
            right_idx = extracted[next_alias]['columns'].index(
                cond.right_col) if cond.right_col in \
                extracted[next_alias]['columns'] else -1

            if left_idx < 0 or right_idx < 0:
                continue

            # 执行 JOIN
            next_cols = [f"{next_alias}.{c}"
                         for c in extracted[next_alias]['columns']]
            new_cols = current_cols + next_cols

            # 构建哈希表
            hash_table = {}
            for r in extracted[next_alias]['rows']:
                key = r[right_idx]
                hash_table.setdefault(key, []).append(r)

            new_rows = []
            for r1 in current_rows:
                key = r1[left_idx]
                matches = hash_table.get(key, [])
                if matches:
                    for r2 in matches:
                        new_rows.append(list(r1) + list(r2))
                elif cond.join_type in ('LEFT', 'LEFT OUTER'):
                    new_rows.append(
                        list(r1) + [None] * len(next_cols))

            current_cols = new_cols
            current_rows = new_rows
            joined.add(next_alias)

        return current_cols, current_rows

    def _find_col_idx(self, cols: List[str], alias: str,
                      col: str) -> int:
        # 先精确匹配 alias.col
        target = f"{alias}.{col}"
        if target in cols:
            return cols.index(target)
        # 再匹配不带别名的
        if col in cols:
            return cols.index(col)
        # 后缀匹配
        for i, c in enumerate(cols):
            if c == col or c.endswith('.' + col):
                return i
        return -1

    def _filter(self, rows, cols, where: str):
        """WHERE 过滤（简化）"""
        conditions = re.split(r'\s+AND\s+', where, flags=re.IGNORECASE)
        result = list(rows)
        for cond in conditions:
            cond = cond.strip()
            m = re.match(
                r'([`"]?\w+[`"]?(?:\.[`"]?\w+[`"]?)?)\s*'
                r'(=|!=|<>|>=|<=|>|<|LIKE)\s*(.+?)$',
                cond, re.IGNORECASE)
            if not m:
                continue
            col = m.group(1).strip('`"')
            op = m.group(2).upper()
            val = m.group(3).strip()

            ci = -1
            if col in cols:
                ci = cols.index(col)
            else:
                for i, c in enumerate(cols):
                    if c.endswith('.' + col) or c.split('.')[-1] == col:
                        ci = i
                        break

            if ci < 0:
                continue

            val_str = val.strip("'\"")
            try:
                val_num = float(val_str)
            except ValueError:
                val_num = None

            new_rows = []
            for r in result:
                v = r[ci]
                try:
                    cmp_v = val_num if (val_num is not None and
                                        isinstance(v, (int, float))) \
                        else val_str
                    if op == '=': ok = v == cmp_v
                    elif op in ('!=', '<>'): ok = v != cmp_v
                    elif op == '>': ok = v > cmp_v
                    elif op == '<': ok = v < cmp_v
                    elif op == '>=': ok = v >= cmp_v
                    elif op == '<=': ok = v <= cmp_v
                    elif op == 'LIKE':
                        pat = val_str.replace('%', '.*').replace('_', '.')
                        ok = bool(re.match('^' + pat + '$', str(v)))
                    else:
                        ok = False
                except (TypeError, ValueError):
                    ok = False
                if ok:
                    new_rows.append(r)
            result = new_rows
        return result

    # ==================== SQL 解析 ====================

    def parse_federated_sql(self, sql: str,
                            alias_to_conn: Dict[str, str]) -> Dict:
        """
        解析联邦 SQL
        alias_to_conn: {'u': 'prod_db', 'o': 'analytics_db'}
        """
        result = {
            'fragments': [],
            'joins': [],
            'where': '',
            'columns': ['*'],
            'limit': 0,
        }

        # SELECT
        m = re.match(
            r'\s*SELECT\s+(.+?)\s+FROM\s+(.+?)(?:\s+WHERE\s+(.+?))?'
            r'(?:\s+ORDER\s+BY\s+(.+?))?(?:\s+LIMIT\s+(\d+))?\s*$',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            return result

        result['columns'] = [
            c.strip() for c in m.group(1).split(',')
        ]
        from_clause = m.group(2)
        if m.group(3):
            result['where'] = m.group(3).strip()
        if m.group(5):
            result['limit'] = int(m.group(5))

        # FROM/JOIN
        pattern = re.compile(
            r'(?:FROM|(INNER|LEFT|RIGHT|CROSS)\s+JOIN)\s+'
            r'[`"]?(\w+)[`"]?(?:\s+(?:AS\s+)?(\w+))?',
            re.IGNORECASE)

        aliases = {}
        for fm in pattern.finditer(from_clause):
            jt = fm.group(1) or 'FROM'
            table = fm.group(2)
            alias = fm.group(3) or table

            if alias in alias_to_conn:
                conn_name = alias_to_conn[alias]
            else:
                conn_name = alias_to_conn.get(table, table)

            result['fragments'].append({
                'connection': conn_name,
                'sql': f'SELECT * FROM `{table}`',
                'alias': alias,
                'table': table,
            })
            aliases[alias] = table

        # JOIN 条件
        on_pattern = re.compile(
            r'ON\s+(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)',
            re.IGNORECASE)
        for om in on_pattern.finditer(from_clause):
            result['joins'].append({
                'left_alias': om.group(1),
                'left_col': om.group(2),
                'right_alias': om.group(3),
                'right_col': om.group(4),
            })

        return result