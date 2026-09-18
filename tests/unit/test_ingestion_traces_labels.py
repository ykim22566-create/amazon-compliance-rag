from __future__ import annotations

from contextlib import nullcontext
from typing import Any, List

from src.observability.dashboard.pages import ingestion_traces


class _FakeStreamlit:
    def __init__(self) -> None:
        self.markdowns: List[str] = []
        self.metrics: List[tuple[str, Any]] = []

    def columns(self, n: int):
        return [nullcontext() for _ in range(n)]

    def metric(self, label: str, value: Any) -> None:
        self.metrics.append((label, value))

    def markdown(self, text: str) -> None:
        self.markdowns.append(text)

    def expander(self, *_args, **_kwargs):
        return nullcontext()


def test_render_upsert_stage_uses_chunk_terminology(monkeypatch) -> None:
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(ingestion_traces, "st", fake_st)

    ingestion_traces._render_upsert_stage(
        {
            "dense_store": {"count": 3, "backend": "chroma", "collection": "default", "path": "/tmp/chroma"},
            "sparse_store": {"count": 3, "backend": "bm25", "collection": "default", "path": "/tmp/bm25"},
            "image_store": {"count": 0},
        }
    )

    assert "**Chunks:** 3" in fake_st.markdowns
    assert "**Documents:** 3" not in fake_st.markdowns
