"""End-to-end smoke test: fetch + parse + ingest 3 critical Amazon help pages.

Picks 3 source_ids that cover the riskiest cases in our 15-question
evaluation set:
    - GMUTB89XM7AATPR3 : 2026 US Low Price FBA fulfillment fee (A1, A2, B2,
                          and the page where the 3.5% surcharge appears)
    - GC3Q44PBK8BXQW3Z : FBA inbound placement service fee (A5, with tables)
    - G48321           : Your merchant token (C1, prose-only sanity check)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.amazon_compliance.orchestrator import UpdatePipeline
from src.amazon_compliance.source_id import Source
from src.core.settings import load_settings


TARGET_IDS = ["GMUTB89XM7AATPR3", "GC3Q44PBK8BXQW3Z", "G48321"]


def main(headless: bool = True):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    settings = load_settings(str(ROOT / "config" / "settings.yaml"))
    pipeline = UpdatePipeline(
        settings,
        registry_path=str(ROOT / "data" / "amazon" / "registry" / "source_registry.yaml"),
        headless=headless,
    )
    selected: list[Source] = []
    for sid in TARGET_IDS:
        try:
            selected.append(pipeline.registry.get(sid))
        except KeyError:
            print(f"⚠️ source_id {sid} not in registry, skipping")
    if not selected:
        print("No targets in registry; abort.")
        return

    report = pipeline.run(sources=selected)
    print("\n=== Smoke test report ===")
    for o in report.outcomes:
        print(f"  [{o.status:9s}] {o.source_id:20s} chunks={o.chunks:3d} rows={o.rows:3d} dur={o.duration_s:5.1f}s")
        if o.error:
            print(f"      error: {o.error}")
    print(f"\n  ingested={report.ingested}  unchanged={report.unchanged}  errors={report.errors}")


if __name__ == "__main__":
    headless = "--no-headless" not in sys.argv
    main(headless=headless)
