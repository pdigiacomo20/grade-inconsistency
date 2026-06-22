#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests


PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PMC_IDCONV_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
PMC_ARTICLE_URL = "https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"

PUBMED_QUERY = (
    '"Cochrane Database Syst Rev"[jour] '
    'AND free full text[filter] '
    'AND ("2025/01/01"[pdat] : "2025/12/31"[pdat])'
)

CATEGORY_LABELS = {
    "risk_of_bias": "risk of bias",
    "imprecision": "imprecision",
    "inconsistency": "inconsistency",
    "indirectness": "indirectness",
    "publication_bias": "publication bias",
}

CATEGORY_PHRASES = {
    "risk_of_bias": ("risk of bias",),
    "imprecision": ("imprecision",),
    "inconsistency": ("inconsistency",),
    "indirectness": ("indirectness",),
    "publication_bias": ("publication bias",),
}

SECTION_RE = re.compile(
    r'(?is)<section\b(?=[^>]*\bclass="[^"]*\btw\b[^"]*")[^>]*>\s*'
    r'<h3\b[^>]*>(?P<title>.*?)</h3>(?P<body>.*?)</section>'
)
TABLE_RE = re.compile(r"(?is)<table\b[^>]*>(.*?)</table>")
ROW_RE = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr>")
CELL_RE = re.compile(r"(?is)<t([dh])\b(?P<attrs>[^>]*)>(?P<body>.*?)</t\1>")
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
DESCRIPTION_RE = re.compile(
    r'(?is)<meta\s+name="description"\s+content="([^"]*)"'
)
FN_BLOCK_RE = re.compile(
    r'(?is)<div\b(?=[^>]*\bclass="[^"]*\bfn\b[^"]*")[^>]*>(.*?)</div>'
)
FOOTNOTE_RE = re.compile(
    r"(?is)<sup>\s*([^<]+?)\s*</sup>\s*(.*?)(?=(?:<sup>\s*[^<]+?\s*</sup>)|$)"
)
TITLE_META_RE = re.compile(r'(?is)<meta\s+name="citation_title"\s+content="([^"]*)"')
SPACE_RE = re.compile(r"\s+")


@dataclass
class Cell:
    html: str
    text: str
    rowspan: int
    colspan: int


@dataclass
class ParsedRow:
    cells: list[Cell | None]
    actual_cell_ids: set[int]


def normalize_space(text: str) -> str:
    return SPACE_RE.sub(" ", text).strip()


def strip_tags(fragment: str) -> str:
    text = re.sub(r"(?is)<br\s*/?>", "\n", fragment)
    text = re.sub(r"(?is)</(p|div|li|tr|table|section|ul|ol|h[1-6])>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\u2028", " ")
    text = text.replace("\ufeff", " ")
    return normalize_space(text)


def extract_sup_labels(fragment: str) -> list[str]:
    labels: list[str] = []
    for raw in re.findall(r"(?is)<sup>\s*([^<]+?)\s*</sup>", fragment):
        unescaped = html.unescape(raw)
        for part in re.split(r"[\s,;/]+", unescaped):
            cleaned = re.sub(r"[^0-9A-Za-z]+", "", part).lower()
            if cleaned:
                labels.append(cleaned)
    return labels


def attrs_dict(raw_attrs: str) -> dict[str, str]:
    return {key.lower(): value for key, value in ATTR_RE.findall(raw_attrs)}


def parse_table_rows(table_html: str) -> list[ParsedRow]:
    open_spans: list[tuple[Cell, int] | None] = []
    matrix: list[ParsedRow] = []

    for row_match in ROW_RE.finditer(table_html):
        row_html = row_match.group(1)
        actual_cells: list[Cell] = []
        for cell_match in CELL_RE.finditer(row_html):
            attrs = attrs_dict(cell_match.group("attrs"))
            body = cell_match.group("body")
            actual_cells.append(
                Cell(
                    html=body,
                    text=strip_tags(body),
                    rowspan=max(int(attrs.get("rowspan", "1") or "1"), 1),
                    colspan=max(int(attrs.get("colspan", "1") or "1"), 1),
                )
            )

        row: list[Cell | None] = []
        col = 0
        cell_index = 0

        while cell_index < len(actual_cells) or any(span is not None for span in open_spans[col:]):
            while col < len(open_spans) and open_spans[col] is not None:
                span_cell, remaining = open_spans[col]
                row.append(span_cell)
                open_spans[col] = (span_cell, remaining - 1) if remaining > 1 else None
                col += 1

            if cell_index >= len(actual_cells):
                next_pending = next(
                    (idx for idx in range(col, len(open_spans)) if open_spans[idx] is not None),
                    None,
                )
                if next_pending is None:
                    break
                if next_pending > col:
                    row.extend([None] * (next_pending - col))
                    col = next_pending
                continue

            cell = actual_cells[cell_index]
            cell_index += 1
            while len(open_spans) < col + cell.colspan:
                open_spans.append(None)
            for offset in range(cell.colspan):
                row.append(cell)
                if cell.rowspan > 1:
                    open_spans[col + offset] = (cell, cell.rowspan - 1)
            col += cell.colspan

        matrix.append(ParsedRow(cells=row, actual_cell_ids={id(cell) for cell in actual_cells}))

    return matrix


def parse_footnote_map(section_html: str) -> dict[str, str]:
    footnotes: dict[str, str] = {}
    for block in FN_BLOCK_RE.findall(section_html):
        for label_fragment, body in FOOTNOTE_RE.findall(block):
            labels = extract_sup_labels(f"<sup>{label_fragment}</sup>")
            text = strip_tags(body)
            for label in labels:
                footnotes[label] = text
    return footnotes


def extract_downgrade_categories(footnote_text: str) -> set[str]:
    categories: set[str] = set()
    lowered = normalize_space(footnote_text).lower()
    clauses = re.split(r"(?:\.\s+|;\s+|\n+)", lowered)
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        if "not downgrad" in clause:
            continue
        if "downgrad" not in clause and "serious" not in clause and "very serious" not in clause:
            continue
        for category, phrases in CATEGORY_PHRASES.items():
            if any(phrase in clause for phrase in phrases):
                categories.add(category)
    return categories


def extract_inconsistency_reason(footnotes: dict[str, str]) -> str:
    reasons: list[str] = []
    for text in footnotes.values():
        lowered = normalize_space(text).lower()
        if "inconsistency" in lowered or "heterogeneity" in lowered or "inconsistent" in lowered:
            reasons.append(text)
    return " ".join(dict.fromkeys(reasons))


def extract_subgroup_differences(footnotes: dict[str, str]) -> int:
    for text in footnotes.values():
        lowered = normalize_space(text).lower()
        has_subgroup_signal = (
            "subgroup" in lowered
            or "sub-group" in lowered
            or "subpopulation" in lowered
            or "population difference" in lowered
            or "differences between groups" in lowered
        )
        if has_subgroup_signal and (
            "not downgrad" in lowered
            or "indirectness" in lowered
            or "inconsistency" in lowered
            or "heterogeneity" in lowered
        ):
            return 1
    return 0


def find_header_labels(rows: list[ParsedRow], certainty_col: int) -> dict[int, str]:
    labels: dict[int, str] = {}
    for row in rows[:5]:
        if certainty_col < len(row.cells):
            cell = row.cells[certainty_col]
            if cell and (
                "certainty of the evidence" in cell.text.lower()
                or "quality of the evidence" in cell.text.lower()
            ):
                for idx, header_cell in enumerate(row.cells):
                    if header_cell and header_cell.text:
                        labels[idx] = header_cell.text
                break
    return labels


def build_outcome_question(section_title: str, outcome_name: str) -> str:
    context = re.sub(r"(?i)\bsummary of findings\b", "", section_title).strip(" :-")
    if context:
        return f"What is the effect on {outcome_name} for {context}?"
    return f"What is the effect on {outcome_name}?"


def extract_consensus_answer(
    row: ParsedRow,
    headers: dict[int, str],
    certainty_col: int,
) -> str:
    consensus_parts: list[str] = []
    excluded_header_terms = (
        "outcome",
        "certainty",
        "quality",
        "grade",
        "participants",
        "studies",
        "follow-up",
        "number of",
    )
    for idx, cell in enumerate(row.cells):
        if idx == 0 or idx == certainty_col or not cell:
            continue
        header = headers.get(idx, "").lower()
        if any(term in header for term in excluded_header_terms):
            continue
        text = cell.text
        if not text:
            continue
        if text not in consensus_parts:
            consensus_parts.append(text)
    return " | ".join(consensus_parts)


def find_certainty_column(rows: list[ParsedRow]) -> int | None:
    for row in rows[:5]:
        for idx, cell in enumerate(row.cells):
            if not cell:
                continue
            lowered = cell.text.lower()
            if "certainty of the evidence" in lowered:
                return idx
            if "quality of the evidence" in lowered:
                return idx
            if "grade" in lowered and ("certainty" in lowered or "evidence" in lowered):
                return idx
    return None


def looks_like_grade_cell(text: str) -> bool:
    lowered = text.lower()
    return (
        "high" in lowered
        or "moderate" in lowered
        or "very low" in lowered
        or re.search(r"\blow\b", lowered) is not None
        or "⊕" in text
    )


def extract_article_title(article_html: str, fallback: str = "") -> str:
    match = TITLE_META_RE.search(article_html)
    if match:
        return html.unescape(match.group(1)).strip()
    return fallback


def is_protocol_article(article_html: str) -> bool:
    match = DESCRIPTION_RE.search(article_html)
    if not match:
        return False
    description = html.unescape(match.group(1)).strip().lower()
    return "this is a protocol for a cochrane review" in description


def extract_summary_sections(article_html: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    for match in SECTION_RE.finditer(article_html):
        title = strip_tags(match.group("title"))
        if "summary of findings" not in title.lower():
            continue
        sections.append((title, match.group(0)))
    return sections


def analyze_summary_section(section_title: str, section_html: str) -> list[dict]:
    table_match = TABLE_RE.search(section_html)
    if not table_match:
        return []

    rows = parse_table_rows(table_match.group(0))
    certainty_col = find_certainty_column(rows)
    if certainty_col is None:
        return []

    headers = find_header_labels(rows, certainty_col)
    footnote_map = parse_footnote_map(section_html)
    extracted: list[dict] = []

    for row in rows:
        unique_cells = {id(cell) for cell in row.cells if cell is not None}
        if len(unique_cells) == 1:
            continue
        if certainty_col >= len(row.cells):
            continue
        certainty_cell = row.cells[certainty_col]
        if not certainty_cell or not looks_like_grade_cell(certainty_cell.text):
            continue
        if id(certainty_cell) not in row.actual_cell_ids:
            continue

        outcome_cell = row.cells[0] if row.cells else None
        outcome_name = outcome_cell.text if outcome_cell else ""
        if not outcome_name:
            continue
        lowered_outcome = outcome_name.lower()
        if lowered_outcome in {"outcomes", "question", "population", "index test", "target condition"}:
            continue

        footnote_labels = extract_sup_labels(certainty_cell.html)
        categories: set[str] = set()
        footnotes_used: dict[str, str] = {}
        for label in footnote_labels:
            if label not in footnote_map:
                continue
            footnote_text = footnote_map[label]
            footnotes_used[label] = footnote_text
            categories.update(extract_downgrade_categories(footnote_text))

        inconsistency = 1 if "inconsistency" in categories else 0
        extracted.append(
            {
                "table_title": section_title,
                "outcome": outcome_name,
                "question": build_outcome_question(section_title, outcome_name),
                "consensus_answer": extract_consensus_answer(row, headers, certainty_col),
                "certainty": certainty_cell.text,
                "footnote_labels": footnote_labels,
                "footnotes": footnotes_used,
                "downgrade_categories": sorted(categories),
                "inconsistency": inconsistency,
                "subgroup_differences": 0 if inconsistency else extract_subgroup_differences(footnotes_used),
                "inconsistency_reason": extract_inconsistency_reason(footnotes_used),
            }
        )

    return extracted


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def fetch_json(session: requests.Session, url: str, params: dict) -> dict:
    last_error = ""
    for attempt in range(5):
        try:
            response = session.get(url, params=params, timeout=60)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(float(retry_after))
                else:
                    time.sleep(2.0 * (attempt + 1))
                last_error = f"HTTP {response.status_code}"
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"{url} ({last_error})")


def fetch_text(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def fetch_article_html(session: requests.Session, url: str, pause_seconds: float) -> str:
    last_error = "missing expected article markers"
    for attempt in range(5):
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            article_html = response.text
            if DESCRIPTION_RE.search(article_html) or TITLE_META_RE.search(article_html):
                time.sleep(pause_seconds)
                return article_html
            last_error = "missing expected article markers"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(max(pause_seconds, 1.0) * (attempt + 1))
    raise RuntimeError(f"{url} ({last_error})")


def search_pubmed(session: requests.Session, limit: int) -> tuple[list[str], int]:
    data = fetch_json(
        session,
        PUBMED_SEARCH_URL,
        {
            "db": "pubmed",
            "term": PUBMED_QUERY,
            "sort": "pub date",
            "retmax": str(limit),
            "retmode": "json",
            "tool": "grade-inconsistency",
        },
    )
    result = data["esearchresult"]
    return result["idlist"][:limit], int(result["count"])


def fetch_pubmed_summaries(session: requests.Session, pmids: list[str]) -> dict[str, dict]:
    summaries: dict[str, dict] = {}
    for chunk in chunked(pmids, 200):
        data = fetch_json(
            session,
            PUBMED_SUMMARY_URL,
            {
                "db": "pubmed",
                "id": ",".join(chunk),
                "retmode": "json",
                "tool": "grade-inconsistency",
            },
        )
        result = data["result"]
        for pmid in result.get("uids", []):
            summaries[pmid] = result[pmid]
    return summaries


def lookup_pmcids(session: requests.Session, pmids: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for chunk in chunked(pmids, 200):
        try:
            data = fetch_json(
                session,
                PMC_IDCONV_URL,
                {
                    "ids": ",".join(chunk),
                    "format": "json",
                    "tool": "grade-inconsistency",
                },
            )
        except RuntimeError as exc:
            print(f"PMCID lookup failed for {','.join(chunk)}: {exc}", file=sys.stderr)
            continue
        for record in data.get("records", []):
            pmid = str(record.get("pmid") or record.get("requested-id") or "")
            pmcid = record.get("pmcid")
            if pmid and pmcid:
                mapping[pmid] = pmcid
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser(description="Helper module for the manual extraction pipeline.")
    parser.parse_args()
    print("Run review ingestion with: python -m pipeline.ingest --config config.yml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
