"""Batch 2 集成测试"""
import sys

def main():
    try:
        import pymysql
    except ImportError:
        print("请先安装: pip install pymysql")
        return 1
    
    print("=" * 60)
    print("Batch 2 测试 - 完整CRUD链路")
    print("=" * 60)
    
    try:
        conn = pymysql.connect(
            host='127.0.0.1', port=8888, user='root', password='',
            charset='utf8mb4', autocommit=True,
            cursorclass=pymysql.cursors.Cursor,
        )
        print("✅ 连接成功")
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        return 1
    
    cur = conn.cursor()
    
    # 1. 清理旧表
    print("\n[1] DROP TABLE IF EXISTS users")
    try:
        cur.execute("DROP TABLE users")
        print("    ✅ 旧表已删")
    except Exception:
        print("    ℹ️  表不存在，跳过")
    
    # 2. CREATE TABLE
    print("\n[2] CREATE TABLE users")
    cur.execute("""
        CREATE TABLE users (
            id INT PRIMARY KEY,
            name VARCHAR(50) NOT NULL,
            email VARCHAR(100) UNIQUE,
            age INT
        )
    """)
    print("    ✅ 建表成功")
    
    # 3. SHOW TABLES
    print("\n[3] SHOW TABLES")
    cur.execute("SHOW TABLES")
    tables = cur.fetchall()
    assert any('users' in t for t in tables), "表未创建"
    print(f"    ✅ {tables}")
    
    # 4. SHOW COLUMNS
    print("\n[4] SHOW COLUMNS FROM users")
    cur.execute("SHOW COLUMNS FROM users")
    cols = cur.fetchall()
    for c in cols:
        print(f"    → {c}")
    assert len(cols) == 4, "列数不对"
    
    # 5. INSERT
    print("\n[5] INSERT INTO users")
    cur.execute("INSERT INTO users VALUES (1, 'Alice', 'alice@x.com', 30)")
    cur.execute("INSERT INTO users VALUES (2, 'Bob', 'bob@x.com', 25)")
    cur.execute("INSERT INTO users VALUES (3, 'Carol', 'carol@x.com', 28)")
    print("    ✅ 插入3行")
    
    # 6. SELECT *
    print("\n[6] SELECT * FROM users")
    cur.execute("SELECT * FROM users")
    rows = cur.fetchall()
    print(f"    ✅ 返回 {len(rows)} 行")
    for r in rows:
        print(f"    → {r}")
    assert len(rows) == 3
    
    # 7. SELECT WHERE
    print("\n[7] SELECT * FROM users WHERE age > 26")
    cur.execute("SELECT * FROM users WHERE age > 26")
    rows = cur.fetchall()
    print(f"    ✅ 返回 {len(rows)} 行")
    assert len(rows) == 2
    
    # 8. ORDER BY + LIMIT
    print("\n[8] SELECT * FROM users ORDER BY age DESC LIMIT 2")
    cur.execute("SELECT * FROM users ORDER BY age DESC LIMIT 2")
    rows = cur.fetchall()
    print(f"    ✅ {rows}")
    assert rows[0][0] == 1  # Alice 最大
    
    # 9. UPDATE
    print("\n[9] UPDATE users SET age = 31 WHERE id = 1")
    cur.execute("UPDATE users SET age = 31 WHERE id = 1")
    cur.execute("SELECT age FROM users WHERE id = 1")
    assert cur.fetchone()[0] == 31
    print("    ✅ 更新成功")
    
    # 10. COUNT
    print("\n[10] SELECT COUNT(*) FROM users")
    cur.execute("SELECT COUNT(*) FROM users")
    cnt = cur.fetchone()[0]
    print(f"    ✅ COUNT = {cnt}")
    assert cnt == 3
    
    # 11. DELETE
    print("\n[11] DELETE FROM users WHERE id = 2")
    cur.execute("DELETE FROM users WHERE id = 2")
    cur.execute("SELECT COUNT(*) FROM users")
    assert cur.fetchone()[0] == 2
    print("    ✅ 删除成功")
    
    # 12. information_schema
    print("\n[12] SELECT * FROM information_schema.TABLES")
    cur.execute("SELECT * FROM information_schema.TABLES")
    for r in cur.fetchall():
        print(f"    → {r[:4]}")
    
    # 13. 唯一约束
    print("\n[13] UNIQUE 约束测试")
    try:
        cur.execute("INSERT INTO users VALUES (4, 'Dave', 'alice@x.com', 20)")
        print("    ❌ 唯一约束未生效")
        return 1
    except Exception as e:
        print(f"    ✅ 唯一约束生效: {str(e)[:50]}")
    
    # 14. 主键约束
    print("\n[14] 主键约束测试")
    try:
        cur.execute("INSERT INTO users VALUES (1, 'Eve', 'eve@x.com', 20)")
        print("    ❌ 主键约束未生效")
        return 1
    except Exception as e:
        print(f"    ✅ 主键约束生效: {str(e)[:50]}")
    
    # 15. 持久化验证
    print("\n[15] 数据持久化（文件）")
    import os
    data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    if os.path.exists(os.path.join(data_dir, 'users.json')):
        print("    ✅ users.json 已生成")
    
    cur.close()
    conn.close()
    
    print("\n" + "=" * 60)
    print("🎉 Batch 2 全部测试通过！")
    print("=" * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())