"""
SQL 片段库
- 内置常用片段
- 用户自定义片段
- 团队共享片段（通过文件）
"""
import json
import time
import os
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List

SNIPPET_DIR = Path.home() / '.lightdb_studio' / 'snippets'
SHARED_SNIPPETS = SNIPPET_DIR / 'shared'


@dataclass
class Snippet:
    name: str
    sql: str
    category: str = "通用"
    description: str = ""
    tags: list = None
    created_at: float = 0
    updated_at: float = 0

    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if not self.created_at:
            self.created_at = time.time()
        if not self.updated_at:
            self.updated_at = self.created_at


# 内置片段
BUILTIN_SNIPPETS = [
    Snippet('查询前 100 行', 'SELECT * FROM `table_name` LIMIT 100;',
            '查询', '快速预览表数据', ['select', 'preview']),
    Snippet('条件查询', "SELECT * FROM `table_name`\nWHERE `col` = 'value'\nLIMIT 100;",
            '查询', '带 WHERE 的查询', ['select', 'where']),
    Snippet('按组统计', 'SELECT `group_col`, COUNT(*) AS cnt\n'
                       'FROM `table_name`\nGROUP BY `group_col`\nORDER BY cnt DESC;',
            '查询', 'GROUP BY 分组统计', ['group', 'count']),
    Snippet('去重查询', 'SELECT DISTINCT `col` FROM `table_name`;',
            '查询', 'DISTINCT 去重', ['distinct']),
    Snippet('多表 JOIN', 'SELECT a.*, b.*\n'
                        'FROM `table_a` a\n'
                        'INNER JOIN `table_b` b ON a.id = b.a_id;',
            '查询', '两表内连接', ['join']),
    Snippet('LEFT JOIN', 'SELECT a.*, b.*\n'
                        'FROM `table_a` a\n'
                        'LEFT JOIN `table_b` b ON a.id = b.a_id;',
            '查询', '左外连接', ['join', 'left']),
    Snippet('INSERT 单行', "INSERT INTO `table_name` (`col1`, `col2`) VALUES ('v1', 'v2');",
            'DML', '插入单行', ['insert']),
    Snippet('INSERT 多行', "INSERT INTO `table_name` (`col1`, `col2`) VALUES\n"
                          "  ('v1', 'v2'),\n  ('v3', 'v4');",
            'DML', '批量插入', ['insert', 'batch']),
    Snippet('UPDATE', "UPDATE `table_name` SET `col` = 'new_value'\nWHERE `id` = 1;",
            'DML', '更新单行', ['update']),
    Snippet('DELETE', "DELETE FROM `table_name` WHERE `id` = 1;",
            'DML', '删除单行', ['delete']),
    Snippet('建表模板', 'CREATE TABLE `new_table` (\n'
                       '  `id` INT PRIMARY KEY,\n'
                       '  `name` VARCHAR(100) NOT NULL,\n'
                       '  `created_at` DATETIME\n'
                       ');',
            'DDL', '新建表模板', ['create', 'table']),
    Snippet('加索引', 'CREATE INDEX `idx_col` ON `table_name` (`col`);',
            'DDL', '创建普通索引', ['index']),
    Snippet('窗口函数-排名', 'SELECT `name`, `salary`,\n'
                            '  ROW_NUMBER() OVER (PARTITION BY `dept` '
                            'ORDER BY `salary` DESC) AS rn\n'
                            'FROM `employees`;',
            '高级', '分组内排名', ['window', 'rank']),
    Snippet('CTE 递归', 'WITH RECURSIVE `nums` AS (\n'
                       '  SELECT 1 AS n\n'
                       '  UNION ALL\n'
                       '  SELECT n + 1 FROM `nums` WHERE n < 10\n'
                       ')\nSELECT * FROM `nums`;',
            '高级', '递归 CTE', ['cte', 'recursive']),
    Snippet('JSON 路径查询', "SELECT * FROM `table_name`\n"
                            "WHERE `json_col`->'$.key' = 'value';",
            '高级', 'JSON 字段过滤', ['json']),
    Snippet('全文检索', "SELECT * FROM `articles`\n"
                       "WHERE MATCH(`content`) AGAINST('keyword');",
            '高级', '全文索引查询', ['fulltext', 'match']),
    Snippet('事务模板', 'BEGIN;\n'
                       '-- 语句1\n'
                       '-- 语句2\n'
                       'COMMIT;',
            '事务', '事务模板', ['transaction']),
    Snippet('存储过程', 'CREATE PROCEDURE `proc_name`(IN `p1` INT)\n'
                       'BEGIN\n'
                       '  -- 过程体\n'
                       'END;',
            '高级', '存储过程模板', ['procedure']),
]


class SnippetManager:
    def __init__(self):
        self.path = SNIPPET_DIR / 'snippets.json'
        self.user_snippets: List[Snippet] = []
        self.shared_snippets: List[Snippet] = []
        SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
        SHARED_SNIPPETS.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self):
        # 用户片段
        if self.path.exists():
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.user_snippets = [Snippet(**s) for s in data]
            except Exception:
                self.user_snippets = []
        else:
            # 首次运行，初始化几个用户片段副本
            self.user_snippets = []

        # 团队共享片段
        self.shared_snippets = []
        for fp in SHARED_SNIPPETS.glob('*.json'):
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for s in data:
                    snip = Snippet(**s)
                    snip.category = f"[团队] {snip.category}"
                    self.shared_snippets.append(snip)
            except Exception:
                pass

    def save(self):
        data = [asdict(s) for s in self.user_snippets]
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def add(self, snippet: Snippet):
        self.user_snippets.append(snippet)
        self.save()

    def update(self, index: int, snippet: Snippet):
        if 0 <= index < len(self.user_snippets):
            snippet.updated_at = time.time()
            self.user_snippets[index] = snippet
            self.save()

    def remove(self, snippet: Snippet):
        self.user_snippets = [s for s in self.user_snippets
                              if not (s.name == snippet.name
                                      and s.sql == snippet.sql)]
        self.save()

    def get_all(self):
        """所有片段（含内置）"""
        return self.user_snippets + BUILTIN_SNIPPETS + self.shared_snippets

    def search(self, keyword: str):
        if not keyword:
            return self.get_all()
        kw = keyword.lower()
        return [s for s in self.get_all()
                if kw in s.name.lower()
                or kw in s.sql.lower()
                or kw in s.description.lower()
                or any(kw in t.lower() for t in s.tags)]

    def export_to_file(self, filepath: str, snippets=None):
        """导出片段到文件"""
        if snippets is None:
            snippets = self.user_snippets
        data = [asdict(s) for s in snippets]
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def import_from_file(self, filepath: str) -> int:
        """从文件导入片段"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        count = 0
        for s in data:
            try:
                # 去重：按 name
                if any(x.name == s['name'] for x in self.user_snippets):
                    s['name'] = s['name'] + ' (导入)'
                self.user_snippets.append(Snippet(**s))
                count += 1
            except Exception:
                pass
        self.save()
        return count

    def save_as_team_snippet(self, snippet: Snippet, team_name: str):
        """保存到共享目录"""
        fp = SHARED_SNIPPETS / f"{team_name}.json"
        data = []
        if fp.exists():
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = []
        # 去重
        data = [d for d in data if d.get('name') != snippet.name]
        data.append(asdict(snippet))
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)