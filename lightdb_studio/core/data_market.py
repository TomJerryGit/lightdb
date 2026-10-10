"""
数据市场
- 数据资产目录
- 订阅机制
- 评分评论
- 使用统计排行
"""
import json
import time
from collections import defaultdict
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional


MARKET_FILE = Path.home() / '.lightdb_studio' / 'data_market.json'


@dataclass
class MarketListing:
    """市场挂牌"""
    table_key: str = ""              # db.table
    title: str = ""
    description: str = ""
    owner: str = ""
    domain: str = ""
    tags: List[str] = field(default_factory=list)
    # 质量评分 0-5
    quality_score: float = 0
    # 评分
    ratings: List[Dict] = field(default_factory=list)  # {user, score, comment, ts}
    # 订阅
    subscribers: List[str] = field(default_factory=list)
    # 状态
    status: str = "published"        # published / draft / archived
    published_at: float = 0
    updated_at: float = 0

    def __post_init__(self):
        now = time.time()
        if not self.published_at:
            self.published_at = now
        if not self.updated_at:
            self.updated_at = now

    def avg_rating(self) -> float:
        if not self.ratings:
            return 0
        return sum(r.get('score', 0) for r in self.ratings) / len(self.ratings)

    def rating_count(self) -> int:
        return len(self.ratings)


class DataMarket:
    """数据市场"""

    def __init__(self, catalog=None):
        self.file = MARKET_FILE
        self.catalog = catalog
        self.listings: Dict[str, MarketListing] = {}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for k, ld in data.get('listings', {}).items():
                self.listings[k] = MarketListing(**ld)
        except Exception:
            pass

    def save(self):
        data = {
            'listings': {k: asdict(v)
                          for k, v in self.listings.items()},
        }
        tmp = self.file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    # ==================== 挂牌 ====================

    def publish(self, table_key: str, title: str = '',
                description: str = '', owner: str = '',
                tags: List[str] = None) -> MarketListing:
        """发布表到市场"""
        # 从目录获取信息
        domain = ''
        if self.catalog and table_key in self.catalog.tables:
            m = self.catalog.tables[table_key]
            domain = m.domain
            if not title:
                title = m.business_name or m.name
            if not description:
                description = m.comment

        listing = MarketListing(
            table_key=table_key,
            title=title or table_key,
            description=description,
            owner=owner,
            domain=domain,
            tags=tags or [],
            status='published',
        )
        self.listings[table_key] = listing
        self.save()
        return listing

    def unpublish(self, table_key: str):
        if table_key in self.listings:
            self.listings[table_key].status = 'archived'
            self.save()

    def remove(self, table_key: str):
        if table_key in self.listings:
            del self.listings[table_key]
            self.save()

    # ==================== 评分 ====================

    def rate(self, table_key: str, user: str, score: float,
             comment: str = ''):
        """评分"""
        if table_key not in self.listings:
            return False
        if not 0 <= score <= 5:
            return False

        listing = self.listings[table_key]
        # 去重：同一用户只保留最新
        listing.ratings = [r for r in listing.ratings
                            if r.get('user') != user]
        listing.ratings.append({
            'user': user,
            'score': score,
            'comment': comment,
            'ts': time.time(),
        })
        listing.updated_at = time.time()
        self.save()
        return True

    # ==================== 订阅 ====================

    def subscribe(self, table_key: str, user: str):
        if table_key not in self.listings:
            return False
        listing = self.listings[table_key]
        if user not in listing.subscribers:
            listing.subscribers.append(user)
            listing.updated_at = time.time()
            self.save()
        return True

    def unsubscribe(self, table_key: str, user: str):
        if table_key not in self.listings:
            return False
        listing = self.listings[table_key]
        if user in listing.subscribers:
            listing.subscribers.remove(user)
            listing.updated_at = time.time()
            self.save()
        return True

    # ==================== 浏览 ====================

    def list_all(self, only_published: bool = True) -> List[MarketListing]:
        items = list(self.listings.values())
        if only_published:
            items = [x for x in items if x.status == 'published']
        return sorted(items, key=lambda x: -x.updated_at)

    def search(self, keyword: str = '', tags: List[str] = None,
               domain: str = '', sort_by: str = 'rating') -> List[MarketListing]:
        keyword = keyword.lower()
        results = []

        for listing in self.listings.values():
            if listing.status != 'published':
                continue
            if keyword and keyword not in (
                    listing.title + listing.description).lower():
                # 也搜索标签
                if not any(keyword in t.lower() for t in listing.tags):
                    continue
            if tags and not any(t in listing.tags for t in tags):
                continue
            if domain and listing.domain != domain:
                continue
            results.append(listing)

        if sort_by == 'rating':
            results.sort(key=lambda x: (-x.avg_rating(), -x.rating_count()))
        elif sort_by == 'subscribers':
            results.sort(key=lambda x: -len(x.subscribers))
        elif sort_by == 'recent':
            results.sort(key=lambda x: -x.updated_at)

        return results

    def recommend_to_user(self, user: str,
                          top_n: int = 10) -> List[Dict]:
        """推荐给用户（基于订阅相似度）"""
        # 找到用户已订阅的域
        my_domains = set()
        my_tables = set()
        for l in self.listings.values():
            if user in l.subscribers:
                my_domains.add(l.domain)
                my_tables.add(l.table_key)

        scored = []
        for l in self.listings.values():
            if l.table_key in my_tables:
                continue
            if l.status != 'published':
                continue

            score = 0
            # 同域
            if l.domain in my_domains:
                score += 3
            # 评分高
            score += l.avg_rating() * 0.5
            # 订阅多
            score += min(5, len(l.subscribers) * 0.2)

            if score > 0:
                scored.append((score, l))

        scored.sort(key=lambda x: -x[0])
        return [
            {
                'table_key': l.table_key,
                'title': l.title,
                'domain': l.domain,
                'rating': l.avg_rating(),
                'subscribers': len(l.subscribers),
                'score': score,
            }
            for score, l in scored[:top_n]
        ]

    def stats(self) -> dict:
        published = [l for l in self.listings.values()
                     if l.status == 'published']
        by_domain = defaultdict(int)
        total_rating = 0
        total_count = 0
        for l in published:
            by_domain[l.domain] += 1
            if l.ratings:
                total_rating += l.avg_rating() * l.rating_count()
                total_count += l.rating_count()

        return {
            'total_listings': len(self.listings),
            'published': len(published),
            'total_ratings': total_count,
            'avg_rating': total_rating / total_count if total_count else 0,
            'by_domain': dict(by_domain),
            'total_subscriptions': sum(
                len(l.subscribers) for l in published),
        }