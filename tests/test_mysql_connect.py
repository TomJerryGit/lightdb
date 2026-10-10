"""
MySQL 兼容性测试 - 验证协议实现
运行前先启动服务器: python main.py
"""
import sys

def test_pymysql():
    try:
        import pymysql
    except ImportError:
        print("请先安装: pip install pymysql")
        return False
    
    try:
        print("[1] 测试连接...")
        conn = pymysql.connect(
            host='127.0.0.1', port=8888,
            user='root', password='',
            charset='utf8mb4',
            connect_timeout=5,
        )
        print("    ✅ 连接成功")
    except Exception as e:
        print(f"    ❌ 连接失败: {e}")
        return False
    
    try:
        cur = conn.cursor()
        
        print("[2] SELECT 1...")
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,), "SELECT 1 失败"
        print("    ✅")
        
        print("[3] SELECT VERSION()...")
        cur.execute("SELECT VERSION()")
        v = cur.fetchone()
        print(f"    ✅ {v}")
        
        print("[4] SELECT DATABASE()...")
        cur.execute("SELECT DATABASE()")
        print(f"    ✅ {cur.fetchone()}")
        
        print("[5] SHOW DATABASES...")
        cur.execute("SHOW DATABASES")
        dbs = cur.fetchall()
        print(f"    ✅ {dbs}")
        
        print("[6] SHOW TABLES...")
        cur.execute("SHOW TABLES")
        print(f"    ✅ {cur.fetchall()}")
        
        print("[7] SHOW VARIABLES LIKE 'version%'...")
        cur.execute("SHOW VARIABLES LIKE 'version%'")
        for r in cur.fetchall():
            print(f"    → {r}")
        
        print("[8] SHOW STATUS...")
        cur.execute("SHOW STATUS")
        print(f"    ✅ 返回 {len(cur.fetchall())} 行")
        
        print("[9] SELECT @@version...")
        cur.execute("SELECT @@version")
        print(f"    ✅ {cur.fetchone()}")
        
        cur.close()
        conn.close()
        print("\n🎉 所有测试通过！")
        return True
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    sys.exit(0 if test_pymysql() else 1)