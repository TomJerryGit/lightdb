"""
LLM 微调管理
- 训练数据集自动构建
- LoRA 微调任务管理
- 模型评估
- 版本管理
"""
import json
import hashlib
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional, Callable

FINETUNE_DIR = Path.home() / '.lightdb_studio' / 'finetune'


@dataclass
class TrainingSample:
    """训练样本"""
    sample_id: str = ""
    instruction: str = ""       # 用户问题
    input: str = ""             # 上下文（schema）
    output: str = ""            # 目标 SQL
    source: str = "manual"      # manual / history / feedback
    quality: str = "unverified" # verified / unverified / rejected
    created_at: float = 0

    def __post_init__(self):
        if not self.sample_id:
            self.sample_id = hashlib.md5(
                (self.instruction + self.output).encode()
            ).hexdigest()[:12]
        if not self.created_at:
            self.created_at = time.time()

    def to_alpaca(self) -> Dict:
        """Alpaca 格式"""
        return {
            'instruction': self.instruction,
            'input': self.input,
            'output': self.output,
        }


@dataclass
class FineTuneJob:
    """微调任务"""
    job_id: str = ""
    base_model: str = "gpt-4o-mini"
    method: str = "lora"           # lora / full / qlora
    # 数据
    train_samples: int = 0
    val_samples: int = 0
    dataset_path: str = ""
    # 超参
    epochs: int = 3
    batch_size: int = 4
    learning_rate: float = 1e-4
    lora_r: int = 8
    lora_alpha: int = 16
    # 状态
    status: str = "pending"         # pending / preparing / training / evaluating / completed / failed
    progress: float = 0
    current_epoch: int = 0
    loss_history: List[float] = field(default_factory=list)
    # 结果
    output_model: str = ""
    eval_score: float = 0
    error: str = ""
    # 时间
    started_at: float = 0
    finished_at: float = 0
    created_at: float = 0

    def __post_init__(self):
        if not self.job_id:
            self.job_id = f"ft_{int(time.time()*1000)%1000000}"
        if not self.created_at:
            self.created_at = time.time()


class DatasetBuilder:
    """训练数据集构建器"""

    def __init__(self):
        self.dir = FINETUNE_DIR / 'datasets'
        self.dir.mkdir(parents=True, exist_ok=True)

    def build_from_history(self, history: List[Dict],
                           schema_provider=None,
                           min_quality: int = 1) -> List[TrainingSample]:
        """
        从查询历史构建数据集
        history: [{'question': '...', 'sql': '...', 'feedback': 1}]
        """
        samples = []
        for h in history:
            fb = h.get('feedback', 0)
            if fb < min_quality:
                continue

            # 构建 schema 上下文
            schema_ctx = ''
            if schema_provider:
                try:
                    tables, cols = schema_provider()
                    lines = []
                    for t in tables[:10]:
                        lines.append(
                            f"- {t}({', '.join(cols.get(t, [])[:10])})")
                    schema_ctx = '\n'.join(lines)
                except Exception:
                    pass

            sample = TrainingSample(
                instruction=h.get('question', ''),
                input=schema_ctx,
                output=h.get('sql', ''),
                source='history',
                quality='verified' if fb > 0 else 'unverified',
            )
            if sample.instruction and sample.output:
                samples.append(sample)

        return samples

    def build_from_feedback(self, feedback_list: List[Dict]) -> List[TrainingSample]:
        """从明确反馈构建"""
        samples = []
        for fb in feedback_list:
            if fb.get('rating', 0) <= 0:
                continue
            samples.append(TrainingSample(
                instruction=fb.get('question', ''),
                input=fb.get('schema', ''),
                output=fb.get('correct_sql', fb.get('sql', '')),
                source='feedback',
                quality='verified',
            ))
        return [s for s in samples if s.instruction and s.output]

    def save_dataset(self, name: str, samples: List[TrainingSample],
                     val_ratio: float = 0.1) -> Dict:
        """保存数据集（Alpaca JSON 格式）"""
        # 去重
        seen = set()
        unique = []
        for s in samples:
            key = (s.instruction, s.output)
            if key in seen:
                continue
            seen.add(key)
            unique.append(s)

        # 划分
        import random
        random.shuffle(unique)
        n_val = max(1, int(len(unique) * val_ratio))
        val = unique[:n_val]
        train = unique[n_val:]

        # 写文件
        train_path = self.dir / f"{name}_train.json"
        val_path = self.dir / f"{name}_val.json"

        with open(train_path, 'w', encoding='utf-8') as f:
            json.dump([s.to_alpaca() for s in train], f,
                      ensure_ascii=False, indent=2)
        with open(val_path, 'w', encoding='utf-8') as f:
            json.dump([s.to_alpaca() for s in val], f,
                      ensure_ascii=False, indent=2)

        return {
            'name': name,
            'train_path': str(train_path),
            'val_path': str(val_path),
            'train_samples': len(train),
            'val_samples': len(val),
            'total': len(unique),
        }

    def validate_dataset(self, path: str) -> Dict:
        """验证数据集格式"""
        if not Path(path).exists():
            return {'ok': False, 'error': '文件不存在'}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            return {'ok': False, 'error': str(e)}

        issues = []
        for i, item in enumerate(data):
            if 'instruction' not in item:
                issues.append(f"样本 {i}: 缺少 instruction")
            if 'output' not in item:
                issues.append(f"样本 {i}: 缺少 output")
            elif not item['output'].strip():
                issues.append(f"样本 {i}: output 为空")

        return {
            'ok': len(issues) == 0,
            'total': len(data),
            'issues': issues[:20],
        }


class FineTuneManager:
    """微调管理器（模拟器）"""

    def __init__(self):
        self.file = FINETUNE_DIR / 'jobs.json'
        FINETUNE_DIR.mkdir(parents=True, exist_ok=True)
        self.jobs: Dict[str, FineTuneJob] = {}
        self._callbacks: List[Callable] = []
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for jid, jd in data.get('jobs', {}).items():
                self.jobs[jid] = FineTuneJob(**jd)
        except Exception:
            pass

    def save(self):
        data = {'jobs': {j: asdict(jb)
                          for j, jb in self.jobs.items()}}
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    def create_job(self, base_model: str, dataset_path: str,
                   method: str = 'lora', **hyperparams) -> FineTuneJob:
        job = FineTuneJob(
            base_model=base_model,
            method=method,
            dataset_path=dataset_path,
        )
        for k, v in hyperparams.items():
            if hasattr(job, k):
                setattr(job, k, v)

        self.jobs[job.job_id] = job
        self.save()
        return job

    def start_job(self, job_id: str,
                  simulator: bool = True):
        """
        启动微调任务
        simulator=True 时模拟训练过程（不实际调用 LLM API）
        """
        job = self.jobs.get(job_id)
        if not job:
            return

        if simulator:
            import threading
            t = threading.Thread(
                target=self._simulate_training, args=(job,),
                daemon=True)
            t.start()
        else:
            # 生产环境：调用 OpenAI Finetune API / LlamaFactory
            job.status = 'preparing'
            job.error = ('实际微调需配置训练环境。'
                          '参考 https://platform.openai.com/docs/guides/fine-tuning')
            self.save()

    def _simulate_training(self, job: FineTuneJob):
        """模拟训练过程"""
        import math
        import random

        job.status = 'preparing'
        job.started_at = time.time()
        self.save()
        time.sleep(0.5)

        # 加载数据集
        try:
            with open(job.dataset_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            total = len(data)
        except Exception:
            total = 100

        job.train_samples = int(total * 0.9)
        job.val_samples = total - job.train_samples

        job.status = 'training'
        self.save()

        # 模拟 epoch
        for epoch in range(1, job.epochs + 1):
            job.current_epoch = epoch
            steps_per_epoch = 10
            for step in range(steps_per_epoch):
                # 模拟损失下降
                base_loss = 2.5 * math.exp(-0.3 * (epoch - 1))
                loss = base_loss * (1 - 0.05 * step) + \
                       random.gauss(0, 0.05)
                job.loss_history.append(max(0.1, loss))

                job.progress = (epoch - 1 + (step + 1) / steps_per_epoch) \
                    / job.epochs
                self.save()
                time.sleep(0.05)

        # 评估
        job.status = 'evaluating'
        self.save()
        time.sleep(0.5)

        # 模拟评估分数
        final_loss = job.loss_history[-1] if job.loss_history else 1.0
        job.eval_score = max(0, min(100, 100 - final_loss * 15))

        job.status = 'completed'
        job.output_model = f"{job.base_model}_lora_{job.job_id}"
        job.finished_at = time.time()
        job.progress = 1.0
        self.save()

        for cb in self._callbacks:
            try:
                cb(job)
            except Exception:
                pass

    def cancel_job(self, job_id: str):
        job = self.jobs.get(job_id)
        if job and job.status in ('pending', 'preparing', 'training'):
            job.status = 'failed'
            job.error = '用户取消'
            self.save()

    def on_complete(self, callback):
        self._callbacks.append(callback)

    def get_job(self, job_id: str) -> Optional[FineTuneJob]:
        return self.jobs.get(job_id)

    def list_jobs(self) -> List[FineTuneJob]:
        return sorted(self.jobs.values(),
                       key=lambda x: -x.created_at)

    def stats(self) -> dict:
        by_status = {}
        for j in self.jobs.values():
            by_status[j.status] = by_status.get(j.status, 0) + 1
        return {
            'total_jobs': len(self.jobs),
            'by_status': by_status,
            'completed': sum(1 for j in self.jobs.values()
                              if j.status == 'completed'),
        }