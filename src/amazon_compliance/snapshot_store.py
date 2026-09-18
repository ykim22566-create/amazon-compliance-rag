"""On-disk store for raw Amazon help-page HTML snapshots.

Layout:
    data/amazon/snapshots/<source_id>/<UTC-timestamp>.html

Why store raw HTML at all (legacy crawler did not):
    - reproducibility for the thesis (we can replay parsing on the same input)
    - the legacy library lost the original HTML, so flattened tables could
      never be recovered without a fresh crawl. We will not repeat that mistake.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class SnapshotRef:
    source_id: str
    fetched_at: datetime
    path: Path


class SnapshotStore:
    def __init__(self, root: str | Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def save(self, source_id: str, html: str, fetched_at: datetime | None = None) -> SnapshotRef:
        ts = fetched_at or datetime.now(timezone.utc)
        dir_ = self._root / source_id
        dir_.mkdir(parents=True, exist_ok=True)
        filename = ts.strftime("%Y%m%dT%H%M%SZ") + ".html"
        path = dir_ / filename
        path.write_text(html, encoding="utf-8")
        return SnapshotRef(source_id=source_id, fetched_at=ts, path=path)

    def latest(self, source_id: str) -> SnapshotRef | None:
        dir_ = self._root / source_id
        if not dir_.exists():
            return None
        snapshots = sorted(dir_.glob("*.html"))
        if not snapshots:
            return None
        path = snapshots[-1]
        ts_str = path.stem
        try:
            fetched_at = datetime.strptime(ts_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            fetched_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return SnapshotRef(source_id=source_id, fetched_at=fetched_at, path=path)
