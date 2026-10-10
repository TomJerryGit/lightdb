"""
交互式血缘可视化
- 图数据构建
- 字段级下钻
- 时间轴回放
"""
import time
from collections import defaultdict
from typing import List, Dict, Set, Optional


class LineageVisual:
    """血缘可视化数据生成器"""

    def __init__(self, lineage_v2=None, enhanced=None):
        self.lineage = lineage_v2
        self.enhanced = enhanced

    def build_graph(self, root: str = None, depth: int = 3,
                    direction: str = 'both',
                    include_columns: bool = False) -> Dict:
        """
        构建图数据
        direction: upstream / downstream / both
        """
        nodes = {}
        edges = []

        if not self.lineage and not self.enhanced:
            return {'nodes': [], 'edges': []}

        # 收集边
        all_edges = []

        if self.lineage:
            for e in self.lineage.edges:
                all_edges.append({
                    'from': e.get('from', ''),
                    'to': e.get('to', ''),
                    'type': e.get('type', ''),
                    'detail': e.get('detail', ''),
                })

        if self.enhanced:
            for e in self.enhanced.edges:
                all_edges.append({
                    'from': e.from_table,
                    'to': e.to_table,
                    'type': 'column',
                    'detail': f"{e.from_column}→{e.to_column}",
                    'from_col': e.from_column,
                    'to_col': e.to_column,
                })

        # 如果指定 root，过滤
        if root:
            all_edges = self._filter_by_root(all_edges, root,
                                              depth, direction)

        # 收集节点
        for e in all_edges:
            for n in (e['from'], e['to']):
                if not n or n.startswith('cte:'):
                    continue
                if n not in nodes:
                    nodes[n] = {
                        'id': n,
                        'label': n.split('.')[-1],
                        'table': n,
                    }

        # 节点度
        in_deg = defaultdict(int)
        out_deg = defaultdict(int)
        for e in all_edges:
            out_deg[e['from']] += 1
            in_deg[e['to']] += 1

        for n, info in nodes.items():
            info['in_degree'] = in_deg.get(n, 0)
            info['out_degree'] = out_deg.get(n, 0)
            # 类型
            if info['in_degree'] == 0 and info['out_degree'] > 0:
                info['type'] = 'source'
            elif info['out_degree'] == 0 and info['in_degree'] > 0:
                info['type'] = 'sink'
            else:
                info['type'] = 'intermediate'

        # 层级
        self._assign_levels(nodes, all_edges)

        return {
            'nodes': list(nodes.values()),
            'edges': all_edges,
            'root': root,
        }

    def _filter_by_root(self, edges: List[Dict], root: str,
                        depth: int, direction: str) -> List[Dict]:
        """从 root 出发 BFS 过滤"""
        result = set()
        if direction in ('downstream', 'both'):
            visited = set()
            queue = [(root, 0)]
            while queue:
                node, lvl = queue.pop(0)
                if lvl >= depth or node in visited:
                    continue
                visited.add(node)
                for e in edges:
                    if e['from'] == node:
                        key = (e['from'], e['to'], e['type'],
                               e.get('detail', ''))
                        if key not in result:
                            result.add(key)
                            queue.append((e['to'], lvl + 1))

        if direction in ('upstream', 'both'):
            visited = set()
            queue = [(root, 0)]
            while queue:
                node, lvl = queue.pop(0)
                if lvl >= depth or node in visited:
                    continue
                visited.add(node)
                for e in edges:
                    if e['to'] == node:
                        key = (e['from'], e['to'], e['type'],
                               e.get('detail', ''))
                        if key not in result:
                            result.add(key)
                            queue.append((e['from'], lvl + 1))

        return [{'from': k[0], 'to': k[1], 'type': k[2],
                 'detail': k[3]} for k in result]

    def _assign_levels(self, nodes: Dict, edges: List[Dict]):
        """分配层级（拓扑）"""
        from collections import deque
        in_deg = defaultdict(int)
        adj = defaultdict(list)
        for e in edges:
            in_deg[e['to']] += 1
            adj[e['from']].append(e['to'])

        levels = {}
        queue = deque()
        for n in nodes:
            if in_deg[n] == 0:
                levels[n] = 0
                queue.append(n)
        while queue:
            n = queue.popleft()
            for nxt in adj[n]:
                if nxt not in levels or levels[nxt] < levels[n] + 1:
                    levels[nxt] = levels[n] + 1
                    queue.append(nxt)

        max_lvl = max(levels.values()) if levels else 0
        for n in nodes:
            if n not in levels:
                levels[n] = max_lvl + 1
            nodes[n]['level'] = levels[n]

    def column_drilldown(self, table: str, column: str) -> Dict:
        """字段级下钻"""
        if not self.enhanced:
            return {'upstream': [], 'downstream': []}

        # 上游
        upstream = self.enhanced.upstream_of_column(table, column, 5)

        # 下游
        downstream = []
        visited = set()

        def walk(t, c, level):
            if level > 5:
                return
            key = f"{t}.{c}"
            if key in visited:
                return
            visited.add(key)
            for e in self.enhanced.edges:
                if e.from_table == t and e.from_column == c:
                    downstream.append({
                        'table': e.to_table,
                        'column': e.to_column,
                        'transform': e.transform,
                        'level': level,
                        'sql': e.sql_preview,
                    })
                    walk(e.to_table, e.to_column, level + 1)

        walk(table, column, 1)

        return {
            'table': table,
            'column': column,
            'upstream': upstream,
            'downstream': downstream,
        }

    def timeline(self, table: str = None,
                 limit: int = 100) -> List[Dict]:
        """时间轴数据"""
        events = []

        if self.enhanced:
            for e in self.enhanced.edges[-limit:]:
                if table and e.from_table != table and e.to_table != table:
                    continue
                events.append({
                    'timestamp': e.timestamp,
                    'type': 'column_lineage',
                    'from': f"{e.from_table}.{e.from_column}",
                    'to': f"{e.to_table}.{e.to_column}",
                    'transform': e.transform,
                    'sql': e.sql_preview,
                })

        events.sort(key=lambda x: x['timestamp'], reverse=True)
        return events[:limit]