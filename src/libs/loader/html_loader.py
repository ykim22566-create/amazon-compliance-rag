"""HTML loader for Amazon Seller Central help pages.

Plugs into the existing BaseLoader contract so the standard IngestionPipeline
can ingest HTML the same way it ingests PDF. The expensive part — extracting
structured rows from tables — is delegated to amazon_compliance.parser.

Output Document layout:
    text:
        page title
        prose body (tables stripped — they are surfaced as structured rows)
        a row-marker section so the natural-language table rows can be
        chunked one-per-Chunk by the modified DocumentChunker.
    metadata:
        source_path:       path of the HTML file
        source_url:        the original URL (when available)
        source_id:         Amazon help-page ID (e.g. GMUTB89XM7AATPR3)
        doc_type:          "html"
        title:             <title> text
        content_hash:      SHA1 of normalized article text + tables
        structured_rows:   list of {table_id, section_title, headers,
                                    cells, rendered_text}
        table_count:       number of tables extracted
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from src.amazon_compliance.parser.html_table_parser import (
    StructuredRow,
    parse_help_html,
)
from src.amazon_compliance.source_id import source_id_from_url
from src.core.types import Document
from src.libs.loader.base_loader import BaseLoader

ROW_MARKER_OPEN = "<<<AMZ_ROW_START>>>"
ROW_MARKER_CLOSE = "<<<AMZ_ROW_END>>>"


def _normalize_for_hash(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _content_hash(prose: str, rows: list[StructuredRow]) -> str:
    h = hashlib.sha1()
    h.update(_normalize_for_hash(prose).encode("utf-8"))
    for r in rows:
        h.update(b"|")
        h.update(_normalize_for_hash(r.render_as_text()).encode("utf-8"))
    return h.hexdigest()


class HtmlLoader(BaseLoader):
    """Load an Amazon help-page HTML file into a Document.

    The file is expected to be a snapshot saved by SnapshotStore. The
    URL/source_id is recovered from a sidecar .meta.json next to the HTML
    file; if that's absent we fall back to deriving the source_id from
    the snapshot's parent directory name (which IS the source_id by
    SnapshotStore's layout convention).
    """

    def __init__(self, default_url_prefix: str = "https://sellercentral.amazon.com/help/hub/reference/external/"):
        self._default_url_prefix = default_url_prefix

    def load(self, file_path: str | Path) -> Document:
        path = self._validate_file(file_path)
        html = path.read_text(encoding="utf-8")

        meta_path = path.with_suffix(path.suffix + ".meta.json")
        sidecar: dict = {}
        if meta_path.exists():
            try:
                sidecar = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                sidecar = {}

        source_id = sidecar.get("source_id") or path.parent.name
        url = sidecar.get("url") or f"{self._default_url_prefix}{source_id}"

        try:
            source_id = source_id_from_url(url)
        except ValueError:
            pass

        page = parse_help_html(html, source_id=source_id)

        rendered_rows = [r.render_as_text() for table in page.tables for r in table.rows]
        text_parts: list[str] = []
        if page.title:
            text_parts.append(f"# {page.title}")
        if page.prose_text:
            text_parts.append(page.prose_text)
        for rendered in rendered_rows:
            text_parts.append(f"{ROW_MARKER_OPEN}\n{rendered}\n{ROW_MARKER_CLOSE}")
        full_text = "\n\n".join(text_parts)

        flat_rows = []
        for table in page.tables:
            for r in table.rows:
                flat_rows.append({
                    "table_id": r.table_id,
                    "row_index": r.row_index,
                    "section_title": r.section_title,
                    "table_title": r.table_title,
                    "headers": list(r.cells.keys()),
                    "cells": dict(r.cells),
                    "rendered_text": r.render_as_text(),
                })

        prose_for_hash = (page.title or "") + "\n" + (page.prose_text or "")
        all_rows = [r for t in page.tables for r in t.rows]
        c_hash = _content_hash(prose_for_hash, all_rows)

        metadata = {
            # source_path is hashed into chunk_id by VectorUpserter, so it
            # MUST be stable across re-fetches of the same page — using the
            # URL (not the timestamped snapshot path) keeps chunk_ids stable.
            "source_path": url,
            "snapshot_path": str(path),
            "source_url": url,
            "source_id": source_id,
            # doc_hash mirrors source_id so cleanup-by-metadata across
            # ChromaDB / BM25 / ImageStorage uses the same key as PDF flow.
            "doc_hash": source_id,
            "doc_type": "html",
            "title": page.title or sidecar.get("title", ""),
            "content_hash": c_hash,
            "structured_rows": flat_rows,
            "table_count": len(page.tables),
            "row_count": len(rendered_rows),
        }
        if sidecar.get("fetched_at"):
            metadata["fetched_at"] = sidecar["fetched_at"]

        return Document(id=source_id, text=full_text, metadata=metadata)


__all__ = ["HtmlLoader", "ROW_MARKER_OPEN", "ROW_MARKER_CLOSE"]
