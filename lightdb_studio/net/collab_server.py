"""
实时协作服务（WebSocket）
- 多人同时编辑 SQL
- 光标位置同步
- 操作广播
"""
import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass, asdict, field
from typing import Dict, Set, List

try:
    from websockets.server import serve
    from websockets.exceptions import ConnectionClosed
    HAS_WS = True
except ImportError:
    HAS_WS = False


@dataclass
class CollabUser:
    user_id: str
    username: str
    color: str
    cursor_pos: int = 0
    last_seen: float = 0
    joined_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.joined_at:
            self.joined_at = now
        if not self.last_seen:
            self.last_seen = now


@dataclass
class CollabRoom:
    room_id: str
    name: str = ""
    content: str = ""       # 当前 SQL 内容
    version: int = 0
    users: Dict[str, CollabUser] = field(default_factory=dict)
    created_at: float = 0
    last_activity: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.last_activity:
            self.last_activity = now


class CollabServer:
    """协作服务器"""

    USER_COLORS = [
        '#4ec9b0', '#569cd6', '#dcdcaa', '#ce9178', '#c586c0',
        '#9cdcfe', '#f48771', '#89d185', '#b5cea8', '#d16969',
    ]

    def __init__(self, host: str = '0.0.0.0', port: int = 8765):
        self.host = host
        self.port = port
        self.rooms: Dict[str, CollabRoom] = {}
        self.clients: Dict[str, 'websockets'] = {}  # client_id -> ws
        self.client_rooms: Dict[str, str] = {}      # client_id -> room_id
        self._lock = threading.RLock()
        self._loop = None
        self._server = None
        self._running = False
        self._thread = None

    # ==================== 启动/停止 ====================

    def start(self):
        if not HAS_WS:
            print("[Collab] 需要 websockets 库: pip install websockets")
            return False
        if self._running:
            return True

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name='CollabServer')
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        if self._server:
            asyncio.run_coroutine_threadsafe(
                self._server.close(), self._loop)
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def _main():
            self._server = await serve(
                self._handle_client, self.host, self.port)
            print(f"[Collab] WebSocket 服务启动: "
                  f"ws://{self.host}:{self.port}")
            await self._server.wait_closed()

        try:
            loop.run_until_complete(_main())
        except Exception as e:
            print(f"[Collab] 服务异常: {e}")
        finally:
            loop.close()

    # ==================== 房间管理 ====================

    def create_room(self, name: str = "") -> CollabRoom:
        room_id = str(uuid.uuid4())[:8]
        room = CollabRoom(room_id=room_id, name=name or f"房间-{room_id}")
        with self._lock:
            self.rooms[room_id] = room
        return room

    def get_room(self, room_id: str) -> CollabRoom:
        return self.rooms.get(room_id)

    def close_room(self, room_id: str):
        with self._lock:
            if room_id in self.rooms:
                del self.rooms[room_id]

    def list_rooms(self) -> List[Dict]:
        return [
            {
                'room_id': r.room_id,
                'name': r.name,
                'users': len(r.users),
                'created_at': r.created_at,
                'last_activity': r.last_activity,
            }
            for r in self.rooms.values()
        ]

    # ==================== 客户端处理 ====================

    async def _handle_client(self, ws):
        """处理单个 WebSocket 连接"""
        client_id = str(uuid.uuid4())[:8]
        username = f"用户_{client_id[:4]}"

        try:
            async for raw_msg in ws:
                try:
                    msg = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get('type', '')

                # 加入房间
                if msg_type == 'join':
                    room_id = msg.get('room_id', '')
                    if not room_id:
                        # 自动创建
                        room = self.create_room(msg.get('name', ''))
                        room_id = room.room_id
                    elif room_id not in self.rooms:
                        await self._send(ws, {
                            'type': 'error',
                            'message': '房间不存在',
                        })
                        continue

                    # 分配颜色
                    room = self.rooms[room_id]
                    color = self.USER_COLORS[
                        len(room.users) % len(self.USER_COLORS)]

                    user = CollabUser(
                        user_id=client_id,
                        username=msg.get('username', username),
                        color=color,
                    )
                    room.users[client_id] = user
                    self.clients[client_id] = ws
                    self.client_rooms[client_id] = room_id

                    # 发送当前状态
                    await self._send(ws, {
                        'type': 'joined',
                        'room_id': room_id,
                        'client_id': client_id,
                        'color': color,
                        'content': room.content,
                        'version': room.version,
                        'users': [asdict(u) for u in room.users.values()],
                    })

                    # 广播用户加入
                    await self._broadcast(room_id, {
                        'type': 'user_joined',
                        'user': asdict(user),
                    }, exclude=client_id)

                # 编辑操作
                elif msg_type == 'edit':
                    room_id = self.client_rooms.get(client_id)
                    if not room_id:
                        continue
                    room = self.rooms.get(room_id)
                    if not room:
                        continue

                    # 全量替换（简化实现）
                    room.content = msg.get('content', room.content)
                    room.version += 1
                    room.last_activity = time.time()

                    # 广播给其他人
                    await self._broadcast(room_id, {
                        'type': 'edit',
                        'from': client_id,
                        'content': room.content,
                        'version': room.version,
                    }, exclude=client_id)

                # 光标位置
                elif msg_type == 'cursor':
                    room_id = self.client_rooms.get(client_id)
                    if not room_id:
                        continue
                    room = self.rooms.get(room_id)
                    if not room:
                        continue
                    user = room.users.get(client_id)
                    if user:
                        user.cursor_pos = msg.get('pos', 0)
                        user.last_seen = time.time()

                    await self._broadcast(room_id, {
                        'type': 'cursor',
                        'from': client_id,
                        'username': user.username if user else '',
                        'pos': user.cursor_pos if user else 0,
                        'color': user.color if user else '#ffffff',
                    }, exclude=client_id)

                # 聊天
                elif msg_type == 'chat':
                    room_id = self.client_rooms.get(client_id)
                    if not room_id:
                        continue
                    room = self.rooms.get(room_id)
                    user = room.users.get(client_id) if room else None
                    if user:
                        await self._broadcast(room_id, {
                            'type': 'chat',
                            'username': user.username,
                            'color': user.color,
                            'text': msg.get('text', '')[:500],
                            'ts': time.time(),
                        })

                # 心跳
                elif msg_type == 'ping':
                    room_id = self.client_rooms.get(client_id)
                    if room_id:
                        room = self.rooms.get(room_id)
                        if room and client_id in room.users:
                            room.users[client_id].last_seen = time.time()
                    await self._send(ws, {'type': 'pong'})

        except ConnectionClosed:
            pass
        except Exception as e:
            print(f"[Collab] 客户端异常: {e}")
        finally:
            # 清理
            self.clients.pop(client_id, None)
            room_id = self.client_rooms.pop(client_id, None)
            if room_id:
                room = self.rooms.get(room_id)
                if room:
                    user = room.users.pop(client_id, None)
                    if user:
                        await self._broadcast(room_id, {
                            'type': 'user_left',
                            'user_id': client_id,
                            'username': user.username,
                        })

    async def _send(self, ws, msg: Dict):
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    async def _broadcast(self, room_id: str, msg: Dict,
                         exclude: str = None):
        room = self.rooms.get(room_id)
        if not room:
            return
        data = json.dumps(msg, ensure_ascii=False)
        for uid, ws in list(self.clients.items()):
            if uid == exclude:
                continue
            if self.client_rooms.get(uid) != room_id:
                continue
            try:
                await ws.send(data)
            except Exception:
                pass

    # ==================== 同步 API（给非 asyncio 环境） ====================

    def broadcast_sync(self, room_id: str, msg: Dict):
        """线程安全广播"""
        if not self._loop or not self._running:
            return
        asyncio.run_coroutine_threadsafe(
            self._broadcast(room_id, msg), self._loop)


class CollabClient:
    """协作客户端（用于桌面版接入）"""

    def __init__(self, url: str = 'ws://localhost:8765'):
        self.url = url
        self.ws = None
        self.client_id = ""
        self.room_id = ""
        self.color = "#ffffff"
        self._listeners = []
        self._thread = None
        self._running = False
        self._loop = None

    def connect(self, room_id: str = '', username: str = '',
                on_event=None):
        if not HAS_WS:
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._run, args=(room_id, username, on_event),
            daemon=True)
        self._thread.start()
        return True

    def _run(self, room_id, username, on_event):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def _client():
            try:
                async with connect(self.url) as ws:
                    self.ws = ws
                    # 加入房间
                    await ws.send(json.dumps({
                        'type': 'join',
                        'room_id': room_id,
                        'username': username,
                    }))

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if msg.get('type') == 'joined':
                            self.client_id = msg.get('client_id', '')
                            self.room_id = msg.get('room_id', '')
                            self.color = msg.get('color', '#ffffff')

                        if on_event:
                            on_event(msg)
            except Exception as e:
                print(f"[CollabClient] 连接失败: {e}")

        try:
            loop.run_until_complete(_client())
        finally:
            loop.close()

    def send(self, msg: Dict):
        if not self.ws or not self._loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.ws.send(json.dumps(msg, ensure_ascii=False)),
                self._loop)
        except Exception:
            pass

    def send_edit(self, content: str):
        self.send({'type': 'edit', 'content': content})

    def send_cursor(self, pos: int):
        self.send({'type': 'cursor', 'pos': pos})

    def send_chat(self, text: str):
        self.send({'type': 'chat', 'text': text})

    def disconnect(self):
        self._running = False
        if self.ws and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.ws.close(), self._loop)
            except Exception:
                pass


try:
    from websockets.client import connect
except ImportError:
    connect = None