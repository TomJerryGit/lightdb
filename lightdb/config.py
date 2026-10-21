"""
LightDB 配置管理（修复版）

修复:
- 所有 dataclass 字段移到方法之前
- ip_whitelist 使用 field(default_factory=list)
- 支持环境变量覆盖
- 支持默认值 + 文件合并
- 加 validate()
- 加派生属性

用法:
    from lightdb.config import Config

    cfg = Config.load('config.json')     # 从文件加载
    cfg = Config.load()                   # 全部默认
    cfg = Config.from_env()               # 从环境变量
    cfg.save('config.json')
"""
import json
import os
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Any, Dict, List, Optional


# ==================== 环境变量映射 ====================
# LIGHTDB_HOST -> host
# LIGHTDB_PORT -> port
# ...

_ENV_MAP = {
    'LIGHTDB_HOST': ('host', str),
    'LIGHTDB_PORT': ('port', int),
    'LIGHTDB_MAX_CONNECTIONS': ('max_connections', int),
    'LIGHTDB_BUFFER_POOL_SIZE': ('buffer_pool_size', int),
    'LIGHTDB_PAGE_SIZE': ('page_size', int),
    'LIGHTDB_DATA_DIR': ('data_dir', str),
    'LIGHTDB_BACKUP_DIR': ('backup_dir', str),
    'LIGHTDB_WORKERS': ('workers', int),
    'LIGHTDB_PARALLEL_WORKERS': ('parallel_workers', int),
    'LIGHTDB_LOG_LEVEL': ('log_level', str),
    'LIGHTDB_LOG_DIR': ('log_dir', str),
    'LIGHTDB_SLOW_QUERY_THRESHOLD': ('slow_query_threshold', float),
    'LIGHTDB_MAX_ALLOWED_PACKET': ('max_allowed_packet', int),
    'LIGHTDB_SSL_ENABLED': ('ssl_enabled', bool),
    'LIGHTDB_SSL_CERT': ('ssl_cert', str),
    'LIGHTDB_SSL_KEY': ('ssl_key', str),
    'LIGHTDB_AUDIT_LEVEL': ('audit_level', str),
    'LIGHTDB_MAX_CONN_PER_IP': ('max_conn_per_ip', int),
    'LIGHTDB_IP_WHITELIST_ENABLED': ('ip_whitelist_enabled', bool),
    # root 初始口令：环境变量优先于「首次启动自动生成随机口令」。
    'LIGHTDB_ROOT_PASSWORD': ('root_password', str),
    # 显式允许 root 空口令（仅限本地开发/演示，默认关闭）。
    'LIGHTDB_ALLOW_EMPTY_PASSWORD': ('allow_empty_password', bool),
    'LIGHTDB_PASSWORD_MIN_LENGTH': ('password_min_length', int),
    'LIGHTDB_PASSWORD_REQUIRE_DIGIT': ('password_require_digit', bool),
    'LIGHTDB_PASSWORD_DENY_WEAK': ('password_deny_weak', bool),
    'LIGHTDB_DEFAULT_ISOLATION': ('default_isolation', str),
    'LIGHTDB_WAL_ENABLED': ('wal_enabled', bool),
    'LIGHTDB_AUTOCOMMIT': ('autocommit', bool),
    'LIGHTDB_SHM_DIR': ('shm_dir', str),
    # OLAP / 列存显式接线（决策 2）
    'LIGHTDB_OLAP_DISPATCH': ('olap_dispatch', bool),
    'LIGHTDB_OLAP_DISPATCH_MIN_ROWS': ('olap_dispatch_min_rows', int),
    'LIGHTDB_OLAP_CROSSCHECK': ('olap_crosscheck', bool),
    'LIGHTDB_OLAP_PARALLEL_WORKERS': ('olap_parallel_workers', int),
    # 真 · append-only binlog（决策 D 项）—— Database 直接读环境变量；
    # 这里登记是为了让配置文件/环境变量两条入口都能解释同一批开关。
    'LIGHTDB_BINLOG_ENABLED': ('binlog_enabled', bool),
    'LIGHTDB_BINLOG_FSYNC': ('binlog_fsync', bool),
    'LIGHTDB_BINLOG_MAX_BYTES': ('binlog_max_bytes', int),
    # 阶段 2：binlog 写失败是否拒绝提交（默认 True；=0 退化为告警）
    'LIGHTDB_BINLOG_STRICT': ('binlog_strict', bool),
}


# ==================== Config ====================

@dataclass
class Config:
    # ---------- 网络 ----------
    # 安全默认：只监听回环地址（旧默认为 0.0.0.0 —— 默认部署会把数据库
    # 直接暴露到网络上）。需要对外提供服务时必须显式指定：
    #   --host 0.0.0.0 / LIGHTDB_HOST=0.0.0.0 / config.json 的 "host"。
    host: str = "127.0.0.1"
    port: int = 8888
    max_connections: int = 100

    # ---------- 存储 ----------
    buffer_pool_size: int = 1000
    page_size: int = 4096
    data_dir: str = "data"
    backup_dir: str = "backups"

    # ---------- 事务 ----------
    default_isolation: str = "READ COMMITTED"
    autocommit: bool = True
    wal_enabled: bool = True

    # ---------- 性能 ----------
    slow_query_threshold: float = 0.5
    max_allowed_packet: int = 16777216
    parallel_workers: int = 4
    workers: int = 0                     # 0 = 自动（CPU 核数）

    # ---------- OLAP / 列存接线（决策 2，4A.2/4A.3） ----------
    #: 是否允许 SQL 执行器把「单表白名单聚合/分组查询」派发给向量化引擎。
    #: **默认 True**（2026-10-20 按用户要求默认开启）；不满足白名单/行数阈值
    #: 时一律返回 None 走行存，绝不静默改道。可用 `LIGHTDB_OLAP_DISPATCH=0`
    #: 或 `Config(olap_dispatch=False)` 显式关闭（关闭时不会 import 任何
    #: OLAP/列存引擎模块）。派发规则见
    #: `lightdb/olap/sql_dispatch.py::try_vectorized_select`。
    olap_dispatch: bool = True
    #: 派发的最小表行数（低于该值不派发：小表上向量化的固定开销大于收益，
    #: 默认 1000 保证「小表行为不变」）。env `LIGHTDB_OLAP_DISPATCH_MIN_ROWS`。
    olap_dispatch_min_rows: int = 1000
    #: 是否同时跑行存/向量化并逐行比对（对拍/排障用，默认 False）。
    #: 不一致时显式报错（RuntimeError），绝不静默返回其中一份结果。
    #: env `LIGHTDB_OLAP_CROSSCHECK=1`。
    olap_crosscheck: bool = False
    #: 向量化聚合/分组的并行线程数。0 = 自动；当前自动档取
    #: `lightdb/olap/vectorized_engine.AUTO_PARALLEL_WORKERS`（实测加速比
    #: 不足 1.2x 时为 1，即默认关闭并行；显式设 2/4 可强制开启）。
    #: env `LIGHTDB_OLAP_PARALLEL_WORKERS`。
    olap_parallel_workers: int = 0

    # ---------- 日志 ----------
    log_level: str = "INFO"
    log_dir: str = ""                    # 空 = 只输出到 stdout

    # ---------- 安全 ----------
    #: 是否启用服务端 TLS（MySQL SSLRequest 协商；见 net/server.py）。
    ssl_enabled: bool = False
    #: 服务端证书/私钥路径。为空且 ssl_enabled=True 时：
    #: 先用 `<data_dir>/ssl/` 下已有证书，否则尝试用 openssl CLI 生成自签证书
    #: （SSLManager）；两者都不可用 → 启动时显式报错（绝不静默降级为明文）。
    ssl_cert: str = ""
    ssl_key: str = ""
    audit_level: str = "DDL"
    # 决策 D 阶段 2：半同步复制**不实现**（死代码已移除，不再有该配置项）。

    # root 初始口令（空 = 未显式指定）。
    # 优先级：LIGHTDB_ROOT_PASSWORD / --root-password > 首次启动随机生成。
    # 明文只在内存/生成的 root.password 文件里，**绝不**打印到日志。
    root_password: str = ""
    # 显式允许 root 空口令（旧的不安全行为）。默认 False：
    # 新库会生成随机口令、旧空口令库会自动迁移出随机口令。
    allow_empty_password: bool = False

    # ---------- 连接限制 ----------
    max_conn_per_ip: int = 20
    ip_whitelist_enabled: bool = False
    ip_whitelist: List[str] = field(default_factory=list)

    # ---------- 密码策略 ----------
    password_min_length: int = 6
    password_require_digit: bool = False
    password_deny_weak: bool = True

    # ---------- 多进程 ----------
    shm_dir: str = ""                    # 空 = 默认 /dev/shm

    # ---------- 真 · append-only binlog（决策 D 项） ----------
    #: 是否启用独立 append-only binlog（`<data_dir>/binlog/binlog.000001`）。
    #: 关闭时 `SHOW MASTER STATUS` 返回空结果集（MySQL log_bin=OFF 同语义），
    #: 从库复制器拉不到事件；默认开启。
    binlog_enabled: bool = True
    #: 组提交是否 fsync（默认关闭，与引擎 autocommit 持久性档位一致）
    binlog_fsync: bool = False
    #: 单文件轮转阈值（默认 32MB）：超过后切换到 binlog.000002、000003…，
    #: 旧文件只读保留；`__BINLOG_FETCH__.lsn` 用跨文件全局字节偏移保证
    #: 轮转后从库位点仍单调可续传。`LIGHTDB_BINLOG_MAX_BYTES` 可配。
    binlog_max_bytes: int = 32 * 1024 * 1024
    #: 阶段 2：binlog 写失败（write/flush/fsync/轮转建文件）是否**拒绝提交**。
    #: True（默认）：先组提交 binlog，失败回滚事务并向客户端报 1180；
    #: False：只告警（排障用；复制位点可能缺事件）。
    binlog_strict: bool = True

    # ==================== 加载 ====================

    @classmethod
    def load(cls, path: str = "config.json",
             env_override: bool = True) -> "Config":
        """
        从 JSON 文件加载，合并默认值。
        可选：用环境变量覆盖。
        """
        cfg = cls()

        # 1. 从文件加载
        if path and os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                cfg._apply_dict(data)
            except Exception as e:
                print(f"[Config] 加载 {path} 失败: {e}")

        # 2. 环境变量覆盖
        if env_override:
            cfg._apply_env()

        # 3. 派生路径规范化
        cfg._normalize()

        return cfg

    @classmethod
    def from_env(cls) -> "Config":
        """仅从环境变量加载"""
        cfg = cls()
        cfg._apply_env()
        cfg._normalize()
        return cfg

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        """从字典加载"""
        cfg = cls()
        cfg._apply_dict(data)
        cfg._normalize()
        return cfg

    # ==================== 保存 ====================

    def save(self, path: str = "config.json",
             indent: int = 2):
        """保存到 JSON 文件"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)

        tmp = p.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(asdict(self), f,
                      indent=indent, ensure_ascii=False)
        tmp.replace(p)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # ==================== 内部 ====================

    def _apply_dict(self, data: Dict[str, Any]):
        """应用字典，忽略未知字段"""
        valid = {f.name for f in fields(self)}
        for k, v in data.items():
            if k in valid:
                try:
                    setattr(self, k, v)
                except Exception:
                    pass

    def _apply_env(self):
        """应用环境变量"""
        for env_key, (attr, caster) in _ENV_MAP.items():
            val = os.environ.get(env_key)
            if val is None:
                continue
            try:
                if caster is bool:
                    parsed = val.lower() in ('1', 'true', 'yes', 'on')
                else:
                    parsed = caster(val)
                setattr(self, attr, parsed)
            except Exception:
                pass

    def _normalize(self):
        """规范化路径"""
        # 数据目录：相对路径转为绝对
        if self.data_dir:
            self.data_dir = os.path.abspath(self.data_dir)
        if self.backup_dir:
            self.backup_dir = os.path.abspath(self.backup_dir)
        if self.log_dir:
            self.log_dir = os.path.abspath(self.log_dir)
        if self.shm_dir:
            self.shm_dir = os.path.abspath(self.shm_dir)

        # Worker 数：0 → CPU 核数
        if self.workers <= 0:
            self.workers = os.cpu_count() or 4

        # 并行 Worker
        if self.parallel_workers <= 0:
            self.parallel_workers = min(8, os.cpu_count() or 4)

    # ==================== 校验 ====================

    def validate(self) -> List[str]:
        """
        校验配置，返回错误列表。
        空列表表示配置有效。
        """
        errors = []

        if not (1 <= self.port <= 65535):
            errors.append(f"port 越界: {self.port}")

        if self.max_connections < 1:
            errors.append(
                f"max_connections 必须 >= 1: {self.max_connections}")

        if self.buffer_pool_size < 16:
            errors.append(
                f"buffer_pool_size 过小: {self.buffer_pool_size}")

        if self.page_size not in (4096, 8192, 16384):
            errors.append(
                f"page_size 只支持 4096/8192/16384: {self.page_size}")

        if self.workers < 0:
            errors.append(f"workers 不能为负: {self.workers}")

        if self.workers > (os.cpu_count() or 4) * 4:
            errors.append(
                f"workers 过大（>4x CPU）: {self.workers}")

        if self.parallel_workers < 1:
            errors.append(
                f"parallel_workers 必须 >= 1: {self.parallel_workers}")

        # ---------- OLAP 接线 ----------
        if self.olap_dispatch_min_rows < 0:
            errors.append(
                f"olap_dispatch_min_rows 不能为负: "
                f"{self.olap_dispatch_min_rows}")
        if self.olap_parallel_workers < 0:
            errors.append(
                f"olap_parallel_workers 不能为负: "
                f"{self.olap_parallel_workers}")
        if self.olap_parallel_workers > max(1, os.cpu_count() or 4) * 4:
            errors.append(
                f"olap_parallel_workers 过大（>4x CPU）: "
                f"{self.olap_parallel_workers}")

        valid_iso = ('READ COMMITTED', 'READ UNCOMMITTED',
                     'REPEATABLE READ', 'SERIALIZABLE')
        if self.default_isolation.upper() not in valid_iso:
            errors.append(
                f"default_isolation 无效: {self.default_isolation}")

        valid_levels = ('DEBUG', 'INFO', 'WARN', 'WARNING',
                        'ERROR', 'CRITICAL')
        if self.log_level.upper() not in valid_levels:
            errors.append(f"log_level 无效: {self.log_level}")

        if self.password_min_length < 1:
            errors.append(
                f"password_min_length 必须 >= 1: "
                f"{self.password_min_length}")

        if self.slow_query_threshold < 0:
            errors.append(
                f"slow_query_threshold 不能为负: "
                f"{self.slow_query_threshold}")

        # ---------- 认证 ----------
        # 两者同时出现是矛盾的：显式口令优先，allow_empty_password 会被忽略，
        # 容易造成"以为放宽了其实没有/以为有口令其实是空的"的误解，直接拒绝。
        if self.allow_empty_password and self.root_password:
            errors.append(
                "root_password 与 allow_empty_password 冲突："
                "请只保留一个（显式口令优先，空口令是显式降级）")
        if not self.host:
            errors.append("host 不能为空")

        return errors

    def validate_or_raise(self):
        errors = self.validate()
        if errors:
            raise ValueError(
                "配置错误:\n  - " + "\n  - ".join(errors))

    # ==================== 派生属性 ====================

    @property
    def effective_data_dir(self) -> str:
        """数据目录（确保存在）"""
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    @property
    def effective_backup_dir(self) -> str:
        p = Path(self.backup_dir)
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    @property
    def effective_log_dir(self) -> Optional[str]:
        if not self.log_dir:
            return None
        p = Path(self.log_dir)
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    @property
    def effective_shm_dir(self) -> Optional[str]:
        if self.shm_dir:
            return self.shm_dir
        if os.path.isdir('/dev/shm'):
            return '/dev/shm'
        return None

    @property
    def is_multi_process(self) -> bool:
        return self.workers > 1

    @property
    def listen_addr(self) -> str:
        return f"{self.host}:{self.port}"

    # ==================== 便捷方法 ====================

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def set(self, key: str, value):
        if hasattr(self, key):
            setattr(self, key, value)

    def update(self, **kwargs):
        """批量更新"""
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
        self._normalize()

    def describe(self) -> str:
        """描述配置（用于启动横幅）"""
        lines = [
            "=" * 60,
            "  LightDB 配置",
            "=" * 60,
            f"  监听地址:       {self.listen_addr}",
            f"  数据目录:       {self.data_dir}",
            f"  备份目录:       {self.backup_dir}",
            f"  日志目录:       {self.log_dir or '(stdout)'}",
            f"  日志级别:       {self.log_level}",
            f"  最大连接数:     {self.max_connections}",
            f"  缓冲池大小:     {self.buffer_pool_size} 页",
            f"  页大小:         {self.page_size} 字节",
            f"  Worker 数:      {self.workers}",
            f"  并行 Worker:    {self.parallel_workers}",
            f"  慢查询阈值:     {self.slow_query_threshold}s",
            f"  默认隔离级别:   {self.default_isolation}",
            f"  SSL:            {self.ssl_enabled}",
            f"  审计级别:       {self.audit_level}",
            f"  root 认证:      {self._root_auth_desc()}",
            f"  允许空口令:     "
            f"{'是（显式降级，仅限本地开发/演示）' if self.allow_empty_password else '否'}",
            f"  连接限制:       总量 {self.max_connections} / 每 IP {self.max_conn_per_ip}",
            f"  IP 白名单:      "
            f"{('启用 ' + str(len(self.ip_whitelist or [])) + ' 条规则') if self.ip_whitelist_enabled else '未启用'}",
            "=" * 60,
        ]
        return '\n'.join(lines)

    def _root_auth_desc(self) -> str:
        """root 初始认证方式（只描述来源，绝不回显口令明文）"""
        if self.root_password:
            return "环境变量/命令行显式指定（不打印明文）"
        if self.allow_empty_password:
            return "显式允许空口令（不安全）"
        return "首次启动自动生成随机口令（写入 data_dir/root.password）"

    def __repr__(self) -> str:
        return f"Config(host={self.host}, port={self.port}, " \
               f"workers={self.workers}, " \
               f"buffer_pool_size={self.buffer_pool_size})"


# ==================== 便捷函数 ====================

def load_config(path: str = "config.json",
                env_override: bool = True) -> Config:
    """加载配置（含校验）"""
    cfg = Config.load(path, env_override=env_override)
    errors = cfg.validate()
    if errors:
        import warnings
        for e in errors:
            warnings.warn(f"[Config] {e}")
    return cfg