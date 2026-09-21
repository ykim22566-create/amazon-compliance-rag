#!/usr/bin/env python
"""Weekly full re-crawl of the Seller Central corpus.

Walks every source in the registry, re-renders it, and compares a
normalized content hash against the last one seen. Only pages that
actually moved are re-chunked and re-embedded — over a recent 4.5-month
window that was 298 of 727 pages, so the hash check is what keeps a full
sweep affordable.

Each run drops a report in data/amazon/runs/ for the dashboard to read.

Usage:
    python scripts/weekly_crawl.py                 # full sweep
    python scripts/weekly_crawl.py --max-sources 5 # smoke run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.amazon_compliance.orchestrator import UpdatePipeline
from src.core.settings import load_settings

RUNS_DIR = _REPO_ROOT / "data" / "amazon" / "runs"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="data/amazon/registry/source_registry.yaml")
    parser.add_argument("--settings", default=None)
    parser.add_argument("--max-sources", type=int, default=None)
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    settings = load_settings(args.settings)
    pipeline = UpdatePipeline(
        settings,
        registry_path=args.registry,
        headless=not args.no_headless,
    )
    report = pipeline.run(max_sources=args.max_sources)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS_DIR / f"run_{stamp}.json"
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n=== 全量更新完成 ===")
    print(f"  扫描:   {report.total}")
    print(f"  有变化: {report.ingested}")
    print(f"  未变化: {report.unchanged}")
    print(f"  失败:   {report.errors}")
    print(f"  报告:   {path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
