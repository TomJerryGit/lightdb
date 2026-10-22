# -*- coding: utf-8 -*-
"""离线 SQL 示例库（不依赖 AI 配置；task-43）。

每条示例都带说明与执行注意事项，SQL 必须能通过引擎 parser；存储过程示例
使用本引擎 CALL 兼容子集（不返回结果集）。UI 可预览/一键插入编辑器。
"""
from collections import OrderedDict

CATEGORY_LABELS = OrderedDict([
    ('table', '建表'),
    ('index', '索引'),
    ('view', '视图'),
    ('procedure', '存储过程'),
    ('query', '查询'),
    ('update', '更新'),
])

EXAMPLES = [
    {
        'id': 'create_users',
        'category': 'table',
        'title': '建表：用户表（主键/非空/唯一/时间）',
        'description': '演示主键自增、NOT NULL、UNIQUE、DEFAULT CURRENT_TIMESTAMP。',
        'warning': '执行前确认表名不冲突；DROP/ALTER 不在示例内。',
        'sql': (
            'CREATE TABLE IF NOT EXISTS demo_users (\n'
            '    id INT PRIMARY KEY AUTO_INCREMENT,\n'
            '    name VARCHAR(64) NOT NULL,\n'
            '    email VARCHAR(128) UNIQUE,\n'
            '    created DATETIME DEFAULT CURRENT_TIMESTAMP\n'
            ');'),
    },
    {
        'id': 'create_orders',
        'category': 'table',
        'title': '建表：订单表（外键语义用普通列表达）',
        'description': '演示 DECIMAL/INT/状态列；本引擎 FK 能力有限，用普通列 + 索引。',
        'warning': '外键约束按引擎能力显式禁用；不要假定 FK 生效。',
        'sql': (
            'CREATE TABLE IF NOT EXISTS demo_orders (\n'
            '    id INT PRIMARY KEY AUTO_INCREMENT,\n'
            '    user_id INT NOT NULL,\n'
            '    amount DECIMAL(12, 2) NOT NULL DEFAULT 0.00,\n'
            '    status VARCHAR(16) NOT NULL DEFAULT \'new\',\n'
            '    created DATETIME DEFAULT CURRENT_TIMESTAMP\n'
            ');'),
    },
    {
        'id': 'index_email',
        'category': 'index',
        'title': '索引：邮箱唯一索引',
        'description': '为 demo_users.email 建唯一索引。',
        'warning': '重复数据会导致建索引失败，先清理/去重。',
        'sql': 'CREATE UNIQUE INDEX idx_demo_users_email '
               'ON demo_users (email);',
    },
    {
        'id': 'view_users',
        'category': 'view',
        'title': '视图：用户概览',
        'description': '基于 demo_users 的只读视图（列子集）。',
        'warning': '视图能力需引擎支持；执行失败会显式报错。',
        'sql': ('CREATE VIEW v_demo_users AS\n'
                'SELECT id, name, email, created\n'
                'FROM demo_users;'),
    },
    {
        'id': 'procedure_counter',
        'category': 'procedure',
        'title': '存储过程：局部变量 + 参数',
        'description': '本引擎 CALL 兼容子集：IN 参数、DECLARE、SET，不返回结果集。',
        'warning': ('本引擎 CALL 不返回多结果集：过程体内不要写会返回结果集的 '
                    'SELECT；示例只做变量计算。'),
        'sql': (
            'CREATE PROCEDURE demo_proc_counter(IN n INT)\n'
            'BEGIN\n'
            '    DECLARE v INT;\n'
            '    SET v = n + 1;\n'
            'END'),
    },
    {
        'id': 'query_users',
        'category': 'query',
        'title': '查询：分页 + 过滤 + 排序',
        'description': '演示 WHERE/ORDER BY/LIMIT 的常用分页查询。',
        'warning': '表/列不存在时引擎会显式报错（示例按 demo_users 命名）。',
        'sql': ('SELECT id, name, created\n'
                'FROM demo_users\n'
                'WHERE id > 0\n'
                'ORDER BY created DESC\n'
                'LIMIT 10;'),
    },
    {
        'id': 'update_user',
        'category': 'update',
        'title': '更新：按主键修改单行',
        'description': '演示带 WHERE 主键的单行更新（防误更新全表）。',
        'warning': '执行前确认 WHERE 条件；示例不提供无 WHERE 的 UPDATE。',
        'sql': ("UPDATE demo_users\n"
                "SET name = 'new-name'\n"
                "WHERE id = 1;"),
    },
]


def all_examples():
    return [dict(e) for e in EXAMPLES]


def examples_by_category(category):
    return [dict(e) for e in EXAMPLES if e['category'] == category]


def get_example(example_id):
    for example in EXAMPLES:
        if example['id'] == example_id:
            return dict(example)
    return None


def validate_examples():
    """全部示例必须通过引擎 parser；存储过程还需符合 CALL 兼容子集。"""
    from ..core.ai_sql_assistant import procedure_compatibility, validate_sql
    errors = []
    for example in EXAMPLES:
        result = validate_sql(example['sql'])
        if not result['ok']:
            errors.append({'id': example['id'], 'error': result['error']})
        if example['category'] == 'procedure':
            issues = procedure_compatibility(example['sql'])
            if issues:
                errors.append({'id': example['id'],
                               'error': '引擎不兼容: ' + '; '.join(issues)})
    return {'ok': not errors, 'errors': errors, 'count': len(EXAMPLES)}


def example_menu_entries():
    """菜单用：[(id, 标题)]。"""
    return [(e['id'], e['title']) for e in EXAMPLES]
