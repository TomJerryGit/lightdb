"""
覆盖索引扫描（Index Only Scan）
- 当 SELECT 的列都在索引中时，不需要回表
- 我们简化实现：主键查询只用 B+Tree
"""
from ..sql.plan import SeqScan, IndexScan


class IndexOnlyScanOptimizer:
    """判断是否可用覆盖索引"""

    @staticmethod
    def can_use_index_only(parsed, tbl) -> bool:
        """
        简化判断：如果只查询主键列且用主键过滤 → 可走 index only
        """
        if parsed.get('command') != 'SELECT':
            return False
        cols = parsed.get('columns', [])
        if cols == ['*']:
            return False

        pk_col = tbl.pk_col
        # 所有查询列必须都在主键中（简化：只查 pk）
        for c in cols:
            name = c.strip().split('.')[-1]
            if name != pk_col:
                return False

        # WHERE 必须是主键等值
        where = parsed.get('where')
        if not where or 'column' not in where:
            return False
        if where['column'].split('.')[-1] != pk_col:
            return False
        return where['op'] == '='

    @staticmethod
    def execute_index_only(tbl, pk_value):
        """仅返回主键（不回表）"""
        if tbl.bt.search(pk_value) is None:
            return None
        return [pk_value]