"""
通知系统
- 邮件（SMTP）
- Webhook（钉钉/企微/Slack/自定义）
- 桌面通知
- 站内消息
"""
import json
import smtplib
import threading
import time
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List

NOTIFY_FILE = Path.home() / '.lightdb_studio' / 'notifications.json'


@dataclass
class NotificationChannel:
    name: str = ""
    channel_type: str = "webhook"    # email / webhook / desktop
    enabled: bool = True

    # Webhook
    webhook_url: str = ""
    webhook_type: str = "dingtalk"   # dingtalk / wechat / slack / custom
    secret: str = ""

    # Email
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_ssl: bool = True
    email_from: str = ""
    email_to: List[str] = field(default_factory=list)

    # 触发条件
    on_error: bool = True
    on_success: bool = False
    on_schedule_fail: bool = True


@dataclass
class NotificationMessage:
    title: str
    content: str
    level: str = "info"     # info / warning / error
    source: str = ""        # 来源（schedule/review/workflow）
    ts: float = 0

    def __post_init__(self):
        if not self.ts:
            self.ts = time.time()


class NotificationManager:
    def __init__(self):
        self.file = NOTIFY_FILE
        self.channels: List[NotificationChannel] = []
        self.history: List[dict] = []
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for c in data.get('channels', []):
                self.channels.append(NotificationChannel(**c))
            self.history = data.get('history', [])
        except Exception:
            pass

    def save(self):
        data = {
            'channels': [asdict(c) for c in self.channels],
            'history': self.history[-200:],
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 渠道管理 ====================

    def add_channel(self, ch: NotificationChannel):
        self.channels.append(ch)
        self.save()

    def update_channel(self, name: str, ch: NotificationChannel):
        for i, c in enumerate(self.channels):
            if c.name == name:
                self.channels[i] = ch
                self.save()
                return

    def remove_channel(self, name: str):
        self.channels = [c for c in self.channels if c.name != name]
        self.save()

    def get_channel(self, name: str):
        for c in self.channels:
            if c.name == name:
                return c
        return None

    # ==================== 发送 ====================

    def notify(self, msg: NotificationMessage, channels: List[str] = None):
        """异步发送通知"""
        def _run():
            for ch in self.channels:
                if not ch.enabled:
                    continue
                if channels and ch.name not in channels:
                    continue
                # 检查触发条件
                if msg.level == 'error' and not ch.on_error:
                    continue
                if msg.level == 'info' and not ch.on_success:
                    continue

                try:
                    if ch.channel_type == 'webhook':
                        self._send_webhook(ch, msg)
                    elif ch.channel_type == 'email':
                        self._send_email(ch, msg)
                    elif ch.channel_type == 'desktop':
                        self._send_desktop(ch, msg)

                    self.history.append({
                        'channel': ch.name,
                        'title': msg.title,
                        'level': msg.level,
                        'ts': time.time(),
                        'status': 'ok',
                    })
                except Exception as e:
                    self.history.append({
                        'channel': ch.name,
                        'title': msg.title,
                        'level': msg.level,
                        'ts': time.time(),
                        'status': 'failed',
                        'error': str(e),
                    })

            self.save()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def notify_sync(self, msg: NotificationMessage, channels=None):
        """同步发送"""
        for ch in self.channels:
            if not ch.enabled:
                continue
            if channels and ch.name not in channels:
                continue
            try:
                if ch.channel_type == 'webhook':
                    self._send_webhook(ch, msg)
                elif ch.channel_type == 'email':
                    self._send_email(ch, msg)
            except Exception as e:
                print(f"通知失败 [{ch.name}]: {e}")

    # ==================== Webhook ====================

    def _send_webhook(self, ch: NotificationChannel, msg: NotificationMessage):
        if ch.webhook_type == 'dingtalk':
            self._send_dingtalk(ch, msg)
        elif ch.webhook_type == 'wechat':
            self._send_wechat(ch, msg)
        elif ch.webhook_type == 'slack':
            self._send_slack(ch, msg)
        else:
            self._send_custom(ch, msg)

    def _send_dingtalk(self, ch, msg):
        icon = {'info': 'ℹ️', 'warning': '⚠️', 'error': '🚨'}.get(
            msg.level, 'ℹ️')
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": msg.title,
                "text": f"### {icon} {msg.title}\n\n"
                        f"**级别**: {msg.level}\n\n"
                        f"**来源**: {msg.source}\n\n"
                        f"**内容**:\n\n{msg.content}\n\n"
                        f"**时间**: "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.ts))}"
            }
        }
        if ch.secret:
            payload = self._sign_dingtalk(payload, ch.secret)

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            ch.webhook_url, data=data, method='POST',
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)

    def _sign_dingtalk(self, payload, secret):
        import hmac
        import hashlib
        import base64
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}"
        hmac_code = hmac.new(secret.encode(),
                              string_to_sign.encode(),
                              digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(
            base64.b64encode(hmac_code).decode())
        # 需要修改 url
        return payload

    def _send_wechat(self, ch, msg):
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"### {msg.title}\n"
                           f"> 级别: {msg.level}\n"
                           f"> 来源: {msg.source}\n\n"
                           f"{msg.content}"
            }
        }
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            ch.webhook_url, data=data, method='POST',
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)

    def _send_slack(self, ch, msg):
        emoji = {'info': ':information_source:',
                 'warning': ':warning:',
                 'error': ':rotating_light:'}.get(msg.level, ':bell:')
        payload = {
            "text": f"{emoji} *{msg.title}*\n"
                    f"级别: {msg.level}\n"
                    f"来源: {msg.source}\n"
                    f"{msg.content}"
        }
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            ch.webhook_url, data=data, method='POST',
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)

    def _send_custom(self, ch, msg):
        payload = {
            'title': msg.title,
            'content': msg.content,
            'level': msg.level,
            'source': msg.source,
            'ts': msg.ts,
        }
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            ch.webhook_url, data=data, method='POST',
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)

    # ==================== Email ====================

    def _send_email(self, ch: NotificationChannel, msg: NotificationMessage):
        m = MIMEMultipart('alternative')
        m['Subject'] = f"[LightDB {msg.level.upper()}] {msg.title}"
        m['From'] = ch.email_from or ch.smtp_user
        m['To'] = ', '.join(ch.email_to)

        text = f"""{msg.title}

级别: {msg.level}
来源: {msg.source}
时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.ts))}

{msg.content}
"""
        html = f"""<html><body>
<h2>{msg.title}</h2>
<p><b>级别:</b> {msg.level}</p>
<p><b>来源:</b> {msg.source}</p>
<p><b>时间:</b> {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg.ts))}</p>
<hr>
<pre>{msg.content}</pre>
</body></html>"""

        m.attach(MIMEText(text, 'plain', 'utf-8'))
        m.attach(MIMEText(html, 'html', 'utf-8'))

        if ch.smtp_ssl:
            server = smtplib.SMTP_SSL(ch.smtp_host, ch.smtp_port,
                                       timeout=15)
        else:
            server = smtplib.SMTP(ch.smtp_host, ch.smtp_port, timeout=15)
            server.starttls()

        server.login(ch.smtp_user, ch.smtp_password)
        server.sendmail(m['From'], ch.email_to, m.as_string())
        server.quit()

    def _send_desktop(self, ch, msg):
        """桌面通知"""
        try:
            from plyer import notification
            notification.notify(
                title=f"LightDB: {msg.title}",
                message=msg.content[:200],
                timeout=5,
            )
        except ImportError:
            # Fallback: 打印
            print(f"[通知] {msg.title}: {msg.content}")

    # ==================== 辅助 ====================

    def test_channel(self, name: str) -> tuple:
        """测试渠道"""
        ch = self.get_channel(name)
        if not ch:
            return False, "渠道不存在"

        msg = NotificationMessage(
            title="测试通知",
            content="这是一条来自 LightDB Studio 的测试通知。",
            level="info",
            source="test",
        )

        try:
            if ch.channel_type == 'webhook':
                self._send_webhook(ch, msg)
            elif ch.channel_type == 'email':
                self._send_email(ch, msg)
            elif ch.channel_type == 'desktop':
                self._send_desktop(ch, msg)
            return True, "发送成功"
        except Exception as e:
            return False, str(e)

    def list_history(self, limit: int = 50):
        return list(reversed(self.history[-limit:]))