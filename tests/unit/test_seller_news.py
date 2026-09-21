"""Unit tests for public official announcement parsing and local persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.amazon_compliance.seller_news import (
    FORUM_BASE_URL,
    SellerNewsFetcher,
    SellerNewsItem,
    SellerNewsStore,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_listing_keeps_only_official_news_posts_and_deduplicates_responsive_cards() -> None:
    html = (FIXTURES / "seller_news_listing.html").read_text(encoding="utf-8")

    items = SellerNewsFetcher.parse_listing(html)

    assert len(items) == 1
    assert items[0].item_id == "first-post"
    assert items[0].source_url == f"{FORUM_BASE_URL}/seller-forums/discussions/t/first-post"


def test_article_extracts_source_link_id_topics_and_official_text() -> None:
    html = (FIXTURES / "seller_news_article.html").read_text(encoding="utf-8")

    item = SellerNewsFetcher.parse_article(
        html, f"{FORUM_BASE_URL}/seller-forums/discussions/t/first-post"
    )

    assert item.title == "New requirements for product bundles starting January 11"
    assert item.policy_source_ids == ("G200442350",)
    assert item.policy_urls == (f"{FORUM_BASE_URL}/help/hub/reference/G200442350",)
    assert "Starting January 11" in item.content
    assert "listing" in item.topics


def test_store_upsert_is_idempotent(tmp_path: Path) -> None:
    store = SellerNewsStore(tmp_path / "seller_news.sqlite3")
    item = SellerNewsItem(
        item_id="post-1",
        title="A fee update",
        published_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        source_url="https://sellercentral.amazon.com/seller-forums/discussions/t/post-1",
        content="A fee update.",
        topics=("fees",),
    )

    store.upsert(item)
    stored = store.upsert(item)

    assert stored == item
    assert store.list_items() == [item]
