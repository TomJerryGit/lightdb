"""打包完整性检查（CI 与本地都用这一条）。

校验"可交付"的最低门槛：
1. `pip wheel .` 产出的 wheel 里必须包含 **lightdb 引擎包**、lightdb_studio、web；
2. `entry_points.txt` 必须提供 lightdb-server / lightdb-studio / lightdb-studio-web，
   且不再包含指向不存在模块的 lightdb-studio-cli；
3. 包元数据版本与 `lightdb.__version__` 一致（单一版本来源）。

用法：
    python scripts/check_packaging.py
退出码：0 = 通过；1 = 任一检查失败。
"""
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from lightdb import __version__ as engine_version  # noqa: E402

    failures = []
    with tempfile.TemporaryDirectory(prefix="lightdb-wheel-") as tmp:
        out = Path(tmp) / "wheel"
        out.mkdir()
        cmd = [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
               "--no-build-isolation", "-w", str(out)]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stdout[-4000:])
            print(proc.stderr[-4000:])
            print("[FAIL] pip wheel 构建失败")
            return 1
        wheels = sorted(out.glob("*.whl"))
        if not wheels:
            print("[FAIL] 没有产出 wheel")
            return 1
        wheel = wheels[0]
        print(f"[OK] 构建 wheel: {wheel.name} ({wheel.stat().st_size} bytes)")

        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()

        def _has_prefix(prefix):
            return any(n == prefix or n.startswith(prefix + "/") for n in names)

        for pkg in ("lightdb", "lightdb_studio", "web"):
            if _has_prefix(pkg):
                print(f"[OK] wheel 包含 {pkg}/")
            else:
                failures.append(f"wheel 缺少 {pkg}/ 包")
        for required in ("lightdb/main.py", "lightdb_studio/app.py",
                         "web/start.py"):
            if required not in names:
                failures.append(f"wheel 缺少关键文件 {required}")

        ep_name = next((n for n in names if n.endswith("entry_points.txt")), None)
        if not ep_name:
            failures.append("wheel 缺少 entry_points.txt")
        else:
            text = zf.read(ep_name).decode("utf-8", "replace")
            for entry in ("lightdb-server = lightdb.main:main",
                          "lightdb-studio = lightdb_studio.app:run",
                          "lightdb-studio-web = web.start:main"):
                if entry.replace(" = ", "=") in text.replace(" = ", "="):
                    print(f"[OK] entry point: {entry.split(' = ')[0]}")
                else:
                    failures.append(f"entry point 缺失: {entry}")
            if "lightdb-studio-cli" in text:
                failures.append("仍存在指向不存在模块的 lightdb-studio-cli 入口")

        meta_name = next((n for n in names if n.endswith("METADATA")), None)
        if not meta_name:
            failures.append("wheel 缺少 METADATA")
        else:
            meta = zf.read(meta_name).decode("utf-8", "replace")
            m = re.search(r"^Version: (.+)$", meta, re.MULTILINE)
            if not m:
                failures.append("METADATA 缺少 Version")
            elif m.group(1).strip() != engine_version:
                failures.append(
                    f"版本不一致：METADATA={m.group(1).strip()} "
                    f"lightdb.__version__={engine_version}")
            else:
                print(f"[OK] 版本单一来源: {engine_version}")

    if failures:
        print("\n打包检查失败：")
        for f in failures:
            print("  -", f)
        return 1
    print("\n[PASS] 打包完整性检查通过（引擎包 + 入口 + 版本）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
