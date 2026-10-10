"""
索引推荐引擎
- 分析慢查询
- 推荐索引
- 评估收益
"""
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Set


@dataclass
class IndexSuggestion:
    """索引建议"""
    table: str
    columns: List[str]
    create_sql: str
    reason: str
    priority: str = 'medium'    # high / medium / low
    estimated_gain_pct: float = 0
    query_count: int = 0
    redundant: bool = False     # 是否已有类似索引


class IndexAdvisor:
    """索引推荐器"""

    def __init__(self, conn=None, catalog=None):
        self.conn = conn
        self.catalog = catalog
        # 缓存已有索引
        self._index_cache: Dict[str, List] = {}

    # ==================== 已有索引 ====================

    def get_existing_indexes(self, table: str) -> List[Dict]:
        if table in self._index_cache:
            return self._index_cache[table]
        if not self.conn:
            return []
        try:
            cols, rows = self.conn.query(
                f"SHOW INDEX FROM `{table}`")
            col_names = [c.lower() if isinstance(c, str) else str(c).lower()
                          for c in cols]
            indexes = defaultdict(list)
            for r in rows:
                d = dict(zip(col_names, r))
                name = d.get('key_name', '')
                col = d.get('column_name', '')
                seq = int(d.get('seq_in_index', 1))
                indexes[name].append((seq, col))
            result = []
            for name, cols_list in indexes.items():
                cols_list.sort()
                result.append({
                    'name': name,
                    'columns': [c for _, c in cols_list],
                    'unique': False,  # 简化
                })
            self._index_cache[table] = result
            return result
        except Exception:
            return []

    def is_covered(self, table: str, columns: List[str]) -> bool:
        """检查是否已被现有索引覆盖（最左前缀）"""
        existing = self.get_existing_indexes(table)
        for idx in existing:
            idx_cols = idx['columns']
            # 我的列必须是 idx 的前缀
            if len(columns) <= len(idx_cols):
                if all(columns[i] == idx_cols[i]
                        for i in range(len(columns))):
                    return True
        return False

    # ==================== 分析 ====================

    def analyze_queries(self, queries: List[str]) -> List[IndexSuggestion]:
        """从查询列表分析"""
        # 按 (table, columns) 聚合
        candidates = defaultdict(lambda: {
            'count': 0,
            'ops': Counter(),
            'query': '',
        })

        for q in queries:
            q_upper = q.upper()

            # 提取表
            m = re.search(r'\bFROM\s+[`"]?(\w+)[`"]?',
                          q, re.IGNORECASE)
            if not m:
                continue
            table = m.group(1)

            # WHERE
            wm = re.search(
                r'\bWHERE\s+(.+?)(?=\s+(?:GROUP|ORDER|LIMIT)\b|$)',
                q, re.IGNORECASE | re.DOTALL)
            if not wm:
                continue

            where = wm.group(1)
            for cm in re.finditer(
                    r'[`"]?(\w+)[`"]?\s*(=|>|<|>=|<=|LIKE|IN|BETWEEN)',
                    where, re.IGNORECASE):
                col = cm.group(1)
                op = cm.group(2).upper()
                if col.upper() in ('AND', 'OR', 'NOT'):
                    continue

                key = (table, col)
                candidates[key]['count'] += 1
                candidates[key]['ops'][op] += 1
                candidates[key]['query'] = q[:200]

        # 生成建议
        suggestions = []
        for (table, col), info in candidates.items():
            if info['count'] < 2:
                continue

            if self.is_covered(table, [col]):
                continue

            priority = 'high' if info['count'] >= 10 else \
                       'medium' if info['count'] >= 3 else 'low'

            # 索引名
            idx_name = f"idx_{table}_{col}"

            sugg = IndexSuggestion(
                table=table,
                columns=[col],
                create_sql=(f"CREATE INDEX {idx_name} "
                             f"ON `{table}` (`{col}`)"),
                reason=f"列 {col} 在 {info['count']} 个查询中被过滤",
                priority=priority,
                estimated_gain_pct=min(90, info['count'] * 5),
                query_count=info['count'],
            )
            suggestions.append(sugg)

        # 排序：高优先级优先
        priority_order = {'high': 0, 'medium': 1, 'low': 2}
        suggestions.sort(key=lambda x: (
            priority_order[x.priority], -x.query_count))

        return suggestions

    def analyze_from_catalog(self) -> List[IndexSuggestion]:
        """从 catalog 的 top_queries 分析"""
        if not self.catalog:
            return []

        queries = []
        for table_key, meta in self.catalog.tables.items():
            queries.extend(meta.top_queries)

        return self.analyze_queries(queries)

    # ==================== 冗余检测 ====================

    def find_redundant(self, table: str) -> List[Dict]:
        """查找冗余索引"""
        existing = self.get_existing_indexes(table)
        redundant = []

        for i, idx1 in enumerate(existing):
            for idx2 in existing:
                if idx1['name'] == idx2['name']:
                    continue
                # idx1 是 idx2 的前缀
                c1, c2 = idx1['columns'], idx2['columns']
                if len(c1) <= len(c2):
                    if all(c1[i] == c2[i] for i in range(len(c1))):
                        redundant.append({
                            'redundant_index': idx1['name'],
                            'covered_by': idx2['name'],
                            'columns': c1,
                            'suggestion': f"DROP INDEX {idx1['name']}",
                        })
                        break

        return redundant

    # ==================== 报告 ====================

    def generate_report(self,
                        suggestions: List[IndexSuggestion] = None) -> str:
        if suggestions is None:
            suggestions = self.analyze_from_catalog()

        lines = ["# 索引推荐报告", ""]

        if not suggestions:
            lines.append("✅ 未发现需要的索引")
            return '\n'.join(lines)

        lines.append(f"共 {len(suggestions)} 条建议")
        lines.append("")

        # 按优先级分组
        by_priority = defaultdict(list)
        for s in suggestions:
            by_priority[s.priority].append(s)

        for priority in ['high', 'medium', 'low']:
            items = by_priority.get(priority, [])
            if not items:
                continue
            icon = {'high': '🔴', 'medium': '🟡',
                    'low': '🔵'}[priority]
            lines.append(f"## {icon} {priority.upper()} ({len(items)})")
            lines.append("")

            for s in items:
                lines.append(f"### {s.table}")
                lines.append(f"- 列: `{', '.join(s.columns)}`")
                lines.append(f"- 原因: {s.reason}")
                lines.append(f"- 出现在 {s.query_count} 个查询")
                lines.append(f"- 预估收益: {s.estimated_gain_pct:.0f}%")
                lines.append(f"```sql\n{s.create_sql};\n```")
                lines.append("")

        return '\n'.join(lines)