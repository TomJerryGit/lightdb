# cython: language_level=3, boundscheck=False, wraparound=False
# =============================================================================
# ⚠️ 未在本机验证编译：本机未安装 Cython，也没有任何 C 编译器
#    （import Cython → ModuleNotFoundError；gcc / cl / clang 全部未找到），
#    因此本文件**从未被编译过**，以下修复只是按 Cython 语义做的静态更正。
#    验证条件：pip install cython 且具备 C 编译器（Windows 用 MSVC cl.exe，
#    Linux 用 gcc），然后运行 scripts/build_cython.sh。
#
# 额外修复（审计只列了三处，以下这处是同类必然编译失败，一并修掉）:
#   C. _match_like 是 ``cdef`` 函数，函数体里却写了 ``import re`` ——
#      cdef（C 函数）内不允许 Python import 语句，编译期报错。
#      现把 ``import re`` 提到模块级（别名 _re）。
# =============================================================================
"""
谓词求值（Cython）
"""
from libc.stdint cimport int64_t
from libc.string cimport memcpy, strcmp
from cpython.bytes cimport PyBytes_AsString

# 修复 C：cdef 函数体里不能出现 import，这里提到模块级
import re as _re


cdef class CompiledPredicate:
    """
    编译后的谓词
    """
    cdef int col_idx
    cdef str op
    cdef object value      # 原值
    cdef int64_t int_value # 整数版本
    cdef double float_value
    cdef bytes str_value
    cdef int value_type    # 0=null, 1=int, 2=float, 3=str

    def __cinit__(self, int col_idx, str op, object value):
        self.col_idx = col_idx
        self.op = op
        self.value = value
        self.value_type = 0

        if value is None:
            return

        if isinstance(value, bool):
            self.value_type = 1
            self.int_value = 1 if value else 0
        elif isinstance(value, int):
            self.value_type = 1
            self.int_value = <int64_t>value
        elif isinstance(value, float):
            self.value_type = 2
            self.float_value = <double>value
        else:
            self.value_type = 3
            self.str_value = str(value).encode('utf-8')

    cpdef bint match(self, list row):
        """匹配一行"""
        if self.col_idx >= len(row):
            return False

        cdef object v = row[self.col_idx]
        if v is None:
            return self.op == 'IS NULL'

        cdef str op = self.op

        # 快速路径：整数比较
        if self.value_type == 1 and isinstance(v, int):
            cdef int64_t iv = <int64_t>v
            cdef int64_t cv = self.int_value
            if op == '=': return iv == cv
            if op == '!=': return iv != cv
            if op == '<': return iv < cv
            if op == '>': return iv > cv
            if op == '<=': return iv <= cv
            if op == '>=': return iv >= cv

        # 浮点
        if self.value_type == 2:
            try:
                cdef double dv = <double>float(v)
                cdef double cv = self.float_value
                if op == '=': return dv == cv
                if op == '!=': return dv != cv
                if op == '<': return dv < cv
                if op == '>': return dv > cv
                if op == '<=': return dv <= cv
                if op == '>=': return dv >= cv
            except (ValueError, TypeError):
                return False

        # 字符串
        if self.value_type == 3:
            cdef str sv = str(v)
            cdef bytes svb = sv.encode('utf-8')
            cdef const char* a = svb
            cdef const char* b = self.str_value
            if op == '=':
                return strcmp(a, b) == 0
            if op == '!=':
                return strcmp(a, b) != 0
            if op == '<':
                return strcmp(a, b) < 0
            if op == '>':
                return strcmp(a, b) > 0
            if op == '<=':
                return strcmp(a, b) <= 0
            if op == '>=':
                return strcmp(a, b) >= 0
            if op == 'LIKE':
                return self._match_like(sv)
            if op == 'STARTS':
                return sv.startswith(self.value)
            if op == 'ENDS':
                return sv.endswith(self.value)
            if op == 'CONTAINS':
                return self.value in sv

        return False

    cdef bint _match_like(self, str s):
        """LIKE 匹配"""
        cdef str pat = self.value
        # 简化：用 Python 正则（_re 在模块级导入，cdef 内不允许 import）
        regex = '^' + pat.replace('%', '.*').replace('_', '.') + '$'
        return bool(_re.match(regex, s))


cdef class PredicateSet:
    """
    谓词集合
    """
    cdef list predicates

    def __cinit__(self, list predicates):
        self.predicates = predicates

    cpdef bint match(self, list row):
        cdef CompiledPredicate p
        for p in self.predicates:
            if not p.match(row):
                return False
        return True

    cpdef list filter(self, list rows):
        """批量过滤"""
        cdef list result = []
        cdef list row
        for row in rows:
            if self.match(row):
                result.append(row)
        return result

    cpdef int count_matches(self, list rows):
        """计数（不返回列表）"""
        cdef int count = 0
        cdef list row
        for row in rows:
            if self.match(row):
                count += 1
        return count


# ==================== 快速整数过滤 ====================

cpdef list int_range_filter(list rows, int col_idx,
                              int64_t lo, int64_t hi):
    """
    整数范围过滤（极快）
    """
    cdef list result = []
    cdef list row
    cdef int64_t v
    for row in rows:
        if col_idx >= len(row):
            continue
        v = row[col_idx]
        if v is not None and lo <= v <= hi:
            result.append(row)
    return result


cpdef list int_eq_filter(list rows, int col_idx, int64_t value):
    """整数等值过滤"""
    cdef list result = []
    cdef list row
    cdef int64_t v
    for row in rows:
        if col_idx >= len(row):
            continue
        v = row[col_idx]
        if v is not None and v == value:
            result.append(row)
    return result