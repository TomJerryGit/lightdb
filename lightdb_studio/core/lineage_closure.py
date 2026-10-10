"""
血缘闭包
- 影响度评估（P0/P1/P2）
- 变更风险评估
- 自动测试生成
"""
import json
import re
import time
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Set, Optional


@dataclass
class ImpactItem:
    table: str
    column: str = ""
    level: str = "P2"       # P0 / P1 / P2
    depth: int = 1
    score: int = 0
    reason: str = ""
    path: List[str] = field(default_factory=list)


class LineageClosure:
    """血缘闭包分析器"""

    def __init__(self, catalog=None, lineage=None,
                 deep_lineage=None):
        self.catalog = catalog
        self.lineage = lineage
        self.deep_lineage = deep_lineage

    # ==================== 影响度评估 ====================

    def assess_impact(self, table: str, column: str = None) -> Dict:
        """
        评估修改某列/表的影响
        返回影响清单 + 风险等级
        """
        # 收集所有下游
        downstream = self._collect_downstream(table, column)
        upstream = self._collect_upstream(table, column)

        # 分级
        for item in downstream:
            item['level'] = self._level_by_criteria(item)
            item['score'] = self._impact_score(item)

        # 整体风险
        risk = self._overall_risk(downstream)

        # 生成建议
        advice = self._generate_advice(table, column, downstream, risk)

        return {
            'table': table,
            'column': column,
            'downstream': downstream,
            'upstream': upstream,
            'risk_level': risk['level'],
            'risk_score': risk['score'],
            'p0_count': sum(1 for i in downstream if i['level'] == 'P0'),
            'p1_count': sum(1 for i in downstream if i['level'] == 'P1'),
            'p2_count': sum(1 for i in downstream if i['level'] == 'P2'),
            'advice': advice,
        }

    def _collect_downstream(self, table: str,
                            column: str = None) -> List[Dict]:
        """收集所有下游"""
        result = []
        visited = set()

        def walk(t, c, depth, path):
            if depth > 6:
                return
            key = f"{t}.{c}" if c else t
            if key in visited:
                return
            visited.add(key)

            # 从增强血缘获取
            if self.lineage and column:
                for e in self.lineage.edges:
                    if e.from_table == t and e.from_column == c:
                        new_path = path + [f"{e.to_table}.{e.to_column}"]
                        result.append({
                            'table': e.to_table,
                            'column': e.to_column,
                            'depth': depth,
                            'transform': e.transform,
                            'path': new_path,
                            'sql': e.sql_preview,
                        })
                        walk(e.to_table, e.to_column, depth + 1, new_path)
            elif self.deep_lineage:
                for e in self.deep_lineage.edges:
                    if e.get('from') == t:
                        new_path = path + [e.get('to')]
                        result.append({
                            'table': e.get('to'),
                            'column': '',
                            'depth': depth,
                            'transform': e.get('type', ''),
                            'path': new_path,
                        })
                        walk(e.get('to'), None, depth + 1, new_path)
            elif self.lineage:
                for e in self.lineage.edges:
                    if e.from_table == t:
                        new_path = path + [e.to_table]
                        result.append({
                            'table': e.to_table,
                            'column': e.to_column,
                            'depth': depth,
                            'transform': e.transform,
                            'path': new_path,
                            'sql': e.sql_preview,
                        })
                        walk(e.to_table, e.to_column, depth + 1, new_path)

        walk(table, column, 1, [f"{table}.{column}" if column else table])
        return result

    def _collect_upstream(self, table: str, column: str = None) -> List[Dict]:
        """收集上游来源"""
        if not self.lineage:
            return []
        if column:
            return self.lineage.upstream_of_column(table, column, 5)
        return []

    def _level_by_criteria(self, item: Dict) -> str:
        """根据多个因素判定优先级"""
        # 因素：
        # 1. 深度近 P0/P1
        # 2. 是否为输出表（sink）
        # 3. 是否有下游（度数）
        # 4. 表重要度（域/敏感度）

        score = 0

        depth = item.get('depth', 1)
        if depth == 1:
            score += 5
        elif depth == 2:
            score += 3
        elif depth == 3:
            score += 1

        # 检查是否下游还有依赖
        t = item['table']
        has_downstream = False
        if self.lineage:
            for e in self.lineage.edges:
                if e.from_table == t:
                    has_downstream = True
                    break
        if not has_downstream:
            score += 3   # 叶子节点，可能是最终报表

        # 检查是否有消费者契约
        # 简化：优先高敏感度
        if self.catalog:
            for key, meta in self.catalog.tables.items():
                if meta.name == t or key.endswith(f'.{t}'):
                    if meta.sensitivity in ('pii', 'restricted'):
                        score += 4
                    elif meta.sensitivity == 'confidential':
                        score += 2
                    break

        if score >= 8:
            return 'P0'
        elif score >= 5:
            return 'P1'
        return 'P2'

    def _impact_score(self, item: Dict) -> int:
        """影响分数 0-100"""
        score = 30
        depth = item.get('depth', 1)
        if depth == 1:
            score += 40
        elif depth == 2:
            score += 20
        elif depth == 3:
            score += 10
        else:
            score += 5

        # 有 SQL 说明是实际写入
        if item.get('sql'):
            score += 15

        return min(100, score)

    def _overall_risk(self, downstream: List[Dict]) -> Dict:
        if not downstream:
            return {'level': 'low', 'score': 0}

        # 权重
        p0 = sum(1 for i in downstream if i.get('level') == 'P0')
        p1 = sum(1 for i in downstream if i.get('level') == 'P1')
        p2 = sum(1 for i in downstream if i.get('level') == 'P2')

        score = min(100, p0 * 25 + p1 * 10 + p2 * 3)

        if score >= 60:
            level = 'high'
        elif score >= 30:
            level = 'medium'
        else:
            level = 'low'

        return {'level': level, 'score': score}

    def _generate_advice(self, table: str, column: str,
                         downstream: List[Dict],
                         risk: Dict) -> List[str]:
        """生成建议"""
        advice = []

        if risk['level'] == 'high':
            advice.append("⚠️ 高风险变更，需要正式变更流程")
            advice.append("1. 提交 SQL 审查（review workflow）")
            advice.append("2. 通知所有下游负责人")
            advice.append("3. 在测试环境完整回归")
            advice.append("4. 准备回滚方案")
        elif risk['level'] == 'medium':
            advice.append("⚡ 中等风险，建议谨慎操作")
            advice.append("1. 通知 P0 下游负责人")
            advice.append("2. 在测试环境验证")
        else:
            advice.append("✅ 风险较低，可正常操作")

        # 具体建议
        p0_tables = {i['table'] for i in downstream
                      if i.get('level') == 'P0'}
        if p0_tables:
            advice.append(f"P0 影响表: {', '.join(list(p0_tables)[:5])}")

        return advice

    # ==================== 测试生成 ====================

    def generate_tests(self, table: str,
                       column: str = None) -> List[Dict]:
        """根据血缘生成回归测试用例"""
        impact = self.assess_impact(table, column)
        tests = []

        # 基础测试
        tests.append({
            'name': f'检查 {table} 行数变化',
            'type': 'row_count',
            'target': table,
            'sql': f"SELECT COUNT(*) FROM `{table}`",
            'baseline': None,
        })

        # 下游测试
        for item in impact['downstream'][:20]:
            t = item['table']
            if not t:
                continue
            tests.append({
                'name': f'下游 {t} 数据一致性',
                'type': 'consistency',
                'target': t,
                'level': item.get('level', 'P2'),
                'sql': f"SELECT COUNT(*) FROM `{t}`",
                'path': item.get('path', []),
            })

        # 值域检查
        if column:
            tests.append({
                'name': f'{table}.{column} 值域检查',
                'type': 'value_range',
                'target': table,
                'column': column,
                'sql': (f"SELECT MIN(`{column}`), MAX(`{column}`), "
                        f"COUNT(*) FROM `{table}`"),
                'baseline': None,
            })

        return tests

    # ==================== 变更模拟 ====================

    def simulate_change(self, table: str, column: str,
                        change_type: str) -> Dict:
        """
        模拟变更并估算影响
        change_type: drop / rename / type_change / nullable_change
        """
        impact = self.assess_impact(table, column)

        # 按变更类型调整风险
        multipliers = {
            'drop': 1.5,
            'rename': 1.3,
            'type_change': 1.2,
            'nullable_change': 0.8,
            'add': 0.3,
        }
        mult = multipliers.get(change_type, 1.0)

        adjusted_score = min(100, int(impact['risk_score'] * mult))
        if adjusted_score >= 70:
            adjusted_level = 'critical'
        elif adjusted_score >= 45:
            adjusted_level = 'high'
        elif adjusted_score >= 20:
            adjusted_level = 'medium'
        else:
            adjusted_level = 'low'

        # 潜在失败场景
        scenarios = []
        if change_type == 'drop':
            scenarios.append("下游 INSERT/UPDATE 会报错")
            scenarios.append("SELECT 引用该列会失败")
        elif change_type == 'rename':
            scenarios.append("下游硬编码列名会失败")
            scenarios.append("视图/存储过程需重建")
        elif change_type == 'type_change':
            scenarios.append("数据可能截断或溢出")
            scenarios.append("索引/主键受影响")
        elif change_type == 'nullable_change':
            scenarios.append("NOT NULL 约束可能导致插入失败")

        # 需要重建的对象
        rebuild_targets = []
        for item in impact['downstream']:
            if self.deep_lineage:
                obj = self.deep_lineage.get_object(item['table'])
                if obj and obj.obj_type in ('view', 'procedure'):
                    rebuild_targets.append({
                        'name': item['table'],
                        'type': obj.obj_type,
                    })

        return {
            'change_type': change_type,
            'table': table,
            'column': column,
            'original_risk': impact['risk_score'],
            'adjusted_risk': adjusted_score,
            'adjusted_level': adjusted_level,
            'scenarios': scenarios,
            'rebuild_targets': rebuild_targets,
            'p0_count': impact['p0_count'],
            'advice': impact['advice'],
        }

    # ==================== 统计 ====================

    def stats(self) -> dict:
        total_edges = 0
        if self.lineage:
            total_edges += len(self.lineage.edges)
        if self.deep_lineage:
            total_edges += len(self.deep_lineage.edges)
        return {
            'total_edges': total_edges,
            'has_enhanced_lineage': self.lineage is not None,
            'has_deep_lineage': self.deep_lineage is not None,
        }