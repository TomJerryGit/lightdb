"""
数据科学工具
- 统计分析（相关系数、分布检验）
- 时间序列预测
- 聚类分析
- 异常检测
"""
import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple


@dataclass
class StatResult:
    name: str = ""
    value: float = 0
    extra: Dict = field(default_factory=dict)


class StatAnalyzer:
    """统计分析"""

    def __init__(self, conn=None):
        self.conn = conn

    def set_connection(self, conn):
        self.conn = conn

    # ==================== 描述性统计 ====================

    def describe(self, table: str, column: str) -> Dict:
        """描述性统计"""
        if not self.conn:
            return {}

        try:
            _, r = self.conn.query(
                f"SELECT COUNT(*), COUNT(`{column}`), "
                f"COUNT(DISTINCT `{column}`), "
                f"MIN(`{column}`), MAX(`{column}`), "
                f"AVG(`{column}`) "
                f"FROM `{table}`")
            if not r:
                return {}

            row = r[0]
            total = row[0] or 0
            non_null = row[1] or 0
            distinct = row[2] or 0

            result = {
                'total': total,
                'non_null': non_null,
                'null_count': total - non_null,
                'null_ratio': (total - non_null) / total if total else 0,
                'distinct': distinct,
                'distinct_ratio': distinct / non_null if non_null else 0,
                'min': row[3],
                'max': row[4],
                'mean': float(row[5]) if row[5] is not None else None,
            }

            # 标准差（采样）
            if non_null > 0:
                sample_size = min(10000, non_null)
                _, r2 = self.conn.query(
                    f"SELECT `{column}` FROM `{table}` "
                    f"WHERE `{column}` IS NOT NULL LIMIT {sample_size}")
                values = []
                for rr in r2:
                    try:
                        values.append(float(rr[0]))
                    except (ValueError, TypeError):
                        pass

                if values:
                    result['std'] = statistics.stdev(values) \
                        if len(values) > 1 else 0
                    result['median'] = statistics.median(values)
                    # 四分位
                    sorted_v = sorted(values)
                    n = len(sorted_v)
                    result['q1'] = sorted_v[n // 4]
                    result['q3'] = sorted_v[3 * n // 4]
                    result['iqr'] = result['q3'] - result['q1']

            return result
        except Exception as e:
            return {'error': str(e)}

    # ==================== 相关性 ====================

    def correlation(self, table: str, col1: str, col2: str,
                    sample_size: int = 5000) -> Dict:
        """皮尔逊相关系数"""
        if not self.conn:
            return {}

        try:
            _, r = self.conn.query(
                f"SELECT `{col1}`, `{col2}` FROM `{table}` "
                f"WHERE `{col1}` IS NOT NULL AND `{col2}` IS NOT NULL "
                f"LIMIT {sample_size}")
        except Exception as e:
            return {'error': str(e)}

        xs, ys = [], []
        for row in r:
            try:
                xs.append(float(row[0]))
                ys.append(float(row[1]))
            except (ValueError, TypeError):
                continue

        n = len(xs)
        if n < 3:
            return {'error': '样本不足'}

        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        cov = sum((xs[i] - mean_x) * (ys[i] - mean_y)
                  for i in range(n)) / n
        std_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs) / n)
        std_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys) / n)

        if std_x == 0 or std_y == 0:
            return {'correlation': 0, 'samples': n}

        corr = cov / (std_x * std_y)

        # 强度
        strength = '无'
        a = abs(corr)
        if a > 0.8:
            strength = '强'
        elif a > 0.5:
            strength = '中'
        elif a > 0.3:
            strength = '弱'

        return {
            'correlation': corr,
            'strength': strength,
            'direction': '正' if corr > 0 else '负',
            'samples': n,
        }

    # ==================== 时间序列 ====================

    def time_series_forecast(self, table: str, time_col: str,
                             value_col: str,
                             periods: int = 7,
                             method: str = 'ma') -> Dict:
        """
        时间序列预测
        method: ma (移动平均) / linear (线性回归) / exp (指数平滑)
        """
        if not self.conn:
            return {}

        # 拉取数据（按天聚合）
        try:
            _, r = self.conn.query(
                f"SELECT DATE(`{time_col}`) AS d, "
                f"SUM(`{value_col}`) AS v "
                f"FROM `{table}` "
                f"WHERE `{time_col}` IS NOT NULL "
                f"GROUP BY DATE(`{time_col}`) "
                f"ORDER BY d")
        except Exception as e:
            return {'error': str(e)}

        if len(r) < 3:
            return {'error': '数据点不足'}

        dates = [str(row[0]) for row in r]
        values = []
        for row in r:
            try:
                values.append(float(row[1]) if row[1] else 0)
            except (ValueError, TypeError):
                values.append(0)

        forecast = []

        if method == 'ma':
            window = min(7, len(values))
            ma = sum(values[-window:]) / window
            forecast = [ma] * periods

        elif method == 'linear':
            # 简单线性回归
            n = len(values)
            xs = list(range(n))
            mean_x = sum(xs) / n
            mean_y = sum(values) / n
            num = sum((xs[i] - mean_x) * (values[i] - mean_y)
                      for i in range(n))
            den = sum((x - mean_x) ** 2 for x in xs)
            slope = num / den if den != 0 else 0
            intercept = mean_y - slope * mean_x
            for i in range(1, periods + 1):
                forecast.append(slope * (n + i - 1) + intercept)

        elif method == 'exp':
            # 指数平滑
            alpha = 0.3
            s = values[0]
            for v in values[1:]:
                s = alpha * v + (1 - alpha) * s
            forecast = [s] * periods

        # 趋势
        if len(values) >= 4:
            first_half = sum(values[:len(values)//2]) / max(1, len(values)//2)
            second_half = sum(values[len(values)//2:]) / \
                max(1, len(values) - len(values)//2)
            trend = (second_half - first_half) / first_half \
                if first_half else 0
        else:
            trend = 0

        return {
            'dates': dates,
            'values': values,
            'forecast': forecast,
            'forecast_dates': [
                self._next_date(dates[-1], i) for i in range(1, periods + 1)
            ],
            'method': method,
            'trend': trend,
        }

    def _next_date(self, date_str: str, days: int) -> str:
        from datetime import datetime, timedelta
        try:
            d = datetime.strptime(date_str[:10], '%Y-%m-%d')
            return (d + timedelta(days=days)).strftime('%Y-%m-%d')
        except Exception:
            return f"+{days}d"

    # ==================== 聚类 ====================

    def kmeans(self, table: str, columns: List[str],
               k: int = 3, sample_size: int = 1000,
               max_iter: int = 100) -> Dict:
        """
        简易 K-Means 聚类
        """
        if not self.conn or not columns:
            return {}

        cols_str = ', '.join(f"`{c}`" for c in columns)
        try:
            _, r = self.conn.query(
                f"SELECT {cols_str} FROM `{table}` LIMIT {sample_size}")
        except Exception as e:
            return {'error': str(e)}

        # 数据预处理
        data = []
        for row in r:
            try:
                point = [float(v) for v in row]
                data.append(point)
            except (ValueError, TypeError):
                continue

        if len(data) < k:
            return {'error': '样本不足'}

        n_dims = len(data[0])
        # 归一化
        mins = [min(p[i] for p in data) for i in range(n_dims)]
        maxs = [max(p[i] for p in data) for i in range(n_dims)]
        ranges = [maxs[i] - mins[i] if maxs[i] > mins[i] else 1
                  for i in range(n_dims)]

        normalized = [
            [(p[i] - mins[i]) / ranges[i] for i in range(n_dims)]
            for p in data
        ]

        # 初始化质心（K-Means++）
        import random
        centroids = [normalized[random.randint(0, len(normalized) - 1)]]
        for _ in range(k - 1):
            dists = []
            for p in normalized:
                min_d = min(self._dist(p, c) for c in centroids)
                dists.append(min_d ** 2)
            total = sum(dists)
            if total == 0:
                centroids.append(normalized[random.randint(
                    0, len(normalized) - 1)])
                continue
            r_val = random.random() * total
            acc = 0
            for i, d in enumerate(dists):
                acc += d
                if acc >= r_val:
                    centroids.append(normalized[i])
                    break

        # 迭代
        labels = [0] * len(normalized)
        for iteration in range(max_iter):
            # 分配
            changed = False
            for i, p in enumerate(normalized):
                best = min(range(k),
                           key=lambda c: self._dist(p, centroids[c]))
                if labels[i] != best:
                    labels[i] = best
                    changed = True

            if not changed:
                break

            # 更新质心
            for c in range(k):
                members = [normalized[i] for i in range(len(normalized))
                           if labels[i] == c]
                if not members:
                    continue
                centroids[c] = [
                    sum(m[d] for m in members) / len(members)
                    for d in range(n_dims)
                ]

        # 反归一化质心
        real_centroids = []
        for c in centroids:
            real = [c[i] * ranges[i] + mins[i] for i in range(n_dims)]
            real_centroids.append(real)

        # 统计
        sizes = [labels.count(i) for i in range(k)]

        return {
            'labels': labels,
            'centroids': real_centroids,
            'sizes': sizes,
            'k': k,
            'dimensions': columns,
            'data': data[:500],   # 限制返回量
        }

    def _dist(self, a, b):
        return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(len(a))))

    # ==================== 异常检测 ====================

    def detect_outliers(self, table: str, column: str,
                        method: str = 'iqr',
                        threshold: float = 1.5) -> Dict:
        """
        异常值检测
        method: iqr / zscore
        """
        if not self.conn:
            return {}

        try:
            _, r = self.conn.query(
                f"SELECT `{column}` FROM `{table}` "
                f"WHERE `{column}` IS NOT NULL LIMIT 10000")
        except Exception as e:
            return {'error': str(e)}

        values = []
        for row in r:
            try:
                values.append(float(row[0]))
            except (ValueError, TypeError):
                continue

        if len(values) < 10:
            return {'error': '样本不足'}

        outliers = []

        if method == 'iqr':
            sorted_v = sorted(values)
            n = len(sorted_v)
            q1 = sorted_v[n // 4]
            q3 = sorted_v[3 * n // 4]
            iqr = q3 - q1
            lower = q1 - threshold * iqr
            upper = q3 + threshold * iqr
            for v in values:
                if v < lower or v > upper:
                    outliers.append(v)
            info = {'q1': q1, 'q3': q3, 'iqr': iqr,
                    'lower': lower, 'upper': upper}

        elif method == 'zscore':
            mean = sum(values) / len(values)
            std = math.sqrt(sum((v - mean) ** 2 for v in values) /
                            len(values))
            if std == 0:
                return {'outliers': [], 'method': method}
            for v in values:
                z = (v - mean) / std
                if abs(z) > threshold:
                    outliers.append(v)
            info = {'mean': mean, 'std': std,
                    'threshold': threshold}

        return {
            'outliers': outliers[:100],   # 限制返回量
            'outlier_count': len(outliers),
            'total': len(values),
            'ratio': len(outliers) / len(values) if values else 0,
            'method': method,
            **info,
        }