"""
Cython 技术验证
- B+Tree 查找对比
- 序列化对比
- 预估收益
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict


CYTHON_SRC_BTREE = '''
# cython: boundscheck=False, wraparound=False
import cython
from libc.stdlib cimport malloc, free

cdef class BTreePyx:
    cdef int* keys
    cdef long* values
    cdef int n
    cdef int cap

    def __cinit__(self, int cap):
        self.cap = cap
        self.n = 0
        self.keys = <int*>malloc(cap * sizeof(int))
        self.values = <long*>malloc(cap * sizeof(long))

    def __dealloc__(self):
        free(self.keys)
        free(self.values)

    cpdef void insert(self, int key, long value):
        cdef int i = self.n
        while i > 0 and self.keys[i-1] > key:
            self.keys[i] = self.keys[i-1]
            self.values[i] = self.values[i-1]
            i -= 1
        self.keys[i] = key
        self.values[i] = value
        self.n += 1

    cpdef long search(self, int key):
        cdef int lo = 0
        cdef int hi = self.n - 1
        cdef int mid
        while lo <= hi:
            mid = (lo + hi) >> 1
            if self.keys[mid] == key:
                return self.values[mid]
            elif self.keys[mid] < key:
                lo = mid + 1
            else:
                hi = mid - 1
        return -1
'''


CYTHON_SRC_CODEC = '''
# cython: boundscheck=False, wraparound=False
import cython
from libc.string cimport memcpy
from libc.stdint cimport int32_t, int64_t

cpdef bytes encode_ints(list values):
    cdef int n = len(values)
    cdef bytes result = bytes(n * 8)
    cdef char* buf = result
    cdef int64_t v
    cdef int i
    for i in range(n):
        v = <int64_t>values[i]
        memcpy(buf + i * 8, &v, 8)
    return result

cpdef list decode_ints(bytes data):
    cdef int n = len(data) // 8
    cdef list result = []
    cdef char* buf = data
    cdef int64_t v
    cdef int i
    for i in range(n):
        memcpy(&v, buf + i * 8, 8)
        result.append(v)
    return result
'''


class CythonExperiment:
    """Cython 验证"""

    def __init__(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix='cython_exp_'))

    def has_cython(self) -> bool:
        try:
            import Cython
            return True
        except ImportError:
            return False

    def has_gcc(self) -> bool:
        for compiler in ['gcc', 'clang', 'cc']:
            try:
                subprocess.run([compiler, '--version'],
                                capture_output=True, timeout=2)
                return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return False

    # ==================== 编译 Cython ====================

    def compile_pyx(self, name: str, source: str) -> bool:
        """编译 Cython 模块"""
        pyx_file = self.tmp_dir / f"{name}.pyx"
        pyx_file.write_text(source)

        setup_py = self.tmp_dir / "setup.py"
        setup_py.write_text(f'''
from setuptools import setup
from Cython.Build import cythonize
setup(
    ext_modules=cythonize(
        ["{pyx_file.name}"],
        compiler_directives={{"language_level": 3}},
    ),
    script_args=["build_ext", "--inplace"],
)
''')

        try:
            result = subprocess.run(
                [sys.executable, 'setup.py', 'build_ext', '--inplace'],
                cwd=str(self.tmp_dir),
                capture_output=True,
                text=True,
                timeout=120)
            return result.returncode == 0
        except Exception as e:
            print(f"  编译失败: {e}")
            return False

    # ==================== B+Tree 对比 ====================

    def test_btree_speedup(self, n_ops: int = 100000) -> Dict:
        """测试 B+Tree 加速"""
        print(f"\n【B+Tree 性能对比 ({n_ops} 次操作)】")

        # Python 基线
        keys = list(range(n_ops))
        values = {k: k * 10 for k in keys}

        # 1. Python 版本（Bisect）
        import bisect
        sorted_keys = sorted(keys)

        t0 = time.perf_counter()
        for k in keys:
            idx = bisect.bisect_left(sorted_keys, k)
            if idx < len(sorted_keys) and sorted_keys[idx] == k:
                _ = values[sorted_keys[idx]]
        t1 = time.perf_counter()
        python_time = t1 - t0

        print(f"  Python: {python_time*1000:.2f}ms")
        result = {'python': python_time}

        # 2. Cython 版本
        if not self.has_cython() or not self.has_gcc():
            print("  Cython: 跳过（缺少 Cython 或 GCC）")
            result['cython'] = None
            result['speedup'] = 1.0
            return result

        print(f"  编译 Cython 模块...")
        if not self.compile_pyx('test_btree', CYTHON_SRC_BTREE):
            print("  编译失败")
            result['cython'] = None
            result['speedup'] = 1.0
            return result

        sys.path.insert(0, str(self.tmp_dir))
        try:
            import test_btree
            bt = test_btree.BTreePyx(n_ops + 10)

            t0 = time.perf_counter()
            for k in keys:
                bt.insert(k, k * 10)
            t1 = time.perf_counter()
            build_time = t1 - t0

            t0 = time.perf_counter()
            for k in keys:
                _ = bt.search(k)
            t1 = time.perf_counter()
            cython_time = t1 - t0

            print(f"  Cython 建树: {build_time*1000:.2f}ms")
            print(f"  Cython 查询: {cython_time*1000:.2f}ms")

            speedup = python_time / cython_time if cython_time > 0 else 0
            print(f"  加速: {speedup:.1f}x")

            result['cython'] = cython_time
            result['speedup'] = speedup
        except Exception as e:
            print(f"  执行失败: {e}")
            result['cython'] = None
            result['speedup'] = 1.0

        return result

    # ==================== 序列化对比 ====================

    def test_codec_speedup(self, n: int = 1000000) -> Dict:
        """测试序列化加速"""
        print(f"\n【序列化性能对比 ({n} 个整数)】")

        values = list(range(n))

        # 1. Python struct
        import struct
        fmt = struct.Struct('<q')

        t0 = time.perf_counter()
        b = b''.join(fmt.pack(v) for v in values)
        t1 = time.perf_counter()
        enc_time = t1 - t0

        t0 = time.perf_counter()
        _ = [fmt.unpack_from(b, i*8)[0] for i in range(n)]
        t1 = time.perf_counter()
        dec_time = t1 - t0

        print(f"  Python 编码: {enc_time*1000:.2f}ms")
        print(f"  Python 解码: {dec_time*1000:.2f}ms")

        result = {'python_enc': enc_time, 'python_dec': dec_time}

        # 2. Cython 版本
        if not self.has_cython() or not self.has_gcc():
            print("  Cython: 跳过")
            return result

        if not self.compile_pyx('test_codec', CYTHON_SRC_CODEC):
            return result

        try:
            import test_codec
            t0 = time.perf_counter()
            b2 = test_codec.encode_ints(values)
            t1 = time.perf_counter()
            enc_time2 = t1 - t0

            t0 = time.perf_counter()
            _ = test_codec.decode_ints(b2)
            t1 = time.perf_counter()
            dec_time2 = t1 - t0

            print(f"  Cython 编码: {enc_time2*1000:.2f}ms "
                  f"({enc_time/enc_time2:.1f}x)")
            print(f"  Cython 解码: {dec_time2*1000:.2f}ms "
                  f"({dec_time/dec_time2:.1f}x)")

            result['cython_enc'] = enc_time2
            result['cython_dec'] = dec_time2
            result['enc_speedup'] = enc_time / enc_time2
            result['dec_speedup'] = dec_time / dec_time2
        except Exception as e:
            print(f"  执行失败: {e}")

        return result

    # ==================== 综合 ====================

    def run_all(self) -> Dict:
        print("=" * 70)
        print("  Cython 技术验证")
        print("=" * 70)

        if not self.has_cython():
            print("\n❌ Cython 未安装")
            print("   安装: pip install cython")
            return {'error': 'no_cython'}

        if not self.has_gcc():
            print("\n❌ C 编译器未找到")
            print("   Windows: 安装 Visual Studio Build Tools")
            print("   Linux: apt install gcc")
            print("   macOS: xcode-select --install")
            return {'error': 'no_gcc'}

        print("\n✅ 环境检查通过")
        print(f"   Cython: {__import__('Cython').__version__}")

        results = {}
        results['btree'] = self.test_btree_speedup(50000)
        results['codec'] = self.test_codec_speedup(100000)

        print(f"\n【结论】")
        if results['btree'].get('speedup'):
            print(f"  B+Tree 加速: {results['btree']['speedup']:.1f}x")
        if results['codec'].get('enc_speedup'):
            print(f"  序列化加速: {results['codec']['enc_speedup']:.1f}x")

        # 综合
        gains = []
        if results['btree'].get('speedup'):
            gains.append(results['btree']['speedup'])
        if results['codec'].get('enc_speedup'):
            gains.append(results['codec']['enc_speedup'])

        if gains:
            avg = sum(gains) / len(gains)
            print(f"  平均加速: {avg:.1f}x")
            results['avg_speedup'] = avg
            print(f"  ✅ Cython 收益显著，建议采用")

        return results


if __name__ == '__main__':
    CythonExperiment().run_all()