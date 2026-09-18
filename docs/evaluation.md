# Evaluation

Two layers are reported: retrieval quality in isolation, and end-to-end answer quality against a bare-LLM baseline.

## Test set

15 questions written by hand against the corpus, spanning fees (4), tax (2), account health (2), appeals (2), returns, inventory, listing/brand, dangerous goods and operations. Each has a gold source page and a reference answer.

Questions are tagged by type:

- **A** — a single figure or date lives on one page (e.g. *when does the 3.5% fuel surcharge take effect?*)
- **B** — the answer requires combining a rule with a table row (e.g. *what is the peak fulfillment fee for a small standard item under $10?*)
- **C** — procedural, spanning several pages (e.g. *how do I appeal a suspended listing?*)

## Layer 1 — Retrieval only

Measured on the 10 questions with an unambiguous single gold page.

| Configuration | Hit@5 | Hit@10 | MRR |
|---|---|---|---|
| Hybrid + LLM rerank | 70.0% | 80.0% | 0.667 |
| Hybrid, no rerank | 70.0% | 80.0% | 0.564 |

Reranking moves MRR from 0.564 to 0.667 but leaves hit rate unchanged — it reorders the candidate set rather than recalling more of it. If a page is not in the fused top-10, reranking cannot recover it.

Both failures are appeals questions (APL-1, APL-2), which miss at every K. Appeals content is procedural prose spread across several pages with heavy boilerplate overlap, so neither dense nor sparse retrieval separates the right page from its neighbours. This is the clearest target for the next iteration.

## Layer 2 — End-to-end answers

Each answer scored 0–2 on four dimensions, 8 points per question, 120 total.

- **precision** — is the specific figure, date or condition correct?
- **source** — is a verifiable official source cited?
- **freshness** — does it reflect the current rate card rather than a superseded one?
- **authority** — is it grounded in Amazon's own documentation rather than third-party commentary?

| | This system | Bare LLM baseline |
|---|---|---|
| **Total** | **102/120 (85.0%)** | 83/120 (69.2%) |
| precision | 22/30 (73%) | 20/30 (67%) |
| **source** | **27/30 (90%)** | 17/30 (57%) |
| freshness | 23/30 (77%) | 20/30 (67%) |
| authority | 30/30 (100%) | 26/30 (87%) |

### Per question

| QID | Topic | Type | This system | Baseline |
|---|---|---|---|---|
| ACH-1 | account-health | B | 6 | 5 |
| ACH-2 | account-health | C | 6 | 5 |
| APL-1 | appeals | C | 6 | 6 |
| APL-2 | appeals | B | 5 | 6 |
| FEE-1 | fees | A | 8 | 5 |
| FEE-2 | fees | B | 7 | 6 |
| FEE-3 | fees | A | 5 | 4 |
| FEE-4 | fees | B | 8 | 0 |
| TAX-1 | tax | B | 6 | 2 |
| TAX-2 | tax | A | 7 | 7 |
| RTN-1 | returns | A | 8 | 7 |
| INV-1 | inventory | B | 7 | 7 |
| BRD-1 | listing-brand | C | 7 | 8 |
| DG-1 | dangerous-goods | C | 8 | 8 |
| OPS-1 | operations | C | 8 | 7 |

### Reading the results

The margin is concentrated where it should be. On **source attribution** the gap is 90% vs 57%: the baseline answers fluently but cannot point at the page, and on fee questions it often reproduces a prior year's rate card with no indication that it has done so.

**FEE-4** is the clearest case — a peak-season fee for a specific size tier and weight band. The baseline scored 0 on every dimension; the answer lives in a table row that only exists as retrievable content because of structure-aware extraction.

The system loses to the baseline on two questions. **APL-2** is a retrieval miss (see Layer 1). **BRD-1** is a case where the retrieved pages were correct but the answer was narrower than the question deserved — a generation problem, not a retrieval one.

## Known limitations

- **The test set is small.** 15 questions end-to-end, 10 for retrieval. Enough to show a directional gap, not enough for a tight confidence interval. Expanding it is the highest-value next step.
- **Scoring is single-rater.** The 4-dimension rubric was applied by one person without a second pass, so it carries the usual single-annotator bias.
- **Appeals coverage is weak.** Two of ten retrieval questions fail outright, both procedural multi-page topics.
- **LLM-as-judge is not reported.** A Ragas layer was wired up but two of its three metrics return degenerate zeros across every question, so the harness is not trustworthy yet and its numbers are deliberately omitted rather than presented.

## Reproducing

```bash
python scripts/evaluate.py
```

Retrieval-only metrics can be regenerated separately; see `scripts/evaluate.py --help`.
