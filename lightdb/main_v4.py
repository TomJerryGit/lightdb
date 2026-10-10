"""
LightDB v4.0 多进程启动（修复版）

修复:
- Executor.start_event_scheduler 用 try/except 兜底
- Database / Executor 初始化失败处理
- 用临时 JSON 配置文件传给 Master（避免 asdict 问题）
- 独立信号处理

用法:
    from lightdb.main_v4 import run_multi
    run_multi(cfg)

    # 或直接:
    python -m lightdb.main_v4 --workers 8
"""
import argparse
import json
import os
import signal
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))

# 控制台编码保护：中文 Windows 控制台为 GBK，emoji 会让 print 抛
# UnicodeEncodeError 并中断启动（多进程模式下这正是 Worker 池启动失败的原因之一）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except Exception:
        pass


# ==================== 信号处理 ====================

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


def _install_signal_handlers():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


# ==================== 主入口 ====================

def run_multi(cfg) -> int:
    """
    启动多进程模式。

    参数:
        cfg: lightdb.config.Config 实例

    返回:
        退出码 (0=正常)
    """
    from lightdb.logger import setup_logger

    # ---------- 日志 ----------
    log = setup_logger(
        level=cfg.log_level,
        log_dir=cfg.effective_log_dir)

    # ---------- Worker 数 ----------
    workers = cfg.workers or os.cpu_count() or 4
    if workers < 1:
        workers = 1

    log.info("=" * 60)
    log.info("  LightDB v4.0 多进程模式")
    log.info("=" * 60)
    log.info(f"  版本:         {_get_version()}")
    log.info(f"  监听:         {cfg.listen_addr}")
    log.info(f"  数据目录:     {cfg.data_dir}")
    log.info(f"  日志目录:     {cfg.log_dir or '(stdout)'}")
    log.info(f"  Worker 数:    {workers}")
    log.info(f"  CPU 核数:     {os.cpu_count()}")
    log.info(f"  共享内存目录: {cfg.effective_shm_dir or '(无)'}")
    log.info("=" * 60)

    # ---------- 准备临时配置 ----------
    tmp_config_path = None
    try:
        # 把 cfg 转成 dict，写入临时文件
        cfg_dict = asdict(cfg)
        cfg_dict['workers'] = workers

        tmp = tempfile.NamedTemporaryFile(
            mode='w',
            suffix='.json',
            prefix='lightdb_v4_',
            delete=False,
            encoding='utf-8')
        json.dump(cfg_dict, tmp, ensure_ascii=False, indent=2)
        tmp.close()
        tmp_config_path = tmp.name

        log.debug(f"临时配置: {tmp_config_path}")

    except Exception as e:
        log.exception("写入临时配置失败")
        return 1

    # ---------- 加载 Master ----------
    try:
        from lightdb.net.master import run_master
    except ImportError as e:
        log.exception("无法加载 Master 模块")
        log.error(
            "  请确认 lightdb/net/master.py 存在且可导入")
        _cleanup(tmp_config_path)
        return 1

    # ---------- 安装信号处理 ----------
    _install_signal_handlers()

    # ---------- 启动 Master ----------
    exit_code = 0
    try:
        log.info("  正在启动 Master...")
        log.info("=" * 60)

        run_master(tmp_config_path)

    except KeyboardInterrupt:
        log.info("收到中断信号，正在关闭...")
    except Exception as e:
        log.exception("Master 异常")
        exit_code = 1
    finally:
        _cleanup(tmp_config_path)
        log.info("多进程服务已退出")
        try:
            log.close()
        except Exception:
            pass

    return exit_code


# ==================== 工具 ====================

def _cleanup(path):
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except Exception:
            pass


def _get_version() -> str:
    try:
        from lightdb import __version__
        return __version__
    except Exception:
        return "unknown"


# ==================== CLI ====================

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='lightdb-server-v4',
        description='LightDB v4.0 多进程数据库服务')
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--host', default=None)
    ap.add_argument('--port', type=int, default=None)
    ap.add_argument('--workers', type=int, default=None,
                    help='Worker 进程数（0=CPU核数）')
    ap.add_argument('--data-dir', default=None)
    ap.add_argument('--log-dir', default=None)
    ap.add_argument('--log-level', default=None,
                    choices=['DEBUG', 'INFO', 'WARN',
                             'WARNING', 'ERROR'])
    ap.add_argument('--check', action='store_true',
                    help='仅校验配置后退出')
    ap.add_argument('--version', action='store_true')
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.version:
        print(f"LightDB {_get_version()}")
        return 0

    # ---------- 加载配置 ----------
    from lightdb.config import Config

    cfg = Config.load(args.config, env_override=True)

    # 命令行覆盖
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.workers is not None:
        cfg.workers = args.workers
    if args.data_dir:
        cfg.data_dir = os.path.abspath(args.data_dir)
    if args.log_dir:
        cfg.log_dir = os.path.abspath(args.log_dir)
    if args.log_level:
        cfg.log_level = args.log_level

    cfg._normalize()

    # ---------- 校验 ----------
    errors = cfg.validate()
    if errors:
        print("[错误] 配置错误:", file=sys.stderr)
        for e in errors:
            print(f"   - {e}", file=sys.stderr)
        return 1

    if args.check:
        print("[OK] 配置校验通过")
        print(cfg.describe())
        return 0

    # ---------- 启动 ----------
    return run_multi(cfg)


if __name__ == '__main__':
    sys.exit(main())