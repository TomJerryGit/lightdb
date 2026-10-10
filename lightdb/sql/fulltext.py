"""全文索引 - 倒排索引 + BM25 排序"""
import hashlib
import json
import math
import re
from collections import Counter, defaultdict


def tokenize(text):
    """混合分词：英文按空格，中文按 2-gram"""
    if not text:
        return []
    s = str(text).lower()
    tokens = []
    # 英文/数字
    tokens.extend(re.findall(r'[a-z0-9]+', s))
    # 中文 2-gram
    for seg in re.findall(r'[\u4e00-\u9fff]+', s):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            for i in range(len(seg) - 1):
                tokens.append(seg[i:i + 2])
    return tokens


class InvertedIndex:
    """倒排索引"""
    def __init__(self):
        self.index = defaultdict(dict)    # token -> {pk: tf}
        self.doc_len = {}                 # pk -> token 总数
        self.df = {}                      # token -> 文档频率
        self.n_docs = 0

    def add(self, pk, text):
        tokens = tokenize(text)
        if not tokens:
            return
        tf = Counter(tokens)
        for t, c in tf.items():
            self.index[t][pk] = c
            self.df[t] = self.df.get(t, 0) + 1
        self.doc_len[pk] = len(tokens)
        self.n_docs += 1

    def remove(self, pk):
        if pk in self.doc_len:
            del self.doc_len[pk]
            self.n_docs = max(0, self.n_docs - 1)
        for t in list(self.index.keys()):
            if pk in self.index[t]:
                del self.index[t][pk]
                self.df[t] = self.df.get(t, 0) - 1
                if self.df[t] <= 0:
                    del self.df[t]
                    del self.index[t]

    def search(self, query, limit=1000):
        """返回 {pk: score}"""
        q_tokens = tokenize(query)
        if not q_tokens:
            return {}

        candidates = set()
        for t in q_tokens:
            if t in self.index:
                candidates.update(self.index[t].keys())
        if not candidates:
            return {}

        avg_len = sum(self.doc_len.values()) / max(1, len(self.doc_len))
        k1, b = 1.5, 0.75
        scores = {}

        for pk in candidates:
            score = 0.0
            d_len = self.doc_len.get(pk, 1)
            for t in q_tokens:
                if t not in self.index or pk not in self.index[t]:
                    continue
                tf = self.index[t][pk]
                df = self.df.get(t, 1)
                idf = math.log((self.n_docs - df + 0.5) / (df + 0.5) + 1)
                num = tf * (k1 + 1)
                denom = tf + k1 * (1 - b + b * d_len / avg_len)
                score += idf * num / denom
            if score > 0:
                scores[pk] = score

        return scores

    def to_dict(self):
        return {
            'index': {t: dict(d) for t, d in self.index.items()},
            'doc_len': self.doc_len,
            'df': self.df,
            'n_docs': self.n_docs,
        }

    @classmethod
    def from_dict(cls, d):
        idx = cls()
        idx.index = defaultdict(dict, {t: dict(v) for t, v in d.get('index', {}).items()})
        idx.doc_len = d.get('doc_len', {})
        idx.df = d.get('df', {})
        idx.n_docs = d.get('n_docs', 0)
        return idx


class FullTextManager:
    """某张表的全文索引集合"""
    def __init__(self):
        self.indexes = {}    # col_name -> InvertedIndex

    def create_index(self, col_name):
        self.indexes.setdefault(col_name, InvertedIndex())

    def index_row(self, pk, row, col_names):
        for col_name, idx in self.indexes.items():
            if col_name in col_names:
                ci = col_names.index(col_name)
                if ci < len(row):
                    idx.add(pk, row[ci])

    def remove_row(self, pk):
        for idx in self.indexes.values():
            idx.remove(pk)

    def search(self, col_name, query):
        idx = self.indexes.get(col_name)
        if idx is None:
            return {}
        return idx.search(query)

    def to_dict(self):
        return {c: idx.to_dict() for c, idx in self.indexes.items()}

    @classmethod
    def from_dict(cls, d):
        m = cls()
        for c, idx_d in d.items():
            m.indexes[c] = InvertedIndex.from_dict(idx_d)
        return m