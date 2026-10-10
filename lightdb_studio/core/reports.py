"""
报表系统
- 保存查询为报表模板
- 定时执行
- 导出 Excel / HTML / CSV
"""
import json
import os
import time
import html
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional

REPORT_DIR = Path.home() / '.lightdb_studio' / 'reports'
OUTPUT_DIR = REPORT_DIR / 'outputs'


@dataclass
class ReportTemplate:
    name: str = ""
    description: str = ""
    sql: str = ""
    connection: str = ""          # 连接名
    database: str = ""
    # 图表配置（可选）
    chart: Dict = field(default_factory=dict)
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


class ReportEngine:
    def __init__(self):
        self.dir = REPORT_DIR
        self.out_dir = OUTPUT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.templates: List[ReportTemplate] = []
        self._load()

    def _path(self):
        return self.dir / 'templates.json'

    def _load(self):
        if not self._path().exists():
            return
        try:
            with open(self._path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.templates = [ReportTemplate(**t) for t in data]
        except Exception:
            self.templates = []

    def save(self):
        data = [asdict(t) for t in self.templates]
        tmp = self._path().with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self._path())

    def add(self, tmpl: ReportTemplate):
        self.templates.append(tmpl)
        self.save()

    def update(self, idx: int, tmpl: ReportTemplate):
        if 0 <= idx < len(self.templates):
            tmpl.updated_at = time.time()
            self.templates[idx] = tmpl
            self.save()

    def remove(self, idx: int):
        if 0 <= idx < len(self.templates):
            del self.templates[idx]
            self.save()

    def get(self, name: str) -> Optional[ReportTemplate]:
        for t in self.templates:
            if t.name == name:
                return t
        return None

    # ==================== 导出 ====================

    def export_csv(self, columns, rows, filepath):
        import csv
        with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
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
            cells = ''.join(
                f'<td>{"<i style=\'color:#888\'>NULL</i>" if v is None else html.escape(str(v))}</td>'
                for v in r)
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