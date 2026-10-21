"""
报表系统（P2-3：参数化查询 / 预览 / 模板产品化 / 导出校验）
- 保存查询为报表模板
- 定时执行
- 导出 Excel / HTML / CSV

向后兼容：旧模板 JSON 缺少 `params` / `preview_limit` / `chart` 时自动补默认值；
既有 `ReportEngine.add/update/remove/get/export_*` API 语义不变。
"""
import csv as _csv
import json
import os
import re
import time
import html
from pathlib import Path
from dataclasses import dataclass, asdict, field, fields
from typing import List, Dict, Optional

REPORT_DIR = Path.home() / '.lightdb_studio' / 'reports'
OUTPUT_DIR = REPORT_DIR / 'outputs'


# ==================== 参数化查询（P2-3） ====================

class ReportError(Exception):
    """报表系统异常基类。"""


class ReportValidationError(ReportError):
    """模板/SQL 校验失败。"""


class ReportParameterError(ReportValidationError):
    """参数缺失/多余/占位符混用等。"""


_NAMED_PARAM_RE = re.compile(r'(?<!:):([A-Za-z_]\w*)')


def _mask_sql_literals(sql: str) -> str:
    """把字符串字面量/注释/反引号内容替换成空格（等长），用于安全扫描占位符。"""
    s = '' if sql is None else str(sql)
    out = []
    i, n = 0, len(s)
    quote = None
    while i < n:
        ch = s[i]
        if quote:
            if ch == quote:
                if quote == "'" and i + 1 < n and s[i + 1] == "'":
                    out.extend('  ')
                    i += 2
                    continue
                quote = None
                out.append(' ')
            else:
                out.append(' ')
            i += 1
            continue
        if ch in ("'", '"', '`'):
            quote = ch
            out.append(' ')
            i += 1
            continue
        if ch == '-' and i + 1 < n and s[i + 1] == '-':
            j = s.find('\n', i)
            j = n if j < 0 else j
            out.extend(' ' * (j - i))
            i = j
            continue
        if ch == '/' and i + 1 < n and s[i + 1] == '*':
            j = s.find('*/', i + 2)
            j = n if j < 0 else j + 2
            out.extend(' ' * (j - i))
            i = j
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def extract_sql_params(sql: str) -> Dict:
    """解析 SQL 占位符，返回 {'style','names','qmark_count','mixed'}。

    - `:name` 命名占位符（跳过 `::` 与字符串/注释内的内容）；
    - `?` 位置占位符（按出现顺序）；
    - 两者混用 → mixed=True（调用方应显式报错）。
    """
    masked = _mask_sql_literals(sql)
    names = _NAMED_PARAM_RE.findall(masked)
    qmark_count = masked.count('?')
    if names and qmark_count:
        style = 'mixed'
    elif names:
        style = 'named'
    elif qmark_count:
        style = 'qmark'
    else:
        style = 'none'
    return {'style': style, 'names': list(dict.fromkeys(names)),
            'qmark_count': qmark_count, 'mixed': style == 'mixed'}


def convert_named_params(sql: str) -> str:
    """把 `:name` 占位符转成 pymysql 的 `%(name)s`（只改占位符，不动值）。"""
    masked = _mask_sql_literals(sql)
    matches = list(_NAMED_PARAM_RE.finditer(masked))
    out = ''
    last = 0
    for m in matches:
        out += sql[last:m.start()] + f"%({m.group(1)})s"
        last = m.end()
    out += sql[last:]
    return out


def bind_params(sql: str, values=None):
    """把用户参数绑定到 SQL。

    返回 ``(execute_sql, params, meta)``：
      - 命名参数 → execute_sql 已转 `%(name)s`，params 为 dict；
      - `?` → params 为 list；
      - 无占位符 → params=None；
      - 缺参/多参/混用 → 抛 `ReportParameterError`（显式、可断言）。
    """
    req = extract_sql_params(sql)
    style = req['style']
    if style == 'mixed':
        raise ReportParameterError(
            'SQL 不能混用 :name 与 ? 两种占位符')
    if style == 'none':
        if values:
            raise ReportParameterError('SQL 没有占位符，但传入了多余参数')
        return sql, None, req
    if style == 'named':
        if values is None:
            raise ReportParameterError(
                '缺少参数: ' + ', '.join(req['names']))
        if not isinstance(values, dict):
            raise ReportParameterError(':name 占位符需要 dict 形式的参数')
        known = set(req['names'])
        missing = [n for n in req['names'] if n not in values]
        extra = [k for k in values if k not in known]
        if missing:
            raise ReportParameterError('缺少参数: ' + ', '.join(missing))
        if extra:
            raise ReportParameterError(
                '多余参数: ' + ', '.join(str(k) for k in extra))
        return (convert_named_params(sql),
                {n: values[n] for n in req['names']}, req)
    # qmark
    count = req['qmark_count']
    if values is None:
        raise ReportParameterError(f'缺少 {count} 个 ? 参数')
    if isinstance(values, dict):
        values = list(values.values())
    seq = list(values)
    if len(seq) < count:
        raise ReportParameterError(
            f'缺少 {count - len(seq)} 个 ? 参数（需要 {count} 个）')
    if len(seq) > count:
        raise ReportParameterError(
            f'多余 {len(seq) - count} 个参数（需要 {count} 个）')
    return sql, seq, req


def _html_cell(value) -> str:
    """把单个单元格转成 <td>，NULL 用灰色斜体表示。

    原先这里写成单行 f-string 内嵌反斜杠转义，Python 3.11 及以下会直接
    抛 SyntaxError（PEP 701 之后才允许），导致整个 lightdb_studio 无法导入。
    """
    if value is None:
        return '<td><i style="color:#888">NULL</i></td>'
    return f'<td>{html.escape(str(value))}</td>'


@dataclass
class ReportTemplate:
    name: str = ""
    description: str = ""
    sql: str = ""
    connection: str = ""          # 连接名
    database: str = ""
    # 图表配置（可选；旧模板没有该字段也能加载）
    chart: Dict = field(default_factory=dict)
    # P2-3 参数默认值 {name: value}（? 占位符时可为空/不使用）
    params: Dict = field(default_factory=dict)
    # 预览行数（50/100/500，默认 100）
    preview_limit: int = 100
    # 定时
    schedule: str = ""            # 空 = 不定时；否则 cron 简化: "daily 09:00"
    last_run: float = 0
    last_status: str = ""
    created_at: float = 0
    updated_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        if not isinstance(self.chart, dict):
            self.chart = {}
        if not isinstance(self.params, dict):
            # 旧数据/外部传入 list 时容错为空 dict，避免加载失败
            self.params = {}
        try:
            self.preview_limit = int(self.preview_limit or 100)
        except (TypeError, ValueError):
            self.preview_limit = 100
        if self.preview_limit <= 0:
            self.preview_limit = 100

    def validate(self):
        """保存前校验（名称/SQL 非空、占位符合法），失败抛 ReportValidationError。"""
        if not str(self.name or '').strip():
            raise ReportValidationError('报表名称不能为空')
        if not str(self.sql or '').strip():
            raise ReportValidationError('SQL 不能为空')
        extract_sql_params(self.sql)       # mixed 时抛参数错误
        req = extract_sql_params(self.sql)
        if req['mixed']:
            raise ReportValidationError('SQL 不能混用 :name 与 ? 占位符')
        return True


class ReportEngine:
    def __init__(self, base_dir=None):
        if base_dir is not None:
            self.dir = Path(base_dir)
            self.out_dir = self.dir / 'outputs'
        else:
            self.dir = REPORT_DIR
            self.out_dir = OUTPUT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.templates: List[ReportTemplate] = []
        self._load()

    def _path(self):
        return self.dir / 'templates.json'

    @staticmethod
    def _template_from_dict(d) -> Optional[ReportTemplate]:
        if not isinstance(d, dict):
            return None
        known = {f.name for f in fields(ReportTemplate)}
        data = {k: v for k, v in d.items() if k in known}
        try:
            return ReportTemplate(**data)
        except Exception:
            try:
                return ReportTemplate(name=str(d.get('name', '')),
                                     sql=str(d.get('sql', '')))
            except Exception:
                return None

    def _load(self):
        if not self._path().exists():
            return
        self.templates = []
        try:
            with open(self._path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            for item in data:
                tmpl = self._template_from_dict(item)
                if tmpl is not None:
                    self.templates.append(tmpl)
        except Exception:
            self.templates = []

    def save(self):
        data = [asdict(t) for t in self.templates]
        tmp = self._path().with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self._path())

    def add(self, tmpl: ReportTemplate):
        tmpl.validate()
        self.templates.append(tmpl)
        self.save()
        return tmpl

    def update(self, idx: int, tmpl: ReportTemplate):
        tmpl.validate()
        if 0 <= idx < len(self.templates):
            tmpl.updated_at = time.time()
            self.templates[idx] = tmpl
            self.save()
        return tmpl

    def remove(self, idx: int):
        if 0 <= idx < len(self.templates):
            del self.templates[idx]
            self.save()

    def get(self, name: str) -> Optional[ReportTemplate]:
        for t in self.templates:
            if t.name == name:
                return t
        return None

    def names(self) -> List[str]:
        return [t.name for t in self.templates]

    # ---------- P2-3 模板产品化 ----------

    def upsert(self, tmpl: ReportTemplate) -> ReportTemplate:
        """按 name 存在则更新、否则新增；保存前校验。"""
        tmpl.validate()
        existing = self.get(tmpl.name)
        if existing is not None:
            idx = self.templates.index(existing)
            tmpl.updated_at = time.time()
            self.templates[idx] = tmpl
            self.save()
        else:
            self.add(tmpl)
        return tmpl

    def rename(self, idx: int, new_name: str):
        new_name = str(new_name or '').strip()
        if not new_name:
            raise ReportValidationError('报表名称不能为空')
        if not (0 <= idx < len(self.templates)):
            raise ReportValidationError('模板不存在')
        if any(t.name == new_name for i, t in enumerate(self.templates)
               if i != idx):
            raise ReportValidationError(f'报表名称已存在: {new_name}')
        self.templates[idx].name = new_name
        self.templates[idx].updated_at = time.time()
        self.save()
        return self.templates[idx]

    def duplicate(self, idx: int, new_name: str = '') -> ReportTemplate:
        import copy
        if not (0 <= idx < len(self.templates)):
            raise ReportValidationError('模板不存在')
        src = self.templates[idx]
        base = str(new_name or '').strip() or f'{src.name} 副本'
        name = base
        n = 2
        while self.get(name) is not None:
            name = f'{base} ({n})'
            n += 1
        tmpl = copy.deepcopy(src)
        tmpl.name = name
        tmpl.created_at = time.time()
        tmpl.updated_at = time.time()
        tmpl.last_run = 0
        tmpl.last_status = ''
        tmpl.validate()
        self.templates.append(tmpl)
        self.save()
        return tmpl

    def remove_by_name(self, name: str) -> bool:
        tmpl = self.get(name)
        if tmpl is None:
            return False
        self.remove(self.templates.index(tmpl))
        return True

    def validate_template(self, tmpl: ReportTemplate):
        return tmpl.validate()

    # ---------- 参数化查询 ----------

    def resolve_query(self, sql: str, values=None):
        """返回 (execute_sql, params, meta)；缺参/多参显式抛 ReportParameterError。"""
        return bind_params(sql, values)

    def run_query(self, conn, sql: str, values=None):
        """安全执行参数化查询：占位符转 pymysql 风格，值走 params。"""
        exec_sql, params, _meta = bind_params(sql, values)
        if params is None:
            return conn.query(exec_sql)
        return conn.query(exec_sql, params)

    def preview_query(self, conn, sql: str, values=None, limit=None) -> Dict:
        """执行查询并截断到前 limit 行（预览）；返回结果元信息。"""
        exec_sql, params, meta = bind_params(sql, values)
        cols, rows = (conn.query(exec_sql) if params is None
                      else conn.query(exec_sql, params))
        cols = list(cols or [])
        rows = list(rows or [])
        total = len(rows)
        if limit is None:
            limit = 0
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0:
            rows = rows[:limit]
        return {'columns': cols, 'rows': rows, 'total': total,
                'returned': len(rows), 'limit': limit,
                'sql': exec_sql, 'params': params,
                'style': meta['style'], 'param_names': meta['names']}

    # ==================== 导出 ====================

    def export_data(self, columns, rows, filepath, fmt='csv', title='',
                    chart_svg=''):
        """按格式导出（csv/xlsx/html），返回文件路径。"""
        fmt = str(fmt or 'csv').lower()
        if fmt in ('xlsx', 'excel'):
            return self.export_excel(columns, rows, filepath, title=title)
        if fmt == 'html':
            return self.export_html(columns, rows, filepath, title=title,
                                    chart_svg=chart_svg)
        return self.export_csv(columns, rows, filepath)

    def export_csv(self, columns, rows, filepath):
        with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
            w = _csv.writer(f)
            w.writerow(columns)
            for r in rows:
                w.writerow(['' if v is None else v for v in r])
        return filepath

    def export_excel(self, columns, rows, filepath,
                     sheet_name='Sheet1', title=''):
        """导出 Excel（需要 openpyxl）"""
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, Alignment, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            raise RuntimeError("需要安装 openpyxl: pip install openpyxl")

        wb = Workbook()
        ws = wb.active
        ws.title = sheet_name

        row_offset = 0
        # 标题
        if title:
            ws.merge_cells(start_row=1, start_column=1,
                           end_row=1, end_column=max(len(columns), 1))
            cell = ws.cell(row=1, column=1, value=title)
            cell.font = Font(size=14, bold=True)
            cell.alignment = Alignment(horizontal='center')
            row_offset = 2
        else:
            row_offset = 1

        # 表头
        header_fill = PatternFill(start_color='0E639C',
                                  end_color='0E639C',
                                  fill_type='solid')
        header_font = Font(bold=True, color='FFFFFF')
        for i, col in enumerate(columns, 1):
            cell = ws.cell(row=row_offset, column=i, value=col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center')

        # 数据
        for r_idx, row in enumerate(rows, row_offset + 1):
            for c_idx, val in enumerate(row, 1):
                cell = ws.cell(row=r_idx, column=c_idx,
                               value=val if val is not None else '')
                if isinstance(val, (int, float)):
                    cell.alignment = Alignment(horizontal='right')

        # 自动列宽
        for i, col in enumerate(columns, 1):
            max_len = len(str(col))
            for row in rows[:200]:
                if i - 1 < len(row):
                    v = row[i - 1]
                    if v is not None:
                        max_len = max(max_len, len(str(v)))
            ws.column_dimensions[get_column_letter(i)].width = min(
                max(max_len + 2, 10), 50)

        wb.save(filepath)
        return filepath

    def export_html(self, columns, rows, filepath,
                    title='', chart_svg=''):
        """导出 HTML 报表"""
        # 表格
        thead = ''.join(f'<th>{html.escape(str(c))}</th>'
                        for c in columns)
        tbody_rows = []
        for r in rows:
            cells = ''.join(_html_cell(v) for v in r)
            tbody_rows.append(f'<tr>{cells}</tr>')
        tbody = '\n'.join(tbody_rows)

        # 统计信息
        summary = (f"<p>生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} | "
                   f"共 {len(rows)} 行 × {len(columns)} 列</p>")

        html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{html.escape(title or 'LightDB Report')}</title>
<style>
body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif;
        margin: 20px; background: #f5f5f5; color: #2c2c2c; }}
h1 {{ color: #0078d4; border-bottom: 3px solid #0078d4;
       padding-bottom: 8px; }}
h1 .meta {{ font-size: 13px; color: #666; font-weight: normal;
            float: right; margin-top: 12px; }}
table {{ border-collapse: collapse; width: 100%; background: white;
         box-shadow: 0 2px 8px rgba(0,0,0,0.1); margin-top: 20px; }}
th {{ background: #0078d4; color: white; padding: 10px 12px;
      text-align: left; font-weight: bold; position: sticky; top: 0; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #eee; }}
tr:hover {{ background: #f0f8ff; }}
tr:nth-child(even) {{ background: #fafafa; }}
.footer {{ margin-top: 30px; color: #999; font-size: 12px;
           text-align: center; }}
.chart {{ margin: 20px 0; text-align: center; }}
</style>
</head>
<body>
<h1>{html.escape(title or 'LightDB Report')}
<span class="meta">{summary}</span></h1>
{chart_svg}
<table>
<thead><tr>{thead}</tr></thead>
<tbody>{tbody}</tbody>
</table>
<div class="footer">Generated by LightDB Studio</div>
</body>
</html>"""

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        return filepath

    def list_outputs(self):
        """列出历史输出"""
        outputs = []
        for f in self.out_dir.glob('*'):
            if f.is_file():
                outputs.append({
                    'path': str(f),
                    'name': f.name,
                    'size': f.stat().st_size,
                    'mtime': f.stat().st_mtime,
                })
        return sorted(outputs, key=lambda x: x['mtime'], reverse=True)

    def cleanup_outputs(self, keep_days: int = 30):
        """清理旧输出"""
        cutoff = time.time() - keep_days * 86400
        n = 0
        for f in self.out_dir.glob('*'):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        return n