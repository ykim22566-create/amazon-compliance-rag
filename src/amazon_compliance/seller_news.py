"""Public official Amazon seller-announcement ingestion.

Amazon's personalized Seller News feed lives behind Seller Central sign-in.
For this local product demo we intentionally ingest only the publicly visible
official ``News_Amazon`` posts in Seller Forums' News and Announcements area.
The source is still Amazon-operated and attributable, but it must never be
presented as a customer's private Seller News inbox.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

FORUM_BASE_URL = "https://sellercentral.amazon.com"
PUBLIC_ANNOUNCEMENTS_URL = (
    f"{FORUM_BASE_URL}/seller-forums/discussions?"
    "categories%5B%5D=amzn1.spce.category.8b1ad9d2&dateRange=threeMonths"
    "&replies=repliesAll&sortBy=lastActivityTime"
)

_POST_PATH = "/seller-forums/discussions/t/"
_SOURCE_ID_PATTERN = re.compile(
    r"/(?:help/hub/reference|gp/help/external)/([A-Z][A-Z0-9]+)", re.IGNORECASE
)
_WHITESPACE = re.compile(r"\s+")


class SellerNewsFetchError(RuntimeError):
    """Raised when the public official announcement source cannot be read."""


@dataclass(frozen=True)
class SellerNewsItem:
    """One attributable official announcement and its linked policy evidence."""

    item_id: str
    title: str
    published_at: datetime
    source_url: str
    content: str = ""
    policy_urls: tuple[str, ...] = ()
    policy_source_ids: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    author: str = "News_Amazon"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["published_at"] = self.published_at.isoformat()
        return payload


def _clean_text(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _stable_post_id(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def classify_topics(text: str) -> tuple[str, ...]:
    """Return transparent keyword topics; this is not an LLM classification."""
    normalized = text.casefold()
    rules = {
        "account_health": ("account health", "account status", "performance notification"),
        "fees": ("fee", "fees", "surcharge", "cost of selling"),
        "listing": ("listing", "product detail page", "product bundle", "bundle"),
        "fulfillment": ("fba", "fulfillment", "inventory", "fulfilment"),
        "product_compliance": ("compliance", "safety", "certification", "hazmat"),
        "program": ("program", "service provider", "seller fulfilled"),
    }
    return tuple(topic for topic, phrases in rules.items() if any(p in normalized for p in phrases))


class SellerNewsFetcher:
    """Fetch and parse the public official forum feed without browser automation."""

    def __init__(self, downloader: Callable[[str], str] | None = None) -> None:
        self._downloader = downloader or self._download

    @staticmethod
    def _download(url: str) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": (
                    "SellerPolicyWorkspace/0.1 (+local product demo; "
                    "official-public-announcement sync)"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed official HTTPS host
                return response.read().decode("utf-8", errors="replace")
        except OSError as error:
            raise SellerNewsFetchError("无法读取 Amazon 公开官方公告源。") from error

    def fetch_listing(self) -> list[SellerNewsItem]:
        return self.parse_listing(self._downloader(PUBLIC_ANNOUNCEMENTS_URL))

    def fetch_article(self, url: str) -> SellerNewsItem:
        return self.parse_article(self._downloader(url), url)

    def fetch_latest(self, *, limit: int = 12) -> list[SellerNewsItem]:
        articles: list[SellerNewsItem] = []
        for item in self.fetch_listing()[: max(1, limit)]:
            articles.append(self.fetch_article(item.source_url))
        return articles

    @staticmethod
    def parse_listing(html: str) -> list[SellerNewsItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[SellerNewsItem] = []
        seen_urls: set[str] = set()

        for card in soup.select('[data-testid="search-post-compact-layout"]'):
            if "News_Amazon" not in card.get_text(" ", strip=True):
                continue
            link = card.select_one(f'a[href*="{_POST_PATH}"]')
            if link is None or not link.get("href"):
                continue
            source_url = urljoin(FORUM_BASE_URL, str(link["href"]))
            if source_url in seen_urls:
                continue
            seen_urls.add(source_url)
            timestamp = card.select_one("time[datetime]")
            items.append(
                SellerNewsItem(
                    item_id=_stable_post_id(source_url),
                    title=_clean_text(link.get_text(" ", strip=True)),
                    published_at=_parse_datetime(
                        str(timestamp["datetime"]) if timestamp is not None else None
                    ),
                    source_url=source_url,
                )
            )
        return items

    @staticmethod
    def parse_article(html: str, source_url: str) -> SellerNewsItem:
        soup = BeautifulSoup(html, "html.parser")
        article = next(
            (
                candidate
                for candidate in soup.select('article[id^="post-"]')
                if "News_Amazon" in candidate.get_text(" ", strip=True)
                and candidate.select_one('[data-testid="post-content"]') is not None
            ),
            None,
        )
        if article is None:
            raise SellerNewsFetchError("公开公告页未返回可验证的 News_Amazon 正文。")

        title_node = article.select_one("h1") or soup.select_one("h1")
        content_node = article.select_one('[data-testid="post-content"]')
        assert content_node is not None
        timestamp = article.select_one("time[datetime]")
        policy_urls: list[str] = []
        policy_source_ids: list[str] = []
        for link in content_node.select("a[href]"):
            href = urljoin(FORUM_BASE_URL, str(link["href"]))
            if "/help/" not in href:
                continue
            if href not in policy_urls:
                policy_urls.append(href)
            source_match = _SOURCE_ID_PATTERN.search(href)
            if source_match and source_match.group(1).upper() not in policy_source_ids:
                policy_source_ids.append(source_match.group(1).upper())

        content = _clean_text(content_node.get_text(" ", strip=True))
        title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else "Official update"
        return SellerNewsItem(
            item_id=(article.get("id") or f"post-{_stable_post_id(source_url)}").removeprefix("post-"),
            title=title,
            published_at=_parse_datetime(
                str(timestamp["datetime"]) if timestamp is not None else None
            ),
            source_url=source_url,
            content=content,
            policy_urls=tuple(policy_urls),
            policy_source_ids=tuple(policy_source_ids),
            topics=classify_topics(f"{title} {content}"),
        )


class SellerNewsStore:
    """A small idempotent local cache of source-attributed announcements."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS seller_news_items (
            item_id                  TEXT PRIMARY KEY,
            title                    TEXT NOT NULL,
            published_at             TEXT NOT NULL,
            source_url               TEXT NOT NULL,
            content                  TEXT NOT NULL,
            policy_urls_json         TEXT NOT NULL,
            policy_source_ids_json   TEXT NOT NULL,
            topics_json              TEXT NOT NULL,
            author                   TEXT NOT NULL,
            fetched_at               TEXT NOT NULL
        )
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(self._SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._db_path))
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _from_row(row: sqlite3.Row) -> SellerNewsItem:
        published_at = _parse_datetime(str(row["published_at"]))
        return SellerNewsItem(
            item_id=str(row["item_id"]),
            title=str(row["title"]),
            published_at=published_at,
            source_url=str(row["source_url"]),
            content=str(row["content"]),
            policy_urls=tuple(json.loads(row["policy_urls_json"])),
            policy_source_ids=tuple(json.loads(row["policy_source_ids_json"])),
            topics=tuple(json.loads(row["topics_json"])),
            author=str(row["author"]),
        )

    def upsert(self, item: SellerNewsItem) -> SellerNewsItem:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO seller_news_items (
                    item_id, title, published_at, source_url, content,
                    policy_urls_json, policy_source_ids_json, topics_json,
                    author, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    title = excluded.title,
                    published_at = excluded.published_at,
                    source_url = excluded.source_url,
                    content = excluded.content,
                    policy_urls_json = excluded.policy_urls_json,
                    policy_source_ids_json = excluded.policy_source_ids_json,
                    topics_json = excluded.topics_json,
                    author = excluded.author,
                    fetched_at = excluded.fetched_at
                """,
                (
                    item.item_id,
                    item.title,
                    item.published_at.astimezone(timezone.utc).isoformat(),
                    item.source_url,
                    item.content,
                    json.dumps(item.policy_urls),
                    json.dumps(item.policy_source_ids),
                    json.dumps(item.topics),
                    item.author,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM seller_news_items WHERE item_id = ?", (item.item_id,)
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._from_row(row)

    def list_items(self, *, limit: int = 50) -> list[SellerNewsItem]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM seller_news_items
                ORDER BY published_at DESC, item_id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get_item(self, item_id: str) -> SellerNewsItem | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM seller_news_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None


__all__ = [
    "FORUM_BASE_URL",
    "PUBLIC_ANNOUNCEMENTS_URL",
    "SellerNewsFetchError",
    "SellerNewsFetcher",
    "SellerNewsItem",
    "SellerNewsStore",
    "classify_topics",
]
