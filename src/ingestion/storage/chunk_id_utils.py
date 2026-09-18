"""Utilities for the current storage-level chunk ID format.

The existing production chunk IDs stored in ChromaDB and BM25 follow:

    {source_path_hash}_{chunk_index:04d}_{content_hash}

This module centralizes the *current* prefix derivation so cleanup paths
can target the same ID family without changing the existing on-disk ID
scheme.
"""

from __future__ import annotations

import hashlib


def storage_chunk_prefix(source_path: str) -> str:
    """Return the current storage chunk-ID prefix for a source path.

    Args:
        source_path: Stable source path / URL used when the chunk was stored.

    Returns:
        First 8 chars of SHA256(source_path), matching VectorUpserter.
    """
    return hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:8]

