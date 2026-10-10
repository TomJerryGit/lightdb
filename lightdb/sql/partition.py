"""分区表 - HASH / RANGE / LIST"""
import hashlib


def _hash_int(k):
    if isinstance(k, int):
        return k
    s = str(k)
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)


class Partition:
    __slots__ = ('name', 'ptype', 'value', '_hash_index')

    def __init__(self, name, ptype, value):
        self.name = name
        self.ptype = ptype.upper()
        self.value = value
        self._hash_index = -1

    def match(self, key):
        if self.ptype == 'RANGE':
            lo, hi = self.value
            if lo is not None and key < lo:
                return False
            if hi is not None and key >= hi:
                return False
            return True
        if self.ptype == 'HASH':
            n = self.value
            return _hash_int(key) % n == self._hash_index
        if self.ptype == 'LIST':
            return key in self.value
        return False

    def to_dict(self):
        v = list(self.value) if isinstance(self.value, (list, tuple)) else self.value
        return {'name': self.name, 'ptype': self.ptype, 'value': v}

    @classmethod
    def from_dict(cls, d):
        v = d['value']
        if d['ptype'] == 'RANGE' and isinstance(v, list):
            v = tuple(v)
        p = cls(d['name'], d['ptype'], v)
        return p


class PartitionManager:
    def __init__(self):
        self.partitions = []
        self.pk_col = None

    @property
    def enabled(self):
        return len(self.partitions) > 0

    def define_hash(self, col, num):
        self.pk_col = col
        for i in range(num):
            p = Partition(f'p{i}', 'HASH', num)
            p._hash_index = i
            self.partitions.append(p)

    def define_range(self, col, ranges):
        """ranges: [(lo, hi, name), ...] hi=None 表示 MAXVALUE"""
        self.pk_col = col
        for lo, hi, name in ranges:
            self.partitions.append(Partition(name, 'RANGE', (lo, hi)))

    def define_list(self, col, lists):
        """lists: [([values...], name), ...]"""
        self.pk_col = col
        for vals, name in lists:
            self.partitions.append(Partition(name, 'LIST', vals))

    def locate(self, key):
        for p in self.partitions:
            if p.match(key):
                return p
        return None

    def prune(self, where):
        """WHERE 裁剪 → 返回候选分区名集合，None 表示全扫"""
        if not self.partitions or not where or 'column' not in where:
            return None
        if where['column'].split('.')[-1] != self.pk_col:
            return None
        op, val = where['op'], where['value']
        if op == '=':
            try:
                if isinstance(val, str):
                    try:
                        val = int(val)
                    except ValueError:
                        pass
            except (ValueError, TypeError):
                pass
            p = self.locate(val)
            return [p.name] if p else []
        return None

    def to_dict(self):
        d = {'partitions': [p.to_dict() for p in self.partitions],
             'pk_col': self.pk_col}
        # 恢复 _hash_index
        return d

    @classmethod
    def from_dict(cls, d):
        m = cls()
        m.pk_col = d.get('pk_col')
        for pd in d.get('partitions', []):
            p = Partition.from_dict(pd)
            if p.ptype == 'HASH':
                # 从名字反推索引
                try:
                    p._hash_index = int(p.name[1:])
                except ValueError:
                    p._hash_index = 0
            m.partitions.append(p)
        return m