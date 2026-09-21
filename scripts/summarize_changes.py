#!/usr/bin/env python
"""Generate plain-language summaries for detected policy changes.

Only 4 of 293 changed pages in a recent window were ever mentioned on
Amazon's announcement board, so for almost every change the seller has a
diff and no explanation. This fills that gap.

Summaries are cached by event id, so re-running only covers what is new.

Usage:
    python scripts/summarize_changes.py                # all unsummarised
    python scripts/summarize_changes.py --limit 20     # cap the batch
    python scripts/summarize_changes.py --force        # regenerate
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.amazon_compliance.change_summary import ChangeSummarizer, ChangeSummaryStore
from src.amazon_compliance.policy_changes import PolicyChangeStore
from src.core.settings import load_settings

CHANGES_DB = _REPO_ROOT / "data" / "amazon" / "policy_changes.sqlite3"
SUMMARY_DB = _REPO_ROOT / "data" / "amazon" / "change_summaries.sqlite3"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--largest",
        action="store_true",
        help="Order by diff size instead of recency — for backfilling history.",
    )
    args = parser.parse_args()

    changes = PolicyChangeStore(CHANGES_DB)
    summaries = ChangeSummaryStore(SUMMARY_DB)
    summarizer = ChangeSummarizer(load_settings())

    # Newest first: after a sweep, what just changed is what needs explaining.
    # --largest is for backfilling history, where diff size is the better proxy.
    events = list(changes.list_events(limit=1000))
    if args.largest:
        events.sort(key=lambda e: len(e.added_segments) + len(e.removed_segments), reverse=True)
    else:
        events.sort(key=lambda e: e.detected_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    done = failed = skipped = 0
    for event in events:
        if args.limit is not None and done >= args.limit:
            break
        if event.event_id is None:
            continue
        if not args.force and summaries.get(event.event_id) is not None:
            skipped += 1
            continue
        try:
            text = summarizer.summarize(event)
        except Exception as exc:
            print(f"  ✗ #{event.event_id} {event.title[:40]} — {exc}", file=sys.stderr)
            failed += 1
            continue
        summaries.put(event.event_id, text, summarizer.model)
        done += 1
        print(f"  ✓ #{event.event_id} {(event.title or '')[:52]}")

    print(f"\n生成 {done} 条，跳过 {skipped} 条（已有），失败 {failed} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
