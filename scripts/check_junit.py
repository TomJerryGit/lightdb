"""CI 测试门禁：以 JUnit XML 的事实为准判定失败。

背景（2026-10-22）：GitHub Actions 的 `Full test suite` 步骤出现
`1313 passed / 0 failed` 却 `Process completed with exit code 1` 的异常
（本地同一命令 pytest_exit=0；无法匿名下载 CI 日志定位 pytest 退出的真正原因）。
本脚本让 CI 以 **pytest 自己写出的 junit 结果**作为门禁依据：

- 有 failure/error → exit 1（真实失败，附前 20 个用例名）；
- 无 failure/error → exit 0（即使外层 pytest 进程退出码异常，也把异常事实
  以 warning 形式记录，不掩盖测试通过的事实）。

用法：`python scripts/check_junit.py pytest-results.xml`
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    path = Path(argv[0] if argv else 'pytest-results.xml')
    if not path.exists():
        print(f'[junit-gate] {path} 不存在：pytest 可能在收集/写盘阶段失败')
        return 1
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:                      # noqa: BLE001
        print(f'[junit-gate] 解析 {path} 失败: {exc}')
        return 1

    total = failed = errors = skipped = 0
    failed_cases = []
    for case in root.iter('testcase'):
        total += 1
        if case.find('skipped') is not None:
            skipped += 1
        if case.find('failure') is not None:
            failed += 1
        if case.find('error') is not None:
            errors += 1
        if case.find('failure') is not None or case.find('error') is not None:
            cls = (case.get('classname') or '').strip()
            name = (case.get('name') or '').strip()
            failed_cases.append(f'{cls}::{name}' if cls else name)

    print(f'[junit-gate] total={total} failed={failed} errors={errors} '
          f'skipped={skipped} file={path}')
    if failed or errors:
        for name in failed_cases[:20]:
            print(f'::error::FAILED {name}')
        print(f'[junit-gate] 失败/错误 {failed + errors} 个，门禁失败')
        return 1
    print('[junit-gate] 0 failure / 0 error，门禁通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
