"""Unit tests for versioned Seller Policy Watch evidence events."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from src.amazon_compliance.policy_changes import (
    PolicyChangeStore,
    PolicyDiffEngine,
    backfill_change_events,
)

SOURCE_ID = "GMUTB89XM7AATPR3"
SOURCE_URL = f"https://sellercentral.amazon.com/help/hub/reference/external/{SOURCE_ID}"


def _snapshot(root: Path, timestamp: str, paragraphs: list[str]) -> Path:
    directory = root / SOURCE_ID
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{timestamp}.html"
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    path.write_text(
        f"<html><head><title>Fuel surcharge policy</title></head>"
        f"<body><article><h1>Fuel surcharge policy</h1>{body}</article></body></html>",
        encoding="utf-8",
    )
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(
            {
                "source_id": SOURCE_ID,
                "url": SOURCE_URL,
                "title": "Fuel surcharge policy",
                "fetched_at": datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ")
                .replace(tzinfo=timezone.utc)
                .isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_diff_retains_before_after_evidence_and_marks_comparable_pages_ready(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "20260101T000000Z",
        ["This fee applies to eligible shipments from April 1, 2026.", "Contact Seller Support."],
    )
    after = _snapshot(
        tmp_path,
        "20260102T000000Z",
        ["This fee applies to eligible shipments from April 17, 2026.", "Contact Seller Support."],
    )

    event = PolicyDiffEngine().compare(before, after)

    assert event is not None
    assert event.source_id == SOURCE_ID
    assert event.verification_status == "ready"
    assert any("April 1, 2026" in segment for segment in event.removed_segments)
    assert any("April 17, 2026" in segment for segment in event.added_segments)
    assert event.source_url == SOURCE_URL


def test_large_render_shrink_requires_human_review(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "20260101T000000Z",
        ["This fee applies to eligible shipments." * 30],
    )
    after = _snapshot(tmp_path, "20260102T000000Z", ["This fee applies to eligible shipments."])

    event = PolicyDiffEngine().compare(before, after)

    assert event is not None
    assert event.verification_status == "review_required"
    assert event.review_reason is not None


def test_chinese_to_english_render_is_excluded_from_policy_change_queue(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "20260101T000000Z",
        ["此费用适用于符合条件的货件，并将于 2026 年 4 月 1 日开始收取。"],
    )
    after = _snapshot(
        tmp_path,
        "20260102T000000Z",
        ["This fee applies to eligible shipments and takes effect April 1, 2026."],
    )

    event = PolicyDiffEngine().compare(before, after)

    assert event is not None
    assert event.verification_status == "excluded_locale_switch"
    assert event.review_reason is not None


def test_store_and_backfill_are_idempotent(tmp_path: Path) -> None:
    _snapshot(tmp_path, "20260101T000000Z", ["The surcharge begins on April 1."])
    _snapshot(tmp_path, "20260102T000000Z", ["The surcharge begins on April 17."])
    store = PolicyChangeStore(tmp_path / "policy_changes.sqlite3")

    first = backfill_change_events(tmp_path, store)
    second = backfill_change_events(tmp_path, store)

    assert len(first) == 1
    assert len(second) == 1
    assert store.counts()["total"] == 1
    event = store.list_events()[0]
    assert event.event_id is not None
    assert event.source_id == SOURCE_ID


def test_review_outcome_is_stored_without_rewriting_detector_status(tmp_path: Path) -> None:
    _snapshot(tmp_path, "20260101T000000Z", ["The surcharge begins on April 1."])
    _snapshot(tmp_path, "20260102T000000Z", ["The surcharge begins on April 17."])
    store = PolicyChangeStore(tmp_path / "policy_changes.sqlite3")
    event = backfill_change_events(tmp_path, store)[0]

    reviewed = store.record_review(
        event.event_id or 0,
        outcome="notify",
        note="Fee effective date changed; alert the FBA operations owner.",
    )

    assert reviewed.verification_status == "ready"
    assert reviewed.review_outcome == "notify"
    assert reviewed.review_note == "Fee effective date changed; alert the FBA operations owner."
    assert reviewed.reviewed_at is not None
    assert store.counts()["pending_review"] == 0


def test_store_excludes_historical_locale_switches_from_the_review_count(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "20260101T000000Z",
        ["此费用适用于符合条件的货件，并将于 2026 年 4 月 1 日开始收取。"],
    )
    after = _snapshot(
        tmp_path,
        "20260102T000000Z",
        ["This fee applies to eligible shipments and takes effect April 1, 2026."],
    )
    store = PolicyChangeStore(tmp_path / "policy_changes.sqlite3")
    event = PolicyDiffEngine().compare(before, after)
    assert event is not None
    store.record(replace(event, verification_status="review_required", review_reason="historical"))

    assert store.exclude_locale_switch_events() == 1
    counts = store.counts()
    assert counts["excluded_locale_switch"] == 1
    assert counts["actionable"] == 0
    assert counts["pending_review"] == 0
