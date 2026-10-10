"""
数据 Mesh
- 域自治：每个域有自己的数据产品和负责人
- 数据产品：可发现、可寻址、可信、可组合
- 域间契约：通过标准接口交换数据
"""
import json
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional


MESH_FILE = Path.home() / '.lightdb_studio' / 'data_mesh.json'


@dataclass
class DataProduct:
    """
    数据产品：对外的可组合数据单元
    比表更抽象，可以是虚拟表、视图或数据服务
    """
    product_id: str = ""            # 全局唯一
    name: str = ""
    domain: str = ""                # 所属域
    description: str = ""
    owner: str = ""                 # 数据产品负责人
    team: str = ""                  # 团队
    # 接口（输出）
    output_type: str = "table"      # table / view / api / virtual_table
    output_ref: str = ""            # 表的 db.table 或 api name
    output_schema: List[Dict] = field(default_factory=list)
    # 输入（依赖）
    inputs: List[Dict] = field(default_factory=list)
    # [{'product_id': 'xxx', 'role': 'source'}, ...]

    # 元数据
    sla: Dict = field(default_factory=dict)
    # {'freshness': 3600, 'availability': 0.99, 'quality': 0.95}
    version: str = "1.0.0"
    tags: List[str] = field(default_factory=list)
    # 生命周期
    status: str = "active"          # active / deprecated / draft
    tier: str = "internal"          # critical / important / internal / experimental
    # 使用情况
    consumer_count: int = 0
    query_count: int = 0
    last_used: float = 0
    # 时间
    created_at: float = 0
    updated_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.product_id:
            self.product_id = f"dp_{uuid.uuid4().hex[:12]}"
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


@dataclass
class Domain:
    """数据域"""
    name: str = ""
    description: str = ""
    owner: str = ""
    team_members: List[str] = field(default_factory=list)
    # 域内数据产品
    products: List[str] = field(default_factory=list)   # product_ids
    # 域间依赖
    upstream_domains: List[str] = field(default_factory=list)
    downstream_domains: List[str] = field(default_factory=list)
    # 自治
    autonomy_level: str = "full"    # full / supervised / managed
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class ProductContract:
    """数据产品契约（域间）"""
    contract_id: str = ""
    producer_product: str = ""
    consumer_product: str = ""
    producer_domain: str = ""
    consumer_domain: str = ""
    # 接口
    interface: Dict = field(default_factory=dict)
    # SLA
    sla: Dict = field(default_factory=dict)
    # 状态
    status: str = "active"          # active / pending / broken
    signed_at: float = 0
    expires_at: float = 0
    # 违约
    violations: List[Dict] = field(default_factory=list)

    def __post_init__(self):
        if not self.contract_id:
            self.contract_id = f"c_{uuid.uuid4().hex[:8]}"
        if not self.signed_at:
            self.signed_at = time.time()


class DataMeshManager:
    """数据 Mesh 管理器"""

    def __init__(self):
        self.file = MESH_FILE
        self.domains: Dict[str, Domain] = {}
        self.products: Dict[str, DataProduct] = {}
        self.contracts: Dict[str, ProductContract] = {}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for n, dd in data.get('domains', {}).items():
                self.domains[n] = Domain(**dd)
            for pid, pd in data.get('products', {}).items():
                self.products[pid] = DataProduct(**pd)
            for cid, cd in data.get('contracts', {}).items():
                self.contracts[cid] = ProductContract(**cd)
        except Exception:
            pass

    def save(self):
        data = {
            'domains': {n: asdict(d) for n, d in self.domains.items()},
            'products': {p: asdict(pd) for p, pd in self.products.items()},
            'contracts': {c: asdict(ct) for c, ct in self.contracts.items()},
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 域管理 ====================

    def create_domain(self, name: str, description: str = '',
                      owner: str = '', autonomy: str = 'full') -> Domain:
        d = Domain(
            name=name,
            description=description,
            owner=owner,
            autonomy_level=autonomy,
        )
        self.domains[name] = d
        self.save()
        return d

    def update_domain(self, name: str, **kwargs):
        if name not in self.domains:
            return False
        d = self.domains[name]
        for k, v in kwargs.items():
            if hasattr(d, k):
                setattr(d, k, v)
        self.save()
        return True

    def remove_domain(self, name: str):
        if name in self.domains:
            # 移除域内的产品
            for pid in self.domains[name].products:
                self.products.pop(pid, None)
            del self.domains[name]
            self.save()

    # ==================== 数据产品 ====================

    def create_product(self, name: str, domain: str,
                       description: str = '', owner: str = '',
                       output_type: str = 'table',
                       output_ref: str = '',
                       output_schema: List[Dict] = None,
                       tags: List[str] = None,
                       tier: str = 'internal') -> DataProduct:
        if domain not in self.domains:
            raise ValueError(f"域 {domain} 不存在")

        p = DataProduct(
            name=name,
            domain=domain,
            description=description,
            owner=owner,
            team=self.domains[domain].team_members[0]
                 if self.domains[domain].team_members else '',
            output_type=output_type,
            output_ref=output_ref,
            output_schema=output_schema or [],
            tags=tags or [],
            tier=tier,
        )
        self.products[p.product_id] = p
        self.domains[domain].products.append(p.product_id)
        self.save()
        return p

    def update_product(self, product_id: str, **kwargs):
        p = self.products.get(product_id)
        if not p:
            return False
        for k, v in kwargs.items():
            if hasattr(p, k):
                setattr(p, k, v)
        p.updated_at = time.time()
        self.save()
        return True

    def remove_product(self, product_id: str):
        p = self.products.pop(product_id, None)
        if p:
            if p.domain in self.domains:
                lst = self.domains[p.domain].products
                if product_id in lst:
                    lst.remove(product_id)
            self.save()

    def search_products(self, keyword: str = '',
                        domain: str = '',
                        tier: str = '',
                        tags: List[str] = None) -> List[DataProduct]:
        kw = keyword.lower()
        result = []
        for p in self.products.values():
            if p.status == 'draft':
                continue
            if kw and kw not in (p.name + p.description).lower():
                if not any(kw in t.lower() for t in p.tags):
                    continue
            if domain and p.domain != domain:
                continue
            if tier and p.tier != tier:
                continue
            if tags and not any(t in p.tags for t in tags):
                continue
            result.append(p)
        return sorted(result, key=lambda x: (
            -x.consumer_count, -x.query_count))

    # ==================== 产品契约 ====================

    def create_contract(self, producer_pid: str, consumer_pid: str,
                        sla: Dict = None) -> ProductContract:
        producer = self.products.get(producer_pid)
        consumer = self.products.get(consumer_pid)
        if not producer or not consumer:
            raise ValueError("产品不存在")

        c = ProductContract(
            producer_product=producer_pid,
            consumer_product=consumer_pid,
            producer_domain=producer.domain,
            consumer_domain=consumer.domain,
            interface={
                'schema': producer.output_schema,
                'output_type': producer.output_type,
            },
            sla=sla or {
                'freshness': 3600,
                'availability': 0.99,
            },
        )
        self.contracts[c.contract_id] = c

        # 更新域依赖
        pd = self.domains.get(producer.domain)
        cd = self.domains.get(consumer.domain)
        if pd and cd and pd.name != cd.name:
            if cd.name not in pd.downstream_domains:
                pd.downstream_domains.append(cd.name)
            if pd.name not in cd.upstream_domains:
                cd.upstream_domains.append(pd.name)

        self.save()
        return c

    def break_contract(self, contract_id: str, reason: str = ''):
        c = self.contracts.get(contract_id)
        if c:
            c.status = 'broken'
            c.violations.append({
                'ts': time.time(),
                'reason': reason,
            })
            self.save()

    # ==================== 域地图 ====================

    def domain_map(self) -> Dict:
        """返回域之间的关系图"""
        nodes = []
        edges = []

        for name, d in self.domains.items():
            # 统计
            product_count = len(d.products)
            total_consumers = sum(
                self.products[pid].consumer_count
                for pid in d.products if pid in self.products)
            total_queries = sum(
                self.products[pid].query_count
                for pid in d.products if pid in self.products)

            # 域间契约数
            inflow = sum(
                1 for c in self.contracts.values()
                if c.consumer_domain == name and c.status == 'active')
            outflow = sum(
                1 for c in self.contracts.values()
                if c.producer_domain == name and c.status == 'active')

            nodes.append({
                'name': name,
                'owner': d.owner,
                'product_count': product_count,
                'autonomy': d.autonomy_level,
                'consumers': total_consumers,
                'queries': total_queries,
                'inflow': inflow,
                'outflow': outflow,
            })

        # 边：域间契约
        domain_pairs = {}
        for c in self.contracts.values():
            if c.status != 'active':
                continue
            key = (c.producer_domain, c.consumer_domain)
            if key not in domain_pairs:
                domain_pairs[key] = 0
            domain_pairs[key] += 1

        for (a, b), count in domain_pairs.items():
            edges.append({
                'from': a,
                'to': b,
                'count': count,
            })

        return {'nodes': nodes, 'edges': edges}

    def product_lineage(self, product_id: str, depth: int = 3) -> Dict:
        """产品血缘"""
        # 上游
        upstream = []
        visited = set()

        def walk_up(pid, level):
            if level > depth or pid in visited:
                return
            visited.add(pid)
            p = self.products.get(pid)
            if not p:
                return
            for inp in p.inputs:
                src_pid = inp.get('product_id')
                if src_pid and src_pid in self.products:
                    sp = self.products[src_pid]
                    upstream.append({
                        'product_id': src_pid,
                        'name': sp.name,
                        'domain': sp.domain,
                        'level': level,
                    })
                    walk_up(src_pid, level + 1)

        walk_up(product_id, 1)

        # 下游
        downstream = []
        visited2 = set()

        def walk_down(pid, level):
            if level > depth or pid in visited2:
                return
            visited2.add(pid)
            for other in self.products.values():
                for inp in other.inputs:
                    if inp.get('product_id') == pid:
                        downstream.append({
                            'product_id': other.product_id,
                            'name': other.name,
                            'domain': other.domain,
                            'level': level,
                        })
                        walk_down(other.product_id, level + 1)
                        break

        walk_down(product_id, 1)

        return {
            'product_id': product_id,
            'upstream': upstream,
            'downstream': downstream,
        }

    def stats(self) -> dict:
        active_products = [p for p in self.products.values()
                            if p.status == 'active']
        by_tier = {}
        for p in active_products:
            by_tier[p.tier] = by_tier.get(p.tier, 0) + 1
        by_domain = {n: len(d.products)
                      for n, d in self.domains.items()}
        return {
            'total_domains': len(self.domains),
            'total_products': len(self.products),
            'active_products': len(active_products),
            'total_contracts': len(self.contracts),
            'active_contracts': sum(
                1 for c in self.contracts.values()
                if c.status == 'active'),
            'broken_contracts': sum(
                1 for c in self.contracts.values()
                if c.status == 'broken'),
            'by_tier': by_tier,
            'by_domain': by_domain,
        }