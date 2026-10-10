"""
智能推荐 2.0
- 协同过滤：基于用户-表使用矩阵
- 表关联推荐：基于血缘 / JOIN 关系
- Join 路径推荐
- 相似 SQL 推荐
"""
import json
import math
import re
import time
from collections import defaultdict, Counter
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Set, Tuple, Optional


RECOMMEND_FILE = Path.home() / '.lightdb_studio' / 'recommend.json'


@dataclass
class UserTableUsage:
    user: str
    table: str
    count: int = 1
    last_used: float = 0

    def __post_init__(self):
        if not self.last_used:
            self.last_used = time.time()


class Recommender:
    """智能推荐引擎"""

    def __init__(self, catalog=None, lineage=None):
        self.file = RECOMMEND_FILE
        self.catalog = catalog
        self.lineage = lineage

        # 用户-表使用记录
        self.usage: List[UserTableUsage] = []
        # JOIN 记录: (table1, table2) -> count
        self.join_pairs: Dict[Tuple[str, str], int] = {}

        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for u in data.get('usage', []):
                self.usage.append(UserTableUsage(**u))
            self.join_pairs = {
                tuple(k.split('|')): v
                for k, v in data.get('join_pairs', {}).items()
            }
        except Exception:
            pass

    def save(self):
        data = {
            'usage': [asdict(u) for u in self.usage[-5000:]],
            'join_pairs': {
                f"{k[0]}|{k[1]}": v
                for k, v in self.join_pairs.items()
            },
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 学习 ====================

    def record_usage(self, user: str, table: str):
        """记录用户访问表"""
        for u in self.usage:
            if u.user == user and u.table == table:
                u.count += 1
                u.last_used = time.time()
                self.save()
                return
        self.usage.append(UserTableUsage(
            user=user, table=table))
        self.save()

    def learn_from_sql(self, user: str, sql: str):
        """从 SQL 学习"""
        from .lineage import LineageAnalyzer
        tables = LineageAnalyzer.extract_tables(sql)
        table_names = []
        for t in tables:
            tname = t['table']
            if tname and not tname.startswith('__'):
                self.record_usage(user, tname)
                table_names.append(tname)

        # JOIN 关系
        if len(table_names) >= 2:
            for i in range(len(table_names)):
                for j in range(i + 1, len(table_names)):
                    a, b = sorted([table_names[i], table_names[j]])
                    key = (a, b)
                    self.join_pairs[key] = self.join_pairs.get(key, 0) + 1
            self.save()

    # ==================== 协同过滤推荐 ====================

    def recommend_tables_for_user(self, user: str,
                                   top_n: int = 10) -> List[Dict]:
        """为用户推荐表（协同过滤）"""
        # 构建用户-表矩阵
        user_tables = defaultdict(set)
        table_users = defaultdict(set)
        for u in self.usage:
            user_tables[u.user].add(u.table)
            table_users[u.table].add(u.user)

        my_tables = user_tables.get(user, set())
        if not my_tables:
            # 冷启动：返回热门表
            table_count = Counter()
            for u in self.usage:
                table_count[u.table] += u.count
            return [
                {'table': t, 'score': c, 'reason': '热门表'}
                for t, c in table_count.most_common(top_n)
            ]

        # 用户相似度（Jaccard）
        similarities = []
        for other_user, other_tables in user_tables.items():
            if other_user == user:
                continue
            inter = my_tables & other_tables
            union = my_tables | other_tables
            if not union:
                continue
            sim = len(inter) / len(union)
            if sim > 0:
                similarities.append((sim, other_user))

        similarities.sort(key=lambda x: -x[0])

        # 推荐：相似用户用过、我没用过的表
        scores = defaultdict(float)
        reasons = defaultdict(list)
        for sim, other_user in similarities[:20]:
            for t in user_tables[other_user]:
                if t in my_tables:
                    continue
                scores[t] += sim
                if len(reasons[t]) < 3:
                    reasons[t].append(f"用户 {other_user} (相似度 {sim:.2f})")

        ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_n]
        return [
            {'table': t, 'score': s,
             'reason': '; '.join(reasons[t])}
            for t, s in ranked
        ]

    # ==================== 关联表推荐 ====================

    def related_tables(self, table: str,
                       top_n: int = 10) -> List[Dict]:
        """给定表，推荐常一起使用的表"""
        related = {}
        for (a, b), count in self.join_pairs.items():
            if a == table:
                related[b] = related.get(b, 0) + count
            elif b == table:
                related[a] = related.get(a, 0) + count

        # 结合血缘
        if self.lineage:
            for e in self.lineage.edges:
                if e.from_table == table:
                    t = e.to_table
                    if t != table:
                        related[t] = related.get(t, 0) + 5
                elif e.to_table == table:
                    t = e.from_table
                    if t != table:
                        related[t] = related.get(t, 0) + 5

        ranked = sorted(related.items(), key=lambda x: -x[1])[:top_n]
        return [{'table': t, 'score': c} for t, c in ranked]

    # ==================== Join 路径推荐 ====================

    def recommend_join_path(self, table_a: str,
                            table_b: str) -> List[Dict]:
        """推荐 A → B 的 JOIN 路径"""
        if not self.lineage:
            return []

        # 构建图
        graph = defaultdict(list)
        for e in self.lineage.edges:
            graph[e.from_table].append(e.to_table)
            graph[e.to_table].append(e.from_table)

        # BFS 最短路
        if table_a not in graph:
            return []

        from collections import deque
        queue = deque([(table_a, [table_a])])
        visited = {table_a}
        while queue:
            node, path = queue.popleft()
            if node == table_b:
                return [{'path': path, 'length': len(path) - 1}]
            if len(path) > 5:
                continue
            for nxt in graph[node]:
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, path + [nxt]))

        return []

    def recommend_join_keys(self, table_a: str,
                            table_b: str) -> List[Dict]:
        """推荐 A、B 之间的 JOIN 键"""
        if not self.lineage:
            return []

        result = []
        for e in self.lineage.edges:
            if (e.from_table == table_a and e.to_table == table_b) or \
               (e.from_table == table_b and e.to_table == table_a):
                result.append({
                    'left': f"{e.from_table}.{e.from_column}",
                    'right': f"{e.to_table}.{e.to_column}",
                    'count': 1,
                })

        # 基于命名匹配
        if self.catalog and not result:
            meta_a = None
            meta_b = None
            for key, m in self.catalog.tables.items():
                if m.name == table_a:
                    meta_a = m
                if m.name == table_b:
                    meta_b = m

            if meta_a and meta_b:
                for ca in meta_a.columns:
                    # a 的 id 匹配 b 的 a_id
                    if ca.name == 'id':
                        target = f"{table_a}_id"
                        for cb in meta_b.columns:
                            if cb.name == target or cb.name == f"{table_a}id":
                                result.append({
                                    'left': f"{table_a}.id",
                                    'right': f"{table_b}.{cb.name}",
                                    'reason': '命名推断',
                                })
                for cb in meta_b.columns:
                    if cb.name == 'id':
                        target = f"{table_b}_id"
                        for ca in meta_a.columns:
                            if ca.name == target or ca.name == f"{table_b}id":
                                result.append({
                                    'left': f"{table_a}.{ca.name}",
                                    'right': f"{table_b}.id",
                                    'reason': '命名推断',
                                })

        return result

    # ==================== 相似 SQL 推荐 ====================

    def similar_sql(self, sql: str, history: List[str],
                    top_n: int = 5) -> List[Dict]:
        """从历史中找相似 SQL"""
        def tokens(s):
            s = s.lower()
            words = re.findall(r'[a-z_]+', s)
            return set(words)

        my_tokens = tokens(sql)
        scored = []
        for h in history:
            if not h.strip() or h == sql:
                continue
            h_tokens = tokens(h)
            if not h_tokens:
                continue
            inter = my_tokens & h_tokens
            union = my_tokens | h_tokens
            if not union:
                continue
            sim = len(inter) / len(union)
            scored.append((sim, h))

        scored.sort(key=lambda x: -x[0])
        return [
            {'sql': h, 'similarity': sim}
            for sim, h in scored[:top_n] if sim > 0.3
        ]

    # ==================== 热门推荐 ====================

    def trending_tables(self, hours: int = 24,
                        top_n: int = 10) -> List[Dict]:
        """最近热门表"""
        cutoff = time.time() - hours * 3600
        counts = Counter()
        for u in self.usage:
            if u.last_used >= cutoff:
                counts[u.table] += u.count

        return [
            {'table': t, 'count': c}
            for t, c in counts.most_common(top_n)
        ]

    def stats(self) -> dict:
        users = {u.user for u in self.usage}
        tables = {u.table for u in self.usage}
        return {
            'total_usage': len(self.usage),
            'total_users': len(users),
            'total_tables': len(tables),
            'total_join_pairs': len(self.join_pairs),
        }