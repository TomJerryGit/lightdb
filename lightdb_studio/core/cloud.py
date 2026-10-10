"""
多云管理
- 跨云连接池
- 云资源发现
- 成本分析
- 多云拓扑
"""
import json
import time
from collections import defaultdict
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional, Callable


CLOUD_FILE = Path.home() / '.lightdb_studio' / 'cloud.json'


# 云厂商定义
CLOUD_PROVIDERS = {
    'aws': {'label': 'AWS', 'color': '#ff9900', 'icon': '☁️'},
    'gcp': {'label': 'Google Cloud', 'color': '#4285f4', 'icon': '🌤'},
    'azure': {'label': 'Azure', 'color': '#0078d4', 'icon': '🟦'},
    'aliyun': {'label': '阿里云', 'color': '#ff6a00', 'icon': '🟧'},
    'tencent': {'label': '腾讯云', 'color': '#00a4ff', 'icon': '🟨'},
    'onprem': {'label': '本地机房', 'color': '#89d185', 'icon': '🏢'},
    'edge': {'label': '边缘节点', 'color': '#dcdcaa', 'icon': '📡'},
}

# 实例类型单价（每 GB / 每小时，用于成本估算）
INSTANCE_PRICING = {
    'aws': {'db.t3.small': 0.034, 'db.t3.medium': 0.068,
            'db.r5.large': 0.24, 'db.r5.xlarge': 0.48},
    'gcp': {'db-n1-standard-1': 0.0413, 'db-n1-standard-2': 0.0826,
            'db-n1-standard-4': 0.1652},
    'azure': {'B2s': 0.032, 'D2s_v3': 0.096, 'E2s_v3': 0.13},
    'aliyun': {'rds.mysql.s2.large': 0.04,
               'rds.mysql.m1.medium': 0.08},
    'onprem': {'local': 0.0},
}


@dataclass
class CloudNode:
    """云节点"""
    node_id: str = ""
    name: str = ""
    provider: str = "onprem"        # aws / gcp / azure / aliyun / ...
    region: str = ""
    instance_type: str = ""
    # 连接信息
    host: str = ""
    port: int = 8888
    connection_name: str = ""       # 关联到 ConnectionInfo
    # 资源
    vcpu: int = 2
    memory_gb: int = 4
    disk_gb: int = 100
    # 状态
    status: str = "online"          # online / offline / degraded
    role: str = "primary"           # primary / replica / backup / cache
    # 指标（最近采集）
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    disk_usage: float = 0.0
    qps: float = 0.0
    connections: int = 0
    # 时间
    created_at: float = 0
    last_seen: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.last_seen:
            self.last_seen = now

    def provider_info(self) -> Dict:
        return CLOUD_PROVIDERS.get(self.provider, {
            'label': self.provider, 'color': '#569cd6', 'icon': '☁️'})

    def hourly_cost(self) -> float:
        pricing = INSTANCE_PRICING.get(self.provider, {})
        return pricing.get(self.instance_type, 0.0)

    def monthly_cost(self) -> float:
        return self.hourly_cost() * 24 * 30


@dataclass
class CostRecord:
    node_id: str
    timestamp: float
    hourly_cost: float = 0
    compute_cost: float = 0
    storage_cost: float = 0
    network_cost: float = 0

    def total(self) -> float:
        return (self.hourly_cost + self.compute_cost +
                self.storage_cost + self.network_cost)


class CloudManager:
    """多云管理器"""

    def __init__(self, conn_provider: Callable = None):
        self.file = CLOUD_FILE
        self.conn_provider = conn_provider
        self.nodes: Dict[str, CloudNode] = {}
        self.cost_history: List[CostRecord] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for nid, nd in data.get('nodes', {}).items():
                self.nodes[nid] = CloudNode(**nd)
            for cr in data.get('cost_history', [])[-5000:]:
                self.cost_history.append(CostRecord(**cr))
        except Exception:
            pass

    def save(self):
        data = {
            'nodes': {n: asdict(nd) for n, nd in self.nodes.items()},
            'cost_history': [asdict(c) for c in self.cost_history[-2000:]],
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 节点管理 ====================

    def register_node(self, name: str, provider: str,
                      region: str = '', host: str = '',
                      port: int = 8888, connection_name: str = '',
                      instance_type: str = '',
                      role: str = 'primary',
                      vcpu: int = 2, memory_gb: int = 4,
                      disk_gb: int = 100) -> CloudNode:
        import uuid
        n = CloudNode(
            node_id=f"node_{uuid.uuid4().hex[:8]}",
            name=name,
            provider=provider,
            region=region,
            host=host,
            port=port,
            connection_name=connection_name,
            instance_type=instance_type,
            role=role,
            vcpu=vcpu,
            memory_gb=memory_gb,
            disk_gb=disk_gb,
        )
        self.nodes[n.node_id] = n
        self.save()
        return n

    def update_node(self, node_id: str, **kwargs):
        n = self.nodes.get(node_id)
        if not n:
            return False
        for k, v in kwargs.items():
            if hasattr(n, k):
                setattr(n, k, v)
        n.last_seen = time.time()
        self.save()
        return True

    def remove_node(self, node_id: str):
        if node_id in self.nodes:
            del self.nodes[node_id]
            self.save()

    # ==================== 指标采集 ====================

    def collect_metrics(self, node_id: str) -> Dict:
        """采集节点指标"""
        n = self.nodes.get(node_id)
        if not n:
            return {'error': '节点不存在'}

        conn = self.conn_provider(n.connection_name) \
            if self.conn_provider and n.connection_name else None
        if not conn:
            n.status = 'offline'
            self.save()
            return {'error': '连接不可用'}

        try:
            cols, rows = conn.query("SHOW STATUS")
            status = dict(rows)

            def _int(v):
                try:
                    return int(v)
                except (ValueError, TypeError):
                    return 0

            connections = _int(status.get('Threads_connected', 0))
            threads_running = _int(status.get('Threads_running', 0))

            n.connections = connections
            n.qps = threads_running * 50   # 简化估算
            n.status = 'online'
            n.last_seen = time.time()

            # 计算成本
            self._record_cost(n)

            self.save()
            return {
                'node_id': node_id,
                'connections': connections,
                'qps': n.qps,
                'status': n.status,
            }
        except Exception as e:
            n.status = 'degraded'
            self.save()
            return {'error': str(e)}

    def _record_cost(self, node: CloudNode):
        """记录成本"""
        c = CostRecord(
            node_id=node.node_id,
            timestamp=time.time(),
            hourly_cost=node.hourly_cost(),
            storage_cost=node.disk_gb * 0.0001,   # 简化
        )
        self.cost_history.append(c)
        if len(self.cost_history) > 5000:
            self.cost_history = self.cost_history[-2000:]

    # ==================== 成本分析 ====================

    def cost_summary(self, hours: int = 24) -> Dict:
        """成本汇总"""
        cutoff = time.time() - hours * 3600
        recent = [c for c in self.cost_history
                   if c.timestamp >= cutoff]

        by_node = defaultdict(float)
        by_provider = defaultdict(float)
        total = 0

        for c in recent:
            amt = c.total()
            total += amt
            by_node[c.node_id] += amt
            node = self.nodes.get(c.node_id)
            if node:
                by_provider[node.provider] += amt

        # 预测月度
        hours_elapsed = min(hours, (time.time() - min(
            (c.timestamp for c in recent), default=time.time()
        )) / 3600) if recent else 1
        monthly_forecast = (total / max(1, hours_elapsed)) * 24 * 30

        return {
            'period_hours': hours,
            'total': total,
            'by_node': {
                self.nodes[nid].name if nid in self.nodes else nid: amt
                for nid, amt in by_node.items()
            },
            'by_provider': dict(by_provider),
            'monthly_forecast': monthly_forecast,
            'records': len(recent),
        }

    def cost_optimization_suggestions(self) -> List[Dict]:
        """成本优化建议"""
        suggestions = []

        for n in self.nodes.values():
            # 空转节点
            if n.status == 'online' and n.qps < 1 and n.connections < 2:
                suggestions.append({
                    'node': n.name,
                    'type': 'idle',
                    'message': f"{n.name} 长期空转",
                    'potential_saving': n.monthly_cost(),
                    'action': '考虑下线或降级',
                })

            # 高配低用
            if n.cpu_usage < 0.2 and n.memory_usage < 0.3:
                if 'large' in n.instance_type.lower() or \
                   'xlarge' in n.instance_type.lower():
                    suggestions.append({
                        'node': n.name,
                        'type': 'oversized',
                        'message': f"{n.name} 资源配置过高",
                        'potential_saving': n.monthly_cost() * 0.5,
                        'action': '考虑降配到更小实例',
                    })

            # 副本过多
            replicas = [x for x in self.nodes.values()
                         if x.role == 'replica']
            if len(replicas) > 3:
                suggestions.append({
                    'node': 'cluster',
                    'type': 'too_many_replicas',
                    'message': f"存在 {len(replicas)} 个副本节点",
                    'potential_saving': sum(
                        r.monthly_cost() for r in replicas[2:]),
                    'action': '考虑减少副本数量',
                })
                break

        return suggestions

    # ==================== 拓扑 ====================

    def topology(self) -> Dict:
        """云拓扑图"""
        nodes = []
        edges = []

        for n in self.nodes.values():
            pi = n.provider_info()
            nodes.append({
                'id': n.node_id,
                'name': n.name,
                'provider': n.provider,
                'provider_label': pi['label'],
                'color': pi['color'],
                'icon': pi['icon'],
                'region': n.region,
                'role': n.role,
                'status': n.status,
                'qps': n.qps,
                'connections': n.connections,
                'monthly_cost': n.monthly_cost(),
            })

        # 主从关系
        primaries = [n for n in self.nodes.values()
                      if n.role == 'primary']
        replicas = [n for n in self.nodes.values()
                     if n.role == 'replica']

        for p in primaries:
            for r in replicas:
                if r.provider == p.provider and r.region == p.region:
                    edges.append({
                        'from': p.node_id,
                        'to': r.node_id,
                        'type': 'replication',
                    })

        return {'nodes': nodes, 'edges': edges}

    def stats(self) -> Dict:
        by_provider = defaultdict(int)
        by_region = defaultdict(int)
        by_status = defaultdict(int)
        total_monthly = 0

        for n in self.nodes.values():
            by_provider[n.provider] += 1
            by_region[n.region or '(未指定)'] += 1
            by_status[n.status] += 1
            total_monthly += n.monthly_cost()

        return {
            'total_nodes': len(self.nodes),
            'by_provider': dict(by_provider),
            'by_region': dict(by_region),
            'by_status': dict(by_status),
            'monthly_cost_estimate': total_monthly,
        }