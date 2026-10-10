"""
游标（CURSOR）
用于存储过程中的行遍历
"""
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class CursorDef:
    """游标定义"""
    name: str
    query: str
    columns: List[str] = field(default_factory=list)
    rows: List[List] = field(default_factory=list)
    position: int = -1
    is_open: bool = False
    is_done: bool = False


class CursorError(Exception):
    pass


class CursorManager:
    """游标管理器（每个 PL 会话一份）"""

    def __init__(self, executor=None):
        self.cursors: dict = {}
        self.executor = executor

    # ==================== 声明 ====================

    def declare(self, name: str, query: str):
        """DECLARE cursor_name CURSOR FOR SELECT ..."""
        name = name.lower()
        if name in self.cursors:
            raise CursorError(f"游标 {name} 已存在")
        self.cursors[name] = CursorDef(name=name, query=query)

    # ==================== 打开 ====================

    def open(self, name: str, session=None):
        """OPEN cursor_name"""
        name = name.lower()
        cur = self.cursors.get(name)
        if not cur:
            raise CursorError(f"游标 {name} 未声明")

        # 执行查询
        if self.executor is None:
            raise CursorError("无执行器")

        try:
            result = self.executor.execute_sql(cur.query, session)
            if isinstance(result, dict) and result.get('type') == 'resultset':
                cur.columns = result['columns']
                cur.rows = [list(r) for r in result['rows']]
            elif isinstance(result, tuple):
                cur.columns = result[0]
                cur.rows = [list(r) for r in result[1]]
            else:
                cur.columns = []
                cur.rows = []
        except Exception as e:
            raise CursorError(f"游标查询失败: {e}")

        cur.position = -1
        cur.is_open = True
        cur.is_done = False

    # ==================== 读取 ====================

    def fetch(self, name: str) -> Optional[List[Any]]:
        """FETCH cursor_name INTO vars"""
        name = name.lower()
        cur = self.cursors.get(name)
        if not cur or not cur.is_open:
            raise CursorError(f"游标 {name} 未打开")

        cur.position += 1
        if cur.position >= len(cur.rows):
            cur.is_done = True
            # MySQL 语义：没数据时设置 SQLSTATE 02000
            return None
        return cur.rows[cur.position]

    # ==================== 关闭 ====================

    def close(self, name: str):
        """CLOSE cursor_name"""
        name = name.lower()
        cur = self.cursors.get(name)
        if cur:
            cur.is_open = False
            cur.is_done = True

    # ==================== 清理 ====================

    def clear(self):
        self.cursors.clear()

    def list_cursors(self) -> list:
        return list(self.cursors.keys())


# ==================== 集成到 PL 解释器 ====================

class CursorExecutionContext:
    """游标执行上下文"""

    def __init__(self, cursor_manager: CursorManager):
        self.cm = cursor_manager
        self.not_found_handler = None    # CONTINUE HANDLER FOR NOT FOUND

    def set_not_found_handler(self, handler):
        """设置未找到处理器"""
        self.not_found_handler = handler

    def on_not_found(self):
        """触发 NOT FOUND"""
        if self.not_found_handler:
            self.not_found_handler()