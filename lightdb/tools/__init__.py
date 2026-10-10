"""
LightDB 运维工具集

CLI 命令（安装后可直接调用）:
    lightdb            # 交互式客户端 (cli.py)
    lightdbdump        # 逻辑备份    (dump.py)
    lightdbadmin       # 管理工具    (admin.py)
    lightdb-bench      # 性能基准    (benchmark.py)
    lightdb-check      # 健康检查    (check.py)
    lightdb-tune       # 参数调优    (tune.py)
    lightdb-index-advisor  # 索引推荐 (index_advisor.py)
    lightdb-dashboard  # Grafana 生成 (dashboard.py)
    lightdb-diagnose   # 性能诊断    (diagnose.py)
    lightdb-migrate    # 数据库迁移  (migrate.py)
"""

# 注意: 各工具的 main() 与 CLI 参数解析都在各自模块里，
# 这里不主动 import，避免依赖缺失导致 lightdb 包无法导入。

AVAILABLE_TOOLS = {
    'cli':             ('lightdb',              'lightdb.tools.cli'),
    'dump':            ('lightdbdump',          'lightdb.tools.dump'),
    'admin':           ('lightdbadmin',         'lightdb.tools.admin'),
    'benchmark':       ('lightdb-bench',        'lightdb.tools.benchmark'),
    'check':           ('lightdb-check',        'lightdb.tools.check'),
    'tune':            ('lightdb-tune',         'lightdb.tools.tune'),
    'index_advisor':   ('lightdb-index-advisor','lightdb.tools.index_advisor'),
    'dashboard':       ('lightdb-dashboard',    'lightdb.tools.dashboard'),
    'diagnose':        ('lightdb-diagnose',     'lightdb.tools.diagnose'),
    'migrate':         ('lightdb-migrate',      'lightdb.tools.migrate'),
}


def list_tools():
    """列出所有工具及其命令名"""
    return [
        {'key': k, 'command': v[0], 'module': v[1]}
        for k, v in AVAILABLE_TOOLS.items()
    ]


def run_tool(name: str, args=None):
    """
    动态运行某个工具。

    用法:
        from lightdb.tools import run_tool
        run_tool('check', ['-h', '127.0.0.1'])
    """
    if name not in AVAILABLE_TOOLS:
        raise ValueError(
            f"未知工具: {name}，可用: {list(AVAILABLE_TOOLS.keys())}")

    import importlib
    import sys

    _, module_path = AVAILABLE_TOOLS[name]
    mod = importlib.import_module(module_path)

    if args is not None:
        old_argv = sys.argv
        try:
            sys.argv = [module_path] + list(args)
            mod.main()
        finally:
            sys.argv = old_argv
    else:
        mod.main()


__all__ = ['AVAILABLE_TOOLS', 'list_tools', 'run_tool']