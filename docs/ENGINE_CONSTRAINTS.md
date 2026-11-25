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

## 多列索引 + LIKE 前缀组合优化（D4，beta.148）

- 支持 **等值前缀 + 后续列 LIKE 前缀** 走二级索引：
  `WHERE a=? AND b LIKE 'x%'`（idx(a,b)）、
  `WHERE a=? AND b=? AND c LIKE 'x%'`（idx(a,b,c)）。
- 实现：`Table._secondary_like_plan()` 找到 LIKE 列下标 k，要求 0..k-1 列都有
  `=` 条件（值冲突/缺失则安全回退）；`_secondary_like_candidate_pks()` 在
  `maps[k+1]` 上按等值元组 + 字符串前缀过滤，结果仍逐行 `_match` 复核。
- 不支持：`%x%`、`x%y`、`_` 通配、OR、LIKE 列之前有范围条件（
  `a=? AND b BETWEEN ... AND c LIKE ...` 无法在范围后使用后续列索引）→ 安全
  全表扫描，结果不变。
- 观测：命中时 `Table._index_stats['like_prefix']` 自增；`EXPLAIN` 仍显示
  ALL（与既有前缀索引一致，未新增计划输出）。
- 测试：`tests/test_composite_index_like.py`（等值前缀、二级等值前缀、
  回退形态、NULL/空结果、重启一致性、2000 行量化）。

## 触发器（D3，beta.149）

```sql
CREATE TRIGGER trg_name BEFORE INSERT ON t FOR EACH ROW
BEGIN
  IF NEW.age IS NULL THEN SET NEW.age = 18; END IF;
  INSERT INTO log_t (id, msg) VALUES (NEW.id, 'inserted');
END;
```

支持范围：

* `CREATE TRIGGER <name> {BEFORE|AFTER} {INSERT|UPDATE|DELETE} ON <table>
  FOR EACH ROW <body>`；body 可为单条语句或 `BEGIN ... END` 顺序多条。
* body 允许：`INSERT` / `UPDATE` / `DELETE`（单表）、`SET NEW.col = 表达式`
  （BEFORE 可改 NEW；UPDATE 的 NEW 值按列类型转换后参与算术/比较）、
  `IF ... THEN ... [ELSE ...] END IF`（嵌套 IF 支持）、NEW.col/OLD.col 引用。
* BEFORE 在行落库前执行，可修改 NEW（INSERT/UPDATE）；AFTER 在行落库后执行。
* 触发器内 DML 与主语句**同事务**：主语句成功则一起提交，触发器失败则
  整条语句回滚（含 AFTER 触发器写入后的失败）；显式事务由用户 ROLLBACK。
* **只执行一层**：触发器体内的 DML 不会再次触发其它触发器（与 MySQL 默认
  行为一致；非递归）。
* 显式拒绝：子查询/SELECT、DDL、CALL/REPLACE、`SET @变量`、ELSEIF；触发器
  体错误在 **CREATE 定义期** 1064 拒绝并指出触发器名，不会留下"坏触发器
  锁表"。
* 元数据：持久化到 `Database._meta.json` 的 `triggers` 键（旧
  `_triggers.json` 自动迁移），重启后生效；`SHOW TRIGGERS` /
  `SHOW CREATE TRIGGER` 回显真实定义；`DROP TRIGGER [IF EXISTS]`。
* 备份恢复：dump 末尾写 `-- lightdb:trigger <json>` 哨兵，恢复在全部表/数据
  之后 CREATE TRIGGER（避免恢复 INSERT 提前触发业务触发器）；重复恢复先
  `DROP TRIGGER IF EXISTS`，触发器在后恢复库中仍真实生效。
* 测试：`tests/test_triggers_basic.py`（BEFORE/AFTER × I/U/D、NEW/OLD、
  多语句、失败回滚、递归抑制、DDL/子查询拒绝、SHOW/DROP、重启、备份恢复）。
