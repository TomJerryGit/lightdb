import os
import re
from pathlib import Path

# 乱码替换映射：将树形符号的乱码还原为标准字符
REPLACEMENTS = {
    'в”њ': '├',
    'в””': '└',
    'в”‚': '│',
    'в”Ђ': '─',
}

def expand_range(name):
    """展开类似 'test_batch1.py ~ test_batch15.py' 的范围表示"""
    if ' ~ ' not in name:
        return [name]
    parts = name.split(' ~ ')
    if len(parts) != 2:
        return [name]
    left, right = parts[0].strip(), parts[1].strip()
    # 尝试提取前缀、数字、后缀
    m1 = re.match(r'^(.*?)(\d+)(\.[^.]+)$', left)
    m2 = re.match(r'^(.*?)(\d+)(\.[^.]+)$', right)
    if not m1 or not m2:
        return [name]
    prefix1, num1, suffix1 = m1.groups()
    prefix2, num2, suffix2 = m2.groups()
    if prefix1 != prefix2 or suffix1 != suffix2:
        return [name]
    start, end = int(num1), int(num2)
    if start > end:
        start, end = end, start
    return [f"{prefix1}{i}{suffix1}" for i in range(start, end + 1)]

def parse_tree(file_path):
    """解析项目树文本，返回 (类型, 路径) 列表"""
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # 替换乱码
    processed_lines = []
    for line in lines:
        for old, new in REPLACEMENTS.items():
            line = line.replace(old, new)
        processed_lines.append(line.rstrip('\n'))

    stack = []          # 当前目录栈
    paths = []          # 收集所有要创建的路径

    for line in processed_lines:
        if not line.strip():
            continue
        # 去掉行尾注释（如果有 # 则截断，但你的文本中没有 #，这里做通用处理）
        if '#' in line:
            line = line.split('#')[0].rstrip()
        if not line.strip():
            continue

        # 匹配树形前缀和路径名
        m = re.match(r'^([│├└─ ]*)(.*)$', line)
        if not m:
            continue
        prefix, rest = m.groups()
        rest = rest.strip()
        if not rest:
            continue

        # 只取第一个空白分隔的 token 作为名称（忽略后面的乱码注释）
        name = rest.split()[0] if rest.split() else rest
        if not name:
            continue

        # 每个层级固定 4 个字符宽度
        depth = len(prefix) // 4

        # 调整栈到当前深度
        if depth < len(stack):
            stack = stack[:depth]
        elif depth > len(stack):
            # 深度跳跃，可能是解析错误，跳过
            print(f"警告：深度跳跃 {depth} > {len(stack)}，行：{line}")
            continue

        # 判断是目录还是文件
        is_dir = name.endswith('/')
        clean_name = name.rstrip('/')

        # 展开范围表示（如 test_batch1.py ~ test_batch15.py）
        expanded_names = expand_range(clean_name) if not is_dir else [clean_name]

        for item in expanded_names:
            if is_dir:
                stack.append(item)
                full_path = os.path.join(*stack) if stack else item
                paths.append(('dir', full_path))
            else:
                full_path = os.path.join(*stack, item) if stack else item
                paths.append(('file', full_path))

    return paths

def create_project(paths, base_dir='.'):
    """根据解析结果创建目录和空文件"""
    for type_, path in paths:
        full_path = os.path.join(base_dir, path)
        if type_ == 'dir':
            os.makedirs(full_path, exist_ok=True)
            print(f"创建目录: {full_path}")
        else:
            parent = os.path.dirname(full_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            Path(full_path).touch()
            print(f"创建文件: {full_path}")

if __name__ == '__main__':
    tree_file = 'lightdb_project.txt'
    if not os.path.exists(tree_file):
        print(f"找不到 {tree_file}，请确保文件存在且编码为 UTF-8。")
    else:
        paths = parse_tree(tree_file)
        create_project(paths)
        print("\n项目结构创建完成。")