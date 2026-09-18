from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

if "jieba" not in sys.modules:
    jieba_stub = types.ModuleType("jieba")
    jieba_stub.lcut = lambda text: text.split()
    sys.modules["jieba"] = jieba_stub

if "langchain_text_splitters" not in sys.modules:
    splitters_stub = types.ModuleType("langchain_text_splitters")

    class RecursiveCharacterTextSplitter:
        def __init__(self, chunk_size=800, chunk_overlap=120, separators=None):
            self.chunk_size = chunk_size
            self.chunk_overlap = chunk_overlap
            self.separators = separators or []

        def split_text(self, text: str) -> list[str]:
            return [text] if text.strip() else []

    splitters_stub.RecursiveCharacterTextSplitter = RecursiveCharacterTextSplitter
    sys.modules["langchain_text_splitters"] = splitters_stub

from src.amazon_compliance.amazon_ingest import AmazonIngestor
from src.core.types import Document
from src.ingestion.storage.chunk_id_utils import storage_chunk_prefix


def test_amazon_ingest_uses_storage_prefix_for_bm25_cleanup_and_add():
    ingestor = AmazonIngestor.__new__(AmazonIngestor)
    ingestor.collection = "amazon_compliance"
    ingestor.vector_upserter = MagicMock()
    ingestor.vector_upserter.vector_store.delete_by_metadata.return_value = 3
    ingestor.vector_upserter.upsert.return_value = ["v0"]
    ingestor.bm25_indexer = MagicMock()
    ingestor.bm25_indexer.remove_document.return_value = True
    ingestor.batch_processor = MagicMock()
    ingestor.batch_processor.process.return_value = SimpleNamespace(
        dense_vectors=[[0.1, 0.2]],
        sparse_stats=[{"chunk_id": "old", "term_frequencies": {"fee": 1}, "doc_length": 1}],
    )

    source_url = "https://sellercentral.amazon.com/help/hub/reference/external/GMUTB89XM7AATPR3"
    document = Document(
        id="GMUTB89XM7AATPR3",
        text="# Title\n\nOne short paragraph.",
        metadata={
            "source_path": source_url,
            "source_url": source_url,
            "source_id": "GMUTB89XM7AATPR3",
            "doc_hash": "GMUTB89XM7AATPR3",
            "doc_type": "html",
        },
    )

    result = ingestor.ingest_document(document)

    prefix = storage_chunk_prefix(source_url)
    ingestor.vector_upserter.vector_store.delete_by_metadata.assert_called_once_with(
        {"doc_hash": document.id}
    )
    ingestor.bm25_indexer.remove_document.assert_called_once_with(
        prefix, ingestor.collection
    )
    _, kwargs = ingestor.bm25_indexer.add_documents.call_args
    assert kwargs["doc_id"] == prefix
    assert result.deleted_chunks == 3
