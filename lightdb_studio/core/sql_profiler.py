"""
SQL 性能分析器
- 自动 EXPLAIN
- 慢查询归因
- 索引推荐
- 执行计划对比
"""
import re
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class ProfileResult:
    sql: str
    elapsed_ms: float = 0
    rows_returned: int = 0
    rows_scanned: int = 0
    scan_type: str = ""            # ALL / index / ref / const / range
    index_used: str = ""
    plan: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    score: int = 100               # 0-100 性能评分

    def to_dict(self):
        return {
            'sql': self.sql,
            'elapsed_ms': self.elapsed_ms,
            'rows_returned': self.rows_returned,
            'rows_scanned': self.rows_scanned,
            'scan_type': self.scan_type,
            'index_used': self.index_used,
            'plan': self.plan,
            'warnings': self.warnings,
            'suggestions': self.suggestions,
            'score': self.score,
        }


class SQLProfiler:
    """SQL 性能分析器"""

    def __init__(self, conn):
        self.conn = conn

    # ==================== 主入口 ====================

    def profile(self, sql: str, execute: bool = True) -> ProfileResult:
        """分析 SQL"""
        result = ProfileResult(sql=sql)

        # 1. 静态分析
        self._static_analysis(sql, result)

        # 2. EXPLAIN
        try:
            self._explain(sql, result)
        except Exception as e:
            result.warnings.append(f"EXPLAIN 失败: {e}")

        # 3. 实际执行（可选）
        if execute:
            try:
                self._measure(sql, result)
            except Exception as e:
                result.warnings.append(f"执行失败: {e}")

        # 4. 索引推荐
        self._recommend_index(sql, result)

        # 5. 评分
        self._calc_score(result)

        return result

    # ==================== 静态分析 ====================

    def _static_analysis(self, sql: str, result: ProfileResult):
        up = sql.upper()

        # SELECT *
        if re.search(r'SELECT\s+\*', up):
            result.warnings.append("使用 SELECT * 会返回所有列，可能降低性能")
            result.suggestions.append(
                "明确列出需要的列，特别是大表或含 TEXT/BLOB 的表")

        # 前置 LIKE
        if re.search(r"LIKE\s+'%", up):
            result.warnings.append("前置通配符 LIKE '%...' 无法使用索引")
            result.suggestions.append(
                "考虑使用全文索引（FULLTEXT）或改用后缀匹配")

        # 函数包裹列
        if re.search(r'WHERE\s+\w+\s*\([^)]*\)\s*=', up):
            result.warnings.append("WHERE 中函数包裹列会导致索引失效")
            result.suggestions.append(
                "将函数移到比较值一侧，或使用生成列")

        # 隐式类型转换
        if re.search(r"=\s*'[0-9]+'", sql):
            result.warnings.append("数字列用字符串比较，可能导致索引失效")
            result.suggestions.append("去掉数字两侧的引号")

        # OR 条件
        or_count = len(re.findall(r'\bOR\b', up))
        if or_count >= 2:
            result.suggestions.append(
                f"发现 {or_count} 处 OR，建议改写为 UNION ALL 以提升性能")

        # 无 LIMIT 的 SELECT
        if up.startswith('SELECT') and 'LIMIT' not in up and \
           'COUNT(' not in up and 'GROUP BY' not in up:
            result.suggestions.append(
                "大表查询建议加 LIMIT 限制返回行数")

        # NOT IN
        if re.search(r'\bNOT\s+IN\b', up):
            result.warnings.append("NOT IN 子查询性能差，且不处理 NULL")
            result.suggestions.append(
                "改写为 LEFT JOIN ... WHERE ... IS NULL，或使用 NOT EXISTS")

        # ORDER BY 无 LIMIT
        if re.search(r'\bORDER\s+BY\b', up) and 'LIMIT' not in up:
            result.suggestions.append(
                "ORDER BY 加 LIMIT 才能发挥 Top-N 优化")

        # 子查询
        if up.count('SELECT') > 2:
            result.suggestions.append(
                "多层子查询考虑改写为 JOIN 或 CTE")

    # ==================== EXPLAIN ====================

    def _explain(self, sql: str, result: ProfileResult):
        if not sql.strip().upper().startswith(
                ('SELECT', 'UPDATE', 'DELETE', 'INSERT', 'WITH')):
            return

        try:
            cols, rows = self.conn.query("EXPLAIN " + sql)
        except Exception:
            # 有些数据库可能不支持
            return

        if not rows:
            return

        # 存储完整计划
        for row in rows:
            line = ' | '.join(
                str(v) if v is not None else '' for v in row)
            result.plan.append(line)

        # 提取关键信息（假设列顺序接近 MySQL）
        # id, select_type, table, type, possible_keys, key, key_len,
        # ref, rows, Extra
        try:
            first = rows[0]
            # 尝试智能定位
            col_map = {c.lower(): i for i, c in enumerate(cols)}

            type_idx = col_map.get('type')
            key_idx = col_map.get('key')
            rows_idx = col_map.get('rows')
            extra_idx = col_map.get('extra')

            if type_idx is not None and len(first) > type_idx:
                result.scan_type = str(first[type_idx] or '')

            if key_idx is not None and len(first) > key_idx:
                result.index_used = str(first[key_idx] or '')

            if rows_idx is not None and len(first) > rows_idx:
                try:
                    result.rows_scanned = int(first[rows_idx] or 0)
                except (ValueError, TypeError):
                    pass

            if extra_idx is not None and len(first) > extra_idx:
                extra = str(first[extra_idx] or '').upper()
                if 'USING FILESORT' in extra:
                    result.warnings.append("使用文件排序（filesort）")
                    result.suggestions.append(
                        "为 ORDER BY 的列添加索引")
                if 'USING TEMPORARY' in extra:
                    result.warnings.append("使用临时表")
                    result.suggestions.append(
                        "优化 GROUP BY / DISTINCT 字段的索引")
                if 'USING WHERE' in extra:
                    result.suggestions.append(
                        "存在 Using where，检查过滤条件是否走索引")
        except Exception:
            pass

        # 扫描类型告警
        if result.scan_type.upper() == 'ALL':
            result.warnings.append("全表扫描（type=ALL）")
            result.suggestions.append(
                "为 WHERE 涉及的列添加索引")
        elif result.scan_type.upper() in ('INDEX', 'ALL'):
            result.suggestions.append(
                "考虑使用覆盖索引避免回表")

    # ==================== 测量 ====================

    def _measure(self, sql: str, result: ProfileResult):
        t0 = time.time()
        cols, rows = self.conn.query(sql)
        elapsed = (time.time() - t0) * 1000

        result.elapsed_ms = elapsed
        result.rows_returned = len(rows)

        if elapsed > 1000:
            result.warnings.append(f"执行耗时 {elapsed:.0f}ms（慢查询）")
        elif elapsed > 200:
            result.warnings.append(f"执行耗时 {elapsed:.0f}ms")

    # ==================== 索引推荐 ====================

    def _recommend_index(self, sql: str, result: ProfileResult):
        if result.index_used:
            return

        # 提取 WHERE 条件列
        m = re.search(
            r'\bWHERE\s+(.+?)(?=\s+(?:GROUP|ORDER|LIMIT|HAVING)\b|$)',
            sql, re.IGNORECASE | re.DOTALL)
        if not m:
            return

        where = m.group(1)
        # 找等值/范围条件
        cols = []
        for cm in re.finditer(
                r'[`"]?(\w+)[`"]?\s*(=|>|<|>=|<=|LIKE|IN)\s*',
                where, re.IGNORECASE):
            c = cm.group(1)
            if c.upper() not in ('AND', 'OR'):
                cols.append(c)

        if cols:
            cols_unique = list(dict.fromkeys(cols))[:3]  # 最多 3 列
            cols_str = ', '.join(f'`{c}`' for c in cols_unique)
            result.suggestions.append(
                f"建议添加索引: CREATE INDEX idx_xxx ON table ({cols_str})")

        # ORDER BY 列
        m = re.search(r'\bORDER\s+BY\s+([`"]?\w+[`"]?)',
                      sql, re.IGNORECASE)
        if m:
            col = m.group(1).strip('`"')
            result.suggestions.append(
                f"为 ORDER BY 列 `{col}` 添加索引可避免排序")

    # ==================== 评分 ====================

    def _calc_score(self, result: ProfileResult):
        score = 100

        # 扫描类型
        st = (result.scan_type or '').upper()
        if st == 'ALL':
            score -= 30
        elif st == 'INDEX':
            score -= 15
        elif st in ('RANGE', 'REF'):
            score -= 5
        elif st in ('CONST', 'EQ_REF', 'SYSTEM'):
            score += 0

        # 扫描行数
        if result.rows_scanned > 100000:
            score -= 20
        elif result.rows_scanned > 10000:
            score -= 10
        elif result.rows_scanned > 1000:
            score -= 5

        # 执行时间
        if result.elapsed_ms > 1000:
            score -= 20
        elif result.elapsed_ms > 500:
            score -= 15
        elif result.elapsed_ms > 100:
            score -= 5

        # 警告
        score -= len(result.warnings) * 5

        # 结果比
        if result.rows_returned and result.rows_scanned:
            ratio = result.rows_scanned / result.rows_returned
            if ratio > 1000:
                score -= 15
            elif ratio > 100:
                score -= 8
            elif ratio > 10:
                score -= 3

        result.score = max(0, min(100, score))


# ==================== 慢查询聚合分析 ====================

class SlowQueryAnalyzer:
    """慢查询日志分析"""

    def __init__(self, logger):
        self.logger = logger

    def analyze(self, top_n: int = 10) -> Dict:
        entries = list(self.logger.slow_log)
        if not entries:
            return {'total': 0, 'top_slow': [], 'by_pattern': []}

        parsed = []
        for line in entries:
            m = re.search(r'([\d.]+)s:\s*(.+)$', line)
            if m:
                parsed.append((float(m.group(1)), m.group(2).strip()))

        parsed.sort(reverse=True)

        # 归一化聚合
        from collections import Counter, defaultdict
        patterns = Counter()
        pattern_time = defaultdict(float)
        for elapsed, sql in parsed:
            pat = re.sub(r'\d+', '?', sql)
            pat = re.sub(r"'[^']*'", "'?'", pat)
            patterns[pat] += 1
            pattern_time[pat] += elapsed

        return {
            'total': len(parsed),
            'top_slow': parsed[:top_n],
            'by_pattern': [
                (pat, cnt, pattern_time[pat])
                for pat, cnt in patterns.most_common(top_n)
            ],
            'avg_time': sum(e for e, _ in parsed) / len(parsed),
            'max_time': parsed[0][0] if parsed else 0,
        }

    def generate_report(self, stats: Dict) -> str:
        lines = [
            "# 慢查询分析报告",
            f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 概览",
            f"- 慢查询总数: {stats['total']}",
            f"- 平均耗时: {stats.get('avg_time', 0):.3f}s",
            f"- 最大耗时: {stats.get('max_time', 0):.3f}s",
            "",
            "## Top 10 最慢",
            "",
        ]
        for elapsed, sql in stats['top_slow']:
            lines.append(f"### {elapsed:.3f}s")
            lines.append(f"```sql\n{sql[:300]}\n```")
            lines.append("")

        lines.append("## 按模式聚合")
        lines.append("")
        for pat, cnt, total in stats['by_pattern']:
            lines.append(f"- **x{cnt}** ({total:.2f}s) `{pat[:80]}`")

        return '\n'.join(lines)