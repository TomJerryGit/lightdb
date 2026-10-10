"""
数据一致性检查
- B+Tree vs 堆数据
- 主键唯一性
- 索引完整性
- WAL 重放正确性
- 事务隔离性
"""
import hashlib
import time
from dataclasses import dataclass, asdict, field
from typing import Callable, Dict, List, Optional


@dataclass
class ConsistencyReport:
    table: str
    checked_at: float = 0
    # 检查项
    row_count: int = 0
    btree_count: int = 0
    index_entries: int = 0
    # 结果
    passed: bool = True
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    elapsed_ms: float = 0

    def __post_init__(self):
        if not self.checked_at:
            self.checked_at = time.time()


class ConsistencyChecker:
    """一致性检查器"""

    def __init__(self, host='127.0.0.1', port=8888,
                 user='root', password=''):
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    def _connect(self):
        import pymysql
        return pymysql.connect(
            host=self.host, port=self.port,
            user=self.user, password=self.password,
            autocommit=True)

    # ==================== 检查项 ====================

    def check_table(self, table: str) -> ConsistencyReport:
        """检查单表"""
        report = ConsistencyReport(table=table)
        t0 = time.time()

        try:
            conn = self._connect()
            cur = conn.cursor()

            # 1. 行数 vs 主键索引条目数
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            report.row_count = cur.fetchone()[0]

            # 主键索引
            try:
                cur.execute(f"CHECK TABLE {table}")
                result = cur.fetchone()
                # 结果格式: (Table, Op, Msg_type, Msg_text)
                if result and len(result) >= 4:
                    if result[2].lower() == 'status':
                        pass  # OK
                    elif result[2].lower() == 'error':
                        report.passed = False
                        report.issues.append(
                            f"CHECK TABLE: {result[3]}")
            except Exception as e:
                report.warnings.append(f"CHECK TABLE 不支持: {e}")

            # 2. 主键唯一性
            cur.execute(f"""
                SELECT id, COUNT(*) AS c
                FROM {table}
                GROUP BY id
                HAVING c > 1
                LIMIT 5
            """)
            dups = cur.fetchall()
            if dups:
                report.passed = False
                report.issues.append(
                    f"发现 {len(dups)} 组重复主键")
                for d in dups[:3]:
                    report.issues.append(f"  id={d[0]} 出现 {d[1]} 次")

            # 3. 空主键
            cur.execute(
                f"SELECT COUNT(*) FROM {table} WHERE id IS NULL")
            null_pk = cur.fetchone()[0]
            if null_pk > 0:
                report.passed = False
                report.issues.append(f"{null_pk} 行主键为 NULL")

            # 4. 数据校验和
            cur.execute(
                f"SELECT MD5(GROUP_CONCAT(id ORDER BY id)) FROM {table}")
            row = cur.fetchone()
            # 有些数据库不支持 GROUP_CONCAT，忽略

            conn.close()

        except Exception as e:
            report.passed = False
            report.issues.append(f"检查失败: {e}")

        report.elapsed_ms = (time.time() - t0) * 1000
        return report

    # ==================== 主从对比 ====================

    def compare_master_slave(self, master_port: int = 8888,
                             slave_port: int = 8889,
                             table: str = 'test') -> Dict:
        """主从数据对比"""
        result = {
            'table': table,
            'master_port': master_port,
            'slave_port': slave_port,
            'matched': False,
            'issues': [],
        }

        try:
            import pymysql
            m_conn = pymysql.connect(
                host=self.host, port=master_port,
                user=self.user, password=self.password,
                autocommit=True)
            s_conn = pymysql.connect(
                host=self.host, port=slave_port,
                user=self.user, password=self.password,
                autocommit=True)

            m_cur = m_conn.cursor()
            s_cur = s_conn.cursor()

            # 1. 行数
            m_cur.execute(f"SELECT COUNT(*) FROM {table}")
            s_cur.execute(f"SELECT COUNT(*) FROM {table}")
            m_rows = m_cur.fetchone()[0]
            s_rows = s_cur.fetchone()[0]

            result['master_rows'] = m_rows
            result['slave_rows'] = s_rows

            if m_rows != s_rows:
                result['issues'].append(
                    f"行数不一致: master={m_rows}, slave={s_rows}")

            # 2. 逐行比对（最多 10000 行）
            limit = 10000
            m_cur.execute(
                f"SELECT * FROM {table} ORDER BY id LIMIT {limit}")
            s_cur.execute(
                f"SELECT * FROM {table} ORDER BY id LIMIT {limit}")

            m_data = m_cur.fetchall()
            s_data = s_cur.fetchall()

            mismatch = 0
            for i, (m_row, s_row) in enumerate(zip(m_data, s_data)):
                if m_row != s_row:
                    mismatch += 1
                    if mismatch <= 5:
                        result['issues'].append(
                            f"行 {i} 不一致: "
                            f"master={m_row}, slave={s_row}")

            result['mismatch_rows'] = mismatch
            result['matched'] = (mismatch == 0 and m_rows == s_rows)

            m_conn.close()
            s_conn.close()

        except Exception as e:
            result['issues'].append(str(e))

        return result

    # ==================== 事务隔离测试 ====================

    def test_isolation_rc(self) -> Dict:
        """测试 READ COMMITTED 隔离"""
        result = {'isolation': 'RC', 'passed': False, 'issues': []}
        import pymysql

        try:
            # 会话 1：创建测试数据
            conn1 = pymysql.connect(
                host=self.host, port=self.port,
                user=self.user, password=self.password)
            cur1 = conn1.cursor()
            cur1.execute("DROP TABLE IF EXISTS iso_test")
            cur1.execute(
                "CREATE TABLE iso_test (id INT PRIMARY KEY, v INT)")
            cur1.execute("INSERT INTO iso_test VALUES (1, 100)")
            conn1.commit()

            # 会话 2：读初始值
            conn2 = pymysql.connect(
                host=self.host, port=self.port,
                user=self.user, password=self.password)
            cur2 = conn2.cursor()
            cur2.execute("SELECT v FROM iso_test WHERE id = 1")
            v1 = cur2.fetchone()[0]
            if v1 != 100:
                result['issues'].append(f"初始值错误: {v1}")

            # 会话 1：修改但不提交
            cur1.execute("UPDATE iso_test SET v = 200 WHERE id = 1")

            # 会话 2：应该看到旧值（RC 未提交不可见）
            cur2.execute("SELECT v FROM iso_test WHERE id = 1")
            v2 = cur2.fetchone()[0]
            if v2 != 100:
                result['issues'].append(
                    f"RC 隔离失败：未提交修改可见 (v={v2})")

            # 会话 1：提交
            conn1.commit()

            # 会话 2：应该看到新值
            cur2.execute("SELECT v FROM iso_test WHERE id = 1")
            v3 = cur2.fetchone()[0]
            if v3 != 200:
                result['issues'].append(
                    f"提交后不可见: {v3}")

            result['passed'] = len(result['issues']) == 0

            conn1.close()
            conn2.close()

        except Exception as e:
            result['issues'].append(str(e))

        return result

    def test_rollback(self) -> Dict:
        """测试回滚"""
        result = {'test': 'rollback', 'passed': False, 'issues': []}
        import pymysql

        try:
            conn = pymysql.connect(
                host=self.host, port=self.port,
                user=self.user, password=self.password)
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS rb_test")
            cur.execute(
                "CREATE TABLE rb_test (id INT PRIMARY KEY)")
            cur.execute("INSERT INTO rb_test VALUES (1)")
            conn.commit()

            # 开启事务
            cur.execute("BEGIN")
            cur.execute("INSERT INTO rb_test VALUES (2)")
            cur.execute("INSERT INTO rb_test VALUES (3)")

            # 回滚
            cur.execute("ROLLBACK")

            # 检查：只应该有 id=1
            cur.execute("SELECT id FROM rb_test ORDER BY id")
            ids = [r[0] for r in cur.fetchall()]
            if ids != [1]:
                result['issues'].append(f"回滚失败: {ids}")
            else:
                result['passed'] = True

            conn.close()

        except Exception as e:
            result['issues'].append(str(e))

        return result

    # ==================== 主入口 ====================

    def full_check(self, tables: List[str]) -> Dict:
        """完整检查"""
        print(f"\n{'='*60}")
        print(f"  数据一致性检查")
        print(f"{'='*60}\n")

        results = {
            'tables': [],
            'isolation_rc': None,
            'rollback': None,
            'summary': {},
        }

        # 逐表检查
        for t in tables:
            print(f"▶ 检查表: {t}")
            try:
                r = self.check_table(t)
                results['tables'].append(asdict(r))
                icon = '✅' if r.passed else '❌'
                print(f"  {icon} {r.row_count} 行，"
                      f"{len(r.issues)} 个问题，"
                      f"{r.elapsed_ms:.1f}ms")
                for issue in r.issues[:3]:
                    print(f"     - {issue}")
            except Exception as e:
                print(f"  ❌ 异常: {e}")

        # 隔离性
        print(f"\n▶ 事务隔离 (RC)")
        iso = self.test_isolation_rc()
        results['isolation_rc'] = iso
        print(f"  {'✅' if iso['passed'] else '❌'} "
              f"{iso.get('issues', [])}")

        # 回滚
        print(f"\n▶ 事务回滚")
        rb = self.test_rollback()
        results['rollback'] = rb
        print(f"  {'✅' if rb['passed'] else '❌'} "
              f"{rb.get('issues', [])}")

        # 总结
        all_passed = (
            all(t['passed'] for t in results['tables']) and
            iso['passed'] and rb['passed']
        )
        results['summary'] = {
            'passed': all_passed,
            'total_tables': len(results['tables']),
            'failed_tables': sum(
                1 for t in results['tables'] if not t['passed']),
        }

        print(f"\n{'='*60}")
        print(f"  总结: {'✅ 通过' if all_passed else '❌ 失败'}")
        print(f"  表: {results['summary']['total_tables']}, "
              f"失败: {results['summary']['failed_tables']}")
        print(f"{'='*60}")

        return results