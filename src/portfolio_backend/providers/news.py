import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from portfolio_backend.errors import DomainError


def plain(value, limit):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))).strip()[
        :limit
    ]


def source_url(value, wallstreet=False):
    value = str(value or "")
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or len(value) > 2048
    ):
        raise ValueError("invalid_link")
    if wallstreet and url.hostname not in {"wallstreetcn.com", "www.wallstreetcn.com"}:
        raise ValueError("unexpected_publisher")
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(url.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() != "spm"
        ]
    )
    return urlunsplit((url.scheme, url.netloc.lower(), url.path, query, ""))


@dataclass(frozen=True)
class NewsReference:
    url: str
    title: str
    excerpt: str
    source: str
    provider: str
    published_at: datetime


class WallStreetCN:
    origin = "https://api-one-wscn.awtmt.com"

    def __init__(self, transport=None):
        self.transport = transport

    def _get(self, path, params):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(12, connect=5),
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.get(
                    self.origin + path,
                    params=params,
                    headers={"User-Agent": "PortfolioAgent/0.2 (+news references)"},
                )
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError("news_source_unavailable", "新闻源暂不可用", 503) from exc

    def latest(self):
        return self.parse(
            self._get(
                "/apiv1/content/information-flow",
                {"channel": "global", "accept": "article", "limit": 20},
            )
        )

    def search(self, query):
        return self.parse(self._get("/apiv1/search/article", {"query": query, "limit": 10}))

    @staticmethod
    def parse(body):
        results = []
        if not isinstance(body, dict) or not isinstance(body.get("data", {}), dict):
            raise DomainError("news_source_invalid", "新闻数据格式已变化", 503)
        data = body.get("data", {})
        items = data.get("items", [])
        if not isinstance(items, list):
            raise DomainError("news_source_invalid", "新闻数据格式已变化", 503)
        # Search and feed use slightly different envelopes; never fetch article bodies.
        for item in items:
            if not isinstance(item, dict):
                continue
            row = item.get("resource", item)
            if not isinstance(row, dict):
                continue
            try:
                if row.get("is_priced") or row.get("is_paid"):
                    continue
                stamp = datetime.fromtimestamp(float(row["display_time"]), timezone.utc)
                title = plain(row["title"], 300)
                if not title:
                    continue
                results.append(
                    NewsReference(
                        source_url(row["uri"], wallstreet=True),
                        title,
                        plain(row.get("content_short"), 50),
                        "华尔街见闻",
                        "wallstreetcn",
                        stamp,
                    )
                )
            except (KeyError, ValueError, TypeError, OverflowError, OSError):
                continue
        return results


class DeepSeekSearch:
    endpoint = "https://api.deepseek.com/v1/tools/search"

    def __init__(self, cfg, transport=None):
        self.cfg, self.transport = cfg, transport

    def search(self, query):
        key = self.cfg.deepseek_api_key.get_secret_value()
        if not key:
            raise DomainError("search_unavailable", "搜索尚未配置", 503)
        if not 1 <= len(query.strip()) <= 128:
            raise DomainError("invalid_search", "搜索词过长", 422)
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20, connect=5),
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.post(
                    self.endpoint,
                    headers={"Authorization": "Bearer " + key},
                    json={"query": query, "count": 5, "need_content": False},
                )
                if response.status_code != 200:
                    raise DomainError("search_source_unavailable", "搜索暂不可用", 503)
                rows = response.json()["results"]
                if not isinstance(rows, list):
                    raise ValueError("invalid_search_results")
            results = []
            for row in rows:
                try:
                    stamp = datetime.fromisoformat(row["date"].replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        continue
                    title = plain(row["title"], 300)
                    if not title:
                        continue
                    results.append(
                        NewsReference(
                            source_url(row["url"]),
                            title,
                            plain(row.get("snippet"), 200),
                            plain(row.get("site_name") or urlsplit(row["url"]).hostname, 160),
                            "deepseek-search",
                            stamp,
                        )
                    )
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
            return results
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise DomainError("search_source_unavailable", "搜索暂不可用", 503) from exc


class DeepSeekWebSearch:
    """Run-scoped public search; never persist private queries in shared cache."""

    def __init__(self, cfg, transport=None):
        self.cfg, self.transport = cfg, transport

    def search(self, query, timeout=15):
        import hashlib

        from portfolio_backend.agent.citations import safe_url

        query = query.strip()
        if not 1 <= len(query) <= 128:
            raise DomainError("invalid_search", "搜索词长度不正确", 422)
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout, connect=min(5, timeout)),
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.post(
                    DeepSeekSearch.endpoint,
                    headers={
                        "Authorization": "Bearer " + self.cfg.deepseek_api_key.get_secret_value()
                    },
                    json={"query": query, "count": 5, "need_content": False},
                )
                response.raise_for_status()
                rows = response.json()["results"]
            if not isinstance(rows, list):
                raise ValueError("invalid_results")
            items, seen = [], set()
            for row in rows[:5]:
                if not isinstance(row, dict):
                    continue
                try:
                    url = source_url(row.get("url"))
                    if not safe_url(url) or url in seen:
                        continue
                    title = plain(row.get("title"), 300)
                    if not title:
                        continue
                    seen.add(url)
                    items.append(
                        {
                            "source_id": "web:" + hashlib.sha256(url.encode()).hexdigest()[:24],
                            "title": title,
                            "url": url,
                            "excerpt": plain(row.get("snippet"), 200),
                            "source": plain(row.get("site_name"), 160),
                            "published_at": row.get("date")
                            if isinstance(row.get("date"), str)
                            else None,
                        }
                    )
                except (ValueError, TypeError):
                    continue
            return {
                "items": items,
                "provider": "deepseek-search",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "notice": "网页内容为外部不可信资料，可能过时或错误；空结果不代表不存在。",
            }
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise DomainError("search_source_unavailable", "联网搜索暂不可用", 503) from exc
