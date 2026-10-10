"""
数据产品运营
- 产品健康度评分
- 使用分析
- 价值评估
"""
import time
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional


@dataclass
class HealthScore:
    """健康度评分"""
    product_id: str
    overall: int = 0
    dimensions: Dict = field(default_factory=dict)
    # {
    #   'usage': int,      # 使用活跃度 0-100
    #   'quality': int,    # 数据质量 0-100
    #   'freshness': int,  # 数据新鲜度 0-100
    #   'documentation': int,  # 文档完善度
    #   'ownership': int,  # 负责人明确度
    # }
    issues: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    computed_at: float = 0

    def __post_init__(self):
        if not self.computed_at:
            self.computed_at = time.time()


@dataclass
class UsageMetrics:
    """使用指标"""
    product_id: str
    # 使用
    query_count: int = 0
    unique_users: int = 0
    # 时间
    last_used: float = 0
    usage_trend: float = 0   # 相比上期变化百分比
    # 分布
    by_user: Dict = field(default_factory=dict)
    by_hour: Dict = field(default_factory=dict)


@dataclass
class ValueAssessment:
    """价值评估"""
    product_id: str
    # 价值维度
    consumption_score: int = 0      # 消费量
    diversity_score: int = 0        # 使用方多样性
    criticality_score: int = 0      # 关键性（下游依赖数）
    replaceability_score: int = 0   # 可替代性
    # 综合
    total_value: int = 0
    tier: str = "standard"          # strategic / important / standard / low


class ProductOpsEngine:
    """数据产品运营引擎"""

    def __init__(self, mesh_mgr=None, catalog=None):
        self.mesh = mesh_mgr
        self.catalog = catalog
        self.health_cache: Dict[str, HealthScore] = {}

    # ==================== 健康度 ====================

    def compute_health(self, product_id: str) -> HealthScore:
        """计算产品健康度"""
        if not self.mesh:
            return HealthScore(product_id=product_id)

        product = self.mesh.products.get(product_id)
        if not product:
            return HealthScore(product_id=product_id,
                                issues=['产品不存在'])

        score = HealthScore(product_id=product_id)

        # 1. 使用活跃度
        usage_score = 100
        if product.query_count == 0:
            usage_score = 0
            score.issues.append("从未被查询")
        elif product.last_used > 0:
            days_idle = (time.time() - product.last_used) / 86400
            if days_idle > 30:
                usage_score = 30
                score.issues.append(f"已闲置 {int(days_idle)} 天")
            elif days_idle > 7:
                usage_score = 70
            # 消费者数
            if product.consumer_count < 1:
                usage_score = max(0, usage_score - 30)

        score.dimensions['usage'] = usage_score

        # 2. 数据质量（从 catalog 推断）
        quality_score = 100
        if self.catalog:
            for key, meta in self.catalog.tables.items():
                if meta.name == product.output_ref.split('.')[-1] or \
                   key == product.output_ref:
                    # 敏感度
                    if meta.sensitivity == 'pii' and not product.tags:
                        quality_score -= 10
                    # 主键
                    has_pk = any(c.is_primary for c in meta.columns)
                    if not has_pk:
                        quality_score -= 20
                        score.issues.append("输出表无主键")
                    break

        score.dimensions['quality'] = max(0, quality_score)

        # 3. 数据新鲜度
        freshness_score = 100
        if product.sla:
            expected_freshness = product.sla.get('freshness', 3600)
            actual_age = time.time() - product.updated_at
            if actual_age > expected_freshness * 2:
                freshness_score = 30
                score.issues.append("数据超期未更新")
            elif actual_age > expected_freshness:
                freshness_score = 70

        score.dimensions['freshness'] = freshness_score

        # 4. 文档完善度
        doc_score = 0
        if product.description:
            doc_score += 40
        if product.output_schema:
            doc_score += 30
        if product.tags:
            doc_score += 30

        score.dimensions['documentation'] = doc_score
        if doc_score < 50:
            score.issues.append("文档不完善")

        # 5. 负责人
        owner_score = 100 if product.owner else 0
        if not product.owner:
            score.issues.append("未指定负责人")
        score.dimensions['ownership'] = owner_score

        # 综合
        weights = {
            'usage': 0.35,
            'quality': 0.25,
            'freshness': 0.15,
            'documentation': 0.15,
            'ownership': 0.1,
        }
        total = sum(score.dimensions[k] * weights[k]
                     for k in weights)
        score.overall = int(total)

        # 建议
        if usage_score < 50:
            score.recommendations.append(
                "推动产品使用：联系潜在消费者")
        if doc_score < 70:
            score.recommendations.append(
                "补充业务文档和字段说明")
        if not product.owner:
            score.recommendations.append(
                "指定产品负责人")

        self.health_cache[product_id] = score
        return score

    # ==================== 使用分析 ====================

    def analyze_usage(self, product_id: str,
                      usage_records: List[Dict] = None) -> UsageMetrics:
        """分析使用情况"""
        metrics = UsageMetrics(product_id=product_id)
        if not usage_records:
            return metrics

        # 过滤当前产品的记录
        records = [r for r in usage_records
                    if r.get('product_id') == product_id]
        if not records:
            return metrics

        metrics.query_count = len(records)
        users = {r.get('user') for r in records if r.get('user')}
        metrics.unique_users = len(users)

        # 时间
        metrics.last_used = max(r.get('ts', 0) for r in records)

        # 用户分布
        by_user = {}
        for r in records:
            u = r.get('user', 'unknown')
            by_user[u] = by_user.get(u, 0) + 1
        metrics.by_user = by_user

        # 时段分布
        import time as _t
        by_hour = {}
        for r in records:
            try:
                h = _t.localtime(r.get('ts', 0)).tm_hour
                by_hour[h] = by_hour.get(h, 0) + 1
            except Exception:
                pass
        metrics.by_hour = by_hour

        return metrics

    # ==================== 价值评估 ====================

    def assess_value(self, product_id: str) -> ValueAssessment:
        """评估产品价值"""
        va = ValueAssessment(product_id=product_id)
        if not self.mesh:
            return va

        product = self.mesh.products.get(product_id)
        if not product:
            return va

        # 1. 消费量
        if product.query_count > 1000:
            va.consumption_score = 100
        elif product.query_count > 500:
            va.consumption_score = 80
        elif product.query_count > 100:
            va.consumption_score = 60
        elif product.query_count > 10:
            va.consumption_score = 40
        else:
            va.consumption_score = 20

        # 2. 使用方多样性
        if product.consumer_count >= 10:
            va.diversity_score = 100
        elif product.consumer_count >= 5:
            va.diversity_score = 80
        elif product.consumer_count >= 2:
            va.diversity_score = 60
        elif product.consumer_count >= 1:
            va.diversity_score = 40
        else:
            va.diversity_score = 0

        # 3. 关键性（下游依赖数）
        downstream_count = 0
        for p in self.mesh.products.values():
            for inp in p.inputs:
                if inp.get('product_id') == product_id:
                    downstream_count += 1
                    break

        if downstream_count >= 5:
            va.criticality_score = 100
        elif downstream_count >= 3:
            va.criticality_score = 80
        elif downstream_count >= 1:
            va.criticality_score = 60
        else:
            va.criticality_score = 20

        # 4. 可替代性（高 = 容易替代 = 低价值）
        # 同域有多个同类产品 → 可替代
        same_domain = [p for p in self.mesh.products.values()
                        if p.domain == product.domain and
                        p.product_id != product_id]
        if len(same_domain) >= 3:
            va.replaceability_score = 40   # 容易替代 = 价值低
        elif len(same_domain) >= 1:
            va.replaceability_score = 70
        else:
            va.replaceability_score = 100  # 独特 = 价值高

        # 综合
        weights = {
            'consumption': 0.3,
            'diversity': 0.25,
            'criticality': 0.35,
            'replaceability': 0.1,
        }
        total = (
            va.consumption_score * weights['consumption'] +
            va.diversity_score * weights['diversity'] +
            va.criticality_score * weights['criticality'] +
            va.replaceability_score * weights['replaceability']
        )
        va.total_value = int(total)

        # 分级
        if va.total_value >= 80:
            va.tier = 'strategic'
        elif va.total_value >= 60:
            va.tier = 'important'
        elif va.total_value >= 40:
            va.tier = 'standard'
        else:
            va.tier = 'low'

        return va

    # ==================== 组合报告 ====================

    def generate_report(self) -> str:
        """生成运营报告"""
        if not self.mesh:
            return "# 数据产品运营报告\n\n未配置 Mesh 管理器"

        lines = [
            "# 数据产品运营报告",
            f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"## 概览",
            f"- 产品总数: {len(self.mesh.products)}",
            f"- 域总数: {len(self.mesh.domains)}",
            "",
            "## 健康度排行",
            "",
            "| 产品 | 域 | 健康度 | 使用 | 质量 | 新鲜度 |",
            "|------|-----|--------|------|------|--------|",
        ]

        # 计算所有健康度
        healths = []
        for pid, p in self.mesh.products.items():
            h = self.compute_health(pid)
            healths.append((h.overall, h, p))

        healths.sort(key=lambda x: -x[0])

        for overall, h, p in healths[:20]:
            d = h.dimensions
            lines.append(
                f"| {p.name} | {p.domain} | {overall} | "
                f"{d.get('usage', 0)} | "
                f"{d.get('quality', 0)} | "
                f"{d.get('freshness', 0)} |")

        lines.append("")
        lines.append("## 价值排行")
        lines.append("")

        values = []
        for pid, p in self.mesh.products.items():
            va = self.assess_value(pid)
            values.append((va.total_value, va, p))

        values.sort(key=lambda x: -x[0])

        lines.append("| 产品 | 价值 | 分级 | 消费 | 关键性 |")
        lines.append("|------|------|------|------|--------|")
        for tv, va, p in values[:20]:
            lines.append(
                f"| {p.name} | {tv} | {va.tier} | "
                f"{va.consumption_score} | {va.criticality_score} |")

        return '\n'.join(lines)

    def stats(self) -> dict:
        if not self.mesh:
            return {}
        healths = [self.compute_health(pid)
                    for pid in self.mesh.products.keys()]
        values = [self.assess_value(pid)
                   for pid in self.mesh.products.keys()]

        avg_health = sum(h.overall for h in healths) / len(healths) \
            if healths else 0

        tiers = {}
        for v in values:
            tiers[v.tier] = tiers.get(v.tier, 0) + 1

        return {
            'total_products': len(self.mesh.products),
            'avg_health': round(avg_health, 1),
            'critical_products': sum(1 for h in healths if h.overall < 50),
            'value_tiers': tiers,
        }