"""End-to-end Amazon compliance update loop.

For each source in the registry:
    1. Fetch the rendered article via Selenium (HelpFetcher)
    2. Save the raw HTML snapshot (SnapshotStore)
    3. Load + parse + structure the snapshot (HtmlLoader)
    4. Compare the new content_hash with the last seen hash (ChangeDetector)
       - unchanged + already in vector store  -> skip ingest, just update last_seen
       - new or changed                       -> run AmazonIngestor (delete-replace)
    5. Persist the new hash + change timestamp

Designed to be re-run safely: any subset of sources can be processed,
and a partial run leaves the registry/snapshot/index in a consistent
state thanks to the per-source delete-then-replace at the ingest layer.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.amazon_compliance.amazon_ingest import AmazonIngestor, IngestResult
from src.amazon_compliance.change_detector import ChangeDetector
from src.amazon_compliance.help_fetcher import HelpFetcher
from src.amazon_compliance.snapshot_store import SnapshotStore
from src.amazon_compliance.source_id import Source, SourceRegistry
from src.core.settings import Settings, load_settings, resolve_path
from src.libs.loader.html_loader import HtmlLoader

logger = logging.getLogger(__name__)


@dataclass
class SourceOutcome:
    source_id: str
    url: str
    status: str  # "ingested" | "unchanged" | "error"
    content_hash: str | None = None
    chunks: int = 0
    rows: int = 0
    duration_s: float = 0.0
    error: str | None = None


@dataclass
class RunReport:
    started_at: datetime
    finished_at: datetime
    total: int
    ingested: int
    unchanged: int
    errors: int
    outcomes: list[SourceOutcome] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "total": self.total,
            "ingested": self.ingested,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "outcomes": [o.__dict__ for o in self.outcomes],
        }


class UpdatePipeline:
    def __init__(
        self,
        settings: Settings,
        registry_path: str | Path,
        snapshot_root: str | Path | None = None,
        change_db_path: str | Path | None = None,
        collection: str = "amazon_compliance",
        headless: bool = True,
    ):
        # Wires together every collaborator the loop will need. Each is a
        # standalone, testable module — the orchestrator just sequences them.
        self.settings = settings
        self.collection = collection
        # Registry: 727 (source_id, url) pairs from data/amazon/registry/source_registry.yaml
        self.registry = SourceRegistry.load(registry_path)
        # Snapshot store: writes raw HTML + .meta.json to data/amazon/snapshots/<sid>/...
        self.snapshots = SnapshotStore(
            snapshot_root or resolve_path("data/amazon/snapshots")
        )
        # ChangeDetector: SQLite of (source_id -> last content_hash). The "did Amazon edit this page?" oracle.
        self.change_detector = ChangeDetector(
            change_db_path or resolve_path("data/amazon/change_log.sqlite3")
        )
        # Selenium-driven fetcher; one Chrome process is reused across all 727 fetches.
        self.fetcher = HelpFetcher(headless=headless)
        # Plugs into MODULAR's BaseLoader contract — turns an HTML file on disk into a Document.
        self.html_loader = HtmlLoader()
        # Custom ingest path: chunk + embed + cleanup-then-replace into Chroma + BM25.
        # Reuses MODULAR's encoders/storage but skips the LLM Refine/Enrich/Caption transforms.
        self.ingestor = AmazonIngestor(settings, collection=collection)
        logger.info(
            "UpdatePipeline ready: registry=%d sources, collection=%s, headless=%s",
            len(self.registry), collection, headless,
        )

    def _process_one(self, source: Source) -> SourceOutcome:
        """Run the full per-source pipeline once. Eight numbered stages below.

        Designed to be self-contained: any thrown exception is caught and
        wrapped in a SourceOutcome(status="error", error="<stage>: <msg>"),
        so a single failed page never aborts the surrounding 727-page loop.
        """
        t0 = time.monotonic()

        # ── Stage 1 ─ FETCH ────────────────────────────────────────────────
        # Render the SPA help page in real Chrome and grab its HTML string.
        # Failure modes caught here: network error, login redirect, captcha,
        # or the article element never reaching MIN_ARTICLE_CHARS within
        # wait_timeout (typical for index/landing pages with no real article).
        try:
            fetch = self.fetcher.fetch(source.url)
        except Exception as exc:
            return SourceOutcome(
                source_id=source.source_id,
                url=source.url,
                status="error",
                duration_s=time.monotonic() - t0,
                error=f"fetch: {exc}",
            )

        # ── Stage 2 ─ SNAPSHOT ────────────────────────────────────────────
        # Persist raw HTML so we can re-parse offline (e.g. when the parser
        # is improved post-hoc) without re-crawling. The legacy crawler
        # discarded HTML after html2text; we deliberately do not repeat that.
        # The .meta.json sidecar preserves URL/title/timestamp because the
        # HTML file alone has no notion of where it came from.
        snapshot = self.snapshots.save(
            source.source_id, fetch.html, fetched_at=fetch.fetched_at
        )
        meta_path = snapshot.path.with_suffix(snapshot.path.suffix + ".meta.json")
        meta_path.write_text(json.dumps({
            "source_id": source.source_id,
            "url": source.url,
            "title": fetch.title,
            "fetched_at": fetch.fetched_at.isoformat(),
            "article_text_length": fetch.article_text_length,
        }, ensure_ascii=False), encoding="utf-8")

        # ── Stage 3 ─ PARSE / LOAD ─────────────────────────────────────────
        # HtmlLoader does the structure-aware work:
        #   - find <article>, drop nav/footer/scripts
        #   - per <table>: rowspan/colspan grid expansion + multi-level header
        #     flattening + nearest-heading section_title
        #   - prose_text = article minus all tables (so prose chunks won't
        #     duplicate the row data and create retrieval noise)
        # Output: Document(id=source_id, text=title+prose+row-marker blocks,
        # metadata={source_url, source_id, doc_hash=source_id, content_hash,
        # structured_rows, table_count, ...}).
        try:
            document = self.html_loader.load(snapshot.path)
        except Exception as exc:
            return SourceOutcome(
                source_id=source.source_id,
                url=source.url,
                status="error",
                duration_s=time.monotonic() - t0,
                error=f"parse: {exc}",
            )

        # ── Stage 4 ─ HASH COMPARE ─────────────────────────────────────────
        # content_hash is computed by HtmlLoader as
        #     SHA1(normalized prose + every rendered structured row).
        # Normalization (lowercase + collapse whitespace) suppresses cosmetic
        # re-render diffs (timestamps, dynamic IDs in JS-injected DOM) so we
        # only treat *real* article changes as updates.
        new_hash = document.metadata["content_hash"]
        prior = self.change_detector.get(source.source_id)

        # ── Stage 5 ─ FAST PATH: UNCHANGED ─────────────────────────────────
        # If the page's hash matches what we last persisted, every downstream
        # chunk + embed + upsert step would be a no-op. We just refresh
        # last_seen_at and exit. This is what makes scheduled re-runs cheap.
        if prior is not None and prior.last_content_hash == new_hash:
            self.change_detector.record(source.source_id, new_hash)
            return SourceOutcome(
                source_id=source.source_id,
                url=source.url,
                status="unchanged",
                content_hash=new_hash,
                duration_s=time.monotonic() - t0,
            )

        # ── Stage 6 ─ INGEST (changed or first-time) ──────────────────────
        # AmazonIngestor does, in this order:
        #   6a. chunk_amazon_document(doc) — row blocks become 1 chunk each;
        #       prose splits with RecursiveCharacterTextSplitter.
        #   6b. cleanup of any prior chunks for this source_id:
        #         vector_store.delete_by_metadata({"doc_hash": source_id})
        #         bm25_indexer.remove_document(source_id, collection)
        #       This is the "delete-then-replace" pattern that makes updates
        #       idempotent (no orphan chunks accumulate over time).
        #   6c. encode via reused MODULAR BatchProcessor (dense + sparse).
        #   6d. write to Chroma (VectorUpserter) and BM25 (add_documents).
        try:
            ingest: IngestResult = self.ingestor.ingest_document(document)
        except Exception as exc:
            return SourceOutcome(
                source_id=source.source_id,
                url=source.url,
                status="error",
                content_hash=new_hash,
                duration_s=time.monotonic() - t0,
                error=f"ingest: {exc}",
            )

        # ── Stage 7 ─ COMMIT HASH ─────────────────────────────────────────
        # Only after ingest succeeds do we persist the new hash. If ingest
        # had failed, the prior hash stays in change_log so the next run
        # will retry this source (instead of falsely treating it as "seen").
        self.change_detector.record(source.source_id, new_hash)

        # ── Stage 8 ─ REPORT ──────────────────────────────────────────────
        return SourceOutcome(
            source_id=source.source_id,
            url=source.url,
            status="ingested",
            content_hash=new_hash,
            chunks=ingest.chunk_count,
            rows=ingest.row_chunks,
            duration_s=time.monotonic() - t0,
        )

    def run(
        self,
        sources: Iterable[Source] | None = None,
        max_sources: int | None = None,
        progress_every: int = 10,
    ) -> RunReport:
        """Drive the per-source pipeline over a list of sources.

        Strict serial execution by design — Selenium can't be safely shared
        across threads, and the embedding API is rate-limited at the proxy.
        For 727 sources this means ~14 s/page wall clock. Parallelizing
        fetch (Selenium) with ingest (embed + upsert) is a v2 optimization
        that could roughly halve wall-clock time without functional change.
        """
        started_at = datetime.now(timezone.utc)
        candidates = list(sources) if sources is not None else self.registry.all()
        if max_sources is not None:
            candidates = candidates[:max_sources]
        logger.info("Run starting on %d sources", len(candidates))

        outcomes: list[SourceOutcome] = []
        ingested = unchanged = errors = 0
        try:
            for i, src in enumerate(candidates, start=1):
                outcome = self._process_one(src)
                outcomes.append(outcome)
                if outcome.status == "ingested":
                    ingested += 1
                elif outcome.status == "unchanged":
                    unchanged += 1
                else:
                    errors += 1
                if i % progress_every == 0 or i == len(candidates):
                    logger.info(
                        "Progress %d/%d  (ingested=%d unchanged=%d errors=%d) last=%s status=%s",
                        i, len(candidates), ingested, unchanged, errors,
                        outcome.source_id, outcome.status,
                    )
        finally:
            self.fetcher.close()

        finished_at = datetime.now(timezone.utc)
        return RunReport(
            started_at=started_at,
            finished_at=finished_at,
            total=len(candidates),
            ingested=ingested,
            unchanged=unchanged,
            errors=errors,
            outcomes=outcomes,
        )


def main(
    registry_path: str = "data/amazon/registry/source_registry.yaml",
    settings_path: str | None = None,
    max_sources: int | None = None,
    headless: bool = True,
    report_path: str | None = None,
):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    settings = load_settings(settings_path)
    pipeline = UpdatePipeline(settings, registry_path=registry_path, headless=headless)
    report = pipeline.run(max_sources=max_sources)
    if report_path:
        Path(report_path).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Wrote run report to %s", report_path)
    print(f"\n=== Run complete ===")
    print(f"  total:     {report.total}")
    print(f"  ingested:  {report.ingested}")
    print(f"  unchanged: {report.unchanged}")
    print(f"  errors:    {report.errors}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Amazon compliance update pipeline")
    parser.add_argument("--registry", default="data/amazon/registry/source_registry.yaml")
    parser.add_argument("--settings", default=None)
    parser.add_argument("--max-sources", type=int, default=None,
                        help="Cap number of sources for a smoke run")
    parser.add_argument("--no-headless", action="store_true",
                        help="Show the Chrome window (useful for solving captchas)")
    parser.add_argument("--report", default=None,
                        help="Write JSON report to this path")
    args = parser.parse_args()

    main(
        registry_path=args.registry,
        settings_path=args.settings,
        max_sources=args.max_sources,
        headless=not args.no_headless,
        report_path=args.report,
    )
