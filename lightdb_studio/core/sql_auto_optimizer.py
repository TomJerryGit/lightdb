"""
SQL 端到端自动优化
- 检测 → 分析 → 改写 → 验证
- 自动创建索引建议
- 生成优化前后对比报告
"""
import re
import time
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional, Callable


@dataclass
class OptimizationStep:
    """优化步骤"""
    step_type: str          # analyze / rewrite / index / verify
    description: str
    original: str = ""
    optimized: str = ""
    reason: str = ""
    estimated_gain: float = 0   # 百分比


@dataclass
class OptimizationResult:
    """优化结果"""
    original_sql: str
    optimized_sql: str = ""
    steps: List[OptimizationStep] = field(default_factory=list)
    # 指标
    original_score: int = 0
    optimized_score: int = 0
    estimated_speedup: float = 1.0
    # 索引建议
    index_suggestions: List[Dict] = field(default_factory=list)
    # 警告
    warnings: List[str] = field(default_factory=list)
    # 时间
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


class SQLAutoOptimizer:
    """端到端 SQL 优化器"""

    def __init__(self, conn=None, catalog=None, lineage=None):
        self.conn = conn
        self.catalog = catalog
        self.lineage = lineage
        self.history: List[OptimizationResult] = []

    # ==================== 主流程 ====================

    def optimize(self, sql: str, execute: bool = False) -> OptimizationResult:
        """
        主优化入口
        1. 静态分析
        2. 规则改写
        3. 索引建议
        4. 验证（可选执行）
        """
        result = OptimizationResult(original_sql=sql)

        # Step 1: 分析
        analysis = self._analyze(sql)
        result.original_score = analysis['score']
        result.steps.append(OptimizationStep(
            step_type='analyze',
            description='SQL 静态分析',
            original=sql,
            reason='; '.join(analysis['issues']),
        ))

        # Step 2: 改写
        rewritten, rewrites = self._rewrite(sql)
        result.optimized_sql = rewritten
        for r in rewrites:
            result.steps.append(OptimizationStep(
                step_type='rewrite',
                description=r['description'],
                original=r['before'],
                optimized=r['after'],
                reason=r['reason'],
                estimated_gain=r.get('gain', 0),
            ))

        # Step 3: 索引建议
        result.index_suggestions = self._suggest_indexes(rewritten)

        # Step 4: 验证
        if execute and self.conn:
            try:
                verify = self._verify(sql, rewritten)
                result.optimized_score = verify['optimized_score']
                result.estimated_speedup = verify.get('speedup', 1.0)
                result.steps.append(OptimizationStep(
                    step_type='verify',
                    description='执行验证',
                    original=f"{verify['original_ms']:.1f}ms",
                    optimized=f"{verify['optimized_ms']:.1f}ms",
                    reason=f"加速 {verify.get('speedup', 1.0):.2f}x",
                ))
            except Exception as e:
                result.warnings.append(f"验证失败: {e}")
        else:
            # 未执行时用静态估算
            result.optimized_score = min(100,
                result.original_score + len(rewrites) * 10)

        self.history.append(result)
        return result

    # ==================== 分析 ====================

    def _analyze(self, sql: str) -> Dict:
        """静态分析"""
        issues = []
        score = 100
        up = sql.upper()

        if re.search(r'SELECT\s+\*', up):
            issues.append("使用 SELECT *")
            score -= 10

        if re.search(r"LIKE\s+'%", up):
            issues.append("前置通配 LIKE")
            score -= 15

        if re.search(r'WHERE\s+\w+\s*\([^)]*\)\s*=', up):
            issues.append("WHERE 中函数包裹列")
            score -= 15

        if re.search(r"=\s*'[0-9]+'", sql):
            issues.append("数字被引号包裹（隐式转换）")
            score -= 10

        or_count = len(re.findall(r'\bOR\b', up))
        if or_count >= 2:
            issues.append(f"{or_count} 处 OR 连接")
            score -= or_count * 3

        if up.startswith('SELECT') and 'LIMIT' not in up and \
           'COUNT(' not in up and 'GROUP BY' not in up:
            issues.append("无 LIMIT 的大表扫描")
            score -= 5

        if re.search(r'\bNOT\s+IN\b', up):
            issues.append("NOT IN 性能差")
            score -= 10

        subquery_depth = up.count('SELECT') - 1
        if subquery_depth >= 2:
            issues.append(f"{subquery_depth} 层子查询")
            score -= 8

        return {
            'score': max(0, score),
            'issues': issues,
        }

    # ==================== 改写规则 ====================

    def _rewrite(self, sql: str) -> tuple:
        """规则化改写"""
        rewrites = []
        current = sql

        # 1. SELECT * → 明确列
        if re.search(r'SELECT\s+\*', current, re.IGNORECASE):
            # 尝试从 catalog 推断
            columns = self._infer_columns(current)
            if columns:
                new = re.sub(
                    r'SELECT\s+\*',
                    'SELECT ' + ', '.join(columns),
                    current, count=1, flags=re.IGNORECASE)
                rewrites.append({
                    'description': '展开 SELECT *',
                    'before': 'SELECT *',
                    'after': f"SELECT {', '.join(columns)}",
                    'reason': '避免不必要的大字段读取',
                    'gain': 20,
                })
                current = new

        # 2. NOT IN → NOT EXISTS
        not_in_m = re.search(
            r'(\w+)\s+NOT\s+IN\s*\(\s*SELECT\s+(\w+)\s+FROM\s+(\w+)(.*?)\)',
            current, re.IGNORECASE | re.DOTALL)
        if not_in_m:
            col, sub_col, sub_table, rest = not_in_m.groups()
            replacement = (f"NOT EXISTS (SELECT 1 FROM {sub_table} "
                            f"WHERE {sub_table}.{sub_col} = {col}{rest})")
            new = current[:not_in_m.start()] + replacement + \
                current[not_in_m.end():]
            rewrites.append({
                'description': 'NOT IN 改写为 NOT EXISTS',
                'before': not_in_m.group(0)[:100],
                'after': replacement[:100],
                'reason': 'NOT EXISTS 更高效且正确处理 NULL',
                'gain': 30,
            })
            current = new

        # 3. OR 展开为 UNION ALL
        # SELECT * FROM t WHERE a=1 OR a=2 → SELECT * FROM t WHERE a IN (1,2)
        or_m = re.search(
            r'(\w+)\s*=\s*(\'[^\']*\'|[0-9]+)\s+OR\s+\1\s*=\s*(\'[^\']*\'|[0-9]+)',
            current, re.IGNORECASE)
        if or_m:
            col, v1, v2 = or_m.groups()
            replacement = f"{col} IN ({v1}, {v2})"
            new = current[:or_m.start()] + replacement + \
                current[or_m.end():]
            rewrites.append({
                'description': '同列 OR 改写为 IN',
                'before': or_m.group(0),
                'after': replacement,
                'reason': 'IN 通常比 OR 更快',
                'gain': 10,
            })
            current = new

        # 4. 去除函数包裹
        func_m = re.search(
            r'WHERE\s+(\w+)\s*\(\s*(\w+)\s*\)\s*=\s*(\'[^\']*\'|[0-9]+)',
            current, re.IGNORECASE)
        if func_m:
            fn, col, val = func_m.groups()
            if fn.upper() == 'DATE':
                # DATE(col) = 'x' → col >= 'x 00:00:00' AND col < 'x+1d'
                # 简单版本：直接用 >= 和 <
                val_str = val.strip("'")
                replacement = (f"WHERE {col} >= '{val_str}' "
                                f"AND {col} < DATE_ADD('{val_str}', "
                                f"INTERVAL 1 DAY)")
                new = re.sub(
                    r'WHERE\s+\w+\s*\(\s*\w+\s*\)\s*=\s*\'[^\']*\'',
                    replacement, current, count=1, flags=re.IGNORECASE)
                rewrites.append({
                    'description': '函数包裹列改为范围条件',
                    'before': func_m.group(0),
                    'after': replacement,
                    'reason': '函数包裹导致索引失效',
                    'gain': 40,
                })
                current = new

        # 5. 补全 LIMIT
        if current.strip().upper().startswith('SELECT') and \
           ' LIMIT ' not in current.upper() and \
           ' COUNT(' not in current.upper() and \
           ' GROUP BY ' not in current.upper():
            current = current.rstrip().rstrip(';') + ' LIMIT 1000'
            rewrites.append({
                'description': '追加 LIMIT 保护',
                'before': '(无 LIMIT)',
                'after': 'LIMIT 1000',
                'reason': '避免全表返回',
                'gain': 5,
            })

        return current, rewrites

    def _infer_columns(self, sql: str) -> List[str]:
        """从 catalog 推断 SELECT 列"""
        if not self.catalog:
            return []

        # 找 FROM 表
        m = re.search(r'\bFROM\s+[`"]?(\w+)[`"]?',
                       sql, re.IGNORECASE)
        if not m:
            return []
        table = m.group(1)

        # 从 catalog 找表
        for key, meta in self.catalog.tables.items():
            if meta.name == table:
                # 排除大字段（TEXT/BLOB/JSON）
                safe = []
                for c in meta.columns:
                    ct = (c.type or '').upper()
                    if 'TEXT' in ct or 'BLOB' in ct or 'JSON' in ct:
                        continue
                    if len(safe) >= 10:
                        break
                    safe.append(c.name)
                if safe:
                    return safe
        return []

    # ==================== 索引建议 ====================

    def _suggest_indexes(self, sql: str) -> List[Dict]:
        """索引建议"""
        suggestions = []

        # 提取表和 WHERE 列
        from_m = re.search(r'\bFROM\s+[`"]?(\w+)[`"]?',
                            sql, re.IGNORECASE)
        if not from_m:
            return []
        table = from_m.group(1)

        # WHERE 列
        where_cols = []
        wm = re.search(
            r'\bWHERE\s+(.+?)(?=\s+(?:GROUP|ORDER|LIMIT)\b|$)',
            sql, re.IGNORECASE | re.DOTALL)
        if wm:
            for cm in re.finditer(
                    r'[`"]?(\w+)[`"]?\s*(=|>|<|>=|<=|LIKE|IN)',
                    wm.group(1), re.IGNORECASE):
                c = cm.group(1)
                if c.upper() not in ('AND', 'OR', 'NOT'):
                    where_cols.append(c)

        # ORDER BY 列
        order_cols = []
        om = re.search(r'\bORDER\s+BY\s+([`"]?\w+[`"]?)',
                        sql, re.IGNORECASE)
        if om:
            order_cols.append(om.group(1).strip('`"'))

        # 建议
        if where_cols:
            cols = list(dict.fromkeys(where_cols))[:3]
            cols_str = ', '.join(f'`{c}`' for c in cols)
            suggestions.append({
                'table': table,
                'columns': cols,
                'sql': f"CREATE INDEX idx_{table}_{'_'.join(cols)} "
                        f"ON `{table}` ({cols_str})",
                'reason': f'WHERE 条件列 {cols} 未走索引',
                'priority': 'high',
            })

        if order_cols:
            cols = order_cols[:2]
            cols_str = ', '.join(f'`{c}`' for c in cols)
            suggestions.append({
                'table': table,
                'columns': cols,
                'sql': f"CREATE INDEX idx_{table}_order "
                        f"ON `{table}` ({cols_str})",
                'reason': 'ORDER BY 列索引可避免 filesort',
                'priority': 'medium',
            })

        # 检查是否已存在（从 catalog 或 conn）
        if self.conn:
            try:
                existing = set()
                cols, rows = self.conn.query(
                    f"SHOW INDEX FROM `{table}`")
                for r in rows:
                    try:
                        cn_idx = next(i for i, c in enumerate(cols)
                                       if c.lower() == 'column_name')
                        existing.add(r[cn_idx])
                    except (StopIteration, IndexError):
                        pass

                # 过滤已有
                filtered = []
                for s in suggestions:
                    if not all(c in existing for c in s['columns']):
                        filtered.append(s)
                return filtered
            except Exception:
                pass

        return suggestions

    # ==================== 验证 ====================

    def _verify(self, original: str, optimized: str) -> Dict:
        """执行验证"""
        import time

        # 执行原始
        t0 = time.time()
        try:
            _, rows_a = self.conn.query(original)
            original_ms = (time.time() - t0) * 1000
        except Exception as e:
            original_ms = -1

        # 执行优化后
        t0 = time.time()
        try:
            _, rows_b = self.conn.query(optimized)
            optimized_ms = (time.time() - t0) * 1000
        except Exception as e:
            optimized_ms = -1

        # 计算加速比
        speedup = 1.0
        if original_ms > 0 and optimized_ms > 0:
            speedup = original_ms / optimized_ms

        # 简单评分
        def _score(ms):
            if ms < 0: return 0
            if ms < 10: return 100
            if ms < 50: return 90
            if ms < 100: return 80
            if ms < 500: return 60
            if ms < 1000: return 40
            return 20

        return {
            'original_ms': original_ms,
            'optimized_ms': optimized_ms,
            'speedup': speedup,
            'original_score': _score(original_ms),
            'optimized_score': _score(optimized_ms),
        }

    def format_report(self, result: OptimizationResult) -> str:
        """格式化优化报告"""
        lines = [
            "# SQL 优化报告",
            f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 原始 SQL",
            "```sql",
            result.original_sql,
            "```",
            "",
            "## 优化后 SQL",
            "```sql",
            result.optimized_sql or result.original_sql,
            "```",
            "",
            f"## 评分变化",
            f"- 原始: **{result.original_score}**/100",
            f"- 优化后: **{result.optimized_score}**/100",
        ]

        if result.estimated_speedup > 1:
            lines.append(f"- 预估加速: **{result.estimated_speedup:.2f}x**")

        lines.append("")
        lines.append("## 优化步骤")
        lines.append("")

        for i, s in enumerate(result.steps, 1):
            if s.step_type == 'analyze':
                lines.append(f"### {i}. 分析")
                if s.reason:
                    lines.append(f"- 问题: {s.reason}")
            else:
                icon = {'rewrite': '✏️', 'index': '📇',
                        'verify': '✅'}.get(s.step_type, '•')
                lines.append(f"### {i}. {icon} {s.description}")
                lines.append(f"- 原因: {s.reason}")
                if s.estimated_gain:
                    lines.append(f"- 预估收益: {s.estimated_gain}%")
            lines.append("")

        if result.index_suggestions:
            lines.append("## 索引建议")
            lines.append("")
            for s in result.index_suggestions:
                lines.append(f"```sql\n{s['sql']}\n```")
                lines.append(f"原因: {s['reason']}")
                lines.append("")

        if result.warnings:
            lines.append("## 警告")
            for w in result.warnings:
                lines.append(f"- ⚠️ {w}")

        return '\n'.join(lines)