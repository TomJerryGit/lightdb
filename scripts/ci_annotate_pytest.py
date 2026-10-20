"""把 pytest JUnit XML 的失败用例转成 GitHub Actions 注解（annotations）。

用途：仓库是公开的，远端 CI 日志下载需要认证；但 check-run annotations 可匿名读取，
因此在本步骤把失败用例（文件/用例名/首行错误）打印成 `::error ...::`，让
`GET /repos/<owner>/<repo>/commits/<sha>/check-runs` 的 annotations 直接暴露失败清单。

用法（CI）：`python scripts/ci_annotate_pytest.py pytest-results.xml`
无失败/无文件时打印提示并 exit 0（本步骤不改变 CI 结论）。
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _first_line(node, limit=400) -> str:
    parts = []
    for tag in ('failure', 'error'):
        el = node.find(tag)
        if el is not None:
            text = (el.get('message') or '') + '\n' + (el.text or '')
            parts.extend(text.splitlines())
    for line in parts:
        line = line.strip()
        if line:
            return line[:limit]
    return ''


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    path = Path(argv[0] if argv else 'pytest-results.xml')
    if not path.exists():
        print(f'[ci-annotate] 未找到 {path}（pytest 可能在收集阶段失败）')
        return 0

    try:
        root = ET.parse(path).getroot()
    except Exception as e:                        # noqa: BLE001
        print(f'[ci-annotate] 解析 {path} 失败: {e}')
        return 0

    total = failed = 0
    for case in root.iter('testcase'):
        total += 1
        if case.find('failure') is None and case.find('error') is None:
            continue
        failed += 1
        cls = (case.get('classname') or '').strip()
        name = (case.get('name') or '').strip()
        fname = case.get('file') or (cls.replace('.', '/') + '.py')
        msg = _first_line(case) or 'test failed'
        title = f'FAILED {cls}::{name}' if cls else f'FAILED {name}'
        # GitHub 注解转义：% \r \n
        msg = msg.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        title = title.replace('%', '%25')
        print(f'::error file={fname},title={title}::{msg}')

    print(f'[ci-annotate] tests={total} failed={failed} '
          f'（完整原因见上传的 junit xml artifact）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
