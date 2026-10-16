# 阶段 2 第 3 项（页级回收）—— 未提交，附完整发现与处置

**状态**：**未提交、未合并**。工作树里留着一份**未完成且有已知数据丢失回归**的实现。
**已验证基线**：`25c4cbb`（阶段 2 第 2 项）。本文档用于让下一轮不必重新踩坑。

---

## 1 结论先行

| 目标 | 结果 |
|---|---|
| **`.dat` 真正缩小**（本项头条目标） | ❌ **未达成**：20000 行删 90% → `1257472 B → 1257472 B`（**0.0%**） |
| 空闲页登记**持久化** + 复用 | ✅ vacuum 后 55 个空闲页 → 重启后仍 55 个；再插 1500 行**堆页 +0** |
| 完整性扫描器 | ✅ `dangling_prev` / `rows_on_free_pages` / `bad_magic_pages` 全 0 |
| 快照安全（vacuum 不得回收活跃视图可见版本） | ✅ 老快照可见时 vacuum 回收 **0** 个版本，老快照仍读到 50 行 v0 |
| 崩溃安全 | ✅ 两种 `os._exit(1)` 崩溃点重开后数据完整、索引可用 |
| 既有断言改动 | ✅ **0**（`git diff --stat -- tests/` 为空） |
| **新引入的回归** | ❌ `BEGIN; DELETE 100 行; vacuum(); ROLLBACK` 只恢复 **28/100** 行 |

**未提交的原因**：本会话标准是"正确性优先、绝不接受已知数据丢失"。该回归属
**改动前不存在**的新问题，因此不能进入主干。

---

## 2 顺带修好的 3 处**既有**数据丢失路径（高价值，建议单独一轮重做并保留）

1. **`vacuum` 会回收当前活跃事务自己写的墓碑**
   旧判定 `xmax <= horizon` 对"当前事务自己刚 DELETE 的行"恒真（`horizon` 就是它的号），
   于是它能把自己事务内的删除**永久抹掉**。
   修法：拆成两个独立上界——`horizon`（调用方语义不变）+ `snapshot_safe`
   （= `min(最小活跃号, 所有活跃读视图 max_id)`，**不可被调用方覆盖**），
   判定改为 `xmax <= horizon and xmax < snapshot_safe`。
2. **索引重建给"槽位已清零的墓碑"插索引项**
   `_rebuild_index` / `_rebuild_btree` / `_redo_delete` 会给空槽位建索引项 →
   索引指向空槽位、**该主键永久读不到**（实测 800 项里 **200 项悬空**）。
3. **`Page.compact()` 在页被清空时不清槽位数组**
   导致空页无法被当作插入目标页复用。

这三条都属于"结构对、语义假"的同一族，**值得单独一轮修复并用测试钉住**（它们与页搬迁无关）。

---

## 3 为什么 `.dat` 缩不下去（失败点，已定位）

- 删除是**按行散布**的：删 90% 后 **187 个堆页每页都还剩几个活行**，尾部**没有任何连续空置段**，
  因此"截断尾部"（`trim_tail()`）回收 **0** 页。
- 要真正缩小必须做**前向搬页**（把尾部活行搬到前面的空闲页，再截断）。
  子代理实现了 `_relocate_forward`，但**计划期就产不出有效计划**
  （自诊断输出 `{'rows':2000,'heap_pages':187,'target_pages':38}`，`moved=0`），
  故以 `ENABLE_PAGE_RELOCATION=False` 默认关闭。
- **索引页密集化**（重建 B+Tree）本可给出
  `1257472 → 217088 B`（**82.7%**），但实测在"新树更小却被追加到更高页号"这条路径上
  会**截掉新树自己的页** → 重启后**整表读不到**（dump 2000 行 → 0 行）。
  已加"新树页数不少于旧树就整份回退"的门槛，但覆盖不到该路径，故
  `ENABLE_BTREE_REBUILD=False` 默认关闭。两个开关都保留了完整实现与注释。

**下一轮的正确切法**（强烈建议）：
1. **先做第 2 节的三处数据丢失修复 + 完整性扫描器**（小而确定，可立即验证，收益明确）；
2. 再做"空闲页持久化 + 复用"（本轮已基本可用）；
3. **最后**单独做前向搬页 / 索引密集化——并且**必须有**"搬页后重启 → 全表 dump 与搬迁前逐行相等"的硬断言，
   以及"新树页号必须落在旧树页号区间内，否则整份回退"的前置校验。

---

## 4 复现与回退

**当前工作树内容**（未提交）：
```
 M lightdb/db/database.py        M lightdb/db/table.py
 M lightdb/sql/executor.py       M lightdb/sql/parser.py
 M lightdb/storage/btree.py      M lightdb/storage/buffer_pool.py
 M lightdb/storage/page.py
?? lightdb/storage/freelist.py   ?? lightdb/storage/pgscan.py
?? tests/test_vacuum_pages.py
```

**回退（必须在普通终端跑——DSH 会话内工作区的删除/替换被有意保护，`git checkout --` 会报
`unable to unlink ... Invalid argument`，且 ACL 诊断结论为 `NOT_THIS_CLASS`＝不是权限缺失）**：
```powershell
cd D:\lightdb_project
git checkout -- lightdb/db/database.py lightdb/db/table.py lightdb/sql/executor.py lightdb/sql/parser.py lightdb/storage/btree.py lightdb/storage/buffer_pool.py lightdb/storage/page.py
Remove-Item lightdb\storage\freelist.py, lightdb\storage\pgscan.py, tests\test_vacuum_pages.py -Force
Remove-Item _acl_recovery -Recurse -Force      # ACL 诊断留下的报告目录
git status                                     # 应为 clean
```

**保留哪一部分**：若想保留第 2 节的三处修复，请**新开一轮**从当前工作树出发，只做
"三处修复 + 扫描器 + 单测"，把页搬迁/索引密集化留在开关后面且默认关闭——**不要**带着
`ROLLBACK 只恢复 28/100` 的回归向前走。
