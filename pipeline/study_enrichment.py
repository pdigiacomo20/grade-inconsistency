from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests

from grade_inconsistency import (
    PMC_ARTICLE_URL,
    PMC_IDCONV_URL,
    PUBMED_SEARCH_URL,
    PUBMED_SUMMARY_URL,
    fetch_json,
    strip_tags,
)

PUBMED_ABSTRACT_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_ARTICLE_URL = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

IMG_RE = re.compile(r"(?is)<img\b(?P<attrs>[^>]*)>")
ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*(["\'])(.*?)\2')
WORD_RE = re.compile(r"[a-z0-9]+")
STUDY_LABEL_RE = re.compile(
    r"\b([A-Z][A-Za-z][A-Za-z'’-]+(?:\s+(?:and|&)\s+[A-Z][A-Za-z'’-]+)?"
    r"(?:\s+et\s+al\.?)?\s+(?:19|20)\d{2}[a-z]?)\b"
)
AUTHOR_YEAR_RE = re.compile(r"^(?P<author>[A-Z][A-Za-z'’-]+).*?(?P<year>(?:19|20)\d{2})")

STOP_TOKENS = {
    "adverse",
    "analysis",
    "assumed",
    "certainty",
    "ci",
    "comparison",
    "control",
    "effect",
    "events",
    "evidence",
    "experimental",
    "findings",
    "forest",
    "group",
    "high",
    "intervention",
    "low",
    "moderate",
    "outcome",
    "participants",
    "placebo",
    "ratio",
    "risk",
    "studies",
    "study",
    "summary",
    "total",
    "very",
}


def _attrs(raw_attrs: str) -> dict[str, str]:
    return {key.lower(): html.unescape(value) for key, _, value in ATTR_RE.findall(raw_attrs)}


def _tokens(text: str) -> set[str]:
    return {token for token in WORD_RE.findall(text.lower()) if len(token) > 2 and token not in STOP_TOKENS}


def study_id_for_label(label: str) -> str:
    digest = hashlib.sha1(re.sub(r"\s+", " ", label.lower()).strip().encode("utf-8")).hexdigest()[:16]
    return f"study::{digest}"


def _candidate_extension(image_url: str, content_type: str) -> str:
    suffix = Path(urlparse(image_url).path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        return suffix
    if "svg" in content_type:
        return ".svg"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    if "gif" in content_type:
        return ".gif"
    if "webp" in content_type:
        return ".webp"
    return ".png"


def find_forest_plot(article_html: str, article_url: str, outcome: dict[str, Any]) -> dict[str, Any] | None:
    outcome_text = " ".join(
        str(outcome.get(key, "")) for key in ("outcome", "question", "table_title", "consensus_answer")
    )
    outcome_tokens = _tokens(outcome_text)
    best: dict[str, Any] | None = None

    for match in IMG_RE.finditer(article_html):
        attrs = _attrs(match.group("attrs"))
        raw_src = attrs.get("src") or attrs.get("data-src") or attrs.get("data-original")
        if not raw_src or raw_src.startswith("data:"):
            continue
        start = max(0, match.start() - 2500)
        end = min(len(article_html), match.end() + 2500)
        context_html = article_html[start:end]
        context_text = strip_tags(context_html)
        image_text = " ".join([attrs.get("alt", ""), attrs.get("title", ""), context_text])
        context_tokens = _tokens(image_text)
        overlap = len(outcome_tokens & context_tokens)
        forest_signal = 8 if "forest" in image_text.lower() else 0
        plot_signal = 4 if "plot" in image_text.lower() else 0
        analysis_signal = 2 if "analysis" in image_text.lower() else 0
        score = overlap + forest_signal + plot_signal + analysis_signal
        if score <= 0:
            continue
        candidate = {
            "score": score,
            "image_url": urljoin(article_url, raw_src),
            "source_alt": attrs.get("alt", ""),
            "source_title": attrs.get("title", ""),
            "context_text": context_text,
        }
        if not best or candidate["score"] > best["score"]:
            best = candidate

    if not best or best["score"] < 3:
        return None
    return best


def save_forest_plot(
    session: requests.Session,
    *,
    forest_plot: dict[str, Any],
    output_dir: str | Path,
    pmid: str,
    outcome_id: int,
    force: bool,
) -> dict[str, Any]:
    destination_dir = Path(output_dir) / str(pmid)
    destination_dir.mkdir(parents=True, exist_ok=True)
    stem = f"outcome-{outcome_id}"
    existing = next(destination_dir.glob(f"{stem}.*"), None)
    if existing and not force:
        return {"path": str(existing), "image_url": forest_plot["image_url"]}

    response = session.get(forest_plot["image_url"], timeout=60)
    response.raise_for_status()
    extension = _candidate_extension(forest_plot["image_url"], response.headers.get("Content-Type", ""))
    destination = destination_dir / f"{stem}{extension}"
    destination.write_bytes(response.content)
    return {"path": str(destination), "image_url": forest_plot["image_url"]}


def extract_study_labels(plot_context: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    labels: list[dict[str, str]] = []
    plain = re.sub(r"\s+", " ", plot_context)
    for match in STUDY_LABEL_RE.finditer(plain):
        label = match.group(1).replace(" & ", " and ")
        label = re.sub(r"\s+", " ", label).strip()
        lowered = label.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        sentence_start = max(0, plain.rfind(".", 0, match.start()) + 1)
        sentence_end = plain.find(".", match.end())
        if sentence_end == -1:
            sentence_end = min(len(plain), match.end() + 240)
        context = plain[sentence_start:sentence_end].strip()
        labels.append({"label": label, "context": context})
    return labels


def classify_study(label: dict[str, str], outcome: dict[str, Any]) -> str:
    context = label["context"].lower()
    if any(term in context for term in ("oppos", "opposite", "favours control", "favors control", "heterogeneity")):
        return "opposing"
    if any(term in context for term in ("agree", "same direction", "favours intervention", "favors intervention")):
        return "agreeing"
    return "opposing" if int(outcome.get("inconsistency", 0) or 0) else "agreeing"


def _pubmed_query_for_label(label: str) -> str:
    match = AUTHOR_YEAR_RE.search(label)
    if not match:
        return f'"{label}"'
    author = match.group("author")
    year = match.group("year")
    return f'{author}[Author] AND {year}[Date - Publication]'


def _extract_abstract(xml_text: str) -> str:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return ""
    parts: list[str] = []
    for abstract_text in root.findall(".//AbstractText"):
        label = abstract_text.attrib.get("Label")
        text = "".join(abstract_text.itertext()).strip()
        if not text:
            continue
        parts.append(f"{label}: {text}" if label else text)
    return "\n\n".join(parts)


def _fetch_abstract(session: requests.Session, pmid: str) -> str:
    response = session.get(
        PUBMED_ABSTRACT_URL,
        params={"db": "pubmed", "id": pmid, "retmode": "xml", "tool": "grade-inconsistency"},
        timeout=60,
    )
    response.raise_for_status()
    return _extract_abstract(response.text)


def _lookup_single_pmcid(session: requests.Session, pmid: str) -> str | None:
    try:
        data = fetch_json(
            session,
            PMC_IDCONV_URL,
            {"ids": pmid, "format": "json", "tool": "grade-inconsistency"},
        )
    except RuntimeError:
        return None
    for record in data.get("records", []):
        pmcid = record.get("pmcid")
        if pmcid:
            return str(pmcid)
    return None


def resolve_study(session: requests.Session, label: str) -> dict[str, Any]:
    study_id = study_id_for_label(label)
    pmid = ""
    summary: dict[str, Any] = {}
    abstract = ""
    pmcid = None
    search_error = ""

    try:
        search_data = fetch_json(
            session,
            PUBMED_SEARCH_URL,
            {
                "db": "pubmed",
                "term": _pubmed_query_for_label(label),
                "retmax": "1",
                "retmode": "json",
                "tool": "grade-inconsistency",
            },
        )
        ids = search_data.get("esearchresult", {}).get("idlist", [])
        pmid = str(ids[0]) if ids else ""
        if pmid:
            summary_data = fetch_json(
                session,
                PUBMED_SUMMARY_URL,
                {"db": "pubmed", "id": pmid, "retmode": "json", "tool": "grade-inconsistency"},
            )
            summary = summary_data.get("result", {}).get(pmid, {})
            abstract = _fetch_abstract(session, pmid)
            pmcid = _lookup_single_pmcid(session, pmid)
    except (RuntimeError, requests.RequestException) as exc:
        search_error = str(exc)

    title = str(summary.get("title") or label)
    link_status = "pmc_full_text" if pmcid else "pubmed_abstract" if abstract or pmid else "metadata_only"
    return {
        "study_id": study_id,
        "label": label,
        "pmid": pmid or None,
        "pmcid": pmcid,
        "title": title,
        "journal": str(summary.get("fulljournalname") or summary.get("source") or ""),
        "year": str(summary.get("pubdate") or summary.get("epubdate") or "")[:4],
        "pubmed_url": PUBMED_ARTICLE_URL.format(pmid=pmid) if pmid else None,
        "pmc_url": PMC_ARTICLE_URL.format(pmcid=pmcid) if pmcid else None,
        "abstract": abstract,
        "link_status": link_status,
        "search_query": _pubmed_query_for_label(label),
        "search_error": search_error,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def summarize_study_for_outcome(study: dict[str, Any]) -> dict[str, Any]:
    return {
        "study_id": study.get("study_id"),
        "label": study.get("label") or study.get("title") or study.get("study_id"),
        "title": study.get("title", ""),
        "link_status": study.get("link_status", "metadata_only"),
        "pmid": study.get("pmid"),
        "pmcid": study.get("pmcid"),
    }
