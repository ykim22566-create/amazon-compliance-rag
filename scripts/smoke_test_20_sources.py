"""20-source smoke test covering all compliance categories.

Goal: estimate failure-rate and per-page time before committing to the
full 727-page crawl. Picks pages from every failure-mode bucket in the
15-question evaluation set:

    Fees / surcharges (5)        : A1-A5 area
    Returns / inbound (3)        : touches B5 / refund flows
    Tax (2)                      : B3 / digital services tax
    Account health (2)           : B4 / C4 (AHR / AHA)
    Appeals (2)                  : C2 / C3
    Dangerous goods (2)          : C5 / A3 area
    Policy / merchant ops (4)    : C1 + general program policies
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


# Mix of pages with: long fee tables, multi-tier headers, prose-only,
# tax FAQs, account-status thresholds, appeal procedures, DG status,
# program-policy boilerplate. Diverse enough to flag generic parser bugs.
TARGET_IDS = [
    # Fees / surcharges (heavy tables)
    "GMUTB89XM7AATPR3",  # 2026 US Low Price FBA fulfillment fee
    "GC3Q44PBK8BXQW3Z",  # FBA inbound placement service fee
    "GZGEQLTM3RZXUV6T",  # 2026 returns processing fee changes
    "G201074400",        # FBA features, services, and fees
    "G181",              # Selling on Amazon fee schedule (top doc)
    # Tax
    "GNLQSJBH428VYL69",  # Singapore GST FAQ
    # Account health
    "G200205250",        # Account Health Rating program policy
    # Appeals
    "G41",               # Appeal an account deactivation/listing removal (top doc)
    # Operations / merchant identity (prose-only sanity)
    "G48321",            # Your merchant token
    "G201817090",        # Selected from old top-articles seed
    # Program policies (broad coverage)
    "G521",              # Program policies index
    "G2",                # Help home (will likely be light prose)
    "G201062890",        # From old top-articles seed
    "G201542150",        # From old top-articles seed
    "GSNV3657R94YP9DZ",  # From old top-articles seed
    "GC3QAQ8CCXUG5RE4",  # From old top-articles seed
    "G28141",            # From old top-articles seed
    "G43381",            # From old top-articles seed
    "G69126",            # From old top-articles seed
    "G53921",            # From old top-articles seed
]


def main(headless: bool = True):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    settings = load_settings(str(ROOT / "config" / "settings.yaml"))
    pipeline = UpdatePipeline(
        settings,
        registry_path=str(ROOT / "data" / "amazon" / "registry" / "source_registry.yaml"),
        headless=headless,
    )
    selected: list[Source] = []
    missing: list[str] = []
    for sid in TARGET_IDS:
        try:
            selected.append(pipeline.registry.get(sid))
        except KeyError:
            missing.append(sid)
    if missing:
        print(f"⚠️ {len(missing)} ids not in registry (will skip): {missing}")
    print(f"Running on {len(selected)} sources …\n")

    report = pipeline.run(sources=selected, progress_every=5)

    print("\n=== 20-source smoke report ===")
    for o in report.outcomes:
        marker = "✓" if o.status == "ingested" else ("=" if o.status == "unchanged" else "✗")
        line = f"  {marker} [{o.status:9s}] {o.source_id:22s} chunks={o.chunks:4d} rows={o.rows:4d} dur={o.duration_s:5.1f}s"
        if o.error:
            line += f"\n      error: {o.error}"
        print(line)
    durs = [o.duration_s for o in report.outcomes if o.status != "error"]
    avg = sum(durs) / len(durs) if durs else 0.0
    print(f"\n  ingested={report.ingested}  unchanged={report.unchanged}  errors={report.errors}")
    print(f"  avg seconds/page (excl errors): {avg:.1f}")
    if report.total > 0:
        full_run_min = avg * 727 / 60.0
        print(f"  → projected full-727 run time: {full_run_min:.1f} min")


if __name__ == "__main__":
    headless = "--no-headless" not in sys.argv
    main(headless=headless)
