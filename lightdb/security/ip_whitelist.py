"""IP 白名单"""
import ipaddress
import threading


class IPWhitelist:
    def __init__(self, enabled: bool = False, entries: list = None):
        self.enabled = enabled
        self.lock = threading.RLock()
        self.entries = []
        for e in (entries or []):
            self.add(e)

    def add(self, entry: str):
        """
        entry 可以是:
        - "127.0.0.1"
        - "192.168.1.0/24"
        - "10.0.0.*"
        """
        with self.lock:
            if '*' in entry:
                entry = entry.replace('*', '0')
                if '/' not in entry:
                    # 简单通配转 CIDR
                    parts = entry.split('.')
                    if len(parts) == 4:
                        entry = '.'.join(parts[:3]) + '.0/24'
            try:
                net = ipaddress.ip_network(entry, strict=False)
                self.entries.append(net)
                return True
            except ValueError:
                return False

    def remove(self, entry: str):
        with self.lock:
            try:
                net = ipaddress.ip_network(entry, strict=False)
                self.entries = [n for n in self.entries if n != net]
                return True
            except ValueError:
                return False

    def allow(self, ip: str) -> bool:
        if not self.enabled:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        with self.lock:
            for net in self.entries:
                if addr in net:
                    return True
        return False

    def list_all(self):
        with self.lock:
            return [str(n) for n in self.entries]