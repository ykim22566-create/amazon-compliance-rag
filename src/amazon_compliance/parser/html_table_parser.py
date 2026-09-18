"""Structure-aware extraction of tables from rendered Amazon help-page HTML.

Why a custom parser instead of just pandas.read_html:
    - Amazon fee tables routinely use rowspan in the leftmost column to
      indicate a category (e.g. "Small standard" applies to the next 8 rows).
      Naive read_html drops these category labels on inheritor rows.
    - We need the surrounding heading (h2/h3) as section_title metadata so
      that one table-row chunk can be retrieved without losing context like
      "FBA fulfillment fees (excluding apparel)" or "dangerous goods".
    - Each row needs to ship with the table's full header path, so a chunk
      embedded as natural language ("size tier: Large standard, weight:
      8+ to 12 oz, 2026 non-Peak: $3.38") is self-describing.

Output contract: see StructuredTable / StructuredRow dataclasses below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from bs4 import BeautifulSoup, Tag

ARTICLE_SELECTORS = ("div#help-content", "div.help-content", "article")


@dataclass
class StructuredRow:
    """One semantic row of a table.

    cells maps each header text to the cell text for that row, with rowspan-
    based category inheritance already resolved.
    """

    table_id: str
    row_index: int
    cells: dict[str, str]
    section_title: str
    table_title: str

    def derived_category(self) -> str | None:
        """Best-guess product-category tag derived from section/table title.

        Amazon's fee-table sections all use phrases like "(excluding
        apparel)" / "for apparel" / "for dangerous goods". The literal
        phrase contains "apparel" in BOTH categories, which makes BM25 and
        Dense both prone to confusing them. We normalize to a single, hard
        token (NON-APPAREL / APPAREL / DANGEROUS-GOODS / DG-REMOVAL / ...)
        and prepend it to the rendered chunk so the embedding sees a
        category signal that doesn't tokenize as "apparel".
        """
        haystack = f"{self.section_title} {self.table_title}".lower()
        if "dangerous good" in haystack or "dangerous-good" in haystack:
            return "DANGEROUS-GOODS"
        if "excluding apparel" in haystack or "non-apparel" in haystack or "non apparel" in haystack:
            return "NON-APPAREL"
        if "for apparel" in haystack or "apparel " in haystack and "non" not in haystack:
            return "APPAREL"
        if "removal" in haystack:
            return "REMOVAL-FEE"
        if "inbound placement" in haystack:
            return "INBOUND-PLACEMENT"
        if "returns processing" in haystack:
            return "RETURNS-PROCESSING"
        return None

    def render_as_text(self) -> str:
        """Self-describing natural-language form for embedding/BM25.

        The leading Category: line (when derivable) is what lets dense
        retrieval distinguish the otherwise lexically near-identical
        "FBA fulfillment fees for apparel" vs "FBA fulfillment fees
        (excluding apparel)" sub-tables.
        """
        lines: list[str] = []
        cat = self.derived_category()
        if cat:
            lines.append(f"Category: {cat}")
        if self.section_title:
            lines.append(f"Section: {self.section_title}")
        if self.table_title and self.table_title != self.section_title:
            lines.append(f"Table: {self.table_title}")
        for header, value in self.cells.items():
            if value:
                lines.append(f"{header}: {value}")
        return "\n".join(lines)


@dataclass
class StructuredTable:
    table_id: str
    section_title: str
    table_title: str
    headers: list[str]
    rows: list[StructuredRow] = field(default_factory=list)


@dataclass
class ParsedPage:
    title: str
    prose_text: str
    """All article text with table regions removed."""

    tables: list[StructuredTable]


def _find_article(soup: BeautifulSoup) -> Tag | None:
    for sel in ARTICLE_SELECTORS:
        node = soup.select_one(sel)
        if node is not None:
            return node
    return soup.body


def _nearest_heading_before(node: Tag) -> str:
    """Walk backwards through previous siblings/ancestors to find the closest
    heading text. Used so each table inherits its section context.
    """
    for prev in node.find_all_previous(["h1", "h2", "h3", "h4"]):
        text = prev.get_text(" ", strip=True)
        if text:
            return text
    return ""


def _table_title(table: Tag, fallback: str) -> str:
    cap = table.find("caption")
    if cap and cap.get_text(strip=True):
        return cap.get_text(" ", strip=True)
    # Amazon often puts the table label as a paragraph immediately before
    prev = table.find_previous_sibling()
    if prev and prev.name in ("p", "h2", "h3", "h4", "strong", "b"):
        text = prev.get_text(" ", strip=True)
        if text and len(text) < 200:
            return text
    return fallback


def _extract_grid(table: Tag) -> list[list[str]]:
    """Expand rowspan/colspan into a dense rectangular grid of strings."""
    rows = table.find_all("tr")
    grid: list[list[str]] = []
    pending: dict[tuple[int, int], str] = {}
    for r_idx, tr in enumerate(rows):
        cells = tr.find_all(["th", "td"])
        row: list[str] = []
        c_idx = 0
        cell_iter = iter(cells)
        while True:
            while (r_idx, c_idx) in pending:
                row.append(pending.pop((r_idx, c_idx)))
                c_idx += 1
            try:
                cell = next(cell_iter)
            except StopIteration:
                break
            text = cell.get_text(" ", strip=True)
            try:
                rowspan = int(cell.get("rowspan") or 1)
            except ValueError:
                rowspan = 1
            try:
                colspan = int(cell.get("colspan") or 1)
            except ValueError:
                colspan = 1
            for c_off in range(colspan):
                row.append(text)
                for r_off in range(1, rowspan):
                    pending[(r_idx + r_off, c_idx + c_off)] = text
                c_idx += 1
        grid.append(row)
    return grid


def _split_header_body(table: Tag, grid: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    thead = table.find("thead")
    if thead is not None:
        n_header_rows = len(thead.find_all("tr"))
    else:
        # heuristic: rows whose cells are all <th> count as header rows
        n_header_rows = 0
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            if all(c.name == "th" for c in cells):
                n_header_rows += 1
            else:
                break
        if n_header_rows == 0 and grid:
            n_header_rows = 1  # default: first row is the header
    header_rows = grid[:n_header_rows]
    body_rows = grid[n_header_rows:]
    if not header_rows:
        if not body_rows:
            return [], []
        # fallback: synthesize generic headers
        return [f"col_{i+1}" for i in range(len(body_rows[0]))], body_rows
    if len(header_rows) == 1:
        headers = [c.strip() for c in header_rows[0]]
    else:
        cols = max(len(r) for r in header_rows)
        headers = []
        for c in range(cols):
            parts = []
            seen: set[str] = set()
            for r in header_rows:
                if c < len(r):
                    val = r[c].strip()
                    if val and val not in seen:
                        parts.append(val)
                        seen.add(val)
            headers.append(" / ".join(parts))
    return headers, body_rows


def parse_tables(article: Tag, source_id: str) -> list[StructuredTable]:
    structured: list[StructuredTable] = []
    for t_idx, table in enumerate(article.find_all("table")):
        grid = _extract_grid(table)
        if not grid:
            continue
        headers, body = _split_header_body(table, grid)
        if not headers or not body:
            continue
        section_title = _nearest_heading_before(table)
        table_title = _table_title(table, fallback=section_title)
        table_id = f"{source_id}_t{t_idx:02d}"
        rows: list[StructuredRow] = []
        n_cols = len(headers)
        for r_idx, body_row in enumerate(body):
            cells: dict[str, str] = {}
            for c_idx, header in enumerate(headers):
                value = body_row[c_idx].strip() if c_idx < len(body_row) else ""
                cells[header] = value
            rows.append(
                StructuredRow(
                    table_id=table_id,
                    row_index=r_idx,
                    cells=cells,
                    section_title=section_title,
                    table_title=table_title,
                )
            )
        structured.append(
            StructuredTable(
                table_id=table_id,
                section_title=section_title,
                table_title=table_title,
                headers=headers,
                rows=rows,
            )
        )
    return structured


def _article_to_prose(article: Tag) -> str:
    """Drop tables, then collapse the article into readable plain text.

    Tables go through the structured-row pipeline; leaving their flattened
    text in the prose chunk would just create duplicate retrieval noise.
    """
    clone = BeautifulSoup(str(article), "lxml")
    for tag in clone.find_all(["script", "style", "nav", "aside"]):
        tag.decompose()
    for tbl in clone.find_all("table"):
        tbl.decompose()
    text = clone.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def parse_help_html(html: str, source_id: str) -> ParsedPage:
    soup = BeautifulSoup(html, "lxml")
    article = _find_article(soup)
    if article is None:
        raise ValueError(f"No article element found for source_id={source_id}")
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    tables = parse_tables(article, source_id)
    prose = _article_to_prose(article)
    return ParsedPage(title=title, prose_text=prose, tables=tables)


def iter_rows(tables: Iterable[StructuredTable]) -> Iterable[StructuredRow]:
    for t in tables:
        yield from t.rows
