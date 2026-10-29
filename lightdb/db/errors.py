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


class LegacyBTreeFormatError(Exception):
    """启动时发现旧 B+Tree 页格式（v1 uint32 / v2 uint64）

    v3 页条目为「16 字节大端 128 位键 + pid/sid 或 child」（24/20 字节），
    支持 3–4 列复合主键；v1（12/8B）与 v2（16/12B）条目大小不同，直接按
    v3 读取会把键/页号读错。这里在 `Database` 启动阶段显式拒绝，绝不"猜着
    读"旧页（那会静默返回错行/损坏索引）。迁移步骤见
    `docs/MIGRATION_BTREE_V2.md`，迁移脚本：
    `python scripts/migrate_btree_format.py <旧数据目录> <新数据目录>`。
    """

    def __init__(self, table: str, root_page_id: int, data_file: str):
        self.table = table
        self.root_page_id = root_page_id
        self.data_file = data_file
        super().__init__(
            f"检测到旧版 B+Tree 页格式（v1/v2）：表 '{table}' 的根页 "
            f"{root_page_id}（{data_file}）不是 v3 格式（16 字节大端 128 位"
            f"键）。请先备份数据目录，然后运行："
            f"`python scripts/migrate_btree_format.py <旧数据目录> "
            f"<新数据目录>`；迁移会逐表重建为 v3 并校验行数/主键/索引。"
            f"详见 docs/MIGRATION_BTREE_V2.md。")
