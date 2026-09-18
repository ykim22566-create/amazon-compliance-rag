"""Re-ingest only the snapshots whose pages contain at least one table.

Usage: when html_table_parser's renderer changes (e.g. adding the
Category: NON-APPAREL / APPAREL / DANGEROUS-GOODS prefix), we need the
chunks to reflect the new rendering. We do NOT need to re-crawl — the
raw HTML snapshots are already on disk under data/amazon/snapshots/.
This script:
    1. Scans every saved snapshot.
    2. Runs HtmlLoader on the latest snapshot per source_id.
    3. Ingests via AmazonIngestor only if the parser found tables
       (pages without tables are unaffected by the renderer change).
    4. Skips pages whose content_hash hasn't changed.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.amazon_compliance.amazon_ingest import AmazonIngestor
from src.amazon_compliance.change_detector import ChangeDetector
from src.core.settings import load_settings, resolve_path
from src.libs.loader.html_loader import HtmlLoader

import logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s — %(message)s")


def main():
    settings = load_settings(str(ROOT / "config" / "settings.yaml"))
    ingestor = AmazonIngestor(settings, collection="amazon_compliance")
    loader = HtmlLoader()
    change_detector = ChangeDetector(resolve_path("data/amazon/change_log.sqlite3"))

    snap_root = resolve_path("data/amazon/snapshots")
    source_dirs = sorted(p for p in snap_root.iterdir() if p.is_dir())
    print(f"Scanning {len(source_dirs)} source directories...")

    table_pages = 0
    re_ingested = 0
    skipped_no_tables = 0
    skipped_unchanged = 0
    errors = 0
    t0 = time.monotonic()

    for d in source_dirs:
        snapshots = sorted(d.glob("*.html"))
        if not snapshots:
            continue
        latest = snapshots[-1]
        try:
            doc = loader.load(latest)
        except Exception as exc:
            errors += 1
            print(f"  ERROR loading {d.name}: {exc}")
            continue

        if doc.metadata.get("table_count", 0) == 0:
            skipped_no_tables += 1
            continue

        table_pages += 1
        new_hash = doc.metadata["content_hash"]
        prior = change_detector.get(d.name)
        if prior is not None and prior.last_content_hash == new_hash:
            # Hash didn't change — but we changed the RENDERER, so we
            # explicitly want to re-ingest anyway. Don't skip on hash;
            # only skip if there are no tables.
            pass

        try:
            result = ingestor.ingest_document(doc)
            re_ingested += 1
            change_detector.record(d.name, new_hash)
            if re_ingested % 5 == 0:
                elapsed = time.monotonic() - t0
                print(f"  ... re-ingested {re_ingested}  ({elapsed:.1f}s, last={d.name} chunks={result.chunk_count})")
        except Exception as exc:
            errors += 1
            print(f"  ERROR ingesting {d.name}: {exc}")

    print()
    print(f"=== Re-ingest report ===")
    print(f"  source dirs scanned:    {len(source_dirs)}")
    print(f"  pages with tables:      {table_pages}")
    print(f"  re-ingested:            {re_ingested}")
    print(f"  skipped (no tables):    {skipped_no_tables}")
    print(f"  errors:                 {errors}")
    print(f"  total time:             {time.monotonic() - t0:.1f}s")


if __name__ == "__main__":
    main()
