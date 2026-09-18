"""Stable identifier extraction for Amazon Seller Central help pages.

Each Amazon help page has a URL of the form:
    https://sellercentral.amazon.com/help/hub/reference/external/<ID>

The <ID> segment is stable across content edits (verified against the legacy
crawl), so it is the right choice for a content-independent doc identifier
in the ingestion pipeline. Using URL-derived IDs (instead of file SHA256)
is what enables incremental replacement: when the page content changes,
the doc_id stays the same and we can delete + reinsert chunks under the
same key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_ID_PATTERN = re.compile(r"external/([A-Z0-9]+)")


def source_id_from_url(url: str) -> str:
    """Extract the stable source_id from an Amazon help URL.

    Raises ValueError if the URL does not match the expected pattern.
    """
    match = _ID_PATTERN.search(url)
    if not match:
        raise ValueError(f"URL does not contain external/<ID>: {url}")
    return match.group(1)


@dataclass(frozen=True)
class Source:
    source_id: str
    url: str
    legacy_md_filename: str | None = None


class SourceRegistry:
    """Loads the curated list of Amazon help pages to track."""

    def __init__(self, sources: list[Source]):
        self._sources = sources
        self._by_id = {s.source_id: s for s in sources}

    @classmethod
    def load(cls, registry_path: str | Path) -> "SourceRegistry":
        path = Path(registry_path)
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        sources = [
            Source(
                source_id=item["source_id"],
                url=item["url"],
                legacy_md_filename=item.get("legacy_md_filename"),
            )
            for item in data.get("sources", [])
        ]
        return cls(sources)

    def all(self) -> list[Source]:
        return list(self._sources)

    def get(self, source_id: str) -> Source:
        return self._by_id[source_id]

    def __len__(self) -> int:
        return len(self._sources)
