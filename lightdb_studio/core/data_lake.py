"""
数据湖集成
- CSV / Parquet 文件当表查
- S3 / 本地文件
- Schema 推断
- 分区表
"""
import csv
import io
import json
import os
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional, Callable


LAKE_FILE = Path.home() / '.lightdb_studio' / 'data_lake.json'


@dataclass
class LakeTable:
    """数据湖表"""
    name: str = ""
    path: str = ""             # 文件路径或 s3://...
    format: str = "csv"        # csv / parquet / json / jsonl
    # Schema
    columns: List[Dict] = field(default_factory=list)
    # 选项
    delimiter: str = ","
    has_header: bool = True
    encoding: str = "utf-8"
    sheet_name: str = ""       # Excel 用
    # S3
    s3_bucket: str = ""
    s3_key: str = ""
    s3_region: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    # 分区
    partition_by: List[str] = field(default_factory=list)
    # 统计
    row_count: int = 0
    size_bytes: int = 0
    last_scanned: float = 0
    created_at: float = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


class DataLakeEngine:
    """数据湖引擎"""

    def __init__(self):
        self.file = LAKE_FILE
        self.tables: Dict[str, LakeTable] = {}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # ==================== 持久化 ====================

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for name, td in data.get('tables', {}).items():
                self.tables[name] = LakeTable(**td)
        except Exception:
            pass

    def save(self):
        data = {
            'tables': {n: asdict(t)
                        for n, t in self.tables.items()}
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 注册 ====================

    def register_file(self, name: str, path: str,
                      format: str = "csv",
                      **options) -> LakeTable:
        """注册本地文件"""
        t = LakeTable(name=name, path=path, format=format)
        for k, v in options.items():
            if hasattr(t, k):
                setattr(t, k, v)
        self.tables[name] = t

        # 探测 schema
        self._infer_schema(t)
        self.save()
        return t

    def register_s3(self, name: str, bucket: str, key: str,
                    format: str = "parquet",
                    access_key: str = "",
                    secret_key: str = "",
                    region: str = "us-east-1",
                    **options) -> LakeTable:
        """注册 S3 对象"""
        t = LakeTable(
            name=name,
            path=f"s3://{bucket}/{key}",
            format=format,
            s3_bucket=bucket,
            s3_key=key,
            s3_access_key=access_key,
            s3_secret_key=secret_key,
            s3_region=region,
        )
        for k, v in options.items():
            if hasattr(t, k):
                setattr(t, k, v)
        self.tables[name] = t
        self.save()
        return t

    def remove(self, name: str):
        if name in self.tables:
            del self.tables[name]
            self.save()
            return True
        return False

    def get(self, name: str) -> Optional[LakeTable]:
        return self.tables.get(name)

    def list_all(self) -> List[LakeTable]:
        return list(self.tables.values())

    # ==================== Schema 推断 ====================

    def _infer_schema(self, t: LakeTable):
        """推断文件 schema"""
        try:
            if t.format == "csv":
                self._infer_csv(t)
            elif t.format == "json":
                self._infer_json(t)
            elif t.format == "jsonl":
                self._infer_jsonl(t)
            elif t.format == "parquet":
                self._infer_parquet(t)
        except Exception:
            pass

    def _infer_csv(self, t: LakeTable):
        if not os.path.exists(t.path):
            return
        with open(t.path, 'r', encoding=t.encoding,
                  errors='replace') as f:
            reader = csv.reader(f, delimiter=t.delimiter)
            rows = []
            for i, row in enumerate(reader):
                if i > 100:
                    break
                rows.append(row)

        if not rows:
            return

        if t.has_header:
            headers = rows[0]
            data_rows = rows[1:]
        else:
            headers = [f"col{i+1}" for i in range(len(rows[0]))]
            data_rows = rows

        # 类型推断
        cols = []
        for i, name in enumerate(headers):
            col_type = self._infer_column_type(
                [r[i] for r in data_rows if i < len(r)])
            cols.append({
                'name': name.strip(),
                'type': col_type,
                'index': i,
            })

        t.columns = cols
        t.row_count = len(rows) - (1 if t.has_header else 0)
        t.size_bytes = os.path.getsize(t.path)
        t.last_scanned = time.time()

    def _infer_column_type(self, values: List[str]) -> str:
        if not values:
            return "VARCHAR"
        # 采样
        sample = values[:100]
        types = set()
        for v in sample:
            if v is None or v == '':
                continue
            if re.match(r'^-?\d+$', str(v)):
                types.add('INT')
            elif re.match(r'^-?\d+\.\d+$', str(v)):
                types.add('DOUBLE')
            elif re.match(r'^\d{4}-\d{2}-\d{2}', str(v)):
                types.add('DATE')
            else:
                types.add('VARCHAR')

        if not types:
            return "VARCHAR"
        if types == {'INT'}:
            return "INT"
        if types <= {'INT', 'DOUBLE'}:
            return "DOUBLE"
        if types == {'DATE'}:
            return "DATE"
        return "VARCHAR"

    def _infer_json(self, t: LakeTable):
        if not os.path.exists(t.path):
            return
        with open(t.path, 'r', encoding=t.encoding) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            keys = list(data[0].keys())
            cols = []
            for k in keys:
                values = [str(item.get(k, ''))
                          for item in data[:100]
                          if isinstance(item, dict)]
                col_type = self._infer_column_type(values)
                cols.append({'name': k, 'type': col_type})
            t.columns = cols
            t.row_count = len(data)

    def _infer_jsonl(self, t: LakeTable):
        if not os.path.exists(t.path):
            return
        with open(t.path, 'r', encoding=t.encoding) as f:
            lines = [line.strip() for line in f if line.strip()][:100]
        if not lines:
            return
        objs = [json.loads(l) for l in lines]
        keys = list(objs[0].keys())
        cols = []
        for k in keys:
            values = [str(o.get(k, '')) for o in objs]
            col_type = self._infer_column_type(values)
            cols.append({'name': k, 'type': col_type})
        t.columns = cols
        t.row_count = sum(1 for _ in open(t.path)) if os.path.exists(
            t.path) else len(lines)

    def _infer_parquet(self, t: LakeTable):
        try:
            import pyarrow.parquet as pq
        except ImportError:
            return
        if not os.path.exists(t.path):
            return
        table = pq.read_table(t.path)
        cols = []
        for name, type_ in zip(table.column_names, table.schema.types):
            cols.append({
                'name': name,
                'type': str(type_).upper(),
            })
        t.columns = cols
        t.row_count = table.num_rows
        t.size_bytes = os.path.getsize(t.path)

    # ==================== 查询 ====================

    def query(self, name: str, select: str = "*",
              where: str = "", limit: int = 0) -> tuple:
        """查询数据湖表"""
        t = self.tables.get(name)
        if not t:
            raise ValueError(f"数据湖表 {name} 不存在")

        if t.format == "csv":
            return self._query_csv(t, select, where, limit)
        elif t.format == "json":
            return self._query_json(t, select, where, limit)
        elif t.format == "jsonl":
            return self._query_jsonl(t, select, where, limit)
        elif t.format == "parquet":
            return self._query_parquet(t, select, where, limit)
        else:
            raise ValueError(f"不支持的格式: {t.format}")

    def _query_csv(self, t: LakeTable, select, where, limit):
        if not os.path.exists(t.path):
            return [], []

        with open(t.path, 'r', encoding=t.encoding,
                  errors='replace') as f:
            reader = csv.reader(f, delimiter=t.delimiter)
            all_rows = list(reader)

        if t.has_header:
            headers = all_rows[0]
            data = all_rows[1:]
        else:
            headers = [f"col{i+1}" for i in range(len(all_rows[0]))]
            data = all_rows

        # 投影
        if select == "*":
            cols = headers
            rows = data
        else:
            sel_cols = [c.strip() for c in select.split(',')]
            idx = [headers.index(c) for c in sel_cols
                   if c in headers]
            cols = [headers[i] for i in idx]
            rows = [[r[i] for i in idx] for r in data]

        # WHERE
        if where:
            rows = self._apply_where(rows, cols, where)

        # LIMIT
        if limit:
            rows = rows[:limit]

        return cols, rows

    def _query_json(self, t: LakeTable, select, where, limit):
        with open(t.path, 'r', encoding=t.encoding) as f:
            data = json.load(f)
        if not isinstance(data, list):
            data = [data]

        if not data:
            return [], []

        cols = list(data[0].keys()) if isinstance(data[0], dict) else []

        if select != '*':
            sel_cols = [c.strip() for c in select.split(',')]
            cols = [c for c in sel_cols if c in cols]

        rows = []
        for item in data:
            row = []
            for c in cols:
                v = item.get(c)
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                row.append(v)
            rows.append(row)

        if where:
            rows = self._apply_where(rows, cols, where)
        if limit:
            rows = rows[:limit]
        return cols, rows

    def _query_jsonl(self, t: LakeTable, select, where, limit):
        rows = []
        with open(t.path, 'r', encoding=t.encoding) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rows.append(obj)
                if limit and len(rows) >= limit * 2:
                    break

        if not rows:
            return [], []

        cols = list(rows[0].keys())
        if select != '*':
            sel_cols = [c.strip() for c in select.split(',')]
            cols = [c for c in sel_cols if c in cols]

        data = []
        for o in rows:
            data.append([o.get(c) for c in cols])

        if where:
            data = self._apply_where(data, cols, where)
        if limit:
            data = data[:limit]
        return cols, data

    def _query_parquet(self, t: LakeTable, select, where, limit):
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise RuntimeError("需要 pyarrow: pip install pyarrow")

        table = pq.read_table(t.path)
        cols = table.column_names
        rows = table.to_pylist()

        if select != '*':
            sel_cols = [c.strip() for c in select.split(',')]
            cols = [c for c in sel_cols if c in cols]

        data = [[o.get(c) for c in cols] for o in rows]

        if where:
            data = self._apply_where(data, cols, where)
        if limit:
            data = data[:limit]
        return cols, data

    def _apply_where(self, rows, cols, where):
        """简单 WHERE"""
        conditions = re.split(r'\s+AND\s+', where, flags=re.IGNORECASE)
        result = list(rows)
        for cond in conditions:
            m = re.match(
                r'[`"]?(\w+)[`"]?\s*'
                r'(=|!=|<>|>=|<=|>|<|LIKE)\s*(.+?)$',
                cond.strip(), re.IGNORECASE)
            if not m:
                continue
            col, op, val = m.group(1), m.group(2).upper(), \
                m.group(3).strip().strip("'\"")
            if col not in cols:
                continue
            ci = cols.index(col)

            try:
                val_num = float(val)
            except ValueError:
                val_num = None

            new_rows = []
            for r in result:
                v = r[ci]
                try:
                    if val_num is not None and \
                            isinstance(v, (int, float)):
                        cmp = val_num
                    elif val_num is not None:
                        cmp = val_num
                        v = float(v) if v not in (None, '') else None
                    else:
                        cmp = val
                    if v is None:
                        ok = False
                    elif op == '=': ok = v == cmp
                    elif op in ('!=', '<>'): ok = v != cmp
                    elif op == '>': ok = v > cmp
                    elif op == '<': ok = v < cmp
                    elif op == '>=': ok = v >= cmp
                    elif op == '<=': ok = v <= cmp
                    elif op == 'LIKE':
                        pat = val.replace('%', '.*').replace('_', '.')
                        ok = bool(re.match('^' + pat + '$', str(v)))
                    else:
                        ok = False
                except (TypeError, ValueError):
                    ok = False
                if ok:
                    new_rows.append(r)
            result = new_rows
        return result

    def stats(self) -> dict:
        by_format = {}
        total_size = 0
        for t in self.tables.values():
            by_format[t.format] = by_format.get(t.format, 0) + 1
            total_size += t.size_bytes
        return {
            'total_tables': len(self.tables),
            'by_format': by_format,
            'total_size': total_size,
        }