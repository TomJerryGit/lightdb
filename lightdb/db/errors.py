"""DDL 层异常：**显式报错**的载体（错误码 + 报文一路带到协议层）

为什么单独一个模块：`ALTER TABLE` / `TRUNCATE TABLE` 这类 DDL 需要能把
"MySQL 风格错误码"从 `db` 层（`Database` / `Table`）一路带到 `sql` 层的
执行器，再由执行器原样交给客户端。此前 `db` 层只抛 `ValueError`，执行器
用 `_storage_errno()` 按**报文关键字**猜码（"重复" → 1062 …），DDL 的
语义码（1054 未知列 / 1060 列重复 / 1068 多个主键 / 1090 不能删光列 /
1091 不能删主键或唯一列 / 1138 NOT NULL 却无默认值 / 1146 表不存在 /
3959 CHECK 用到该列）根本表达不出来，只能退化成 1105/1062 这类误导性代码。

只要 errno 恰好等于 1054/1062 这类"存储层关键字也能猜到"的码，也必须走
本异常：`_storage_errno` 是**报文判别**，同一个码可能因为报文措辞变化而
漂移；这里的码是确定的。

约定（与项目"要么真实、要么显式报错"的口径一致）：`DdlError` 只描述
"没法做"，**不**承担"已经改了一半"的语义 —— `Database` 侧保证任何
`DdlError` 抛出时都不会留下半套结构（DDL 的执行序列与崩溃恢复见
`Database.alter_add_column` / `_recover_pending_ddl`）。
"""


class DdlError(Exception):
    """DDL 失败（errno / sqlstate 直接对应给客户端的错误包）"""

    def __init__(self, message: str, errno: int = 1105,
                 sqlstate: str = 'HY000'):
        super().__init__(str(message))
        self.errno = int(errno)
        self.sqlstate = str(sqlstate)

    @property
    def message(self) -> str:
        return str(self)
