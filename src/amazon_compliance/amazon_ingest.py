"""Custom ingest path for Amazon HTML snapshots.

Why a custom path instead of reusing IngestionPipeline.run():
    - The default pipeline keys cleanup on file SHA256 (each new snapshot
      changes hash → old chunks would never be replaced); we need to key
      cleanup on URL-derived source_id so re-fetches replace prior chunks.
    - Table rows have to land as individual chunks (one row = one chunk).
      The default DocumentChunker would feed everything through a recursive
      character splitter and merge / break rows at unpredictable boundaries.
    - LLM-driven Chunk Refinement / Metadata Enrichment / Image Captioning
      are not necessary for our use case and would add ~1-2k LLM calls for
      a 727-document corpus. Skipping them is the responsible default for
      a thesis MVP and can be enabled later.

Flow:
    snapshot.html -> HtmlLoader -> Document
                  -> chunk_amazon_document -> Chunk[]
                  -> BatchProcessor -> dense_vectors + sparse_stats
                  -> cleanup-by-source_id (vector + bm25)
                  -> VectorUpserter.upsert + BM25Indexer.add_documents
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.core.settings import Settings
from src.core.types import Chunk, Document
from src.ingestion.embedding.batch_processor import BatchProcessor
from src.ingestion.embedding.dense_encoder import DenseEncoder
from src.ingestion.embedding.sparse_encoder import SparseEncoder
from src.ingestion.storage.bm25_indexer import BM25Indexer
from src.ingestion.storage.chunk_id_utils import storage_chunk_prefix
from src.ingestion.storage.vector_upserter import VectorUpserter
from src.libs.embedding.embedding_factory import EmbeddingFactory
from src.libs.loader.html_loader import ROW_MARKER_CLOSE, ROW_MARKER_OPEN

logger = logging.getLogger(__name__)


# Metadata keys that are large/non-primitive and must NOT be propagated to
# chunks (ChromaDB only allows primitive metadata values; downstream
# retrieval doesn't need the full per-chunk row dump anyway).
_CHUNK_METADATA_DROP = frozenset({"structured_rows"})


def _scalar_metadata(metadata: dict) -> dict:
    """Strip non-scalar fields from document-level metadata before chunk inheritance."""
    out: dict = {}
    for key, value in metadata.items():
        if key in _CHUNK_METADATA_DROP:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        else:
            # JSON-stringify lists/dicts so retrieval-side debugging is still possible
            try:
                out[key] = json.dumps(value, ensure_ascii=False)[:2000]
            except (TypeError, ValueError):
                out[key] = str(value)[:2000]
    return out


def _chunk_id(doc_id: str, index: int, text: str) -> str:
    return f"{doc_id}_{index:04d}_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"


def chunk_amazon_document(
    document: Document,
    *,
    prose_chunk_size: int = 800,
    prose_chunk_overlap: int = 120,
) -> list[Chunk]:
    """Split a Document produced by HtmlLoader into Chunk objects.

    Row-marker blocks emit one Chunk each (no further splitting); prose
    sections between markers are split with a recursive character splitter
    sized for general English/Chinese policy text.
    """
    text = document.text
    base_metadata = _scalar_metadata(document.metadata)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=prose_chunk_size,
        chunk_overlap=prose_chunk_overlap,
        separators=["\n\n", "\n", ". ", "! ", "? ", "; ", " ", ""],
    )

    fragments: list[tuple[str, str]] = []  # (kind, text); kind in {"prose", "row"}

    cursor = 0
    while cursor < len(text):
        open_at = text.find(ROW_MARKER_OPEN, cursor)
        if open_at == -1:
            tail = text[cursor:].strip()
            if tail:
                fragments.append(("prose", tail))
            break
        prose_segment = text[cursor:open_at].strip()
        if prose_segment:
            fragments.append(("prose", prose_segment))
        close_at = text.find(ROW_MARKER_CLOSE, open_at)
        if close_at == -1:
            # malformed — bail out and treat the rest as prose
            tail = text[open_at:].strip()
            if tail:
                fragments.append(("prose", tail))
            break
        row_body = text[open_at + len(ROW_MARKER_OPEN) : close_at].strip()
        if row_body:
            fragments.append(("row", row_body))
        cursor = close_at + len(ROW_MARKER_CLOSE)

    chunks: list[Chunk] = []
    chunk_index = 0
    for kind, frag_text in fragments:
        if kind == "row":
            sub_texts = [frag_text]
        else:
            sub_texts = [seg for seg in splitter.split_text(frag_text) if seg.strip()]
        for st in sub_texts:
            metadata = dict(base_metadata)
            metadata["chunk_index"] = chunk_index
            metadata["source_ref"] = document.id
            metadata["chunk_kind"] = kind
            chunk_id = _chunk_id(document.id, chunk_index, st)
            chunks.append(Chunk(id=chunk_id, text=st, metadata=metadata))
            chunk_index += 1
    return chunks


@dataclass
class IngestResult:
    source_id: str
    chunk_count: int
    row_chunks: int
    prose_chunks: int
    deleted_chunks: int
    vector_ids: list[str]


class AmazonIngestor:
    """Encode + store Amazon help-page chunks into MODULAR's existing stores."""

    def __init__(self, settings: Settings, collection: str = "amazon_compliance"):
        self.settings = settings
        self.collection = collection

        embedding = EmbeddingFactory.create(settings)
        batch_size = settings.ingestion.batch_size if settings.ingestion else 100
        self.dense_encoder = DenseEncoder(embedding, batch_size=batch_size)
        self.sparse_encoder = SparseEncoder()
        self.batch_processor = BatchProcessor(
            dense_encoder=self.dense_encoder,
            sparse_encoder=self.sparse_encoder,
            batch_size=batch_size,
        )

        self.vector_upserter = VectorUpserter(settings, collection_name=collection)
        bm25_dir = Path(getattr(settings, "data_dir", ".")) / "data" / "db" / "bm25" / collection
        self.bm25_indexer = BM25Indexer(index_dir=str(bm25_dir))

        logger.info("AmazonIngestor ready (collection=%s, batch_size=%d)", collection, batch_size)

    def ingest_document(self, document: Document) -> IngestResult:
        chunks = chunk_amazon_document(document)
        if not chunks:
            logger.warning("source_id=%s produced 0 chunks, skipping", document.id)
            return IngestResult(
                source_id=document.id,
                chunk_count=0,
                row_chunks=0,
                prose_chunks=0,
                deleted_chunks=0,
                vector_ids=[],
            )

        deleted = 0
        storage_prefix = storage_chunk_prefix(str(document.metadata["source_path"]))
        try:
            deleted = self.vector_upserter.vector_store.delete_by_metadata(
                {"doc_hash": document.id}
            ) or 0
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("vector cleanup failed for %s: %s", document.id, exc)
        try:
            removed = self.bm25_indexer.remove_document(
                storage_prefix, self.collection
            )
            if not removed:
                logger.warning(
                    "BM25 cleanup removed no postings for prefix=%s collection=%s",
                    storage_prefix,
                    self.collection,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("bm25 cleanup failed for %s: %s", document.id, exc)

        batch_result = self.batch_processor.process(chunks)
        dense_vectors = batch_result.dense_vectors
        sparse_stats = batch_result.sparse_stats

        vector_ids = self.vector_upserter.upsert(chunks, dense_vectors)
        for stat, vid in zip(sparse_stats, vector_ids):
            stat["chunk_id"] = vid
        self.bm25_indexer.add_documents(
            sparse_stats,
            collection=self.collection,
            doc_id=storage_prefix,
        )

        row_chunks = sum(1 for c in chunks if c.metadata.get("chunk_kind") == "row")
        prose_chunks = len(chunks) - row_chunks
        logger.info(
            "ingested source_id=%s chunks=%d (rows=%d prose=%d) deleted_old=%d",
            document.id, len(chunks), row_chunks, prose_chunks, deleted,
        )
        return IngestResult(
            source_id=document.id,
            chunk_count=len(chunks),
            row_chunks=row_chunks,
            prose_chunks=prose_chunks,
            deleted_chunks=deleted,
            vector_ids=vector_ids,
        )


def iter_chunks(chunks: Iterable[Chunk]) -> Iterable[Chunk]:
    """Public re-export so orchestrator.py can iterate without import gymnastics."""
    yield from chunks
