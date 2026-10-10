"""pytest 全局配置

中文 Windows 控制台默认 GBK(cp936)，而本仓库的测试里有大量 emoji 输出
（✅ / ❌ / → 等）。在 GBK 下这些 print 会直接抛 UnicodeEncodeError，
测试因此被判失败；更麻烦的是异常发生在 `db.shutdown()` 之前，插入的数据
还留在 BufferPool 内存里没有落盘，导致后面依赖同一数据目录的测试读到空表。

这里把标准流改成「无法编码的字符就替换」，与 lightdb/main.py 的处理一致：
中文照常显示，emoji 变问号，但不再中断测试。
"""
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except Exception:
        pass
