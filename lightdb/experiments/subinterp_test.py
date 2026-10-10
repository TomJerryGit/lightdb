"""
子解释器（PEP 684）验证
- Python 3.12+ 支持 subinterpreters
"""
import sys
import time


class SubInterpExperiment:
    """子解释器实验"""

    def has_subinterp(self) -> bool:
        return hasattr(__import__('_xxsubinterpreters', fromlist=['']),
                       'create') if sys.version_info >= (3, 12) else False

    def run_all(self) -> dict:
        print("=" * 60)
        print("  子解释器验证")
        print("=" * 60)
        print(f"  Python: {sys.version.split()[0]}")
        print(f"  支持: {self.has_subinterp()}")

        if not self.has_subinterp():
            return {
                'supported': False,
                'reason': '需要 Python 3.12+',
            }

        try:
            import _xxsubinterpreters as sub
            results = []

            # 单解释器基线
            t0 = time.time()
            self._cpu_work(1_000_000)
            single_time = time.time() - t0

            # 子解释器
            interp_ids = [sub.create() for _ in range(4)]
            t0 = time.time()
            for iid in interp_ids:
                sub.run_string(iid, 'x = 0\nfor i in range(1_000_000): x += i')
            multi_time = time.time() - t0

            for iid in interp_ids:
                sub.destroy(iid)

            return {
                'supported': True,
                'single_time': single_time,
                'multi_time': multi_time,
                'speedup': single_time * 4 / multi_time
                if multi_time > 0 else 0,
            }
        except Exception as e:
            return {'supported': False, 'error': str(e)}

    def _cpu_work(self, n):
        x = 0
        for i in range(n):
            x += i
        return x


if __name__ == '__main__':
    exp = SubInterpExperiment()
    result = exp.run_all()
    print(result)