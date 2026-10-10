#!/bin/bash
# LightDB Cython 构建脚本
# 用法: ./build.sh

set -e

echo "=========================================="
echo "  LightDB Cython 构建"
echo "=========================================="

cd "$(dirname "$0")"

# 1. 检查依赖
echo "[1/4] 检查依赖..."

python -c "import Cython" 2>/dev/null || {
    echo "❌ Cython 未安装"
    echo "   pip install cython"
    exit 1
}
echo "  ✅ Cython: $(python -c 'import Cython; print(Cython.__version__)')"

if command -v gcc &>/dev/null; then
    echo "  ✅ gcc: $(gcc --version | head -1)"
elif command -v clang &>/dev/null; then
    echo "  ✅ clang: $(clang --version | head -1)"
elif command -v cc &>/dev/null; then
    echo "  ✅ cc: $(cc --version | head -1)"
else
    echo "❌ C 编译器未找到"
    echo "   Linux: apt install build-essential"
    echo "   macOS: xcode-select --install"
    exit 1
fi

# 2. 清理旧构建
echo "\n[2/4] 清理旧构建..."
rm -rf build/ cython/*.c cython/*.so cython/*.pyd 2>/dev/null || true
echo "  ✅ 清理完成"

# 3. 编译
echo "\n[3/4] 编译 Cython 模块..."
cd cython
python setup.py build_ext --inplace
cd ..
echo "  ✅ 编译完成"

# 4. 验证
echo "\n[4/4] 验证模块..."
python -c "
from lightdb.cython import status
st = status()
for name, ok in st['available'].items():
    icon = '✅' if ok else '❌'
    print(f'  {icon} {name}')
if not st['all_available']:
    print()
    for name, err in st['errors'].items():
        print(f'  ❌ {name}: {err}')
    exit(1)
"

echo ""
echo "=========================================="
echo "  ✅ 构建完成！"
echo "=========================================="