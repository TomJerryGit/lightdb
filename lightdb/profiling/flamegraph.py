"""
火焰图生成（不依赖 py-spy 的纯 Python 实现）
- 从 cProfile 生成 SVG
- 或调用 py-spy 采集真实火焰图
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

try:
    import pstats
    HAS_PSTATS = True
except ImportError:
    HAS_PSTATS = False


class FlameGraphGenerator:
    """火焰图生成器"""

    def __init__(self, output_dir: str = None):
        self.output_dir = Path(output_dir or
                                 Path.home() / '.lightdb_studio' / 'profiles')
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ==================== py-spy 方式（推荐）====================

    def has_pyspy(self) -> bool:
        """检查 py-spy 是否可用"""
        try:
            subprocess.run(['py-spy', '--version'],
                            capture_output=True, timeout=3)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def install_pyspy_hint(self) -> str:
        return ("pip install py-spy  # 或\n"
                "cargo install py-spy")

    def record_pid(self, pid: int, duration: int = 30,
                   output: str = None) -> Optional[str]:
        """采集运行进程（推荐）"""
        if not self.has_pyspy():
            print(f"❌ py-spy 未安装\n{self.install_pyspy_hint()}")
            return None

        output = output or str(
            self.output_dir / f"flame_{pid}_{int(__import__('time').time())}.svg")

        cmd = [
            'py-spy', 'record',
            '-p', str(pid),
            '-o', output,
            '-d', str(duration),
            '--format', 'flamegraph',
            '--rate', '100',   # 100 Hz 采样
        ]

        try:
            subprocess.run(cmd, check=True, timeout=duration + 10)
            print(f"✅ 火焰图: {output}")
            return output
        except Exception as e:
            print(f"❌ 采集失败: {e}")
            return None

    def record_command(self, command: List[str],
                       output: str = None) -> Optional[str]:
        """采集命令"""
        if not self.has_pyspy():
            return None

        output = output or str(
            self.output_dir / f"flame_cmd_{int(__import__('time').time())}.svg")

        cmd = ['py-spy', 'record',
               '-o', output, '--format', 'flamegraph'] + command

        try:
            subprocess.run(cmd, check=True)
            return output
        except Exception as e:
            print(f"❌ 采集失败: {e}")
            return None

    def dump_pid(self, pid: int) -> str:
        """快速转储（当前栈）"""
        if not self.has_pyspy():
            return "py-spy 未安装"

        try:
            result = subprocess.run(
                ['py-spy', 'dump', '-p', str(pid)],
                capture_output=True, text=True, timeout=10)
            return result.stdout
        except Exception as e:
            return f"失败: {e}"

    # ==================== 从 cProfile 生成 ====================

    def from_cprofile(self, prof_file: str,
                      output: str = None) -> Optional[str]:
        """
        从 cProfile 文件生成简单可视化
        （不依赖 gprof2dot/FlameGraph.pl）
        """
        if not os.path.exists(prof_file):
            return None

        output = output or str(
            self.output_dir / f"cprofile_{Path(prof_file).stem}.txt")

        try:
            stats = pstats.Stats(prof_file)
            with open(output, 'w', encoding='utf-8') as f:
                stats.stream = f
                stats.sort_stats('cumulative')
                stats.print_stats(50)
            return output
        except Exception as e:
            print(f"❌ 生成失败: {e}")
            return None

    def to_svg_from_cprofile(self, prof_file: str,
                              output: str = None) -> Optional[str]:
        """
        通过 gprof2dot + graphviz 生成 SVG
        需要: pip install gprof2dot + apt install graphviz
        """
        if not os.path.exists(prof_file):
            return None

        output = output or str(
            self.output_dir / f"flame_{Path(prof_file).stem}.svg")
        dot_file = tempfile.mktemp(suffix='.dot')

        try:
            # gprof2dot 转 dot
            subprocess.run(
                ['gprof2dot', '-f', 'pstats', prof_file, '-o', dot_file],
                check=True, capture_output=True)
            # dot 转 svg
            subprocess.run(
                ['dot', '-Tsvg', dot_file, '-o', output],
                check=True, capture_output=True)
            os.unlink(dot_file)
            return output
        except FileNotFoundError:
            print("需安装: pip install gprof2dot；apt install graphviz")
            return None
        except Exception as e:
            print(f"❌ 生成失败: {e}")
            return None


# ==================== 常见用途示例 ====================

def example_usage():
    """使用示例"""
    gen = FlameGraphGenerator()

    if not gen.has_pyspy():
        print("安装 py-spy：")
        print(gen.install_pyspy_hint())
        return

    # 1. 采集运行中的 LightDB
    import subprocess
    try:
        out = subprocess.check_output(
            ['pgrep', '-f', 'lightdb.*main.py'],
            stderr=subprocess.DEVNULL)
        pid = int(out.decode().strip().split('\n')[0])
        print(f"采集 PID={pid}")
        svg = gen.record_pid(pid, duration=30)
        print(f"输出: {svg}")
    except Exception as e:
        print(f"未找到 LightDB 进程: {e}")

    # 2. 采集命令
    gen.record_command([sys.executable, '-c',
                        'import time; time.sleep(5)'])


if __name__ == '__main__':
    example_usage()