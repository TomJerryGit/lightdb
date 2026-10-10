"""
统计信息 - 收集 + 持久化
为 CBO 提供基数估算
"""
import json
import os
import threading
from pathlib import Path


class TableStats:
    def __init__(self):
        self.row_count = 0
        self.page_count = 0
        self.col_stats = {}   # col_name -> {distinct, min, max, null_count, histogram}

    def to_dict(self):
        return {
            'row_count': self.row_count,
            'page_count': self.page_count,
            'col_stats': self.col_stats,
        }

    @classmethod
    def from_dict(cls, d):
        s = cls()
        s.row_count = d.get('row_count', 0)
        s.page_count = d.get('page_count', 0)
        s.col_stats = d.get('col_stats', {})
        return s


class StatsManager:
    """统计信息管理器"""
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "_stats.json"
        self.lock = threading.RLock()
        self.stats = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                for tname, d in raw.items():
                    self.stats[tname] = TableStats.from_dict(d)
            except Exception:
                pass

    def save(self):
        with self.lock:
            raw = {t: s.to_dict() for t, s in self.stats.items()}
            tmp = self.path.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)

    def get(self, table_name) -> TableStats:
        return self.stats.get(table_name)

    def analyze_table(self, tbl) -> TableStats:
        """收集一张表的统计信息"""
        st = TableStats()
        st.row_count = tbl.row_count
        st.page_count = tbl.bp.total_pages()

        # 采样（最多1000行）
        sample_rows = []
        max_sample = 1000
        for i, (k, pid, sid) in enumerate(tbl.bt.scan()):
            if i >= max_sample:
                break
            page = tbl.bp.get_page(pid)
            if page is None:
                continue
            rb = page.get_row(sid)
            if rb is None:
                continue
            r = tbl._decode_row(rb)
            if r:
                sample_rows.append(r)

        # 每列统计
        for ci, col in enumerate(tbl.cols):
            cname = col['name']
            values = [r[ci] for r in sample_rows if ci < len(r)]
            non_null = [v for v in values if v is not None]
            distinct = len(set(str(v) for v in non_null))

            # 扩展因子
            factor = st.row_count / len(sample_rows) if sample_rows else 1
            est_distinct = int(distinct * factor) if factor > 1 else distinct

            # 简单直方图（top-10 频次）
            hist = {}
            for v in non_null:
                sv = str(v)
                hist[sv] = hist.get(sv, 0) + 1

            st.col_stats[cname] = {
                'distinct': max(est_distinct, 1),
                'null_count': len(values) - len(non_null),
                'min': min(non_null, default=None) if non_null else None,
                'max': max(non_null, default=None) if non_null else None,
                'histogram': dict(sorted(hist.items(), key=lambda x: -x[1])[:10]),
            }

        with self.lock:
            self.stats[tbl.name] = st
            self.save()
        return st

    def estimate_selectivity(self, table_name, where) -> float:
        """
        估算 WHERE 条件的选择率 (0.0 ~ 1.0)
        """
        st = self.stats.get(table_name)
        if st is None or st.row_count == 0:
            return 0.3   # 默认选择率

        if where is None:
            return 1.0

        if 'and' in where:
            s = 1.0
            for c in where['and']:
                s *= self.estimate_selectivity(table_name, c)
            return s

        if 'or' in where:
            # 粗略：1 - ∏(1-s)
            p = 1.0
            for c in where['or']:
                p *= (1.0 - self.estimate_selectivity(table_name, c))
            return 1.0 - p

        col = where.get('column')
        op = where.get('op')
        val = where.get('value')

        cs = st.col_stats.get(col)
        if cs is None or cs['distinct'] == 0:
            return 0.3

        if op == '=':
            # 等值：1/distinct
            return min(1.0, 1.0 / max(cs['distinct'], 1))
        if op == '!=':
            return max(0.0, 1.0 - 1.0 / max(cs['distinct'], 1))
        if op in ('<', '>', '<=', '>='):
            # 范围：直方图法（简化为 0.33）
            return 0.33
        if op == 'LIKE':
            v = str(val)
            if v.startswith('%') and v.endswith('%'):
                return 0.25
            if v.endswith('%'):
                return 0.10
            if v.startswith('%'):
                return 0.10
            return 1.0 / max(cs['distinct'], 1)

        return 0.3