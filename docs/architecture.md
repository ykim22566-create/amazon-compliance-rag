# Architecture

The system has two halves: an ingestion pipeline that turns Seller Central help pages into retrievable chunks, and a query path that serves them over MCP or a web UI.

```
Seller Central help pages
        │
        ▼
  help_fetcher ──► html_table_parser ──► change_detector ──► snapshot_store
        │
        ▼
    chunking ──► embedding (bge-m3) ──► Chroma + BM25 index
        │
        ▼
  hybrid retrieval: dense + sparse ──► RRF fusion ──► LLM rerank (optional)
        │
        ├──► MCP server (stdio, 3 tools)
        └──► Streamlit UI
```

## Ingestion

### Why the pages need a browser

Seller Central help is a single-page app. A direct HTTP GET returns a ~167 KB React shell containing none of the help text — the article is injected after `window.onload`. Verified empirically: the raw response for the FBA fee pages contains no `3.5%` or `surcharge` token anywhere.

So `help_fetcher` drives a real browser and waits on content rather than on the DOM node. The `<article>` element appears almost immediately as an empty stub, so waiting for its presence returns nothing useful; the fetcher waits until it holds a non-trivial amount of text.

This is the property that makes the whole project worth building. A general-purpose model with web browsing hits the same empty shell and silently falls back to third-party sources.

### Why a custom table parser

`pandas.read_html` and similar general extractors lose exactly the information that makes a fee table answerable:

- **`rowspan` category labels.** A fee table uses `rowspan` in the leftmost column so that "Small standard" covers the next eight rows. Naive extraction drops the label on every inheritor row, leaving eight rows that no longer say what size tier they describe.
- **Surrounding section headings.** Whether a table sits under "FBA fulfillment fees (excluding apparel)" or under "dangerous goods" is the difference between a correct and a dangerously wrong answer, and that context lives in an `h2`/`h3` outside the table.
- **Multi-level headers.** A single cell's meaning depends on the full header path above it, not just its immediate column.

`html_table_parser` walks the table preserving all three, then emits **one chunk per row**, rendered as a self-describing sentence:

```
size tier: Large standard, shipping weight: 8+ to 12 oz, 2026 non-peak: $3.38
```

A chunk in that form embeds meaningfully and can be retrieved by a question about one specific size and weight. The same row inside a flattened table cannot.

4,484 of the corpus's 10,842 chunks are table rows produced this way.

### Why a separate change registry

Re-running the crawler re-renders every page, and a rendered page differs byte-for-byte on every run — timestamps, session tokens, ad slots. Hashing the file therefore reports that everything changed, every time, which makes incremental updates worthless.

`change_detector` keys on a stable `source_id` derived from the URL and compares a **normalized content hash** built from the article text plus the structured rows. Re-ingestion happens only when meaningful content moved. The table is deliberately tiny — SQLite, three columns, one method that matters.

### Why raw HTML is kept

`snapshot_store` writes every fetched page to `data/amazon/snapshots/<source_id>/<timestamp>.html`.

The predecessor pipeline did not, and because it flattened tables on the way in, the structure it discarded could not be recovered without a fresh crawl of every page. Keeping the raw input means parsing can be replayed and improved against the exact bytes that produced the current index.

## Retrieval

Dense and sparse retrieval run in parallel over the same collection and are fused with Reciprocal Rank Fusion.

| Stage | Setting |
|---|---|
| Dense candidates | 20 |
| Sparse candidates (BM25) | 20 |
| RRF constant *k* | 60 |
| Fused output | 10 |
| After rerank | 5 |

Neither retriever is sufficient alone. Dense embeddings match paraphrases — "fuel surcharge" against "fuel and logistics-related surcharge" — but blur the difference between `$3.38` and `$3.65` in adjacent table rows. BM25 nails exact figures and identifiers but misses any question phrased differently from the page. RRF lets each contribute without tuning a weight between incomparable score scales.

LLM reranking is optional and off by default in the public demo. It improves ordering (MRR 0.564 → 0.667) but not recall — see [evaluation.md](evaluation.md).

## Serving

**MCP server.** `main.py` validates configuration, then serves three tools over stdio using the official MCP SDK: `query_knowledge_hub`, `list_collections`, `get_document_summary`. Because stdio reserves stdout for JSON-RPC, every log and human-readable message is routed to stderr; a stray `print` corrupts the protocol stream.

Heavy dependencies are imported eagerly in the main thread before the SDK spins up its I/O threads. Tool handlers run work in `asyncio.to_thread`, and a first-time `import chromadb` inside a worker thread can deadlock against the stdin reader over Python's import lock.

**Streamlit UI.** A search page over the Amazon corpus plus operational views — ingestion and query traces, evaluation, and a data browser.

## Extension points

Provider adapters sit behind factories, so LLM, embedding, vector store, reranker, splitter and evaluator are each swappable from `config/settings.yaml` without touching call sites. Any OpenAI-compatible endpoint works by changing `base_url` and `model`.

Configuration carries `${VAR}` placeholders rather than literal credentials, expanded at load time. A missing variable raises at startup with the name of the variable, instead of surfacing later as an opaque 401 from the provider.
