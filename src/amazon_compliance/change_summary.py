"""Plain-language summaries of what changed on a policy page.

Amazon announces almost nothing: across one recent window only 4 of 293
changed pages appeared on the announcement board. For the other 99% the
seller gets a diff and no explanation, so the summary generated here is
the only account of the change they will see.

Summaries live in their own table rather than as a column on
policy_change_events: they are derived, regenerable, and cost money, so
keeping them separable means a schema that can be dropped and rebuilt
without touching the detection record.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from src.libs.llm.base_llm import Message
from src.libs.llm.llm_factory import LLMFactory

_MAX_DIFF_CHARS = 4000

_PROMPT = """You are summarising a change to an Amazon Seller Central policy page for a third-party seller.

Page: {title}

Content that was ADDED:
{added}

Content that was REMOVED:
{removed}

Write two or three sentences in Simplified Chinese covering:
1. what concretely changed (name the figure, date or rule if one is present);
2. whether a seller needs to act, and by when.

State only what the diff shows. If the change is editorial — rewording,
formatting, a moved paragraph — say so plainly and do not invent significance.
Do not add a preamble."""


@dataclass(frozen=True)
class ChangeSummary:
    event_id: int
    summary: str
    model: str
    created_at: datetime


class ChangeSummaryStore:
    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS change_summaries (
            event_id   INTEGER PRIMARY KEY,
            summary    TEXT NOT NULL,
            model      TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute(self._SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, event_id: int) -> ChangeSummary | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM change_summaries WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        return ChangeSummary(
            event_id=row["event_id"],
            summary=row["summary"],
            model=row["model"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def put(self, event_id: int, summary: str, model: str) -> ChangeSummary:
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO change_summaries (event_id, summary, model, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    summary = excluded.summary,
                    model = excluded.model,
                    created_at = excluded.created_at
                """,
                (event_id, summary, model, now.isoformat()),
            )
            conn.commit()
        return ChangeSummary(event_id, summary, model, now)


def _sentences(text: str) -> list[str]:
    """Split into sentence-ish units.

    Diffing at character level shreds a reorganised page into confetti
    ('stor' -> 'marketplac'), which tells the model nothing. Sentences are
    the smallest unit that still carries meaning.
    """
    parts = re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _changed_fragments(removed: Sequence[str], added: Sequence[str]) -> tuple[str, str]:
    """Extract only the spans that actually differ.

    Truncating each side to its first N characters is wrong on this data:
    a reorganised page can be byte-identical for several screens and then
    diverge, so a head-truncated pair looks unchanged and the model
    confidently reports that nothing happened. Diffing first means the
    budget is spent on the parts that moved.
    """
    old_units = _sentences("\n".join(str(s) for s in removed))
    new_units = _sentences("\n".join(str(s) for s in added))
    matcher = difflib.SequenceMatcher(None, old_units, new_units, autojunk=False)

    gone: list[str] = []
    came: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        gone.extend(u.strip() for u in old_units[i1:i2])
        came.extend(u.strip() for u in new_units[j1:j2])

    def pack(fragments: list[str]) -> str:
        kept, budget = [], _MAX_DIFF_CHARS
        # Longest fragments first: they carry the substance.
        for frag in sorted((f for f in fragments if f), key=len, reverse=True):
            if budget <= 0:
                break
            kept.append(f"- {frag[:budget]}")
            budget -= len(frag)
        if not kept:
            return "(none)"
        if len(kept) < len([f for f in fragments if f]):
            kept.append(f"- (… {len([f for f in fragments if f]) - len(kept)} further fragments omitted)")
        return "\n".join(kept)

    return pack(gone), pack(came)


class ChangeSummarizer:
    """Turns a PolicyChangeEvent's diff into a short Chinese summary."""

    def __init__(self, settings: Any, llm: Any | None = None) -> None:
        self.llm = llm or LLMFactory.create(settings)
        self.model = getattr(self.llm, "model", "unknown")

    def summarize(self, event: Any) -> str:
        removed, added = _changed_fragments(event.removed_segments, event.added_segments)
        prompt = _PROMPT.format(
            title=event.title or event.source_id,
            added=added,
            removed=removed,
        )
        response = self.llm.chat([Message(role="user", content=prompt)])
        return (response.content or "").strip()
