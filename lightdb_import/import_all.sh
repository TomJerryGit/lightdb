#!/bin/bash
# import_all.sh - 用 LightDB CLI 导入

HOST=${1:-127.0.0.1}
PORT=${2:-8888}
USER=${3:-root}

FILES="01_schema.sql 02_versions.sql 03_modules.sql \
       04_features.sql 05_benchmarks.sql 06_issues.sql \
       07_api.sql 08_docs.sql 09_tags.sql 10_files.sql"

for f in $FILES; do
    if [ -f "$f" ]; then
        echo ">>> 导入 $f"
        python -m lightdb.tools.cli \
            -h "$HOST" -P "$PORT" -u "$USER" \
            -e "SOURCE $f"
    else
        echo ">>> 跳过 $f（不存在）"
    fi
done

echo "✅ 全部导入完成"