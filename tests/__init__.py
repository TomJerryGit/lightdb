"""
LightDB 测试套件

运行:
    pytest                          # 全部
    pytest tests/regression         # 回归
    pytest tests/regression_v4      # v4 回归
    pytest -k test_transaction      # 按名称
    pytest --cov=lightdb            # 覆盖率
"""

import os
import sys

# 确保 lightdb 可导入（开发模式）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# pytest 收集时的标记
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: 慢测试（默认跳过，用 -m slow 运行）")
    config.addinivalue_line(
        "markers", "integration: 需要真实数据库的集成测试")
    config.addinivalue_line(
        "markers", "benchmark: 性能基准测试")
    config.addinivalue_line(
        "markers", "stability: 稳定性/长稳测试")