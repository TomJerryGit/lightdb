"""
数据地图
- 业务域视图
- 数据流向图
- 热度热力图
- 交互式探索
"""
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Dict


@dataclass
class DomainNode:
    name: str
    tables: List[str]
    total_rows: int = 0
    query_count: int = 0
    sensitivity: str = "internal"
    color: str = "#569cd6"


@dataclass
class FlowEdge:
    from_domain: str
    to_domain: str
    count: int = 1
    tables: List[str] = None


class DataMap:
    """数据地图构建器"""

    def __init__(self, catalog=None, lineage=None):
        self.catalog = catalog
        self.lineage = lineage

    def build_domain_map(self) -> Dict:
        """构建业务域地图"""
        if not self.catalog:
            return {'domains': [], 'edges': []}

        # 聚合域
        domains = defaultdict(lambda: {
            'tables': [],
            'total_rows': 0,
            'query_count': 0,
            'sensitivity': 'public',
        })

        sens_level = {
            'public': 0, 'internal': 1, 'confidential': 2,
            'restricted': 3, 'pii': 4,
        }

        for key, meta in self.catalog.tables.items():
            d = domains[meta.domain]
            d['tables'].append(key)
            d['total_rows'] += meta.row_count
            d['query_count'] += meta.query_count
            # 取最高敏感度
            cur = sens_level.get(d['sensitivity'], 0)
            new = sens_level.get(meta.sensitivity, 0)
            if new > cur:
                d['sensitivity'] = meta.sensitivity

        # 转换为节点
        nodes = []
        for dname, info in domains.items():
            nodes.append({
                'name': dname,
                'tables': info['tables'],
                'table_count': len(info['tables']),
                'total_rows': info['total_rows'],
                'query_count': info['query_count'],
                'sensitivity': info['sensitivity'],
                'color': self._domain_color(info['sensitivity']),
            })

        # 域间关系
        edges = self._calc_domain_edges(domains)

        return {
            'domains': nodes,
            'edges': edges,
        }

    def _domain_color(self, sensitivity: str) -> str:
        return {
            'public': '#89d185',
            'internal': '#569cd6',
            'confidential': '#dcdcaa',
            'restricted': '#ce9178',
            'pii': '#f48771',
        }.get(sensitivity, '#569cd6')

    def _calc_domain_edges(self, domains) -> List[Dict]:
        """计算域之间的流向"""
        if not self.lineage:
            return []

        # table -> domain
        table_to_domain = {}
        for dname, info in domains.items():
            for t in info['tables']:
                table_to_domain[t] = dname
                # 表名（去库前缀）也映射
                short = t.split('.')[-1]
                table_to_domain[short] = dname

        edges = defaultdict(lambda: {'count': 0, 'tables': []})

        # 从血缘中提取
        for e in self.lineage.edges:
            src = table_to_domain.get(e.from_table) or \
                  table_to_domain.get(e.from_table.split('.')[-1])
            dst = table_to_domain.get(e.to_table) or \
                  table_to_domain.get(e.to_table.split('.')[-1])
            if src and dst and src != dst:
                key = (src, dst)
                edges[key]['count'] += 1
                t = f"{e.from_table}.{e.from_column}→{e.to_table}.{e.to_column}"
                if t not in edges[key]['tables']:
                    edges[key]['tables'].append(t)

        return [
            {
                'from_domain': k[0],
                'to_domain': k[1],
                'count': v['count'],
                'tables': v['tables'][:5],
            }
            for k, v in edges.items()
        ]

    def build_heatmap(self, metric: str = 'query_count') -> Dict:
        """热度热力图"""
        if not self.catalog:
            return {'data': [], 'max': 0}

        data = []
        for key, meta in self.catalog.tables.items():
            if metric == 'query_count':
                value = meta.query_count
            elif metric == 'rows':
                value = meta.row_count
            else:
                value = 0

            data.append({
                'key': key,
                'table': meta.name,
                'database': meta.database,
                'domain': meta.domain,
                'value': value,
                'sensitivity': meta.sensitivity,
            })

        data.sort(key=lambda x: -x['value'])
        max_value = data[0]['value'] if data else 0

        return {'data': data, 'max': max_value, 'metric': metric}

    def build_flow_graph(self) -> Dict:
        """完整流向图（表级）"""
        if not self.lineage:
            return {'nodes': [], 'edges': []}

        nodes = {}
        edges = []

        for e in self.lineage.edges:
            for t in (e.from_table, e.to_table):
                if t not in nodes:
                    # 从 catalog 补充信息
                    domain = '未分类'
                    rows = 0
                    if self.catalog:
                        for key, meta in self.catalog.tables.items():
                            if meta.name == t or key.endswith(f'.{t}'):
                                domain = meta.domain
                                rows = meta.row_count
                                break
                    nodes[t] = {
                        'name': t,
                        'domain': domain,
                        'rows': rows,
                    }

            edges.append({
                'from': e.from_table,
                'to': e.to_table,
                'from_col': e.from_column,
                'to_col': e.to_column,
                'transform': e.transform,
            })

        # 合并同源边
        merged = defaultdict(lambda: {'count': 0, 'cols': []})
        for e in edges:
            key = (e['from'], e['to'])
            merged[key]['count'] += 1
            if len(merged[key]['cols']) < 5:
                merged[key]['cols'].append(
                    f"{e['from_col']}→{e['to_col']}")

        final_edges = [
            {
                'from': k[0],
                'to': k[1],
                'count': v['count'],
                'columns': v['cols'],
            }
            for k, v in merged.items()
        ]

        return {
            'nodes': list(nodes.values()),
            'edges': final_edges,
        }

    def find_hot_paths(self, top_n: int = 10) -> List[Dict]:
        """找出最热的血缘路径"""
        if not self.lineage:
            return []

        path_count = defaultdict(int)
        for e in self.lineage.edges:
            path = f"{e.from_table} → {e.to_table}"
            path_count[path] += 1

        return [
            {'path': p, 'count': c}
            for p, c in sorted(path_count.items(),
                                key=lambda x: -x[1])[:top_n]
        ]

    def unused_tables(self, days: int = 30) -> List[str]:
        """从未或长期未使用的表"""
        if not self.catalog:
            return []

        import time
        cutoff = time.time() - days * 86400
        result = []
        for key, meta in self.catalog.tables.items():
            if meta.query_count == 0:
                result.append(key)
            elif meta.last_queried > 0 and meta.last_queried < cutoff:
                result.append(key)
        return result