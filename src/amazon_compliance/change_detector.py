"""Per-source change registry: last seen content hash + timestamp.

Why a separate hash registry instead of relying on MODULAR's existing
SQLiteIntegrityChecker:
    - SQLiteIntegrityChecker keys on file SHA256, which changes on every
      Selenium re-render even when the meaningful content has not changed.
    - We need to key on a stable source_id (URL-derived) and compare a
      *normalized* content hash (article text + structured rows) instead.

This is intentionally tiny: just SQLite, three columns, one method that
matters. Keeping it small makes it easy to reason about during the thesis
demo: "we recompute the rendered article hash and compare to the stored
one; if different, we re-ingest under the same source_id."
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ChangeRecord:
    source_id: str
    last_content_hash: str
    last_seen_at: datetime
    last_changed_at: datetime


class ChangeDetector:
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS change_log (
            source_id        TEXT PRIMARY KEY,
            content_hash     TEXT NOT NULL,
            last_seen_at     TEXT NOT NULL,
            last_changed_at  TEXT NOT NULL,
            change_count     INTEGER NOT NULL DEFAULT 0
        )
    """

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute(self.SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path))

    def get(self, source_id: str) -> ChangeRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT source_id, content_hash, last_seen_at, last_changed_at "
                "FROM change_log WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        if row is None:
            return None
        return ChangeRecord(
            source_id=row[0],
            last_content_hash=row[1],
            last_seen_at=datetime.fromisoformat(row[2]),
            last_changed_at=datetime.fromisoformat(row[3]),
        )

    def has_changed(self, source_id: str, new_content_hash: str) -> bool:
        record = self.get(source_id)
        return record is None or record.last_content_hash != new_content_hash

    def record(self, source_id: str, new_content_hash: str, *, now: datetime | None = None) -> bool:
        """Persist that we just saw `source_id` with `new_content_hash`.

        Returns True if this counted as a change (new or different hash),
        False if it was an unchanged re-observation.
        """
        ts = (now or datetime.now(timezone.utc)).isoformat()
        with closing(self._connect()) as conn:
            existing = conn.execute(
                "SELECT content_hash, change_count FROM change_log WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO change_log (source_id, content_hash, last_seen_at, last_changed_at, change_count) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (source_id, new_content_hash, ts, ts),
                )
                conn.commit()
                return True
            old_hash, change_count = existing
            if old_hash == new_content_hash:
                conn.execute(
                    "UPDATE change_log SET last_seen_at = ? WHERE source_id = ?",
                    (ts, source_id),
                )
                conn.commit()
                return False
            conn.execute(
                "UPDATE change_log SET content_hash = ?, last_seen_at = ?, "
                "last_changed_at = ?, change_count = ? WHERE source_id = ?",
                (new_content_hash, ts, ts, change_count + 1, source_id),
            )
            conn.commit()
            return True

    def all_seen(self) -> list[ChangeRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT source_id, content_hash, last_seen_at, last_changed_at FROM change_log"
            ).fetchall()
        return [
            ChangeRecord(
                source_id=r[0],
                last_content_hash=r[1],
                last_seen_at=datetime.fromisoformat(r[2]),
                last_changed_at=datetime.fromisoformat(r[3]),
            )
            for r in rows
        ]
