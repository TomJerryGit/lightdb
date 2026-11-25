# 引擎约束（D2）：外键 MVP 与 CHECK

## 外键（单列，RESTRICT / NO ACTION）

```sql
CREATE TABLE parent (id INT PRIMARY KEY, code INT UNIQUE);
CREATE TABLE child (
  id INT PRIMARY KEY,
  pid INT,
  code INT,
  CONSTRAINT fk_child_parent FOREIGN KEY (pid)
    REFERENCES parent(id) ON DELETE RESTRICT ON UPDATE NO ACTION,
  FOREIGN KEY (code) REFERENCES parent(code)
);
```

实现要点：

* **定义期**（`Database.create_table`）校验：父表存在、父列存在、父列有
  主键/UNIQUE/最左索引列；子列存在。失败 → `ERROR 1822 (HY000)` 风格
  （不写半套元数据）。
* 元数据持久化到 `_meta.json`（`foreign_keys`），重启/备份恢复后仍强制；
  `SHOW CREATE TABLE` 通过 `sql/logical_dump.py` 的同一份 DDL 渲染回显。
* **子表 INSERT/UPDATE**：非 NULL 外键值必须在父表可见行中存在，否则
  `ERROR 1452 (23000)`；NULL 允许（与 MySQL 一致）。
* **父表 DELETE / 更新被引用键列**：仍有子行引用 → `ERROR 1451 (23000)`。
  只实现 RESTRICT/NO ACTION；CASCADE / SET NULL / SET DEFAULT 在
  **解析期显式 1064 拒绝**，绝不静默降级。
* 外键检查发生在拿行锁/写 WAL 之前；失败不会留下半条记录，事务回滚不受
  影响（检查只读）。
* 支持匿名约束（自动命名 `<child>_ibfk_N`）与 `CONSTRAINT <name>`；
  多列外键显式拒绝（单列 MVP）；ALTER TABLE ADD/DROP FOREIGN KEY 尚未
  实现（显式报错，不静默）。
* 备份恢复：`table_schemas` 按外键依赖做父表优先的拓扑排序，恢复时
  CREATE 顺序保证父表先建；循环依赖显式报错。

## CHECK（已有实现）

`sql/checks.py` + parser/database/table 已实现行级 CHECK：

* `CHECK (expr)` / 列级 CHECK / 表级 `[CONSTRAINT name] CHECK (expr)`；
* 支持列引用、常量、比较、AND/OR/NOT、IS [NOT] NULL、IN、BETWEEN、
  LIKE、算术与白名单标量函数；子查询等不支持形态**定义期显式拒绝**；
* INSERT/UPDATE 违反 → `ERROR 3819 (HY000)`；三值逻辑下 UNKNOWN 放行；
* 定义持久化到 `_meta.json`，`SHOW CREATE` 回显；ALTER ADD/DROP CHECK
  （如代码路径支持）与备份恢复均不复述于此。

## 明确边界

* 多列外键、级联动作、外键索引自动创建：未实现，显式错误。
* 父表 DROP 时不级联删除子表；引用完整性检查在写路径，不做后台修复。
* `information_schema.KEY_COLUMN_USAGE` / `REFERENTIAL_CONSTRAINTS`
  尚未接入（本 MVP 只保证 `SHOW CREATE TABLE` 与写入路径真实生效）。
* 外键检查走父表行扫描（MVP 未建 FK 专用索引）；父表极大时性能受限，
  但不影响正确性。
