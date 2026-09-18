"""Targeted re-ingest for Amazon sources with repeated local snapshots.

Reads source_ids from a text file or repeated --source-id flags, then runs
the existing UpdatePipeline only for those selected pages.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from src.amazon_compliance.source_id import Source, SourceRegistry


DEFAULT_TARGETS = ROOT / "data" / "amazon" / "reingest_targets_2026-04-30.txt"
DEFAULT_REGISTRY = ROOT / "data" / "amazon" / "registry" / "source_registry.yaml"
DEFAULT_SETTINGS = ROOT / "config" / "settings.yaml"


def _load_target_ids(targets_file: Path, explicit_ids: list[str]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()

    if targets_file.exists() and not explicit_ids:
        for line in targets_file.read_text(encoding="utf-8").splitlines():
            sid = line.strip()
            if not sid or sid.startswith("#"):
                continue
            if sid not in seen:
                ids.append(sid)
                seen.add(sid)

    for sid in explicit_ids:
        sid = sid.strip()
        if sid and sid not in seen:
            ids.append(sid)
            seen.add(sid)

    return ids


def _select_sources(registry: "SourceRegistry", source_ids: list[str]) -> tuple[list["Source"], list[str]]:
    selected: list["Source"] = []
    missing: list[str] = []

    for sid in source_ids:
        try:
            selected.append(registry.get(sid))
        except KeyError:
            missing.append(sid)

    return selected, missing


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-ingest selected Amazon help pages")
    parser.add_argument(
        "--targets-file",
        default=str(DEFAULT_TARGETS),
        help="Text file containing one source_id per line",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="Additional source_id to include; may be repeated",
    )
    parser.add_argument(
        "--registry",
        default=str(DEFAULT_REGISTRY),
        help="Path to Amazon source registry YAML",
    )
    parser.add_argument(
        "--settings",
        default=str(DEFAULT_SETTINGS),
        help="Path to settings.yaml",
    )
    parser.add_argument(
        "--report-path",
        default="",
        help="Optional JSON path to save the run report",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selected sources without running fetch/ingest",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser fetches with a visible Chrome window",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    from src.amazon_compliance.source_id import SourceRegistry

    registry = SourceRegistry.load(args.registry)
    target_ids = _load_target_ids(Path(args.targets_file), args.source_id)
    if not target_ids:
        print("No target source_ids found; abort.")
        return 1

    selected, missing = _select_sources(registry, target_ids)
    if missing:
        print("Missing from registry:")
        for sid in missing:
            print(f"  - {sid}")

    if not selected:
        print("No valid targets in registry; abort.")
        return 1

    print(f"Selected {len(selected)} sources:")
    for src in selected:
        print(f"  - {src.source_id}  {src.url}")

    if args.dry_run:
        return 0

    from src.amazon_compliance.orchestrator import UpdatePipeline
    from src.core.settings import load_settings

    settings = load_settings(args.settings)
    pipeline = UpdatePipeline(
        settings,
        registry_path=args.registry,
        headless=not args.no_headless,
    )
    report = pipeline.run(sources=selected, progress_every=1)

    if args.report_path:
        report_path = Path(args.report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nWrote report to {report_path}")

    print("\n=== Re-ingest report ===")
    for outcome in report.outcomes:
        print(
            f"  [{outcome.status:9s}] {outcome.source_id:20s} "
            f"chunks={outcome.chunks:3d} rows={outcome.rows:3d} dur={outcome.duration_s:5.1f}s"
        )
        if outcome.error:
            print(f"      error: {outcome.error}")

    print(
        f"\n  total={report.total}  ingested={report.ingested}  "
        f"unchanged={report.unchanged}  errors={report.errors}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
