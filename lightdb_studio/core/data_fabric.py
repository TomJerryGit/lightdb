"""
数据编织（Data Fabric）
- 跨组织数据交换
- 零信任架构
- 数据主权
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Dict, Optional, Callable


FABRIC_FILE = Path.home() / '.lightdb_studio' / 'fabric.json'


@dataclass
class FabricPeer:
    """编织节点（跨组织）"""
    peer_id: str = ""
    name: str = ""
    organization: str = ""
    # 认证
    public_key: str = ""
    secret_hash: str = ""
    # 权限
    allowed_products: List[str] = field(default_factory=list)
    # 主权声明
    data_sovereignty: str = ""          # 数据留在本组织
    pii_handling: str = "reject"         # reject / masked / encrypted
    # 状态
    status: str = "pending"              # pending / active / revoked
    created_at: float = 0
    last_used: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.peer_id:
            self.peer_id = f"peer_{uuid.uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = now


@dataclass
class DataExchange:
    """数据交换契约"""
    exchange_id: str = ""
    producer_peer: str = ""
    consumer_peer: str = ""
    product_id: str = ""
    # 交换方式
    mode: str = "pull"                   # push / pull / stream
    # 数据主权
    data_residency: str = "producer"     # producer / consumer / any
    # 加密
    encrypted: bool = True
    encryption_key: str = ""
    # 时间窗口
    start_time: float = 0
    end_time: float = 0
    # 状态
    status: str = "active"               # active / expired / revoked
    # 使用统计
    transferred_rows: int = 0
    last_transfer: float = 0
    created_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.exchange_id:
            self.exchange_id = f"ex_{uuid.uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = now
        if not self.encryption_key:
            self.encryption_key = secrets.token_urlsafe(32)


@dataclass
class AccessRequest:
    """跨组织访问请求"""
    request_id: str = ""
    peer_id: str = ""
    resource: str = ""
    action: str = ""
    purpose: str = ""                    # 用途声明
    # 审批
    status: str = "pending"              # pending / approved / denied
    approved_by: str = ""
    reason: str = ""
    # 时间
    requested_at: float = 0
    decided_at: float = 0
    expires_at: float = 0

    def __post_init__(self):
        if not self.request_id:
            self.request_id = f"req_{uuid.uuid4().hex[:8]}"
        if not self.requested_at:
            self.requested_at = time.time()


class DataFabric:
    """数据编织引擎"""

    def __init__(self):
        self.file = FABRIC_FILE
        self.peers: Dict[str, FabricPeer] = {}
        self.exchanges: Dict[str, DataExchange] = {}
        self.requests: Dict[str, AccessRequest] = {}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for pid, pd in data.get('peers', {}).items():
                self.peers[pid] = FabricPeer(**pd)
            for eid, ed in data.get('exchanges', {}).items():
                self.exchanges[eid] = DataExchange(**ed)
            for rid, rd in data.get('requests', {}).items():
                self.requests[rid] = AccessRequest(**rd)
        except Exception:
            pass

    def save(self):
        data = {
            'peers': {p: asdict(pe) for p, pe in self.peers.items()},
            'exchanges': {e: asdict(ex) for e, ex in self.exchanges.items()},
            'requests': {r: asdict(rq) for r, rq in self.requests.items()},
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 节点管理 ====================

    def register_peer(self, name: str, organization: str,
                      allowed_products: List[str] = None,
                      data_sovereignty: str = '本地',
                      pii_handling: str = 'masked') -> Dict:
        """注册编织节点"""
        secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(secret.encode()).hexdigest()

        peer = FabricPeer(
            name=name,
            organization=organization,
            public_key=base64.b64encode(
                secrets.token_bytes(32)).decode(),
            secret_hash=secret_hash,
            allowed_products=allowed_products or [],
            data_sovereignty=data_sovereignty,
            pii_handling=pii_handling,
            status='active',
        )
        self.peers[peer.peer_id] = peer
        self.save()

        return {
            'peer_id': peer.peer_id,
            'secret': secret,   # 只返回一次
            'peer': asdict(peer),
        }

    def revoke_peer(self, peer_id: str):
        if peer_id in self.peers:
            self.peers[peer_id].status = 'revoked'
            self.save()

    # ==================== 数据交换 ====================

    def create_exchange(self, producer_peer: str,
                        consumer_peer: str,
                        product_id: str,
                        mode: str = 'pull',
                        data_residency: str = 'producer',
                        ttl_hours: int = 24 * 30) -> DataExchange:
        """创建数据交换契约"""
        ex = DataExchange(
            producer_peer=producer_peer,
            consumer_peer=consumer_peer,
            product_id=product_id,
            mode=mode,
            data_residency=data_residency,
            end_time=time.time() + ttl_hours * 3600,
        )
        self.exchanges[ex.exchange_id] = ex
        self.save()
        return ex

    def revoke_exchange(self, exchange_id: str):
        if exchange_id in self.exchanges:
            self.exchanges[exchange_id].status = 'revoked'
            self.save()

    # ==================== 零信任访问 ====================

    def request_access(self, peer_id: str, resource: str,
                       action: str, purpose: str) -> AccessRequest:
        """发起访问请求"""
        req = AccessRequest(
            peer_id=peer_id,
            resource=resource,
            action=action,
            purpose=purpose,
        )
        self.requests[req.request_id] = req
        self.save()
        return req

    def approve_request(self, request_id: str, approved_by: str,
                        reason: str = '', ttl_hours: int = 24) -> bool:
        req = self.requests.get(request_id)
        if not req:
            return False
        req.status = 'approved'
        req.approved_by = approved_by
        req.reason = reason
        req.decided_at = time.time()
        req.expires_at = time.time() + ttl_hours * 3600
        self.save()
        return True

    def deny_request(self, request_id: str, denied_by: str,
                     reason: str = '') -> bool:
        req = self.requests.get(request_id)
        if not req:
            return False
        req.status = 'denied'
        req.approved_by = denied_by
        req.reason = reason
        req.decided_at = time.time()
        self.save()
        return True

    def check_access(self, peer_id: str, resource: str,
                     action: str) -> Dict:
        """
        零信任访问检查
        返回 {allowed, reason, ...}
        """
        peer = self.peers.get(peer_id)
        if not peer:
            return {'allowed': False, 'reason': '节点未注册'}
        if peer.status != 'active':
            return {'allowed': False, 'reason': '节点已停用'}

        # 检查数据主权
        if peer.data_sovereignty and 'foreign' in peer.data_sovereignty.lower():
            return {'allowed': False, 'reason': '数据主权限制'}

        # 检查是否有 approved 请求
        approved = None
        for req in self.requests.values():
            if (req.peer_id == peer_id and
                    req.resource == resource and
                    req.action == action and
                    req.status == 'approved'):
                if req.expires_at > time.time():
                    approved = req
                    break

        if not approved:
            return {'allowed': False,
                    'reason': '需要事前审批'}

        # 检查产品权限
        if peer.allowed_products:
            resource_short = resource.split('.')[-1]
            if not any(p in peer.allowed_products
                        or p == '*' for p in peer.allowed_products):
                if resource_short not in peer.allowed_products:
                    return {'allowed': False,
                            'reason': '产品不在授权列表'}

        return {
            'allowed': True,
            'peer': peer.name,
            'request_id': approved.request_id,
            'expires_at': approved.expires_at,
        }

    # ==================== 数据加解密 ====================

    def encrypt_payload(self, data: str,
                        exchange_id: str) -> Dict:
        """加密数据（简化）"""
        ex = self.exchanges.get(exchange_id)
        if not ex:
            return {'error': '交换不存在'}

        key = ex.encryption_key.encode()
        payload = data.encode()

        # 简化 XOR（生产用 AES）
        import os
        iv = os.urandom(16)
        key_bytes = hashlib.sha256(key).digest()
        out = bytearray()
        for i, b in enumerate(payload):
            out.append(b ^ key_bytes[i % len(key_bytes)] ^ iv[i % 16])

        return {
            'encrypted': True,
            'iv': base64.b64encode(iv).decode(),
            'ciphertext': base64.b64encode(bytes(out)).decode(),
            'algorithm': 'sha256-xor-demo',
        }

    def decrypt_payload(self, envelope: Dict,
                        exchange_id: str) -> Dict:
        """解密"""
        ex = self.exchanges.get(exchange_id)
        if not ex:
            return {'error': '交换不存在'}

        try:
            iv = base64.b64decode(envelope['iv'])
            ct = base64.b64decode(envelope['ciphertext'])
            key = ex.encryption_key.encode()
            key_bytes = hashlib.sha256(key).digest()

            out = bytearray()
            for i, b in enumerate(ct):
                out.append(b ^ key_bytes[i % len(key_bytes)] ^ iv[i % 16])

            return {'data': out.decode()}
        except Exception as e:
            return {'error': str(e)}

    # ==================== 统计 ====================

    def stats(self) -> dict:
        by_status = {}
        for p in self.peers.values():
            by_status[p.status] = by_status.get(p.status, 0) + 1

        req_by_status = {}
        for r in self.requests.values():
            req_by_status[r.status] = req_by_status.get(r.status, 0) + 1

        return {
            'total_peers': len(self.peers),
            'peers_by_status': by_status,
            'total_exchanges': len(self.exchanges),
            'active_exchanges': sum(
                1 for e in self.exchanges.values()
                if e.status == 'active' and e.end_time > time.time()),
            'total_requests': len(self.requests),
            'requests_by_status': req_by_status,
        }