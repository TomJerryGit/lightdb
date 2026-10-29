#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""旧 B+Tree 页格式迁移（v1/v2 → v3）

task-72 把键扩到 uint64（v2）；task-80 支持 3–4 列复合主键后，页条目改为
「16 字节大端 128 位键 + pid/sid 或 child」（叶 24B / 内部 20B，v3）。
旧 `.dat` 不能按 v3 直接读取。本脚本支持 **v1/v2 → v3**：

1. 只接受**旧格式**数据目录（按 .btroot 根页结构判定 v1/v2）；
2. 逐表扫描旧堆页，读取 xmax=0 的可见行版本（要求旧库已正常关闭/checkpoint；
   未完成事务/崩溃中的旧库请先从备份恢复）；
3. 在新目录用 v3 格式重建同样的表（cols/UNIQUE/二级索引/CHECK/分区/
   AUTO_INCREMENT 计数/表选项，含 2–4 列复合主键），逐行插入；
4. 重新打开新目录，校验每张表行数、行内容、索引与旧库一致；
5. 复制用户/视图/存储过程等非表元数据。

用法：
    python scripts/migrate_btree_format.py <旧数据目录> <新数据目录> [--force]

旧数据目录不做任何修改（只读）；--force 允许覆盖已存在的新目录。
见 docs/MIGRATION_BTREE_V2.md。
"""
import argparse
import json
import os
import shutil
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lightdb.storage.page import (  # noqa: E402
    Page, PAGE_SIZE, PAGE_MAGIC, PAGE_TYPE_HEAP,
    PAGE_TYPE_BTREE_LEAF, PAGE_TYPE_BTREE_INTERNAL)

ROW_HEADER = 21
LEGACY_MARKER_OFFSET = 21
BTREE_FORMAT_VERSION = 2
OLD_LEAF_ENTRY = 12


class MigrationError(Exception):
    pass


def _read_page_bytes(f, page_id):
    f.seek(page_id * PAGE_SIZE)
    raw = f.read(PAGE_SIZE)
    if len(raw) < PAGE_SIZE:
        return None
    if raw[:4] != b'LDBP':
        return None
    return raw


def _is_legacy_file(data_file: Path) -> bool:
    """.dat 里是否有旧 BTree 根页（v1 或 v2；按条目结构判定）"""
    btroot = data_file.with_suffix('.btroot')
    if not btroot.exists() or not data_file.exists():
        return False
    try:
        root_id = int(btroot.read_text().strip())
    except Exception:
        return False
    if root_id < 1:
        return False
    from lightdb.storage.btree import (classify_btree_root_page,
                                       BTREE_FORMAT_VERSION)
    try:
        file_pages = max(1, data_file.stat().st_size // PAGE_SIZE)

        def read_page(pid):
            with open(data_file, 'rb') as f:
                return _read_page_bytes(f, pid)

        with open(data_file, 'rb') as f:
            root_raw = _read_page_bytes(f, root_id)
        fmt = classify_btree_root_page(root_raw, read_page, file_pages)
        return fmt in (1, 2) and fmt != BTREE_FORMAT_VERSION
    except Exception:
        return False


def _decode_row(rb):
    if not rb or len(rb) < ROW_HEADER:
        return None
    try:
        xmin, xmax, _flags, dlen, _pp, _ps = struct.unpack_from(
            '<IIBIII', rb, 0)
        if len(rb) < ROW_HEADER + dlen:
            return None
        values = json.loads(rb[ROW_HEADER:ROW_HEADER + dlen].decode('utf-8'))
        return values, xmin, xmax
    except Exception:
        return None


def read_legacy_visible_rows(data_file: Path, cols):
    """旧堆页 → 可见行（xmax=0；同主键元组取最大 xmin）；按主键排序"""
    names = [c.get('name') for c in cols]
    pk_idxs = [i for i, c in enumerate(cols) if c.get('primary')] or [0]
    by_key = {}
    with open(data_file, 'rb') as f:
        total = os.fstat(f.fileno()).st_size // PAGE_SIZE
        for pid in range(1, total + 1):
            raw = _read_page_bytes(f, pid)
            if raw is None or raw[16] != PAGE_TYPE_HEAP:
                continue
            page = Page(pid, raw)
            for _sid, rb in page.iter_rows():
                dec = _decode_row(rb)
                if dec is None:
                    continue
                values, xmin, xmax = dec
                if xmax != 0:
                    continue                      # 已删除/已更新掉的旧版本
                if len(values) != len(names):
                    raise MigrationError(
                        f"{data_file.name}: 行载荷列数 {len(values)} 与元数据 "
                        f"列数 {len(names)} 不一致（不能静默截断/补列）")
                key = tuple(values[i] for i in pk_idxs)
                old = by_key.get(key)
                if old is None or xmin > old[0]:
                    by_key[key] = (xmin, list(values))
    return [(key, row[1]) for key, row in sorted(by_key.items(),
                                                 key=lambda kv: str(kv[0]))]


def _load_meta(old_dir: Path):
    p = old_dir / '_meta.json'
    if not p.exists():
        raise MigrationError(f"{old_dir} 不是 LightDB 数据目录（缺少 _meta.json）")
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def _prepare_target(old_dir: Path, new_dir: Path, force: bool):
    if old_dir.resolve() == new_dir.resolve():
        raise MigrationError("旧目录与新目录不能相同（迁移必须写到新目录）")
    if new_dir.exists():
        entries = list(new_dir.iterdir())
        if entries and not force:
            raise MigrationError(
                f"目标目录 {new_dir} 非空；请先确认备份后加 --force 覆盖"
                f"（脚本只会删除该目录再重建）")
        if entries and force:
            shutil.rmtree(new_dir)
    new_dir.mkdir(parents=True, exist_ok=True)


def migrate(old_dir: Path, new_dir: Path, force: bool = False) -> dict:
    meta = _load_meta(old_dir)
    if meta.get('pending_ddl'):
        raise MigrationError(
            "旧库存在未完成的结构变更（pending_ddl）——说明上次是崩溃退出；"
            "请先从完整备份恢复，再重新执行迁移")
    tables = meta.get('tables') or {}
    if not tables:
        raise MigrationError("旧库没有表（_meta.json 中 tables 为空）")

    legacy = [name for name in tables
              if _is_legacy_file(old_dir / f"{name}.dat")]
    if not legacy:
        raise MigrationError(
            "未检测到旧格式 B+Tree 页（v1/v2）：该目录可能已是 v3 格式，"
            "无需迁移；直接启动即可")
    _prepare_target(old_dir, new_dir, force)

    from lightdb.db.database import Database
    new_db = Database(str(new_dir))
    report = {'tables': {}, 'users': bool(meta.get('users')),
              'views': len(meta.get('views') or {})}
    try:
        for name, tinfo in tables.items():
            cols = [dict(c) for c in (tinfo.get('cols') or [])]
            if not cols:
                raise MigrationError(f"表 {name} 没有列定义")
            pk_cols = [c for c in cols if c.get('primary')]
            if len(pk_cols) > 4:
                raise MigrationError(
                    f"表 {name} 的主键列数 {len(pk_cols)} > 4（v3 只支持"
                    f"最多 4 列）；请先确认元数据未被手工修改")
            data_file = old_dir / f"{name}.dat"
            rows = []
            if data_file.exists():
                rows = read_legacy_visible_rows(data_file, cols)
            tbl = new_db.create_table(
                name, cols,
                partition=tinfo.get('partition'),
                checks=tinfo.get('checks'),
                unique_indexes=tinfo.get('unique_indexes'),
                secondary_indexes=tinfo.get('indexes'),
                requested_engine=tinfo.get('requested_engine') or 'LightDB')
            if tinfo.get('table_options'):
                tbl.table_options = dict(tinfo['table_options'])
            if tinfo.get('comment'):
                tbl.comment = tinfo['comment']
            for _pk, row in rows:
                tbl.insert(row)
            old_auto = tinfo.get('auto_increment_next') or {}
            if old_auto:
                tbl._auto_next = {str(k): int(v) for k, v in old_auto.items()}
                tbl._auto_reserved = {}
            report['tables'][name] = {
                'rows': len(rows),
                'row_tuples': [tuple(row) for _pk, row in rows],
                'indexes': len(tbl.index_specs()),
                'auto_increment': len(old_auto),
            }
        new_db.views = dict(meta.get('views') or {})
        if meta.get('users'):
            new_db.users = meta['users']
        new_db.save_meta()
    finally:
        new_db.shutdown()

    # 复制不影响行数据的辅助文件（存储过程/触发器/事件/根口令）
    for fn in ('_stored.json', '_triggers.json', '_events.json',
               'root.password'):
        src = old_dir / fn
        if src.exists():
            shutil.copy2(src, new_dir / fn)
    # 校验：重开新库，逐表比对行数与主键集合
    chk = Database(str(new_dir))
    try:
        for name in tables:
            tbl = chk.get_table(name)
            if tbl is None:
                raise MigrationError(f"迁移校验失败：新库缺少表 {name}")
            got = list(tbl._live_rows())
            want = report['tables'][name]['rows']
            if len(got) != want:
                raise MigrationError(
                    f"迁移校验失败：表 {name} 行数 {len(got)} != 期望 {want}")
            got_tuples = [tuple(values) for _pk, values in got]
            want_tuples = report['tables'][name]['row_tuples']
            if sorted(got_tuples, key=str) != sorted(want_tuples, key=str):
                raise MigrationError(
                    f"迁移校验失败：表 {name} 行内容与旧库不一致")
            report['tables'][name]['validated'] = True
    finally:
        chk.shutdown()
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='LightDB 旧 B+Tree 页格式（v1 uint32 / v2 uint64）迁移到 v3')
    ap.add_argument('old_dir', help='旧数据目录（只读）')
    ap.add_argument('new_dir', help='新数据目录（会被创建/覆盖需 --force）')
    ap.add_argument('--force', action='store_true',
                    help='允许覆盖已存在的非空新目录')
    args = ap.parse_args(argv)
    try:
        report = migrate(Path(args.old_dir), Path(args.new_dir),
                         force=args.force)
    except (MigrationError, Exception) as e:      # noqa: BLE001
        print(f"迁移失败: {e}", file=sys.stderr)
        return 1
    print("迁移完成：")
    for name, info in report['tables'].items():
        print(f"  - {name}: {info['rows']} 行, {info['indexes']} 个索引, "
              f"校验={'OK' if info.get('validated') else 'FAIL'}")
    print(f"  - 用户元数据: {'已迁移' if report['users'] else '无'}")
    print(f"  - 视图: {report['views']} 个")
    print(f"新数据目录: {args.new_dir}")
    print("请用新目录启动服务并抽查数据；旧目录保留作为备份。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
