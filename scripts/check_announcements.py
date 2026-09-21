#!/usr/bin/env python
"""Check Seller Central's announcement board for new posts.

Cheap enough to run daily — it reads one server-rendered listing page, so
no browser is involved. Roughly half of what Amazon posts there is
marketing (conference discounts, promo rates) rather than policy; an item
that links to no help page is flagged accordingly instead of being
dropped, so the seller still sees it and decides.

Exits 0 with nothing new, 0 after reporting new items. Anything printed to
stdout is intended to be mailable straight from cron.

Usage:
    python scripts/check_announcements.py
    python scripts/check_announcements.py --limit 20
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.amazon_compliance.seller_news import SellerNewsFetcher, SellerNewsStore

DB_PATH = _REPO_ROOT / "data" / "amazon" / "seller_news.sqlite3"
RUNS_DIR = _REPO_ROOT / "data" / "amazon" / "runs"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()

    store = SellerNewsStore(DB_PATH)
    fetched = SellerNewsFetcher().fetch_latest(limit=args.limit)

    new_items = []
    for item in fetched:
        if store.get_item(item.item_id) is None:
            new_items.append(item)
        store.upsert(item)

    stamp = datetime.now(timezone.utc)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / f"news_{stamp.strftime('%Y%m%dT%H%M%SZ')}.json").write_text(
        json.dumps(
            {
                "checked_at": stamp.isoformat(),
                "fetched": len(fetched),
                "new": [
                    {
                        "item_id": i.item_id,
                        "title": i.title,
                        "published_at": i.published_at.isoformat(),
                        "source_url": i.source_url,
                        "policy_source_ids": list(i.policy_source_ids),
                    }
                    for i in new_items
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if not new_items:
        print(f"亚马逊公告：无更新（已检查 {len(fetched)} 条）")
        return 0

    print(f"亚马逊公告：{len(new_items)} 条新内容\n")
    for item in new_items:
        linked = len(item.policy_source_ids)
        tag = f"关联 {linked} 个政策页" if linked else "未关联政策页（可能是活动/促销）"
        print(f"· {item.published_at.date()}  {item.title}")
        print(f"  {tag}")
        print(f"  {item.source_url}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
