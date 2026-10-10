"""
LightDB 主入口（修复版）

用法:
    python -m lightdb.main                    # 默认配置
    python -m lightdb.main --config config.json
    python -m lightdb.main --mode single
    python -m lightdb.main --mode multi --workers 8
    python -m lightdb.main --host 0.0.0.0 --port 8888

也可以直接:
    lightdb-server
"""
import argparse
import os
import signal
import sys
from pathlib import Path

# 确保可以 import lightdb
if __package__ is None:
    sys.path.insert(0, str(Path(__file__).parent.parent))


# ==================== 信号处理 ====================

_shutdown_requested = False


def _handle_signal(signum, frame):
    """收到信号 → 请求关闭"""
    global _shutdown_requested
    _shutdown_requested = True


def _install_signal_handlers():
    """安装信号处理"""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    # SIGHUP 在某些系统上不可用
    if hasattr(signal, 'SIGHUP'):
        try:
            signal.signal(signal.SIGHUP, _handle_signal)
        except Exception:
            pass


# ==================== 参数解析 ====================

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='lightdb-server',
        description='LightDB MySQL 兼容数据库服务',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 默认配置启动（单进程）
  python -m lightdb.main

  # 指定配置文件
  python -m lightdb.main --config /app/config.json

  # 多进程模式（8 Worker）
  python -m lightdb.main --mode multi --workers 8

  # 覆盖端口
  python -m lightdb.main --port 9999

环境变量:
  LIGHTDB_HOST         监听地址
  LIGHTDB_PORT         端口
  LIGHTDB_WORKERS      Worker 数
  LIGHTDB_DATA_DIR     数据目录
  LIGHTDB_LOG_LEVEL    日志级别
  LIGHTDB_LOG_DIR      日志目录
        """)

    # ---------- 模式 ----------
    ap.add_argument(
        '--mode', default='auto',
        choices=['auto', 'single', 'multi'],
        help='运行模式（auto=自动选择）')

    # ---------- 网络 ----------
    ap.add_argument('--host', default=None,
                    help='监听地址')
    ap.add_argument('--port', type=int, default=None,
                    help='监听端口')

    # ---------- Worker ----------
    ap.add_argument('--workers', type=int, default=None,
                    help='Worker 进程数（0=CPU核数）')

    # ---------- 路径 ----------
    ap.add_argument('--config', default='config.json',
                    help='配置文件路径')
    ap.add_argument('--data-dir', default=None,
                    help='数据目录')
    ap.add_argument('--log-dir', default=None,
                    help='日志目录')

    # ---------- 日志 ----------
    ap.add_argument('--log-level', default=None,
                    choices=['DEBUG', 'INFO', 'WARN',
                             'WARNING', 'ERROR'],
                    help='日志级别')

    # ---------- 校验 ----------
    ap.add_argument('--check', action='store_true',
                    help='仅校验配置后退出')

    # ---------- 版本 ----------
    ap.add_argument('--version', action='store_true',
                    help='显示版本后退出')

    return ap


# ==================== 主流程 ====================

def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    # ---------- 版本 ----------
    if args.version:
        from lightdb import __version__
        print(f"LightDB {__version__}")
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

    # 规范化
    cfg._normalize()

    # ---------- 校验 ----------
    errors = cfg.validate()
    if errors:
        print("❌ 配置错误:", file=sys.stderr)
        for e in errors:
            print(f"   - {e}", file=sys.stderr)
        return 1

    if args.check:
        print("✅ 配置校验通过")
        print(cfg.describe())
        return 0

    # ---------- 决定模式 ----------
    mode = args.mode
    if mode == 'auto':
        cpu = os.cpu_count() or 1
        mode = 'multi' if cpu > 1 else 'single'

    # ---------- 多进程模式 ----------
    if mode == 'multi':
        return _run_multi(cfg)

    # ---------- 单进程模式 ----------
    return _run_single(cfg)


# ==================== 单进程 ====================

def _run_single(cfg) -> int:
    """单进程模式（v3 兼容）"""
    from lightdb.logger import setup_logger
    from lightdb.db.database import Database
    from lightdb.sql.executor import Executor
    from lightdb.net.server import MySQLServer

    # ---------- Logger ----------
    log = setup_logger(
        level=cfg.log_level,
        log_dir=cfg.effective_log_dir)

    log.info("=" * 60)
    log.info("  LightDB 单进程模式")
    log.info("=" * 60)
    log.info(f"  版本:   {_get_version()}")
    log.info(f"  监听:   {cfg.listen_addr}")
    log.info(f"  数据:   {cfg.data_dir}")
    log.info(f"  日志:   {cfg.log_dir or '(stdout)'}")
    log.info("=" * 60)

    # ---------- 数据库 ----------
    try:
        db = Database(cfg.effective_data_dir)
        log.info(f"  已加载 {len(db.tables)} 张表")
    except Exception as e:
        log.exception(f"初始化 Database 失败")
        return 1

    # ---------- 执行器 ----------
    try:
        ex = Executor(db)
    except Exception as e:
        log.exception(f"初始化 Executor 失败")
        db.shutdown()
        return 1

    # ---------- 事件调度器（可选） ----------
    scheduler_started = False
    if hasattr(ex, 'start_event_scheduler'):
        try:
            ex.start_event_scheduler()
            scheduler_started = True
            log.info("  事件调度器已启动")
        except Exception as e:
            log.warning(f"事件调度器启动失败: {e}")

    # ---------- 服务器 ----------
    try:
        server = MySQLServer(cfg, db, ex)
    except Exception as e:
        log.exception("初始化 MySQLServer 失败")
        _stop_scheduler(ex, scheduler_started)
        db.shutdown()
        return 1

    _install_signal_handlers()
    log.info("  服务启动中...")
    log.info("=" * 60)

    exit_code = 0
    try:
        server.start()
    except KeyboardInterrupt:
        log.info("收到中断信号，正在关闭...")
    except Exception as e:
        log.exception("服务器异常")
        exit_code = 1
    finally:
        log.info("正在关闭服务...")
        _stop_scheduler(ex, scheduler_started)
        try:
            db.shutdown()
        except Exception:
            pass
        log.info("已退出")
        log.close()

    return exit_code


# ==================== 多进程 ====================

def _run_multi(cfg) -> int:
    """多进程模式（v4）"""
    # 交给 main_v4 处理
    try:
        from lightdb.main_v4 import run_multi
    except ImportError as e:
        print(f"❌ 无法加载多进程模式: {e}", file=sys.stderr)
        print("   请检查 lightdb/main_v4.py 是否存在", file=sys.stderr)
        return 1

    return run_multi(cfg)


# ==================== 工具 ====================

def _stop_scheduler(executor, started: bool):
    if not started:
        return
    if hasattr(executor, 'stop_event_scheduler'):
        try:
            executor.stop_event_scheduler()
        except Exception:
            pass


def _get_version() -> str:
    try:
        from lightdb import __version__
        return __version__
    except Exception:
        return "unknown"


# ==================== 入口 ====================

if __name__ == '__main__':
    sys.exit(main())