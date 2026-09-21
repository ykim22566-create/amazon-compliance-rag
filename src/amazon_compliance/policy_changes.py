"""Version-aware policy change detection for Seller Policy Watch.

The existing :mod:`change_detector` answers a narrow operational question:
"should this page be re-indexed?"  This module answers the product question:
"what changed, what is the evidence, and is the change safe to alert on?"

It deliberately keeps the raw HTML snapshots as the source of truth.  Change
events hold only compact, human-readable excerpts plus paths back to the two
snapshots that produced them.  This makes every dashboard card and MCP answer
auditable without making a model's summary the system of record.
"""

from __future__ import annotations

import difflib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.libs.loader.html_loader import ROW_MARKER_CLOSE, ROW_MARKER_OPEN, HtmlLoader


def _render_locale(text: str) -> str:
    """Identify the dominant script well enough to reject locale flips.

    We do not need language-model classification here.  The tracked Seller
    Central corpus is deliberately English, while the historical false
    positives are Chinese-versus-English renders of the same article.  A
    lightweight script check is deterministic, inspectable, and avoids
    treating a translated page as a policy edit.
    """
    cjk_count = sum("\u4e00" <= character <= "\u9fff" for character in text)
    latin_count = sum(character.isascii() and character.isalpha() for character in text)
    if cjk_count >= 20 and cjk_count >= latin_count / 2:
        return "cjk"
    if latin_count >= 20 and latin_count >= cjk_count * 2:
        return "latin"
    return "unknown"


def _read_sidecar(snapshot_path: Path) -> dict[str, object]:
    """Read optional crawler metadata without making it a runtime dependency."""
    sidecar_path = snapshot_path.with_suffix(snapshot_path.suffix + ".meta.json")
    if not sidecar_path.exists():
        return {}
    try:
        value = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _snapshot_timestamp(snapshot_path: Path, sidecar: dict[str, object]) -> datetime:
    """Resolve the capture time, preferring the crawler's UTC sidecar value."""
    fetched_at = sidecar.get("fetched_at")
    if isinstance(fetched_at, str):
        try:
            parsed = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.strptime(snapshot_path.stem, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return datetime.fromtimestamp(snapshot_path.stat().st_mtime, tz=timezone.utc)


def _segments(text: str) -> list[str]:
    """Turn loader output into readable diff units.

    Paragraphs are preferred over individual words: an operator needs to see
    the policy statement that changed, not a noisy word-level patch.  Table
    rows are already emitted as separate marker blocks by ``HtmlLoader`` and
    remain separate units after this normalization.
    """
    text = text.replace(ROW_MARKER_OPEN, "").replace(ROW_MARKER_CLOSE, "")
    blocks = text.split("\n\n")
    cleaned: list[str] = []
    for block in blocks:
        normalized = " ".join(line.strip() for line in block.splitlines() if line.strip())
        if normalized:
            cleaned.append(normalized)
    return cleaned


@dataclass(frozen=True)
class SnapshotVersion:
    """A parsed snapshot together with its source provenance."""

    source_id: str
    source_url: str
    title: str
    fetched_at: datetime
    path: str
    content_hash: str
    content_length: int
    segments: tuple[str, ...]


@dataclass(frozen=True)
class PolicyChangeEvent:
    """Persistable, evidence-backed policy change event."""

    source_id: str
    source_url: str
    title: str
    detected_at: datetime
    previous_snapshot_path: str
    current_snapshot_path: str
    previous_content_hash: str
    current_content_hash: str
    added_segments: tuple[str, ...]
    removed_segments: tuple[str, ...]
    verification_status: str
    review_reason: str | None
    event_id: int | None = None
    review_outcome: str | None = None
    review_note: str | None = None
    reviewed_at: datetime | None = None

    @property
    def summary(self) -> str:
        change_text = (
            f"{len(self.added_segments)} 个新增、{len(self.removed_segments)} 个删除的证据块"
            f" / {len(self.added_segments)} added and {len(self.removed_segments)} removed evidence blocks"
        )
        if self.verification_status == "excluded_locale_switch":
            return (
                "已排除语言版本切换，不代表政策变更。"
                " / Excluded a locale switch; it does not represent a policy change."
            )
        if self.verification_status == "review_required":
            return f"发现 {change_text}；提醒前请先复核。 / Detected {change_text}; review before alerting. {self.review_reason}"
        return (
            f"发现 {change_text}；来源渲染已通过自动质量检查。"
            f" / Detected {change_text}; source rendering passed the automatic quality check."
        )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["detected_at"] = self.detected_at.isoformat()
        if self.reviewed_at is not None:
            payload["reviewed_at"] = self.reviewed_at.isoformat()
        payload["summary"] = self.summary
        return payload


class PolicyDiffEngine:
    """Creates compact, reviewable semantic blocks from two HTML snapshots."""

    # A large shrink is far more often a partial SPA render than a policy
    # update.  We still keep the event, but make it explicitly review-only.
    _MIN_SIZE_RATIO_FOR_AUTO_READY = 0.65
    _MAX_EXCERPTS_PER_SIDE = 12

    def __init__(self, loader: HtmlLoader | None = None) -> None:
        self._loader = loader or HtmlLoader()

    def load_snapshot(self, snapshot_path: str | Path) -> SnapshotVersion:
        path = Path(snapshot_path)
        document = self._loader.load(path)
        sidecar = _read_sidecar(path)
        return SnapshotVersion(
            source_id=str(document.metadata["source_id"]),
            source_url=str(document.metadata["source_url"]),
            title=str(document.metadata.get("title") or sidecar.get("title") or "Untitled policy"),
            fetched_at=_snapshot_timestamp(path, sidecar),
            path=str(path),
            content_hash=str(document.metadata["content_hash"]),
            content_length=len(document.text),
            segments=tuple(_segments(document.text)),
        )

    def is_locale_switch(self, previous_path: str | Path, current_path: str | Path) -> bool:
        """Return whether two snapshots are the same source in different locales."""
        previous = self.load_snapshot(previous_path)
        current = self.load_snapshot(current_path)
        return {
            _render_locale("\n".join(previous.segments)),
            _render_locale("\n".join(current.segments)),
        } == {"cjk", "latin"}

    def compare(self, previous_path: str | Path, current_path: str | Path) -> PolicyChangeEvent | None:
        previous = self.load_snapshot(previous_path)
        current = self.load_snapshot(current_path)
        if previous.source_id != current.source_id:
            raise ValueError("Snapshots must belong to the same policy source")
        if previous.content_hash == current.content_hash:
            return None

        matcher = difflib.SequenceMatcher(
            a=list(previous.segments), b=list(current.segments), autojunk=False
        )
        added: list[str] = []
        removed: list[str] = []
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag in {"replace", "delete"}:
                removed.extend(previous.segments[old_start:old_end])
            if tag in {"replace", "insert"}:
                added.extend(current.segments[new_start:new_end])

        size_ratio = min(previous.content_length, current.content_length) / max(
            previous.content_length, current.content_length
        )
        review_reason: str | None = None
        status = "ready"
        locales = {
            _render_locale("\n".join(previous.segments)),
            _render_locale("\n".join(current.segments)),
        }
        if locales == {"cjk", "latin"}:
            status = "excluded_locale_switch"
            review_reason = (
                "同一来源在中文与英文渲染之间切换；已从政策变更队列排除。"
                " / The same source switched between Chinese and English rendering; "
                "it was excluded from the policy-change queue."
            )
        elif size_ratio < self._MIN_SIZE_RATIO_FOR_AUTO_READY:
            status = "review_required"
            review_reason = (
                "页面渲染后的文章长度变化超过 35%，这可能是不完整的动态页面渲染，而不是实质政策编辑。"
                " / Rendered article length changed by more than 35%; this may be an incomplete "
                "dynamic-page render rather than a substantive policy edit."
            )

        return PolicyChangeEvent(
            source_id=current.source_id,
            source_url=current.source_url,
            title=current.title,
            detected_at=current.fetched_at,
            previous_snapshot_path=previous.path,
            current_snapshot_path=current.path,
            previous_content_hash=previous.content_hash,
            current_content_hash=current.content_hash,
            added_segments=tuple(added[: self._MAX_EXCERPTS_PER_SIDE]),
            removed_segments=tuple(removed[: self._MAX_EXCERPTS_PER_SIDE]),
            verification_status=status,
            review_reason=review_reason,
        )


class PolicyChangeStore:
    """SQLite event log used by the dashboard and MCP tools.

    The unique ``(source_id, current_snapshot_path)`` key makes historical
    backfills and scheduled updates idempotent.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS policy_change_events (
            event_id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id                   TEXT NOT NULL,
            source_url                  TEXT NOT NULL,
            title                       TEXT NOT NULL,
            detected_at                 TEXT NOT NULL,
            previous_snapshot_path      TEXT NOT NULL,
            current_snapshot_path       TEXT NOT NULL,
            previous_content_hash       TEXT NOT NULL,
            current_content_hash        TEXT NOT NULL,
            added_segments_json         TEXT NOT NULL,
            removed_segments_json       TEXT NOT NULL,
            verification_status         TEXT NOT NULL,
            review_reason               TEXT,
            review_outcome              TEXT,
            review_note                 TEXT,
            reviewed_at                 TEXT,
            UNIQUE(source_id, current_snapshot_path)
        )
    """

    _REVIEW_OUTCOMES = {"monitor", "notify", "dismiss"}

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(self._SCHEMA)
            existing_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(policy_change_events)").fetchall()
            }
            for name, definition in {
                "review_outcome": "TEXT",
                "review_note": "TEXT",
                "reviewed_at": "TEXT",
            }.items():
                if name not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE policy_change_events ADD COLUMN {name} {definition}"
                    )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._db_path))
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _from_row(row: sqlite3.Row) -> PolicyChangeEvent:
        detected_at = datetime.fromisoformat(row["detected_at"])
        if detected_at.tzinfo is None:
            detected_at = detected_at.replace(tzinfo=timezone.utc)
        reviewed_at = row["reviewed_at"]
        parsed_reviewed_at = (
            datetime.fromisoformat(reviewed_at).astimezone(timezone.utc)
            if reviewed_at
            else None
        )
        return PolicyChangeEvent(
            event_id=int(row["event_id"]),
            source_id=str(row["source_id"]),
            source_url=str(row["source_url"]),
            title=str(row["title"]),
            detected_at=detected_at,
            previous_snapshot_path=str(row["previous_snapshot_path"]),
            current_snapshot_path=str(row["current_snapshot_path"]),
            previous_content_hash=str(row["previous_content_hash"]),
            current_content_hash=str(row["current_content_hash"]),
            added_segments=tuple(json.loads(row["added_segments_json"])),
            removed_segments=tuple(json.loads(row["removed_segments_json"])),
            verification_status=str(row["verification_status"]),
            review_reason=row["review_reason"],
            review_outcome=row["review_outcome"],
            review_note=row["review_note"],
            reviewed_at=parsed_reviewed_at,
        )

    def record(self, event: PolicyChangeEvent) -> PolicyChangeEvent:
        """Store an event once and return it with its stable event id."""
        values = (
            event.source_id,
            event.source_url,
            event.title,
            event.detected_at.isoformat(),
            event.previous_snapshot_path,
            event.current_snapshot_path,
            event.previous_content_hash,
            event.current_content_hash,
            json.dumps(event.added_segments, ensure_ascii=False),
            json.dumps(event.removed_segments, ensure_ascii=False),
            event.verification_status,
            event.review_reason,
        )
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO policy_change_events (
                    source_id, source_url, title, detected_at,
                    previous_snapshot_path, current_snapshot_path,
                    previous_content_hash, current_content_hash,
                    added_segments_json, removed_segments_json,
                    verification_status, review_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM policy_change_events WHERE source_id = ? AND current_snapshot_path = ?",
                (event.source_id, event.current_snapshot_path),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._from_row(row)

    def list_events(
        self,
        *,
        source_id: str | None = None,
        verification_status: str | None = None,
        limit: int = 50,
    ) -> list[PolicyChangeEvent]:
        clauses: list[str] = []
        values: list[object] = []
        if source_id:
            clauses.append("source_id = ?")
            values.append(source_id)
        if verification_status:
            clauses.append("verification_status = ?")
            values.append(verification_status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(limit, 200)))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM policy_change_events{where} ORDER BY detected_at DESC, event_id DESC LIMIT ?",
                values,
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, event_id: int) -> PolicyChangeEvent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM policy_change_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def record_review(
        self, event_id: int, *, outcome: str, note: str | None = None
    ) -> PolicyChangeEvent:
        """Persist a human triage decision without overwriting the detector result."""
        if outcome not in self._REVIEW_OUTCOMES:
            choices = ", ".join(sorted(self._REVIEW_OUTCOMES))
            raise ValueError(f"Unknown review outcome '{outcome}'. Choose one of: {choices}.")

        reviewed_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE policy_change_events
                SET review_outcome = ?, review_note = ?, reviewed_at = ?
                WHERE event_id = ?
                """,
                (outcome, note.strip() if note else None, reviewed_at, event_id),
            )
            row = connection.execute(
                "SELECT * FROM policy_change_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            connection.commit()
        if row is None:
            raise ValueError(f"Unknown policy change event: {event_id}")
        return self._from_row(row)

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT verification_status, COUNT(*) AS count FROM policy_change_events GROUP BY verification_status"
            ).fetchall()
        counts = {
            "total": 0,
            "ready": 0,
            "review_required": 0,
            "excluded_locale_switch": 0,
            "actionable": 0,
            "reviewed": 0,
            "pending_review": 0,
        }
        for row in rows:
            status = str(row["verification_status"])
            count = int(row["count"])
            counts["total"] += count
            counts[status] = count
        counts["actionable"] = counts["ready"] + counts["review_required"]
        with closing(self._connect()) as connection:
            reviewed = connection.execute(
                "SELECT COUNT(*) AS count FROM policy_change_events WHERE review_outcome IS NOT NULL"
            ).fetchone()
            pending = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM policy_change_events
                WHERE review_outcome IS NULL
                  AND verification_status IN ('ready', 'review_required')
                """
            ).fetchone()
        counts["reviewed"] = int(reviewed["count"] if reviewed is not None else 0)
        counts["pending_review"] = int(pending["count"] if pending is not None else 0)
        return counts

    def exclude_locale_switch_events(self, engine: PolicyDiffEngine | None = None) -> int:
        """Reclassify historical translated-page diffs without touching human reviews."""
        diff_engine = engine or PolicyDiffEngine()
        reclassified = 0
        for event in self.list_events(limit=200):
            if event.review_outcome is not None or event.verification_status == "excluded_locale_switch":
                continue
            if not diff_engine.is_locale_switch(
                event.previous_snapshot_path, event.current_snapshot_path
            ):
                continue
            reason = (
                "同一来源在中文与英文渲染之间切换；已从政策变更队列排除。"
                " / The same source switched between Chinese and English rendering; "
                "it was excluded from the policy-change queue."
            )
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    UPDATE policy_change_events
                    SET verification_status = ?, review_reason = ?
                    WHERE event_id = ?
                    """,
                    ("excluded_locale_switch", reason, event.event_id),
                )
                connection.commit()
            reclassified += 1
        return reclassified


def backfill_change_events(
    snapshot_root: str | Path,
    store: PolicyChangeStore,
    engine: PolicyDiffEngine | None = None,
) -> list[PolicyChangeEvent]:
    """Create idempotent events from existing snapshots without re-crawling.

    A single early partial render must never be silently presented as a policy
    update.  ``PolicyDiffEngine`` flags the observed large render gaps as
    ``review_required`` instead of claiming an automatic policy change.
    """
    root = Path(snapshot_root)
    diff_engine = engine or PolicyDiffEngine()
    recorded: list[PolicyChangeEvent] = []
    if not root.exists():
        return recorded

    for source_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        snapshots = sorted(source_dir.glob("*.html"))
        previous_path: Path | None = None
        for current_path in snapshots:
            if previous_path is not None:
                event = diff_engine.compare(previous_path, current_path)
                if event is not None:
                    recorded.append(store.record(event))
            previous_path = current_path
    return recorded


def format_event(event: PolicyChangeEvent, *, include_excerpts: bool = True) -> str:
    """Render a concise, citation-ready event for an MCP text response."""
    lines = [
        f"## Policy change #{event.event_id or '-'}",
        f"**Source:** {event.title} ({event.source_id})",
        f"**Official URL:** {event.source_url}",
        f"**Captured:** {event.detected_at.isoformat()}",
        f"**Status:** {event.verification_status}",
        f"**Summary:** {event.summary}",
    ]
    if include_excerpts:
        if event.removed_segments:
            lines.extend(["", "### Earlier evidence"])
            lines.extend(f"- {segment}" for segment in event.removed_segments)
        if event.added_segments:
            lines.extend(["", "### Latest evidence"])
            lines.extend(f"- {segment}" for segment in event.added_segments)
    return "\n".join(lines)


__all__ = [
    "PolicyChangeEvent",
    "PolicyChangeStore",
    "PolicyDiffEngine",
    "SnapshotVersion",
    "backfill_change_events",
    "format_event",
]
