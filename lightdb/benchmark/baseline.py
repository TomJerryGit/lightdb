"""
基线存储与对比
"""
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


BASELINE_DIR = Path.home() / '.lightdb_studio' / 'benchmarks'


class BaselineStore:
    """基线存储"""

    def __init__(self):
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        self.dir = BASELINE_DIR

    # ==================== 存储 ====================

    def save(self, name: str, result: Dict):
        """保存基线"""
        path = self.dir / f"{name}.json"
        data = {
            'name': name,
            'created_at': time.time(),
            'created_at_str': datetime.now().strftime(
                '%Y-%m-%d %H:%M:%S'),
            'workload': ','.join(result.get('results', {}).keys()),
            'data': result,
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 更新 latest 指针
        latest_path = self.dir / 'latest.json'
        with open(latest_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get(self, name: str) -> Optional[Dict]:
        if name == 'latest':
            name = 'latest'
        path = self.dir / f"{name}.json"
        if not path.exists():
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    def get_latest(self) -> Optional[Dict]:
        return self.get('latest')

    def list_all(self) -> List[Dict]:
        baselines = []
        for f in self.dir.glob('*.json'):
            if f.stem == 'latest':
                continue
            try:
                with open(f, 'r', encoding='utf-8') as fp:
                    data = json.load(fp)
                baselines.append({
                    'name': data.get('name', f.stem),
                    'created_at_str': data.get('created_at_str', ''),
                    'workload': data.get('workload', ''),
                    'created_at': data.get('created_at', 0),
                })
            except Exception:
                pass
        return sorted(baselines, key=lambda x: -x['created_at'])

    # ==================== 对比 ====================

    def compare(self, baseline_name: str,
                current_name: str = 'latest') -> Optional[Dict]:
        b = self.get(baseline_name)
        c = self.get(current_name)
        if not b or not c:
            print(f"❌ 基线或当前数据不存在")
            return None

        base_res = b['data'].get('results', {})
        curr_res = c['data'].get('results', {})

        comparison = {
            'baseline': baseline_name,
            'current': current_name,
            'workloads': {},
        }

        all_names = set(base_res.keys()) | set(curr_res.keys())
        for wname in all_names:
            b_data = base_res.get(wname, {})
            c_data = curr_res.get(wname, {})

            b_qps = b_data.get('qps', 0)
            c_qps = c_data.get('qps', 0)

            if b_qps > 0:
                delta_pct = (c_qps - b_qps) / b_qps * 100
            else:
                delta_pct = 0

            comparison['workloads'][wname] = {
                'baseline_qps': b_qps,
                'current_qps': c_qps,
                'qps_delta_pct': round(delta_pct, 2),
                'baseline_p99': b_data.get('latency_ms', {}).get('p99', 0),
                'current_p99': c_data.get('latency_ms', {}).get('p99', 0),
                'p99_delta_pct': self._delta_pct(
                    b_data.get('latency_ms', {}).get('p99', 0),
                    c_data.get('latency_ms', {}).get('p99', 0)),
            }

        return comparison

    def _delta_pct(self, base, curr) -> float:
        if base == 0:
            return 0
        return round((curr - base) / base * 100, 2)

    def format_compare(self, cmp: Dict) -> str:
        lines = []
        lines.append("=" * 80)
        lines.append(f"  性能对比: {cmp['baseline']}  →  {cmp['current']}")
        lines.append("=" * 80)
        lines.append("")
        lines.append(f"{'workload':20s}  {'base QPS':>12s}  "
                      f"{'curr QPS':>12s}  {'QPS Δ':>10s}  "
                      f"{'P99 Δ':>10s}")
        lines.append("-" * 80)

        for wname, w in sorted(cmp['workloads'].items()):
            qps_delta = w['qps_delta_pct']
            arrow = '↑' if qps_delta > 1 else ('↓' if qps_delta < -1 else '≈')
            lines.append(
                f"{wname:20s}  "
                f"{w['baseline_qps']:>12.1f}  "
                f"{w['current_qps']:>12.1f}  "
                f"{arrow} {qps_delta:>+6.1f}%  "
                f"{w['p99_delta_pct']:>+6.1f}%")

        lines.append("=" * 80)
        return '\n'.join(lines)