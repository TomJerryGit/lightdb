"""
工作流可视化支持
- 节点位置
- 连接边
- 布局算法
"""
import math
from typing import List, Dict


class WorkflowLayout:
    """工作流布局"""

    @staticmethod
    def apply_grid(workflow, cols: int = 3, x_gap: int = 260,
                   y_gap: int = 140):
        """网格布局"""
        for i, step in enumerate(workflow.steps):
            r, c = divmod(i, cols)
            step.position_x = c * x_gap + 50
            step.position_y = r * y_gap + 50
        return workflow

    @staticmethod
    def apply_vertical(workflow, x: int = 200, y_gap: int = 120):
        """垂直布局"""
        for i, step in enumerate(workflow.steps):
            step.position_x = x
            step.position_y = i * y_gap + 30
        return workflow

    @staticmethod
    def apply_force(workflow):
        """简易力导向（暂时用圆形）"""
        n = len(workflow.steps)
        if n == 0:
            return workflow
        cx, cy = 400, 300
        radius = max(150, n * 30)
        for i, step in enumerate(workflow.steps):
            angle = 2 * math.pi * i / n - math.pi / 2
            step.position_x = cx + radius * math.cos(angle) - 60
            step.position_y = cy + radius * math.sin(angle) - 20
        return workflow