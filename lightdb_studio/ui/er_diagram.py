# -*- coding: utf-8 -*-
"""ER 图（P2-1：真实外键 / 手工登记关系 / 显式无关系）

关系来源优先级（**禁止按 `_id` 等命名猜测**）：
1. `SHOW CREATE TABLE` 中真实 FOREIGN KEY → source='foreign_key'
2. 用户手工登记关系（可增删，持久化 JSON，round-trip）→ source='manual'
3. 都没有 → source='none'，UI 显式提示"无关系"，不画猜测连线

表/列/类型/主键/唯一/索引均来自 `SHOW COLUMNS` / `SHOW INDEX` /
`SHOW CREATE TABLE`；渲染表盒 + PK/FK/UNIQUE/INDEX 标记 + 关系连线；
导出 PNG 并校验真实文件可读。
"""
import json
import math
import re
import time
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QFileDialog, QMessageBox, QGraphicsView, QGraphicsScene,
    QGraphicsRectItem, QGraphicsTextItem, QGraphicsLineItem,
    QGraphicsPathItem, QComboBox, QInputDialog,
)
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QColor, QPen, QBrush, QFont, QPainter, QImage, QPainterPath,
)

RELATIONS_FILE = Path.home() / '.lightdb_studio' / 'relations.json'

_FK_RE = re.compile(
    r'(?:CONSTRAINT\s+`?(\w+)`?\s+)?FOREIGN\s+KEY\s*\(([^)]+)\)\s*'
    r'REFERENCES\s+`?(\w+)`?\s*\(([^)]+)\)',
    re.IGNORECASE | re.DOTALL)


class ERExportError(RuntimeError):
    pass


# ==================== 手工关系持久化 ====================

class RelationStore:
    """手工登记关系持久化（默认 ~/.lightdb_studio/relations.json）。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else RELATIONS_FILE
        self.relations = []
        self.load()

    def load(self):
        self.relations = []
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(data, list):
                    for item in data:
                        rel = self._normalize(item)
                        if rel:
                            self.relations.append(rel)
        except Exception:
            self.relations = []
        return self.relations

    @staticmethod
    def _normalize(item):
        if not isinstance(item, dict):
            return None
        out = {
            'db': str(item.get('db') or ''),
            'from_table': str(item.get('from_table') or ''),
            'from_col': str(item.get('from_col') or ''),
            'to_table': str(item.get('to_table') or ''),
            'to_col': str(item.get('to_col') or ''),
            'source': 'manual',
            'created_at': item.get('created_at') or time.time(),
        }
        if not all(out[k] for k in ('from_table', 'from_col',
                                    'to_table', 'to_col')):
            return None
        return out

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.relations, ensure_ascii=False,
                                  indent=2), encoding='utf-8')
        tmp.replace(self.path)
        return str(self.path)

    @staticmethod
    def _same(a, b):
        return (a.get('from_table') == b.get('from_table') and
                a.get('from_col') == b.get('from_col') and
                a.get('to_table') == b.get('to_table') and
                a.get('to_col') == b.get('to_col'))

    def add(self, db, from_table, from_col, to_table, to_col):
        if not all(str(x or '').strip() for x in
                   (from_table, from_col, to_table, to_col)):
            raise ValueError('关系两端表/列不能为空')
        rel = {'db': str(db or ''), 'from_table': str(from_table),
               'from_col': str(from_col), 'to_table': str(to_table),
               'to_col': str(to_col), 'source': 'manual',
               'created_at': time.time()}
        for existing in self.relations:
            if existing.get('db') == rel['db'] and self._same(existing, rel):
                return existing
        self.relations.append(rel)
        self.save()
        return rel

    def remove(self, db, from_table, from_col, to_table, to_col):
        target = {'from_table': from_table, 'from_col': from_col,
                  'to_table': to_table, 'to_col': to_col}
        for i, rel in enumerate(self.relations):
            if rel.get('db') == str(db or '') and self._same(rel, target):
                del self.relations[i]
                self.save()
                return True
        return False

    def list_for(self, db):
        return [dict(r) for r in self.relations if r.get('db') == str(db or '')]

    def clear(self, db=None):
        if db is None:
            self.relations = []
        else:
            self.relations = [r for r in self.relations
                              if r.get('db') != str(db)]
        self.save()


# ==================== 真实元数据解析 ====================

def parse_foreign_keys(create_sql, table_name=''):
    """从 SHOW CREATE TABLE 文本解析 FOREIGN KEY 列表（真元数据）。"""
    text = '' if create_sql is None else str(create_sql)
    fks = []
    for m in _FK_RE.finditer(text):
        constraint, from_cols, to_table, to_cols = m.groups()
        left = [c.strip().strip('`') for c in from_cols.split(',')]
        right = [c.strip().strip('`') for c in to_cols.split(',')]
        for fc, tc in zip(left, right):
            fks.append({'constraint': constraint or '',
                        'from_table': str(table_name),
                        'from_col': fc,
                        'to_table': to_table,
                        'to_col': tc,
                        'source': 'foreign_key'})
    return fks


def parse_show_index_rows(rows, table_name=''):
    """SHOW INDEX 行 → [{'name','unique','columns'}]（容错短行）。"""
    index_map = {}
    order = []
    for row in rows or []:
        try:
            if isinstance(row, dict):
                name = str(row.get('Key_name') or row.get('name') or '')
                non_unique = row.get('Non_unique', 1)
                column = str(row.get('Column_name') or row.get('column') or '')
                seq = int(row.get('Seq_in_index') or 0)
            else:
                name = str(row[2]) if len(row) > 2 else ''
                non_unique = row[1] if len(row) > 1 else 1
                column = str(row[4]) if len(row) > 4 else ''
                seq = int(row[3]) if len(row) > 3 and str(row[3]).isdigit() else 0
            if not name:
                continue
            if name not in index_map:
                index_map[name] = {'name': name,
                                   'unique': str(non_unique) in ('0', 'False'),
                                   'columns': []}
                order.append(name)
            if column:
                index_map[name]['columns'].append((seq, column))
        except Exception:
            continue
    out = []
    for name in order:
        idx = index_map[name]
        idx['columns'] = [c for _s, c in sorted(idx['columns'])]
        out.append(idx)
    return out


def _extract_create_sql(rows):
    for row in rows or []:
        for value in (row if isinstance(row, (list, tuple)) else [row]):
            if isinstance(value, str) and 'CREATE TABLE' in value.upper():
                return value
    return ''


def build_er_model(conn, db_name, relation_store=None):
    """构建 ER 模型：真实外键 > 手工关系 > 无关系（不按命名猜测）。"""
    model = {'db': db_name, 'tables': {}, 'relations': [],
             'relation_source': 'none', 'warnings': [],
             'foreign_key_count': 0, 'manual_count': 0,
             'no_relation_message': ''}
    try:
        if db_name:
            conn.switch_database(db_name)
        table_names = list(conn.get_tables() or [])
    except Exception as exc:                          # noqa: BLE001
        model['warnings'].append(f'读取表失败: {exc}')
        return model

    for table in table_names:
        try:
            columns = list(conn.get_columns(table) or [])
        except Exception:
            columns = []
        indexes = []
        try:
            _cols, rows = conn.query(f"SHOW INDEX FROM `{table}`")
            indexes = parse_show_index_rows(rows, table)
        except Exception:
            pass
        if not indexes:
            # 回退：用 SHOW COLUMNS 的 key 信息构造 UNIQUE/PRIMARY 索引
            for col in columns:
                key = str(col.get('key') or '').upper()
                if key in ('PRI', 'UNI'):
                    indexes.append({'name': key, 'unique': True,
                                    'columns': [col.get('name')],
                                    'derived': True})
        create_sql = ''
        try:
            _cols, rows = conn.query(f"SHOW CREATE TABLE `{table}`")
            create_sql = _extract_create_sql(rows)
        except Exception:
            pass
        model['tables'][table] = {'columns': columns, 'indexes': indexes,
                                  'create_sql': create_sql,
                                  'foreign_keys': parse_foreign_keys(create_sql,
                                                                     table)}

    real_fks = []
    for table, info in model['tables'].items():
        real_fks.extend(info['foreign_keys'])
    model['foreign_key_count'] = len(real_fks)

    if real_fks:
        model['relations'] = real_fks
        model['relation_source'] = 'foreign_key'
        return model

    manual = []
    if relation_store is not None:
        for rel in relation_store.list_for(db_name):
            from_info = model['tables'].get(rel['from_table'])
            to_info = model['tables'].get(rel['to_table'])
            if not from_info or not to_info:
                model['warnings'].append(
                    f"手工关系 {rel['from_table']}.{rel['from_col']} → "
                    f"{rel['to_table']}.{rel['to_col']} 引用了不存在的表，已忽略")
                continue
            from_cols = {c.get('name') for c in from_info['columns']}
            to_cols = {c.get('name') for c in to_info['columns']}
            if rel['from_col'] not in from_cols or rel['to_col'] not in to_cols:
                model['warnings'].append(
                    f"手工关系 {rel['from_table']}.{rel['from_col']} → "
                    f"{rel['to_table']}.{rel['to_col']} 列不存在，已忽略")
                continue
            manual.append(dict(rel, source='manual'))
    model['manual_count'] = len(manual)
    if manual:
        model['relations'] = manual
        model['relation_source'] = 'manual'
        return model

    model['relation_source'] = 'none'
    msg = ('无关系：SHOW CREATE TABLE 中未发现 FOREIGN KEY，'
           '也没有手工登记关系（本工具不会按列名/`_id` 猜测关系）')
    model['no_relation_message'] = msg
    model['warnings'].append(msg)
    return model


# ==================== 添加手工关系对话框 ====================

class AddRelationDialog(QDialog):
    def __init__(self, tables_info, parent=None):
        super().__init__(parent)
        self.tables_info = tables_info or {}
        self.setWindowTitle('手工登记关系')
        self.resize(460, 220)
        layout = QVBoxLayout(self)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel('从表:'))
        self.from_table = QComboBox()
        self.from_table.addItems(list(self.tables_info.keys()))
        row1.addWidget(self.from_table)
        row1.addWidget(QLabel('列:'))
        self.from_col = QComboBox()
        row1.addWidget(self.from_col)
        layout.addLayout(row1)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel('到表:'))
        self.to_table = QComboBox()
        self.to_table.addItems(list(self.tables_info.keys()))
        row2.addWidget(self.to_table)
        row2.addWidget(QLabel('列:'))
        self.to_col = QComboBox()
        row2.addWidget(self.to_col)
        layout.addLayout(row2)
        self.from_table.currentTextChanged.connect(self._refresh_cols)
        self.to_table.currentTextChanged.connect(self._refresh_cols)
        self._refresh_cols()
        buttons = QHBoxLayout()
        buttons.addStretch()
        ok = QPushButton('确定')
        ok.clicked.connect(self._accept)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _refresh_cols(self):
        for combo, table in ((self.from_col, self.from_table.currentText()),
                             (self.to_col, self.to_table.currentText())):
            combo.clear()
            info = self.tables_info.get(table) or {}
            combo.addItems([c.get('name') for c in info.get('columns') or []])

    def values(self):
        return {'from_table': self.from_table.currentText(),
                'from_col': self.from_col.currentText(),
                'to_table': self.to_table.currentText(),
                'to_col': self.to_col.currentText()}

    def _accept(self):
        vals = self.values()
        if not all(vals.values()):
            QMessageBox.warning(self, '提示', '请选择两端的表和列')
            return
        if (vals['from_table'] == vals['to_table'] and
                vals['from_col'] == vals['to_col']):
            QMessageBox.warning(self, '提示', '关系两端不能是同一列')
            return
        self.accept()


# ==================== ER 图对话框 ====================

class ERDiagramDialog(QDialog):
    def __init__(self, conn, db_name, parent=None, relation_store=None):
        super().__init__(parent)
        self.conn = conn
        self.db_name = db_name
        self.store = relation_store or RelationStore()
        self.tables_info = {}
        self.relations = []
        self.relation_source = 'none'
        self.warnings = []
        self.no_relation_message = ''
        self.model = {}
        self.tables = {}
        self.setWindowTitle(f'ER 图 - {db_name}')
        self.resize(1200, 800)
        self._build_ui()
        self.load_model()

    # ---------- UI ----------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel('布局:'))
        self.layout_combo = QComboBox()
        self.layout_combo.addItems(['圆形', '网格', '力导向'])
        self.layout_combo.currentTextChanged.connect(self._redraw)
        top.addWidget(self.layout_combo)

        self.source_label = QLabel('关系来源: -')
        self.source_label.setStyleSheet('color: #9cdcfe;')
        top.addWidget(self.source_label)
        top.addStretch()

        self.add_rel_btn = QPushButton('➕ 手工关系')
        self.add_rel_btn.clicked.connect(self._on_add_relation)
        top.addWidget(self.add_rel_btn)
        self.del_rel_btn = QPushButton('🗑 删除关系')
        self.del_rel_btn.clicked.connect(self._on_delete_relation)
        top.addWidget(self.del_rel_btn)
        self.export_btn = QPushButton('📤 导出 PNG')
        self.export_btn.clicked.connect(self._on_export_png)
        top.addWidget(self.export_btn)
        refresh_btn = QPushButton('🔄 刷新')
        refresh_btn.clicked.connect(self.load_model)
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        self.info_label = QLabel('')
        self.info_label.setStyleSheet(
            'color: #969696; padding: 4px; background: #252526;')
        layout.addWidget(self.info_label)

        self.view = QGraphicsView()
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setBackgroundBrush(QBrush(QColor('#1e1e1e')))
        self.scene = QGraphicsScene()
        self.view.setScene(self.scene)
        layout.addWidget(self.view, 1)

    # ---------- 模型 ----------

    def load_model(self):
        self.model = build_er_model(self.conn, self.db_name, self.store)
        self.tables_info = self.model.get('tables') or {}
        self.relations = self.model.get('relations') or []
        self.relation_source = self.model.get('relation_source', 'none')
        self.warnings = self.model.get('warnings') or []
        self.no_relation_message = self.model.get('no_relation_message', '')
        self.tables = {t: {'cols': info.get('columns') or [],
                           'indexes': info.get('indexes') or [],
                           'rect': None}
                       for t, info in self.tables_info.items()}
        source_text = {
            'foreign_key': f"真实外键 ({len(self.relations)})",
            'manual': f"手工登记 ({len(self.relations)})",
            'none': '无关系',
        }.get(self.relation_source, self.relation_source)
        self.source_label.setText(f'关系来源: {source_text}')
        extra = ''
        if self.relation_source == 'none':
            extra = ' | ⚠ ' + self.no_relation_message
        elif self.warnings:
            extra = ' | ⚠ ' + '；'.join(self.warnings[:2])
        self.info_label.setText(
            f"{len(self.tables)} 张表 | {len(self.relations)} 条关系"
            f"（source={self.relation_source}）{extra}")
        self._redraw()
        return self.model

    def manual_relations(self):
        return [r for r in self.relations if r.get('source') == 'manual']

    def add_manual_relation(self, from_table, from_col, to_table, to_col):
        rel = self.store.add(self.db_name, from_table, from_col,
                             to_table, to_col)
        self.load_model()
        return rel

    def remove_manual_relation(self, from_table, from_col, to_table, to_col):
        ok = self.store.remove(self.db_name, from_table, from_col,
                               to_table, to_col)
        self.load_model()
        return ok

    # ---------- 交互 ----------

    def _on_add_relation(self):
        if not self.tables_info:
            QMessageBox.information(self, '提示', '没有可登记关系的表')
            return
        dlg = AddRelationDialog(self.tables_info, self)
        if dlg.exec() == QDialog.Accepted:
            vals = dlg.values()
            try:
                self.add_manual_relation(**vals)
            except Exception as exc:                  # noqa: BLE001
                QMessageBox.critical(self, '错误', str(exc))

    def _on_delete_relation(self):
        rels = self.manual_relations()
        if not rels:
            QMessageBox.information(self, '提示', '没有手工登记的关系')
            return
        labels = [f"{r['from_table']}.{r['from_col']} → "
                  f"{r['to_table']}.{r['to_col']}" for r in rels]
        label, ok = QInputDialog.getItem(
            self, '删除关系', '选择要删除的手工关系:', labels, 0, False)
        if not ok:
            return
        rel = rels[labels.index(label)]
        self.remove_manual_relation(rel['from_table'], rel['from_col'],
                                    rel['to_table'], rel['to_col'])

    # ---------- 布局 ----------

    def _box_height(self, info):
        n = len(info['cols']) + len(info['indexes'])
        return 30 + 18 * max(1, n) + 6

    def _redraw(self):
        self.scene.clear()
        if not self.tables:
            self.scene.addText('无表可展示')
            return
        mode = self.layout_combo.currentText()
        if mode == '圆形':
            self._layout_circular()
        elif mode == '网格':
            self._layout_grid()
        else:
            self._layout_force()
        for rel in self.relations:
            self._draw_relation(rel)
        for name, info in self.tables.items():
            self._draw_table(name, info)
        if not self.relations:
            msg = self.no_relation_message or '无关系'
            text = self.scene.addText(msg)
            text.setDefaultTextColor(QColor('#f48771'))
            text.setFont(QFont('Microsoft YaHei', 11))
            text.setPos(40, 40)

    def _layout_circular(self):
        n = len(self.tables)
        radius = max(220, n * 45)
        cx, cy = 700, 420
        for i, (t, info) in enumerate(self.tables.items()):
            angle = 2 * math.pi * i / max(1, n)
            x = cx + radius * math.cos(angle) - 100
            y = cy + radius * math.sin(angle)
            info['rect'] = QRectF(x, y, 220, self._box_height(info))

    def _layout_grid(self):
        cols = max(1, int(math.sqrt(len(self.tables))) + 1)
        for i, (t, info) in enumerate(self.tables.items()):
            r, c = divmod(i, cols)
            info['rect'] = QRectF(c * 280 + 40, r * (self._box_height(info) + 40),
                                  220, self._box_height(info))

    def _layout_force(self):
        import random
        n = len(self.tables)
        for i, (t, info) in enumerate(self.tables.items()):
            angle = 2 * math.pi * i / max(1, n)
            radius = 260 + random.randint(-40, 40)
            info['rect'] = QRectF(700 + radius * math.cos(angle) - 110,
                                  430 + radius * math.sin(angle),
                                  220, self._box_height(info))

    # ---------- 绘制 ----------

    def _relation_color(self, rel):
        if rel.get('source') == 'manual':
            return QColor('#dcdcaa')
        return QColor('#4ec9b0')

    def _draw_relation(self, rel):
        a = self.tables.get(rel.get('from_table'))
        b = self.tables.get(rel.get('to_table'))
        if not a or not b or a['rect'] is None or b['rect'] is None:
            return
        ra, rb = a['rect'], b['rect']
        p1 = QPointF(ra.center().x(), ra.bottom())
        p2 = QPointF(rb.center().x(), rb.top())
        if rb.center().y() < ra.center().y():
            p1 = QPointF(ra.center().x(), ra.top())
            p2 = QPointF(rb.center().x(), rb.bottom())
        color = self._relation_color(rel)
        pen = QPen(color, 2, Qt.SolidLine if rel.get('source') == 'foreign_key'
                   else Qt.DashLine)
        line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
        line.setPen(pen)
        self.scene.addItem(line)
        # 箭头（指向被引用表）
        arrow = QGraphicsPathItem()
        path = QPainterPath(p2)
        dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
        length = math.hypot(dx, dy) or 1
        ux, uy = dx / length, dy / length
        size = 10
        for sign in (-1, 1):
            px = p2.x() - ux * size - sign * uy * size * 0.5
            py = p2.y() - uy * size + sign * ux * size * 0.5
            path.lineTo(px, py)
        path.closeSubpath()
        arrow.setPath(path)
        arrow.setBrush(QBrush(color))
        arrow.setPen(QPen(Qt.NoPen))
        self.scene.addItem(arrow)

    def _fk_source_cols(self):
        out = set()
        for rel in self.relations:
            out.add((rel.get('from_table'), rel.get('from_col')))
        return out

    def _draw_table(self, name, info):
        r = info['rect']
        if r is None:
            return
        rect = QGraphicsRectItem(r)
        rect.setBrush(QBrush(QColor('#252526')))
        rect.setPen(QPen(QColor('#007acc'), 2))
        self.scene.addItem(rect)
        header = QGraphicsRectItem(QRectF(r.x(), r.y(), r.width(), 26))
        header.setBrush(QBrush(QColor('#0e639c')))
        header.setPen(QPen(Qt.NoPen))
        self.scene.addItem(header)
        title = QGraphicsTextItem(name)
        title.setDefaultTextColor(QColor('#ffffff'))
        title.setFont(QFont('Microsoft YaHei', 10, QFont.Bold))
        title.setPos(r.x() + 8, r.y() + 3)
        self.scene.addItem(title)

        fk_cols = self._fk_source_cols()
        unique_names = set()
        for idx in info['indexes']:
            if idx.get('unique'):
                unique_names.update(idx.get('columns') or [])
        y = r.y() + 32
        for col in info['cols']:
            cname = str(col.get('name', ''))
            key = str(col.get('key') or '').upper()
            marks = []
            if key == 'PRI':
                marks.append('🔑 PK')
            if (name, cname) in fk_cols:
                marks.append('FK')
            if cname in unique_names and key != 'PRI':
                marks.append('UQ')
            prefix = (' '.join(marks) + ' ') if marks else '  '
            text = f"{prefix}{cname} {col.get('type', '')}"
            item = QGraphicsTextItem(text)
            if 'PK' in marks:
                color = QColor('#dcdcaa')
            elif 'FK' in marks:
                color = QColor('#4ec9b0')
            elif 'UQ' in marks:
                color = QColor('#c586c0')
            else:
                color = QColor('#d4d4d4')
            item.setDefaultTextColor(color)
            item.setFont(QFont('Consolas', 8))
            item.setPos(r.x() + 6, y)
            self.scene.addItem(item)
            y += 18
        for idx in info['indexes']:
            iname = str(idx.get('name', '')).upper()
            if idx.get('derived') or iname in ('PRI', 'PRIMARY'):
                continue
            label = ('UNIQUE ' if idx.get('unique') else 'IDX ') + \
                str(idx.get('name')) + '(' + ','.join(idx.get('columns') or []) + ')'
            item = QGraphicsTextItem(label)
            item.setDefaultTextColor(QColor('#808080'))
            item.setFont(QFont('Consolas', 7))
            item.setPos(r.x() + 6, y)
            self.scene.addItem(item)
            y += 16

    # ---------- 导出 ----------

    def export_png(self, path=None, quiet=False):
        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self, '导出 ER 图', f'{self.db_name}_er.png',
                'PNG 图片 (*.png)')
        if not path:
            return ''
        rect = self.scene.itemsBoundingRect()
        if rect.isEmpty():
            rect = QRectF(0, 0, 800, 600)
        margin = 20
        rect.adjust(-margin, -margin, margin, margin)
        width = max(1, int(rect.width()))
        height = max(1, int(rect.height()))
        img = QImage(width, height, QImage.Format_ARGB32)
        img.fill(QColor('#1e1e1e'))
        painter = QPainter(img)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            self.scene.render(painter, target=QRectF(img.rect()),
                              source=rect)
        finally:
            painter.end()
        if img.isNull() or not img.save(path, 'PNG'):
            raise ERExportError(f'PNG 导出失败: {path}')
        check = QImage(path)
        if check.isNull() or check.width() <= 0:
            raise ERExportError(f'PNG 文件无法解析: {path}')
        if not quiet:
            QMessageBox.information(self, '成功', f'已导出:\n{path}')
        return str(path)

    def _on_export_png(self):
        try:
            self.export_png()
        except ERExportError as exc:
            QMessageBox.critical(self, '导出失败', str(exc))
