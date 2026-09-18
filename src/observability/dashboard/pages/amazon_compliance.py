"""Amazon Compliance page – vertical search over Seller Central policy docs.

Displays:
- Corpus statistics (source pages, prose vs table-row chunks)
- Hybrid search (Dense + BM25 + RRF + LLM rerank) with source-linked results
- Evaluation scoreboard against a bare-LLM baseline
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import streamlit as st

COLLECTION = "amazon_compliance"

# Sourced from eval/scoreboard_final.md (15-question hand-labelled gold set,
# 4 dimensions x 2 points each => 8 points per question, 120 total).
EVAL_TOTAL = {"agent": 102, "baseline": 83, "max": 120}
EVAL_DIMENSIONS = [
    # (dimension, agent, baseline, out_of)
    ("精确性 precision", 22, 20, 30),
    ("来源可溯 source", 27, 17, 30),
    ("时效性 freshness", 23, 20, 30),
    ("权威性 authority", 30, 26, 30),
]

EXAMPLE_QUERIES = [
    "When does the 3.5% fuel surcharge take effect?",
    "What are the 2026 holiday peak fulfillment fees?",
    "How do I appeal a suspended listing?",
]


@st.cache_resource(show_spinner=False)
def _components() -> Tuple[Any, Any, Any]:
    """Build the hybrid-search stack once per session.

    Chroma rejects a second client for the same path with different
    settings, so the vector store built here is the only one the page
    uses — corpus statistics read from it rather than opening their own.
    """
    from src.core.settings import load_settings
    from src.core.query_engine.query_processor import QueryProcessor
    from src.core.query_engine.hybrid_search import create_hybrid_search
    from src.core.query_engine.dense_retriever import create_dense_retriever
    from src.core.query_engine.sparse_retriever import create_sparse_retriever
    from src.core.query_engine.reranker import create_core_reranker
    from src.ingestion.storage.bm25_indexer import BM25Indexer
    from src.libs.embedding.embedding_factory import EmbeddingFactory
    from src.libs.vector_store.vector_store_factory import VectorStoreFactory

    settings = load_settings()
    vector_store = VectorStoreFactory.create(settings, collection_name=COLLECTION)

    dense_retriever = create_dense_retriever(
        settings=settings,
        embedding_client=EmbeddingFactory.create(settings),
        vector_store=vector_store,
    )
    sparse_retriever = create_sparse_retriever(
        settings=settings,
        bm25_indexer=BM25Indexer(index_dir=f"data/db/bm25/{COLLECTION}"),
        vector_store=vector_store,
    )
    sparse_retriever.default_collection = COLLECTION

    hybrid_search = create_hybrid_search(
        settings=settings,
        query_processor=QueryProcessor(),
        dense_retriever=dense_retriever,
        sparse_retriever=sparse_retriever,
    )
    return hybrid_search, create_core_reranker(settings=settings), vector_store


@st.cache_data(show_spinner=False)
def _corpus_stats() -> Dict[str, Any]:
    """Count chunks and distinct source pages. Empty dict on failure."""
    try:
        collection = _components()[2].collection
        total = collection.count()
        rows = len(collection.get(where={"chunk_kind": "row"}, include=[])["ids"])
        metas = collection.get(include=["metadatas"])["metadatas"] or []
        sources = {
            m.get("source_url") or m.get("source_path")
            for m in metas
            if m.get("source_url") or m.get("source_path")
        }
        return {"total": total, "rows": rows, "sources": len(sources)}
    except Exception:
        return {}


def _search(query: str, top_k: int, use_rerank: bool) -> List[Any]:
    from src.core.trace import TraceContext, TraceCollector

    hybrid_search, reranker, _ = _components()
    trace = TraceContext(trace_type="query")
    trace.metadata["query"] = query[:200]

    results = hybrid_search.search(query=query, top_k=top_k, filters=None, trace=trace)
    if use_rerank and reranker.is_enabled and results:
        try:
            results = reranker.rerank(
                query=query, results=results, top_k=top_k, trace=trace
            ).results
        except Exception as exc:  # reranking is best-effort
            st.warning(f"重排失败，展示融合排序结果：{exc}")
    TraceCollector().collect(trace)
    return results


def _render_result(rank: int, result: Any) -> None:
    metadata = result.metadata or {}
    url: Optional[str] = metadata.get("source_url") or metadata.get("source_path")
    kind = metadata.get("chunk_kind", "")
    title = metadata.get("title") or "(untitled)"

    badge = "📊 表格行" if kind == "row" else "📄 正文"
    st.markdown(f"**#{rank:02d} · {title}** &nbsp; `{badge}` &nbsp; `score={result.score:.3f}`")
    # Fee tables are full of dollar amounts; unescaped '$' pairs get parsed
    # as LaTeX math delimiters and mangle the whole passage.
    st.write((result.text or "").strip().replace("$", r"\$"))
    if url:
        st.caption(f"来源：[{url}]({url})")
    st.divider()


def render() -> None:
    st.header("🛒 Amazon Compliance Search")
    st.caption(
        "Seller Central 合规文档为动态渲染，通用大模型无法抓取，只能援引第三方二手信息。"
        "本页面在官方原文语料上做混合检索（Dense + BM25 + RRF + LLM 重排），每条结果可回溯到原始页面。"
    )

    stats = _corpus_stats()
    if stats:
        c1, c2, c3 = st.columns(3)
        c1.metric("官方来源页面", f"{stats['sources']:,}")
        c2.metric("语料 chunk 总数", f"{stats['total']:,}")
        c3.metric(
            "表格行级 chunk",
            f"{stats['rows']:,}",
            help="旧 html2text 管线会把费率表拍平成无法检索的文本行，这部分是结构化抽取后新增的可检索内容。",
        )
    else:
        st.info(f"未找到 `{COLLECTION}` 集合，请先运行摄取管线。")
        return

    st.divider()

    if "amz_query" not in st.session_state:
        st.session_state["amz_query"] = EXAMPLE_QUERIES[0]

    st.write("**示例问题**")
    cols = st.columns(len(EXAMPLE_QUERIES))
    for col, example in zip(cols, EXAMPLE_QUERIES):
        if col.button(example, key=f"amz_ex_{example}", use_container_width=True):
            st.session_state["amz_query"] = example

    query = st.text_input("检索", key="amz_query")
    opt1, opt2 = st.columns([1, 3])
    top_k = opt1.number_input("返回条数", min_value=1, max_value=20, value=5, key="amz_top_k")
    use_rerank = opt2.checkbox(
        "启用 LLM 重排",
        value=False,
        key="amz_rerank",
        help="默认关闭以控制公开 Demo 的调用成本。混合检索本身不调用 LLM；"
        "勾选后会对候选集额外做一次 LLM 重排。",
    )

    if st.button("🔍 检索", type="primary", key="amz_search") and query.strip():
        with st.spinner("检索中…"):
            try:
                results = _search(query.strip(), int(top_k), use_rerank)
            except Exception as exc:
                st.error(f"检索失败：{exc}")
                results = []
        if not results:
            st.info("未找到相关文档。")
        for rank, result in enumerate(results, start=1):
            _render_result(rank, result)

    st.divider()
    st.subheader("📏 评测：对比裸 LLM 基线")
    st.caption(
        f"15 题人工标注金标集，4 个维度各 2 分。"
        f"本系统 **{EVAL_TOTAL['agent']}/{EVAL_TOTAL['max']} "
        f"({EVAL_TOTAL['agent'] / EVAL_TOTAL['max']:.1%})**，"
        f"裸 LLM 基线 {EVAL_TOTAL['baseline']}/{EVAL_TOTAL['max']} "
        f"({EVAL_TOTAL['baseline'] / EVAL_TOTAL['max']:.1%})。"
    )
    st.dataframe(
        [
            {
                "维度": dimension,
                "本系统": f"{agent}/{out_of} ({agent / out_of:.0%})",
                "裸 LLM 基线": f"{base}/{out_of} ({base / out_of:.0%})",
            }
            for dimension, agent, base, out_of in EVAL_DIMENSIONS
        ],
        hide_index=True,
        use_container_width=True,
    )
