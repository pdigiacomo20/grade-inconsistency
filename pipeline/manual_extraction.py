from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import re
import time
from typing import Any
from xml.etree import ElementTree

import requests

from grade_inconsistency import (
    PMC_ARTICLE_URL,
    PMC_IDCONV_URL,
    PUBMED_SUMMARY_URL,
    NCBI_REQUEST_DELAY_SECONDS,
    fetch_pmc_xml,
    fetch_json,
    strip_tags,
)
from pipeline.dynamodb import DynamoStore


PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_ARTICLE_URL = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
FIELD_RE = re.compile(
    r"(?ims)^\s*(SoF table|Row|Medical question|Consensus answer|Certainty of evidence|Downgrade reasoning|"
    r"Forest plot title|Effect measure|Line of no effect|Agreeing studies|Opposing studies|Overall notes)\s*:\s*(.*?)(?=^\s*(?:SoF table|Row|Medical question|"
    r"Consensus answer|Certainty of evidence|Downgrade reasoning|Forest plot title|Effect measure|Line of no effect|Agreeing studies|"
    r"Opposing studies|Overall notes)\s*:|\Z)"
)
PUBLICATION_RE = re.compile(
    r"(?ims)^\s*Publication\s+\d+\s*:\s*(.*?)(?=^\s*(?:Publication\s+\d+\s*:|Study\s*:|Effect estimate\s*:|"
    r"Confidence interval begin\s*:|Confidence interval end\s*:|Confidence interval percentage\s*:|Opposing studies\s*:|"
    r"Agreeing studies\s*:|Overall notes\s*:|SoF table\s*:|Row\s*:)|\Z)"
)
STUDY_BLOCK_RE = re.compile(
    r"(?ims)^\s*Study\s*:\s*(.*?)\s*(?=^\s*Study\s*:|^\s*Opposing studies\s*:|^\s*Agreeing studies\s*:|"
    r"^\s*Overall notes\s*:|^\s*SoF table\s*:|^\s*Row\s*:|\Z)"
)
STUDY_FIELD_RE = re.compile(
    r"(?ims)^\s*(Effect estimate|Confidence interval begin|Confidence interval end|Confidence interval percentage)\s*:\s*"
    r"(.*?)(?=^\s*(?:Effect estimate|Confidence interval begin|Confidence interval end|Confidence interval percentage|"
    r"Publication\s+\d+|Study|Opposing studies|Agreeing studies|Overall notes|SoF table|Row)\s*:|\Z)"
)
TITLE_RE = re.compile(
    r"(?P<title>.+?)(?:[.?!])\s+(?P<journal>[A-Z][A-Za-z0-9&().,'’ -]+?)\s+"
    r"(?P<year>(?:19|20)\d{2})[;:]"
)
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
PDF_META_RE = re.compile(r'(?is)<meta\s+name=["\']citation_pdf_url["\']\s+content=["\']([^"\']+)["\']')
PDF_LINK_RE = re.compile(r'(?is)<a\b[^>]+href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']')
OVERALL_NOTES_RE = re.compile(
    r"(?ims)^[ \t]*Overall notes[ \t]*:[ \t]*"
    r"(.*?)(?=^[ \t]*(?:SoF table|Row|Medical question|Consensus answer|Certainty of evidence|Downgrade reasoning|"
    r"Forest plot title|Agreeing studies|Opposing studies)[ \t]*:|^[ \t]*No inconsistency[ \t.]*$|\Z)"
)


@dataclass(frozen=True)
class ParsedSofOutcome:
    outcome_id: int
    sof_table: str
    row: str
    question: str
    consensus_answer: str
    certainty: str
    downgrade_reasoning: str


@dataclass(frozen=True)
class ParsedSofExtraction:
    outcomes: list[dict[str, Any]]
    overall_notes: str
    has_inconsistency: bool


@dataclass(frozen=True)
class ParsedAgreeOpposeExtraction:
    outcomes: list[dict[str, Any]]
    overall_notes: str


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_for_match(text: str) -> str:
    text = text.lower().replace("‐", "-").replace("‑", "-").replace("–", "-").replace("—", "-")
    return _clean(re.sub(r"[^a-z0-9]+", " ", text))


def _fields(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for match in FIELD_RE.finditer(text):
        key = match.group(1).lower()
        result.setdefault(key, []).append(match.group(2).strip())
    return result


def _study_fields(text: str) -> dict[str, str]:
    return {match.group(1).lower(): _clean(match.group(2)) for match in STUDY_FIELD_RE.finditer(text)}


def _sof_key(sof_table: str, row: str) -> str:
    return f"{_clean(sof_table).lower()}::{_clean(row).lower()}"


def _extract_overall_notes(text: str) -> str:
    return _clean("\n\n".join(match.group(1) for match in OVERALL_NOTES_RE.finditer(text) if match.group(1).strip()))


def _is_no_inconsistency(text: str) -> bool:
    without_notes = OVERALL_NOTES_RE.sub("", text).strip()
    normalized = without_notes.rstrip(".").strip().lower()
    return normalized == "no inconsistency"


def parse_sof_extraction(text: str, *, pmid: str, review_id: str) -> ParsedSofExtraction:
    if not text.strip():
        raise ValueError("Paste the Extract SoF output before extracting.")
    overall_notes = _extract_overall_notes(text)
    if _is_no_inconsistency(text):
        return ParsedSofExtraction(outcomes=[], overall_notes=overall_notes, has_inconsistency=False)

    starts = [match.start() for match in re.finditer(r"(?im)^\s*SoF table\s*:", text)]
    if not starts:
        raise ValueError("Could not find any 'SoF table:' blocks in the Extract SoF text.")
    starts.append(len(text))

    outcomes: list[dict[str, Any]] = []
    for index in range(len(starts) - 1):
        block = text[starts[index] : starts[index + 1]].strip()
        fields = _fields(block)
        missing = [
            label
            for label in ("sof table", "row", "medical question", "consensus answer", "certainty of evidence")
            if not fields.get(label)
        ]
        if missing:
            raise ValueError(f"Extract SoF block {index + 1} is missing: {', '.join(missing)}.")
        outcome_id = index + 1
        sof_table = _clean(fields["sof table"][0])
        row = _clean(fields["row"][0])
        outcomes.append(
            {
                "pmid": str(pmid),
                "outcome_id": outcome_id,
                "review_id": review_id,
                "sof_table": sof_table,
                "row": row,
                "outcome_key": _sof_key(sof_table, row),
                "question": _clean(fields["medical question"][0]),
                "consensus_answer": _clean(fields["consensus answer"][0]),
                "certainty": _clean(fields["certainty of evidence"][0]),
                "downgrade_reasoning": _clean((fields.get("downgrade reasoning") or [""])[0]),
                "forest_plot_title": "",
                "agreeing_articles": [],
                "opposing_articles": [],
                "extraction_status": "sof_extracted",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
    return ParsedSofExtraction(outcomes=outcomes, overall_notes=overall_notes, has_inconsistency=True)


def _extract_section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?ims)^\s*{re.escape(heading)}\s*:\s*(.*?)(?=^\s*(?:Agreeing studies|Opposing studies|Overall notes|SoF table|Row)\s*:|\Z)",
        text,
    )
    return match.group(1).strip() if match else ""


def _extract_citations(section: str) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for study_match in STUDY_BLOCK_RE.finditer(section):
        block = study_match.group(1).strip()
        first_line, _, rest = block.partition("\n")
        study_label = _clean(first_line)
        fields = _study_fields(rest)
        for pub_match in PUBLICATION_RE.finditer(rest):
            citation = _clean(pub_match.group(1))
            if citation:
                citations.append(
                    {
                        "study_label": study_label,
                        "citation": citation,
                        "effect_estimate": fields.get("effect estimate", ""),
                        "confidence_interval_begin": fields.get("confidence interval begin", ""),
                        "confidence_interval_end": fields.get("confidence interval end", ""),
                        "confidence_interval_percentage": fields.get("confidence interval percentage", ""),
                    }
                )
    if not citations:
        for pub_match in PUBLICATION_RE.finditer(section):
            citation = _clean(pub_match.group(1))
            if citation:
                citations.append(
                    {
                        "study_label": "",
                        "citation": citation,
                        "effect_estimate": "",
                        "confidence_interval_begin": "",
                        "confidence_interval_end": "",
                        "confidence_interval_percentage": "",
                    }
                )
    return citations


def _validate_effect_citations(citations: list[dict[str, Any]], *, block_index: int, section_name: str) -> None:
    required = (
        ("effect_estimate", "Effect estimate"),
        ("confidence_interval_begin", "Confidence interval begin"),
        ("confidence_interval_end", "Confidence interval end"),
        ("confidence_interval_percentage", "Confidence interval percentage"),
    )
    for citation_index, citation in enumerate(citations, start=1):
        missing = [label for key, label in required if not str(citation.get(key) or "").strip()]
        if missing:
            study = citation.get("study_label") or f"citation {citation_index}"
            raise ValueError(
                f"Extract Agree Oppose block {block_index} {section_name} study '{study}' is missing: "
                f"{', '.join(missing)}."
            )


def parse_agree_oppose_extraction(text: str, existing_outcomes: list[dict[str, Any]]) -> ParsedAgreeOpposeExtraction:
    if not existing_outcomes:
        raise ValueError("Extract SoF must be completed before Extract Agree Oppose.")
    if not text.strip():
        raise ValueError("Paste the Extract Agree Oppose output before extracting.")

    overall_notes = _extract_overall_notes(text)
    starts = [match.start() for match in re.finditer(r"(?im)^\s*SoF table\s*:", text)]
    if not starts:
        raise ValueError("Could not find any 'SoF table:' blocks in the Extract Agree Oppose text.")
    starts.append(len(text))
    outcomes_by_key = {str(item.get("outcome_key")): item for item in existing_outcomes}
    parsed: list[dict[str, Any]] = []

    for index in range(len(starts) - 1):
        block = text[starts[index] : starts[index + 1]].strip()
        fields = _fields(block)
        if not fields.get("sof table") or not fields.get("row"):
            raise ValueError(f"Extract Agree Oppose block {index + 1} is missing SoF table or Row.")
        key = _sof_key(fields["sof table"][0], fields["row"][0])
        outcome = outcomes_by_key.get(key)
        if not outcome:
            raise ValueError(
                f"Extract Agree Oppose block {index + 1} does not match a prior SoF outcome "
                f"(SoF table {fields['sof table'][0]}, Row {fields['row'][0]})."
            )
        agreeing_section = _extract_section(block, "Agreeing studies")
        opposing_section = _extract_section(block, "Opposing studies")
        effect_measure = _clean((fields.get("effect measure") or [""])[0]).lower()
        line_of_no_effect = _clean((fields.get("line of no effect") or [""])[0])
        missing_effect_fields = []
        if not effect_measure:
            missing_effect_fields.append("Effect measure")
        if not line_of_no_effect:
            missing_effect_fields.append("Line of no effect")
        if missing_effect_fields:
            raise ValueError(f"Extract Agree Oppose block {index + 1} is missing: {', '.join(missing_effect_fields)}.")
        agreeing_citations = _extract_citations(agreeing_section)
        opposing_citations = _extract_citations(opposing_section)
        _validate_effect_citations(agreeing_citations, block_index=index + 1, section_name="Agreeing studies")
        _validate_effect_citations(opposing_citations, block_index=index + 1, section_name="Opposing studies")
        parsed.append(
            {
                "outcome": outcome,
                "forest_plot_title": _clean((fields.get("forest plot title") or [""])[0]),
                "effect_measure": effect_measure,
                "line_of_no_effect": line_of_no_effect,
                "agreeing_citations": agreeing_citations,
                "opposing_citations": opposing_citations,
            }
        )
    return ParsedAgreeOpposeExtraction(outcomes=parsed, overall_notes=overall_notes)


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "grade-inconsistency/0.2 (manual extraction; contact: local)",
            "Accept": "text/html,application/xhtml+xml,application/json,application/xml,text/plain",
        }
    )
    return session


def _extract_title_year(citation: str) -> tuple[str, str]:
    title, _journal, year = _extract_citation_metadata(citation)
    return title, year


def _extract_citation_metadata(citation: str) -> tuple[str, str, str]:
    citation = _clean(citation)
    year_match = YEAR_RE.search(citation)
    year = year_match.group(1) if year_match else ""

    # Most pasted references are "Authors. Article title. Journal year;...".
    # Remove the author block first so the title regex cannot consume authors.
    after_authors = citation.split(". ", 1)[1] if ". " in citation else citation
    match = TITLE_RE.search(after_authors)
    if match:
        return _clean(match.group("title")), _clean(match.group("journal")), match.group("year")

    before_year = after_authors[: year_match.start() - (len(citation) - len(after_authors))] if year_match else after_authors
    parts = [part.strip() for part in re.split(r"\.\s+", before_year) if part.strip()]
    if len(parts) >= 2:
        title = ". ".join(parts[:-1])
        journal = parts[-1]
    elif parts:
        title = parts[0]
        journal = ""
    else:
        title = after_authors
        journal = ""
    return _clean(title.rstrip(".?! ")), _clean(journal), year


def _extract_abstract(xml_text: str) -> str:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return ""
    parts: list[str] = []
    for abstract_text in root.findall(".//AbstractText"):
        label = abstract_text.attrib.get("Label")
        text = _clean("".join(abstract_text.itertext()))
        if text:
            parts.append(f"{label}: {text}" if label else text)
    return "\n\n".join(parts)


def _fetch_abstract(session: requests.Session, pmid: str) -> str:
    params = {"db": "pubmed", "id": pmid, "retmode": "xml", "tool": "grade-inconsistency"}
    last_error = ""
    for attempt in range(5):
        try:
            response = session.get(PUBMED_FETCH_URL, params=params, timeout=60)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(float(retry_after))
                else:
                    time.sleep(2.0 * (attempt + 1))
                last_error = f"HTTP {response.status_code}"
                continue
            response.raise_for_status()
            abstract = _extract_abstract(response.text)
            time.sleep(NCBI_REQUEST_DELAY_SECONDS)
            return abstract
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"{PUBMED_FETCH_URL} ({pmid}: {last_error})")


def _lookup_pmcid(session: requests.Session, pmid: str) -> str:
    try:
        data = fetch_json(session, PMC_IDCONV_URL, {"ids": pmid, "format": "json", "tool": "grade-inconsistency"})
    except RuntimeError:
        return ""
    for record in data.get("records", []):
        if record.get("pmcid"):
            return str(record["pmcid"])
    return ""


def _summaries(session: requests.Session, pmids: list[str]) -> dict[str, dict[str, Any]]:
    if not pmids:
        return {}
    data = fetch_json(
        session,
        PUBMED_SUMMARY_URL,
        {"db": "pubmed", "id": ",".join(pmids), "retmode": "json", "tool": "grade-inconsistency"},
    )
    result = data.get("result", {})
    return {pmid: result.get(pmid, {}) for pmid in result.get("uids", [])}


def _ask_openai_for_pmid(
    *,
    api_key: str | None,
    model: str,
    citation: str,
    timeout_seconds: int,
) -> str:
    if not api_key:
        return ""
    payload = {
        "model": model,
        "tools": [
            {
                "type": "web_search",
                "filters": {"allowed_domains": ["pubmed.ncbi.nlm.nih.gov"]},
                "search_context_size": "medium",
            }
        ],
        "input": [
            {
                "role": "user",
                "content": (
                    "Given the following citation, provide the pubmed ID (PMID) corresponding to the article. "
                    "Use web search to first search only the title of the article. Then, when a candidate PMID is "
                    "identified, you will end up with a link like https://pubmed.ncbi.nlm.nih.gov/27040313/ where "
                    "27040313 is the candidate PMID. Ensure that the title, journal, and year of publication all "
                    "match the original citation provided, or else continue with the web search to find the correct "
                    'PMID. Return only the PMID or "FAIL" to say that you could not find the PMID.\n\n'
                    f"Citation:\n{citation}"
                ),
            }
        ],
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        output = data.get("output_text") or ""
        if not output:
            for item in data.get("output", []):
                for content in item.get("content", []):
                    output += str(content.get("text") or "")
        output = output.strip()
        if re.fullmatch(r"(?i)fail", output):
            return ""
        match = re.fullmatch(r"\d{6,9}", output)
        return match.group(0) if match else ""
    except (requests.RequestException, ValueError):
        return ""


def _metadata_matches_citation(citation: str, summary: dict[str, Any]) -> bool:
    title, journal, year = _extract_citation_metadata(citation)
    expected_title = _normalize_for_match(title)
    actual_title = _normalize_for_match(str(summary.get("title") or ""))
    if expected_title and expected_title != actual_title:
        return False

    summary_year = str(summary.get("pubdate") or summary.get("epubdate") or "")[:4]
    if year and summary_year and summary_year != year:
        return False

    expected_journal = _normalize_for_match(journal)
    actual_journal = _normalize_for_match(str(summary.get("fulljournalname") or summary.get("source") or ""))
    if expected_journal and actual_journal and expected_journal not in actual_journal and actual_journal not in expected_journal:
        return False

    return bool(actual_title)


def resolve_pubmed(
    session: requests.Session,
    citation: str,
    *,
    openai_api_key: str | None,
    openai_model: str,
    openai_timeout_seconds: int,
) -> dict[str, Any]:
    pmid = _ask_openai_for_pmid(
        api_key=openai_api_key,
        model=openai_model,
        citation=citation,
        timeout_seconds=openai_timeout_seconds,
    )
    if not pmid:
        return {"pmid": "", "query": "openai_web_search", "match_status": "openai_fail"}

    summary = _summaries(session, [pmid]).get(pmid, {})
    if not summary:
        return {"pmid": "", "query": f"openai_web_search:{pmid}", "match_status": "openai_invalid_pmid"}
    if not _metadata_matches_citation(citation, summary):
        return {"pmid": "", "query": f"openai_web_search:{pmid}", "match_status": "openai_metadata_mismatch"}
    return {"pmid": pmid, "query": f"openai_web_search:{pmid}", "match_status": "openai_web_search"}


def _write_text(directory: str | Path, filename: str, text: str) -> str:
    if not text:
        return ""
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    destination = path / filename
    destination.write_text(text.strip() + "\n", encoding="utf-8")
    return str(destination)


def _fetch_pmc_text(session: requests.Session, pmcid: str) -> str:
    return strip_tags(fetch_pmc_xml(session, pmcid))


def enrich_and_store_article(
    *,
    store: DynamoStore,
    session: requests.Session,
    citation: str,
    study_label: str,
    review: dict[str, Any],
    outcome: dict[str, Any],
    stance: str,
    abstract_dir: str | Path,
    full_text_dir: str | Path,
    openai_api_key: str | None,
    openai_model: str,
    openai_timeout_seconds: int,
    effect_measure: str = "",
    line_of_no_effect: str = "",
    effect_estimate: str = "",
    confidence_interval_begin: str = "",
    confidence_interval_end: str = "",
    confidence_interval_percentage: str = "",
) -> dict[str, Any]:
    article_id = store.next_article_id()
    resolved = resolve_pubmed(
        session,
        citation,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        openai_timeout_seconds=openai_timeout_seconds,
    )
    pmid = resolved["pmid"]
    summary = _summaries(session, [pmid]).get(pmid, {}) if pmid else {}
    pmcid = _lookup_pmcid(session, pmid) if pmid else ""
    abstract = ""
    full_text = ""
    abstract_path = ""
    full_text_path = ""
    errors: list[str] = []

    if pmid:
        try:
            abstract = _fetch_abstract(session, pmid)
            abstract_path = _write_text(abstract_dir, f"{article_id}_abstract.txt", abstract)
        except (RuntimeError, requests.RequestException, OSError) as exc:
            errors.append(f"abstract: {exc}")
    if pmcid:
        try:
            full_text = _fetch_pmc_text(session, pmcid)
            full_text_path = _write_text(full_text_dir, f"{article_id}_full_text.txt", full_text)
            time.sleep(0.34)
        except (RuntimeError, requests.RequestException, OSError) as exc:
            errors.append(f"full_text: {exc}")

    item = {
        "article_id": article_id,
        "review_id": review["review_id"],
        "review_pmid": review["pmid"],
        "outcome_id": int(outcome["outcome_id"]),
        "outcome_key": outcome.get("outcome_key", ""),
        "stance": stance,
        "study_label": study_label,
        "effect_measure": effect_measure or None,
        "effect_estimate": effect_estimate or None,
        "confidence_interval_begin": confidence_interval_begin or None,
        "confidence_interval_end": confidence_interval_end or None,
        "confidence_interval_percentage": confidence_interval_percentage or None,
        "line_of_no_effect": line_of_no_effect or None,
        "citation": citation,
        "pmid": pmid or None,
        "pmcid": pmcid or None,
        "title": str(summary.get("title") or _extract_title_year(citation)[0] or ""),
        "journal": str(summary.get("fulljournalname") or summary.get("source") or ""),
        "year": str(summary.get("pubdate") or summary.get("epubdate") or "")[:4],
        "pubmed_url": PUBMED_ARTICLE_URL.format(pmid=pmid) if pmid else None,
        "pmc_url": PMC_ARTICLE_URL.format(pmcid=pmcid) if pmcid else None,
        "abstract_path": abstract_path or None,
        "full_text_path": full_text_path or None,
        "pubmed_query": resolved.get("query", ""),
        "match_status": resolved.get("match_status", ""),
        "enrichment_errors": errors,
        "created_at": datetime.now(UTC).isoformat(),
    }
    store.put_article(item)
    return item
