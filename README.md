# Amazon Compliance RAG

A vertical retrieval system over Amazon Seller Central's compliance documentation, exposed to AI assistants through the Model Context Protocol (MCP).

**[Architecture](docs/architecture.md)** · **[Evaluation](docs/evaluation.md)** · **[Run it](#running-locally)**

---

## The problem

Amazon's Seller Central help pages are the authoritative source for fee schedules, return windows, account-health rules and appeal procedures. Sellers ask questions about them constantly. But two things make them hard for a general-purpose LLM to answer well:

1. **The pages are dynamically rendered.** Web-browsing models cannot retrieve them, so they fall back on third-party blogs and forum posts — secondary sources that are frequently out of date and cite nothing.
2. **The substance lives in tables.** Fee schedules are multi-level HTML tables with `rowspan` / `colspan` headers. A conventional HTML-to-text pass flattens them, so `Small standard / 4+ to 6 oz / <$10 → $2.76` degrades into an ambiguous line of prose that no retriever can match against a specific question.

The result is that a seller asking *"when does the 3.5% fuel surcharge take effect?"* gets a confident answer with no source, often citing the wrong year's rate card.

## The approach

**Structure-aware ingestion.** A render-then-parse pipeline fetches each help page, extracts tables with their header hierarchy intact (handling `rowspan`, `colspan`, multi-level headers and inherited section headings), and emits **one chunk per table row** with the full header path attached. Of the 10,842 chunks in the corpus, **4,484 are table rows** — content that a flattening pipeline loses entirely.

**Incremental change detection.** Pages are hashed after rendering, so a re-crawl only re-embeds what actually changed rather than rebuilding the index.

**Hybrid retrieval.** Dense vectors (BAAI/bge-m3, 1024-dim) and BM25 run in parallel and are fused with Reciprocal Rank Fusion, with optional LLM reranking on top. Dense retrieval alone misses exact fee figures; BM25 alone misses paraphrases like "fuel surcharge" vs "fuel and logistics-related surcharge".

**Every answer is traceable.** Each result carries the Seller Central URL it came from, so the seller can verify against the official page.

## Results

Measured on a 15-question hand-labelled gold set spanning fees, tax, returns, account health, appeals, inventory, brand and dangerous goods. Each question is scored on four dimensions (0–2 each): precision, source attribution, freshness, authority.

| | This system | Same model, no retrieval |
|---|---|---|
| **Overall** | **102/120 (85.0%)** | 83/120 (69.2%) |
| Precision | 22/30 (73%) | 20/30 (67%) |
| **Source attribution** | **27/30 (90%)** | 17/30 (57%) |
| Freshness | 23/30 (77%) | 20/30 (67%) |
| Authority | 30/30 (100%) | 26/30 (87%) |

The baseline is the same model with retrieval switched off, so the gap isolates what retrieval contributes rather than confounding it with a model difference. It is widest on **source attribution** — which is the point: the baseline answers plausibly but cannot cite the page it came from, and on fee questions it frequently quotes a superseded rate card.

Retrieval-only metrics (10-question subset): Hit@5 70%, Hit@10 80%, MRR 0.667. LLM reranking improves MRR (0.564 → 0.667) without changing hit rate — it reorders the candidate set rather than recalling more of it.

See [docs/evaluation.md](docs/evaluation.md) for methodology, per-question scores and the roadmap.

## Corpus

| | |
|---|---|
| Official source pages | 708 |
| Chunks | 10,842 |
| — prose | 6,358 |
| — table rows | 4,484 |
| Embedding | BAAI/bge-m3, 1024-dim |

## Using it from an AI assistant

The system speaks MCP over stdio and exposes three tools: `query_knowledge_hub`, `list_collections`, `get_document_summary`.

Add to your Claude Desktop config:

```json
{
  "mcpServers": {
    "amazon-compliance-rag": {
      "command": ".venv/bin/python",
      "args": ["main.py"],
      "cwd": "/absolute/path/to/amazon-compliance-rag",
      "env": {
        "DEEPSEEK_API_KEY": "your-key",
        "SILICONFLOW_API_KEY": "your-key"
      }
    }
  }
}
```

Then ask Claude about Amazon fees and it will retrieve from the corpus rather than guess.

## Running locally

The corpus (Chroma + BM25 index) ships with the repo through Git LFS, so install
LFS before cloning — otherwise the index files arrive as pointer stubs and
retrieval comes back empty.

```bash
git lfs install
git clone https://github.com/ykim22566-create/amazon-compliance-rag.git
cd amazon-compliance-rag

python3 -m venv .venv && ./.venv/bin/pip install -e .
export DEEPSEEK_API_KEY=... SILICONFLOW_API_KEY=...
```

Web UI — opens on the Amazon Compliance search page:

```bash
./.venv/bin/streamlit run src/observability/dashboard/app.py
```

Command line:

```bash
./.venv/bin/python scripts/query.py --query "When does the 3.5% fuel surcharge take effect?" --collection amazon_compliance
```

MCP server:

```bash
./.venv/bin/python main.py
```

Credentials are read from the environment — `config/settings.yaml` holds `${DEEPSEEK_API_KEY}` / `${SILICONFLOW_API_KEY}` placeholders and never literal keys. A missing variable fails at startup with an explicit message rather than surfacing later as an opaque 401.

## Architecture

```
Seller Central help pages
        │
        ▼
  help_fetcher ──► html_table_parser ──► change_detector ──► snapshot_store
        │              (rowspan/colspan,      (render-then-hash,
        │               header inheritance)    incremental re-embed)
        ▼
    chunking ──► embedding (bge-m3) ──► Chroma + BM25 index
        │
        ▼
  hybrid retrieval: dense + sparse ──► RRF fusion ──► LLM rerank (optional)
        │
        ├──► MCP server (stdio, 3 tools)
        └──► Streamlit UI
```

Retrieval, embedding, vector store, reranking and evaluation are each behind a factory interface, so swapping DeepSeek for another OpenAI-compatible provider, or Chroma for another store, is a config change rather than a code change.

## Layout

```
config/       settings.yaml + prompt templates
src/
  amazon_compliance/   fetch, parse, change-detect, snapshot
  ingestion/           chunking, embedding, storage
  core/                query engine (hybrid search, rerank, trace)
  libs/                provider adapters (LLM, embedding, vector store, reranker)
  mcp_server/          MCP protocol handler + tools
  observability/       Streamlit UI, tracing, evaluation
scripts/      ingest, query, evaluate, dashboard
tests/        unit, integration, e2e
```
