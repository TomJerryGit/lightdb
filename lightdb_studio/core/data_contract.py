"""
数据契约
- Schema 契约定义
- 版本管理
- 变更检测
- 消费者通知
"""
import json
import time
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional


CONTRACT_FILE = Path.home() / '.lightdb_studio' / 'contracts.json'


@dataclass
class ContractColumn:
    name: str
    type: str = "VARCHAR"
    nullable: bool = True
    required: bool = False
    comment: str = ""
    # 校验规则
    pattern: str = ""
    min_value: float = None
    max_value: float = None
    allowed_values: List[str] = field(default_factory=list)

    def signature(self) -> tuple:
        return (
            self.name.lower(),
            self.type.upper(),
            self.nullable,
        )


@dataclass
class ContractVersion:
    version: str = "1.0.0"
    columns: List[ContractColumn] = field(default_factory=list)
    created_at: float = 0
    created_by: str = ""
    note: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class DataContract:
    """数据契约"""
    table_key: str = ""          # db.table
    name: str = ""
    description: str = ""
    owner: str = ""
    consumers: List[str] = field(default_factory=list)
    current_version: str = "1.0.0"
    versions: List[ContractVersion] = field(default_factory=list)
    # 状态
    status: str = "active"       # active / deprecated
    # SLA
    quality_sla: Dict = field(default_factory=dict)
    # 变更历史
    change_history: List[Dict] = field(default_factory=list)
    created_at: float = 0
    updated_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def get_version(self, v: str) -> Optional[ContractVersion]:
        for ver in self.versions:
            if ver.version == v:
                return ver
        return None

    def latest_version(self) -> Optional[ContractVersion]:
        return self.get_version(self.current_version)


@dataclass
class ContractViolation:
    """契约违规"""
    table_key: str
    contract_version: str
    violation_type: str        # column_missing / type_changed / etc
    severity: str = "warning"  # critical / warning / info
    detail: str = ""
    detected_at: float = 0

    def __post_init__(self):
        if not self.detected_at:
            self.detected_at = time.time()


class ContractManager:
    """数据契约管理器"""

    def __init__(self, conn_provider=None):
        self.file = CONTRACT_FILE
        self.conn_provider = conn_provider
        self.contracts: Dict[str, DataContract] = {}
        self.violations: List[ContractViolation] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for key, cd in data.get('contracts', {}).items():
                versions = []
                for vd in cd.pop('versions', []):
                    cols = [ContractColumn(**c)
                            for c in vd.pop('columns', [])]
                    ver = ContractVersion(**vd)
                    ver.columns = cols
                    versions.append(ver)
                c = DataContract(**cd)
                c.versions = versions
                self.contracts[key] = c
        except Exception:
            pass

    def save(self):
        data = {
            'contracts': {
                k: {
                    **asdict(c),
                    'versions': [
                        {**asdict(v),
                         'columns': [asdict(col) for col in v.columns]}
                        for v in c.versions
                    ]
                }
                for k, c in self.contracts.items()
            }
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 创建契约 ====================

    def create_contract(self, table_key: str, connection: str = '',
                        table: str = '',
                        name: str = '', owner: str = '',
                        description: str = '') -> DataContract:
        """从表结构创建契约"""
        contract = DataContract(
            table_key=table_key,
            name=name or table_key,
            description=description,
            owner=owner,
        )

        # 读取表结构
        cols = []
        try:
            if connection and table:
                conn = self.conn_provider(connection) \
                    if self.conn_provider else None
                if conn:
                    col_metas = conn.get_columns(table)
                    for c in col_metas:
                        cols.append(ContractColumn(
                            name=c['name'],
                            type=c.get('type', 'VARCHAR'),
                            nullable=(c.get('null', 'YES') == 'YES'),
                            required=(c.get('key') == 'PRI'),
                        ))
        except Exception:
            pass

        version = ContractVersion(
            version='1.0.0',
            columns=cols,
            created_by=owner,
            note='初始契约',
        )
        contract.versions.append(version)
        contract.current_version = '1.0.0'

        self.contracts[table_key] = contract
        self.save()
        return contract

    def update_contract(self, table_key: str, new_version: str,
                        columns: List[ContractColumn],
                        note: str = '', created_by: str = ''):
        """发布新版本"""
        if table_key not in self.contracts:
            return False

        contract = self.contracts[table_key]
        old = contract.latest_version()

        version = ContractVersion(
            version=new_version,
            columns=columns,
            created_by=created_by,
            note=note,
        )
        contract.versions.append(version)
        contract.current_version = new_version
        contract.updated_at = time.time()

        # 记录变更
        if old:
            changes = self._diff_columns(old.columns, columns)
            contract.change_history.append({
                'from': old.version,
                'to': new_version,
                'changes': changes,
                'ts': time.time(),
                'note': note,
            })

        self.save()
        return True

    def _diff_columns(self, old: List[ContractColumn],
                      new: List[ContractColumn]) -> List[Dict]:
        """比较列定义"""
        old_map = {c.name.lower(): c for c in old}
        new_map = {c.name.lower(): c for c in new}
        changes = []

        for name, new_col in new_map.items():
            if name not in old_map:
                changes.append({
                    'type': 'column_added',
                    'column': new_col.name,
                    'detail': f"{new_col.type}",
                    'breaking': False,
                })
            elif old_map[name].signature() != new_col.signature():
                old_col = old_map[name]
                detail_parts = []
                if old_col.type != new_col.type:
                    detail_parts.append(
                        f"type {old_col.type} → {new_col.type}")
                if old_col.nullable != new_col.nullable:
                    detail_parts.append(
                        f"nullable {old_col.nullable} → {new_col.nullable}")
                changes.append({
                    'type': 'column_changed',
                    'column': new_col.name,
                    'detail': '; '.join(detail_parts),
                    'breaking': (old_col.type != new_col.type or
                                  (old_col.nullable and not new_col.nullable)),
                })

        for name, old_col in old_map.items():
            if name not in new_map:
                changes.append({
                    'type': 'column_removed',
                    'column': old_col.name,
                    'detail': f"{old_col.type}",
                    'breaking': True,
                })

        return changes

    # ==================== 校验 ====================

    def check_violations(self, table_key: str,
                         connection: str = '',
                         table: str = '') -> List[ContractViolation]:
        """检查当前表结构是否违反契约"""
        if table_key not in self.contracts:
            return []

        contract = self.contracts[table_key]
        version = contract.latest_version()
        if not version:
            return []

        conn = self.conn_provider(connection) \
            if self.conn_provider else None
        if not conn:
            return []

        violations = []

        try:
            actual_cols = conn.get_columns(table or table_key.split('.')[-1])
        except Exception:
            return []

        actual_map = {c['name'].lower(): c for c in actual_cols}
        expected_map = {c.name.lower(): c for c in version.columns}

        # 检查缺失 / 变更
        for name, exp_col in expected_map.items():
            if name not in actual_map:
                v = ContractViolation(
                    table_key=table_key,
                    contract_version=version.version,
                    violation_type='column_missing',
                    severity='critical' if exp_col.required else 'warning',
                    detail=f"列 {exp_col.name} 缺失",
                )
                violations.append(v)
            else:
                act_col = actual_map[name]
                act_type = (act_col.get('type') or '').upper()
                exp_type = exp_col.type.upper()
                # 类型比较（简化）
                if exp_type.split('(')[0] not in act_type:
                    v = ContractViolation(
                        table_key=table_key,
                        contract_version=version.version,
                        violation_type='type_changed',
                        severity='critical',
                        detail=(f"列 {exp_col.name} 类型变更: "
                                f"{exp_type} → {act_type}"),
                    )
                    violations.append(v)

        # 检查新增列
        for name, act_col in actual_map.items():
            if name not in expected_map:
                v = ContractViolation(
                    table_key=table_key,
                    contract_version=version.version,
                    violation_type='column_added',
                    severity='info',
                    detail=f"新列 {act_col['name']} ({act_col.get('type')})",
                )
                violations.append(v)

        # 记录
        self.violations.extend(violations)
        if len(self.violations) > 1000:
            self.violations = self.violations[-500:]

        return violations

    # ==================== 消费者通知 ====================

    def notify_consumers(self, table_key: str,
                         changes: List[Dict],
                         notification_mgr=None):
        """通知契约的消费者"""
        if table_key not in self.contracts:
            return
        contract = self.contracts[table_key]
        if not contract.consumers:
            return

        breaking = [c for c in changes if c.get('breaking')]
        severity = 'error' if breaking else 'info'

        title = f"数据契约变更: {contract.name}"
        lines = [
            f"表: {table_key}",
            f"新版本: {contract.current_version}",
            f"消费者: {', '.join(contract.consumers)}",
            "",
            "变更内容:",
        ]
        for c in changes:
            icon = '🔴' if c.get('breaking') else '🟡'
            lines.append(f"  {icon} {c['type']}: {c.get('column', '')} "
                          f"({c.get('detail', '')})")

        content = '\n'.join(lines)

        if notification_mgr:
            from .notifications import NotificationMessage
            msg = NotificationMessage(
                title=title,
                content=content,
                level=severity,
                source='contract',
            )
            notification_mgr.notify(msg)

        return {'title': title, 'content': content,
                'consumers': contract.consumers}

    # ==================== 查询 ====================

    def get_contract(self, table_key: str) -> Optional[DataContract]:
        return self.contracts.get(table_key)

    def list_contracts(self) -> List[DataContract]:
        return list(self.contracts.values())

    def get_consumer_contracts(self, consumer: str) -> List[DataContract]:
        return [c for c in self.contracts.values()
                if consumer in c.consumers]

    def stats(self) -> dict:
        total_changes = sum(len(c.change_history)
                             for c in self.contracts.values())
        breaking = sum(
            1 for c in self.contracts.values()
            for h in c.change_history
            for ch in h.get('changes', [])
            if ch.get('breaking'))
        return {
            'total_contracts': len(self.contracts),
            'active': sum(1 for c in self.contracts.values()
                          if c.status == 'active'),
            'total_versions': sum(len(c.versions)
                                   for c in self.contracts.values()),
            'total_changes': total_changes,
            'breaking_changes': breaking,
            'recent_violations': len(self.violations),
        }