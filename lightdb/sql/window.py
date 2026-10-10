"""
窗口函数引擎（修复版）

修复:
- RANK / DENSE_RANK 第一行边界
- CUME_DIST 处理重复值
- PERCENT_RANK n=1 边界
- LAG / LEAD 的 default 解析
- NTILE 边界

其余部分（WindowSpec / parse_window_expr / WINDOW_FUNCS）保持原样。
"""
import math
import re
from collections import defaultdict


# ==================== WindowSpec / WindowFuncCall ====================
# （保持原样，此处仅列出签名，具体实现不变）

class WindowSpec:
    def __init__(self):
        self.partition_by = []
        self.order_by = None
        self.frame_type = 'ROWS'
        self.frame_start = None
        self.frame_end = None

    def __repr__(self):
        return (f"WindowSpec(partition={self.partition_by}, "
                f"order={self.order_by})")


class WindowFuncCall:
    def __init__(self, func_name, args, spec, alias=None):
        self.func_name = func_name.upper()
        self.args = args
        self.spec = spec
        self.alias = alias or f"{self.func_name}(...)"


WINDOW_FUNCS = {
    'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'PERCENT_RANK',
    'CUME_DIST', 'NTILE', 'LEAD', 'LAG',
    'FIRST_VALUE', 'LAST_VALUE', 'NTH_VALUE',
    'SUM', 'AVG', 'MIN', 'MAX', 'COUNT',
    'STDDEV', 'STDDEV_POP', 'STDDEV_SAMP',
    'VARIANCE', 'VAR_POP', 'VAR_SAMP',
}


def parse_window_expr(expr: str):
    expr = expr.strip()
    m = re.match(
        r'(\w+)\s*\(\s*([^)]*?)\s*\)\s+OVER\s*\(\s*(.*?)\s*\)\s*$',
        expr, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    func_name = m.group(1).upper()
    if func_name not in WINDOW_FUNCS:
        return None
    args_str = m.group(2).strip()
    args = []
    if args_str:
        args = [a.strip() for a in _split_top(args_str)]
    spec_str = m.group(3).strip()
    spec = _parse_spec(spec_str)
    return WindowFuncCall(func_name, args, spec)


def _split_top(s: str):
    parts, buf, depth = [], '', 0
    for ch in s:
        if ch == '(':
            depth += 1
            buf += ch
        elif ch == ')':
            depth -= 1
            buf += ch
        elif ch == ',' and depth == 0:
            parts.append(buf)
            buf = ''
        else:
            buf += ch
    if buf:
        parts.append(buf)
    return parts


def _parse_spec(s: str) -> WindowSpec:
    spec = WindowSpec()
    s = s.strip()
    if not s:
        return spec

    pm = re.search(
        r'PARTITION\s+BY\s+(.+?)(?=\s+ORDER\s+BY|\s+ROWS|\s+RANGE|$)',
        s, re.IGNORECASE | re.DOTALL)
    if pm:
        spec.partition_by = [
            c.strip() for c in _split_top(pm.group(1))
        ]

    om = re.search(
        r'ORDER\s+BY\s+(.+?)(?=\s+ROWS|\s+RANGE|$)',
        s, re.IGNORECASE | re.DOTALL)
    if om:
        ob = om.group(1).strip()
        parts = ob.split()
        desc = False
        if len(parts) >= 2 and \
                parts[-1].upper() in ('ASC', 'DESC'):
            desc = parts[-1].upper() == 'DESC'
            ob = ' '.join(parts[:-1])
        spec.order_by = (ob.strip(), desc)

    rm = re.search(
        r'(ROWS|RANGE)\s+BETWEEN\s+(.+?)\s+AND\s+(.+?)(?=$|\s)',
        s, re.IGNORECASE)
    if rm:
        spec.frame_type = rm.group(1).upper()
        spec.frame_start = rm.group(2).strip().upper()
        spec.frame_end = rm.group(3).strip().upper()

    return spec


# ==================== WindowExecutor（修复版）====================

class WindowExecutor:
    """窗口函数执行器（修复版）"""

    def __init__(self, rows, cols, window_calls):
        self.rows = rows
        self.cols = cols
        self.window_calls = window_calls

    def execute(self):
        if not self.rows:
            out_cols = self.cols + [
                w.alias for w in self.window_calls
            ]
            return out_cols, []

        results = [None] * len(self.window_calls)
        for i, wc in enumerate(self.window_calls):
            results[i] = self._eval_window(wc)

        out_cols = self.cols + [
            w.alias for w in self.window_calls
        ]
        out_rows = []
        for ri, row in enumerate(self.rows):
            new_row = list(row) + [
                results[i][ri]
                for i in range(len(self.window_calls))
            ]
            out_rows.append(new_row)

        return out_cols, out_rows

    def _eval_window(self, wc: WindowFuncCall):
        spec = wc.spec

        # 分组
        partition_idx = []
        for col in spec.partition_by:
            partition_idx.append(self._resolve_col(col))

        groups = defaultdict(list)
        if partition_idx:
            for ri, row in enumerate(self.rows):
                key = tuple(row[i] for i in partition_idx)
                groups[key].append(ri)
        else:
            groups[()] = list(range(len(self.rows)))

        result = [None] * len(self.rows)

        for key, idx_list in groups.items():
            # 组内排序
            if spec.order_by:
                oi = self._resolve_col(spec.order_by[0])
                desc = spec.order_by[1]
                idx_list.sort(
                    key=lambda ri: (
                        self.rows[ri][oi] is None,
                        self.rows[ri][oi]),
                    reverse=desc)

            self._eval_group(wc, idx_list, result)

        return result

    def _eval_group(self, wc, idx_list, result):
        fn = wc.func_name
        n = len(idx_list)

        if n == 0:
            return

        # ---------- 排序类 ----------

        if fn == 'ROW_NUMBER':
            for pos, ri in enumerate(idx_list, 1):
                result[ri] = pos

        elif fn == 'RANK':
            # ★ 修复: 用第一个 val 初始化 prev_val
            oi = (self._resolve_col(wc.spec.order_by[0])
                  if wc.spec.order_by else None)
            prev_val = None
            rank = 0
            first = True
            for pos, ri in enumerate(idx_list, 1):
                if oi is None:
                    result[ri] = pos
                    continue
                val = self.rows[ri][oi]
                # ★ first 单独处理
                if first or val != prev_val:
                    rank = pos
                    prev_val = val
                    first = False
                result[ri] = rank

        elif fn == 'DENSE_RANK':
            oi = (self._resolve_col(wc.spec.order_by[0])
                  if wc.spec.order_by else None)
            prev_val = None
            rank = 0
            first = True
            for ri in idx_list:
                if oi is None:
                    rank += 1
                else:
                    val = self.rows[ri][oi]
                    if first or val != prev_val:
                        rank += 1
                        prev_val = val
                        first = False
                result[ri] = rank

        elif fn == 'PERCENT_RANK':
            # ★ 修复: n=1 时返回 0
            if n <= 1:
                for ri in idx_list:
                    result[ri] = 0.0
                return
            oi = (self._resolve_col(wc.spec.order_by[0])
                  if wc.spec.order_by else None)
            prev_val = None
            rank = 0
            first = True
            for pos, ri in enumerate(idx_list, 1):
                if oi is None:
                    rank = pos
                else:
                    val = self.rows[ri][oi]
                    if first or val != prev_val:
                        rank = pos
                        prev_val = val
                        first = False
                result[ri] = (rank - 1) / (n - 1)

        elif fn == 'CUME_DIST':
            # ★ 修复: 按 (<=current) / n 计算
            oi = (self._resolve_col(wc.spec.order_by[0])
                  if wc.spec.order_by else None)
            if oi is None or n == 0:
                for pos, ri in enumerate(idx_list, 1):
                    result[ri] = pos / n if n else 0.0
                return

            # 收集所有 value
            vals = [self.rows[ri][oi] for ri in idx_list]
            for pos, ri in enumerate(idx_list, 1):
                cur = vals[pos - 1]
                # 计算 <= cur 的数量
                count_le = sum(
                    1 for v in vals
                    if v is not None and v <= cur)
                result[ri] = count_le / n

        elif fn == 'NTILE':
            buckets = int(wc.args[0]) if wc.args else 1
            if buckets <= 0:
                buckets = 1
            base = n // buckets
            extra = n % buckets
            pos = 0
            for b in range(buckets):
                size = base + (1 if b < extra else 0)
                for _ in range(size):
                    if pos >= len(idx_list):
                        break
                    result[idx_list[pos]] = b + 1
                    pos += 1

        elif fn == 'LEAD':
            # ★ 修复: 严格解析 default
            offset = 1
            default = None
            if len(wc.args) > 1:
                try:
                    offset = int(str(wc.args[1]).strip("'\""))
                except (ValueError, TypeError):
                    offset = 1
            if len(wc.args) > 2:
                default = wc.args[2]
            src_idx = (self._resolve_col(wc.args[0])
                       if wc.args else 0)
            for pos, ri in enumerate(idx_list):
                tgt = pos + offset
                if 0 <= tgt < n:
                    result[ri] = self.rows[idx_list[tgt]][src_idx]
                else:
                    result[ri] = self._coerce_default(default)

        elif fn == 'LAG':
            offset = 1
            default = None
            if len(wc.args) > 1:
                try:
                    offset = int(str(wc.args[1]).strip("'\""))
                except (ValueError, TypeError):
                    offset = 1
            if len(wc.args) > 2:
                default = wc.args[2]
            src_idx = (self._resolve_col(wc.args[0])
                       if wc.args else 0)
            for pos, ri in enumerate(idx_list):
                tgt = pos - offset
                if 0 <= tgt < n:
                    result[ri] = self.rows[idx_list[tgt]][src_idx]
                else:
                    result[ri] = self._coerce_default(default)

        elif fn == 'FIRST_VALUE':
            src_idx = (self._resolve_col(wc.args[0])
                       if wc.args else 0)
            first = self.rows[idx_list[0]][src_idx]
            for ri in idx_list:
                result[ri] = first

        elif fn == 'LAST_VALUE':
            src_idx = (self._resolve_col(wc.args[0])
                       if wc.args else 0)
            last = self.rows[idx_list[-1]][src_idx]
            for ri in idx_list:
                result[ri] = last

        elif fn == 'NTH_VALUE':
            src_idx = (self._resolve_col(wc.args[0])
                       if wc.args else 0)
            nth = 1
            if len(wc.args) > 1:
                try:
                    nth = int(str(wc.args[1]).strip("'\""))
                except (ValueError, TypeError):
                    nth = 1
            if nth <= 0 or nth > n:
                for ri in idx_list:
                    result[ri] = None
            else:
                val = self.rows[idx_list[nth - 1]][src_idx]
                for ri in idx_list:
                    result[ri] = val

        # ---------- 聚合类 ----------

        elif fn in ('COUNT', 'SUM', 'AVG', 'MIN', 'MAX',
                    'STDDEV', 'VARIANCE', 'STDDEV_POP',
                    'STDDEV_SAMP', 'VAR_POP', 'VAR_SAMP'):
            src_idx = None
            if wc.args and wc.args[0] != '*':
                src_idx = self._resolve_col(wc.args[0])

            cumulative = self._is_cumulative_frame(wc.spec)

            if cumulative and wc.spec.order_by:
                acc = []
                for pos, ri in enumerate(idx_list):
                    if src_idx is not None:
                        v = self.rows[ri][src_idx]
                        if v is not None:
                            acc.append(v)
                    result[ri] = self._aggregate(
                        fn, acc, n=pos + 1)
            else:
                all_vals = []
                if src_idx is not None:
                    for ri in idx_list:
                        v = self.rows[ri][src_idx]
                        if v is not None:
                            all_vals.append(v)
                agg_val = self._aggregate(fn, all_vals, n=n)
                for ri in idx_list:
                    result[ri] = agg_val

    def _is_cumulative_frame(self, spec) -> bool:
        if spec.frame_start == 'UNBOUNDED PRECEDING' and \
                spec.frame_end == 'CURRENT ROW':
            return True
        if spec.frame_start is None and spec.order_by is not None:
            return True
        return False

    def _aggregate(self, fn, vals, n=None):
        if fn == 'COUNT':
            return len(vals) if vals else 0
        if not vals:
            return None
        if fn == 'SUM':
            return sum(vals)
        if fn == 'AVG':
            return sum(vals) / len(vals)
        if fn == 'MIN':
            return min(vals)
        if fn == 'MAX':
            return max(vals)
        if fn in ('STDDEV', 'STDDEV_POP'):
            m = sum(vals) / len(vals)
            return math.sqrt(
                sum((v - m) ** 2 for v in vals) / len(vals))
        if fn == 'STDDEV_SAMP':
            if len(vals) < 2:
                return None
            m = sum(vals) / len(vals)
            return math.sqrt(
                sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
        if fn in ('VARIANCE', 'VAR_POP'):
            m = sum(vals) / len(vals)
            return sum((v - m) ** 2 for v in vals) / len(vals)
        if fn == 'VAR_SAMP':
            if len(vals) < 2:
                return None
            m = sum(vals) / len(vals)
            return sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
        return None

    def _coerce_default(self, d):
        # ★ 修复: None / 'NULL' → None; 数字字符串 → int/float
        if d is None:
            return None
        s = str(d).strip().strip("'\"")
        if s.upper() == 'NULL' or s == '':
            return None
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            return s

    def _resolve_col(self, name: str) -> int:
        name = name.strip()
        if name in self.cols:
            return self.cols.index(name)
        suffix = '.' + name
        matches = [
            i for i, c in enumerate(self.cols)
            if c == name or c.endswith(suffix)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"列 '{name}' 歧义")
        raise ValueError(f"列 '{name}' 不存在")


def is_window_expr(expr: str) -> bool:
    if not isinstance(expr, str):
        return False
    return bool(re.search(r'\bOVER\s*\(', expr, re.IGNORECASE))