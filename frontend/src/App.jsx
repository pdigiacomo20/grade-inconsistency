import React, { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowLeft, BarChart3, Download, ExternalLink, FileText, RefreshCw, Search, Trash2, Upload } from "lucide-react";

const API_BASE = import.meta.env.VITE_API_BASE || "";

function apiHref(path) {
  return `${API_BASE}${path}`;
}

async function fetchJson(path, options = {}) {
  const response = await fetch(apiHref(path), {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }
  if (!response.ok) {
    throw new Error(data.detail || `Request failed: ${response.status}`);
  }
  return data;
}

function normalize(value) {
  return String(value || "").toLowerCase();
}

function Pill({ children, tone = "neutral" }) {
  return <span className={`pill ${tone}`}>{children}</span>;
}

function formatRate(value) {
  return value === null || value === undefined ? "n/a" : `${(Number(value) * 100).toFixed(1)}%`;
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : String(value);
}

function markdownPreview(markdown, sentenceLimit = 3) {
  const text = String(markdown || "")
    .replace(/\|/g, " ")
    .replace(/[#*_`>-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "";
  const sentences = text.match(/[^.!?]+[.!?]+(?:\s|$)|[^.!?]+$/g) || [text];
  const preview = sentences.slice(0, sentenceLimit).join(" ").trim();
  return preview.length > 420 ? `${preview.slice(0, 417).trim()}...` : preview;
}

function MarkdownInline({ text }) {
  const parts = String(text || "").split(/(\*\*[^*]+\*\*)/g);
  return (
    <>
      {parts.map((part, index) => (
        part.startsWith("**") && part.endsWith("**")
          ? <strong key={index}>{part.slice(2, -2)}</strong>
          : <React.Fragment key={index}>{part}</React.Fragment>
      ))}
    </>
  );
}

function MarkdownView({ markdown }) {
  const lines = String(markdown || "").split(/\r?\n/);
  const blocks = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2] });
      index += 1;
      continue;
    }
    if (line.trim().startsWith("|")) {
      const tableLines = [];
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        tableLines.push(lines[index]);
        index += 1;
      }
      const rows = tableLines
        .filter((tableLine) => !/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(tableLine))
        .map((tableLine) => tableLine.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim()));
      if (rows.length) blocks.push({ type: "table", rows });
      continue;
    }
    if (/^\s*(?:[-*]|\d+\.)\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*(?:[-*]|\d+\.)\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*(?:[-*]|\d+\.)\s+/, ""));
        index += 1;
      }
      blocks.push({ type: "list", items });
      continue;
    }
    const paragraph = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^(#{1,6})\s+/.test(lines[index]) &&
      !lines[index].trim().startsWith("|") &&
      !/^\s*(?:[-*]|\d+\.)\s+/.test(lines[index])
    ) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    blocks.push({ type: "paragraph", text: paragraph.join(" ") });
  }

  return (
    <div className="markdownBody">
      {blocks.map((block, index) => {
        if (block.type === "heading") {
          const HeadingTag = `h${Math.min(block.level + 2, 6)}`;
          return <HeadingTag key={index}><MarkdownInline text={block.text} /></HeadingTag>;
        }
        if (block.type === "table") {
          const [header, ...body] = block.rows;
          return (
            <div className="markdownTableWrap" key={index}>
              <table className="markdownTable">
                <thead>
                  <tr>{header.map((cell, cellIndex) => <th key={cellIndex}><MarkdownInline text={cell} /></th>)}</tr>
                </thead>
                <tbody>
                  {body.map((row, rowIndex) => (
                    <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}><MarkdownInline text={cell} /></td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        if (block.type === "list") {
          return <ul key={index}>{block.items.map((item, itemIndex) => <li key={itemIndex}><MarkdownInline text={item} /></li>)}</ul>;
        }
        return <p key={index}><MarkdownInline text={block.text} /></p>;
      })}
    </div>
  );
}

function LinkOut({ href, children }) {
  if (!href) return <span className="muted">Unavailable</span>;
  return (
    <a href={href} target="_blank" rel="noreferrer" className="linkIcon">
      {children} <ExternalLink size={14} />
    </a>
  );
}

function ReviewsView({ reviews, onOpen }) {
  const [query, setQuery] = useState("");
  const [hideProtocols, setHideProtocols] = useState(true);
  const [hideNoInconsistency, setHideNoInconsistency] = useState(true);
  const filtered = useMemo(() => {
    const needle = normalize(query);
    return reviews.filter((review) => {
      if (hideProtocols && review.is_protocol_only) return false;
      if (hideNoInconsistency && (review.has_inconsistency === false || review.status === "no_inconsistency")) return false;
      return [review.review_id, review.pmid, review.title, review.year, review.journal, review.status].some((value) =>
        normalize(value).includes(needle),
      );
    });
  }, [reviews, query, hideProtocols, hideNoInconsistency]);

  return (
    <>
      <div className="toolbar">
        <div className="searchBox">
          <Search size={18} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search reviews" />
        </div>
        <label className="checkControl">
          <input type="checkbox" checked={hideProtocols} onChange={(event) => setHideProtocols(event.target.checked)} />
          Hide protocols only
        </label>
        <label className="checkControl">
          <input type="checkbox" checked={hideNoInconsistency} onChange={(event) => setHideNoInconsistency(event.target.checked)} />
          Hide no inconsistency
        </label>
      </div>
      <div className="tableWrap">
        <table className="reviewsTable">
          <thead>
            <tr>
              <th>CSR ID</th>
              <th>Title</th>
              <th>Year</th>
              <th>Journal</th>
              <th>PMC</th>
              <th>Protocol Only</th>
              <th>Inconsistency</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((review) => (
              <tr key={review.pmid}>
                <td>
                  <button className="linkButton" onClick={() => onOpen(review.review_id || review.pmid)}>
                    {review.review_id || review.pmid}
                  </button>
                </td>
                <td className="titleCell">{review.title}</td>
                <td>{review.year}</td>
                <td>{review.journal}</td>
                <td>
                  <LinkOut href={review.pmc_url}>PMC</LinkOut>
                </td>
                <td>{review.is_protocol_only ? <Pill tone="warn">Yes</Pill> : <Pill>No</Pill>}</td>
                <td>{review.has_inconsistency === false || review.status === "no_inconsistency" ? <Pill tone="warn">No</Pill> : <Pill>Yes</Pill>}</td>
                <td>{review.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {filtered.length === 0 && <div className="empty">No reviews match the current filters.</div>}
      </div>
    </>
  );
}

function OverallNotes({ review }) {
  const notes = [
    { label: "SoF overall notes", value: review?.sof_overall_notes },
    { label: "Studies overall notes", value: review?.studies_overall_notes },
    { label: "Characteristics overall notes", value: review?.characteristics_overall_notes },
    { label: "Plain language summary", value: review?.plain_language_summary },
    { label: "Excluded overall notes", value: review?.excluded_overall_notes },
  ].filter((item) => String(item.value || "").trim());

  if (!notes.length) return null;

  return (
    <section className="notesGrid">
      {notes.map((item) => (
        <div className="notesPanel" key={item.label}>
          <h2>{item.label}</h2>
          <p>{item.value}</p>
        </div>
      ))}
    </section>
  );
}

function ReviewMetadata({ review }) {
  const items = [
    { label: "PMID", value: review.pmid },
    { label: "Year", value: review.year || "Year unknown" },
    { label: "Journal", value: review.journal || "Journal unknown" },
    { label: "Status", value: review.status },
    { label: "PMCID", value: review.pmcid },
    { label: "License", value: review.license },
  ];

  return (
    <section className="metadataGrid" aria-label="Review metadata">
      {items.map((item) => (
        <div className="metadataItem" key={item.label}>
          <span>{item.label}</span>
          <strong>{item.value || <span className="muted">Missing</span>}</strong>
        </div>
      ))}
    </section>
  );
}

function OutcomeTable({ outcomes, evaluationByOutcome = {} }) {
  return (
    <div className="tableWrap compact">
      <table>
        <thead>
          <tr>
            <th>Outcome</th>
            <th>SoF Table</th>
            <th>Row</th>
            <th>Medical Question</th>
            <th>Consensus Answer</th>
            <th>Benchmark Answer</th>
            <th>Eval Parametric</th>
            <th>Certainty</th>
            <th>Forest Plot</th>
            <th>Effect Measure</th>
            <th>Unit</th>
            <th>Polarity</th>
            <th>Comparator Effect</th>
            <th>Aggregated Estimate</th>
            <th>Aggregated CI</th>
            <th>Aggregated Sample</th>
            <th>Included Articles</th>
            <th>Downgrade Reasoning</th>
          </tr>
        </thead>
        <tbody>
          {outcomes.map((outcome) => (
            <tr key={outcome.outcome_id}>
              {(() => {
                const evalOutcome = evaluationByOutcome[`${outcome.pmid}::${outcome.outcome_id}`] || {};
                return (
                  <>
              <td>{outcome.outcome_id}</td>
              <td>{outcome.sof_table}</td>
              <td>{outcome.row}</td>
              <td>{outcome.question}</td>
              <td>{outcome.consensus_answer}</td>
              <td>m</td>
              <td>{evalOutcome.parametric?.answer || <span className="muted">No run</span>}</td>
              <td>{outcome.certainty}</td>
              <td>{outcome.forest_plot_title || <span className="muted">Pending</span>}</td>
              <td>{outcome.effect_measure || <span className="muted">Pending</span>}</td>
              <td>{outcome.unit_of_measure || <span className="muted">Pending</span>}</td>
              <td>{outcome.polarity_of_measure || <span className="muted">Pending</span>}</td>
              <td>{outcome.comparator_effect_measure || outcome.line_of_no_effect || <span className="muted">Pending</span>}</td>
              <td>{outcome.aggregated_effect_estimate || <span className="muted">Pending</span>}</td>
              <td>
                {outcome.aggregated_confidence_interval_begin && outcome.aggregated_confidence_interval_end ? (
                  <>
                    {outcome.aggregated_confidence_interval_begin} to {outcome.aggregated_confidence_interval_end}
                    {outcome.aggregated_confidence_interval_percentage ? ` (${outcome.aggregated_confidence_interval_percentage}%)` : ""}
                  </>
                ) : (
                  <span className="muted">Pending</span>
                )}
              </td>
              <td>{outcome.aggregated_sample_size || <span className="muted">Pending</span>}</td>
              <td>{(outcome.included_articles || []).join(", ") || <span className="muted">None</span>}</td>
              <td>{outcome.downgrade_reasoning}</td>
                  </>
                );
              })()}
            </tr>
          ))}
        </tbody>
      </table>
      {outcomes.length === 0 && <div className="empty">No extracted inconsistency outcomes.</div>}
    </div>
  );
}

function ArticlesTable({ articles, onProcessPmid, onManualFailed, evaluationByArticle = {} }) {
  const [sortByOutcome, setSortByOutcome] = useState(true);
  const [articleTab, setArticleTab] = useState("included");
  const [viewMode, setViewMode] = useState("full");
  const [pmidInputs, setPmidInputs] = useState({});
  const [processingArticle, setProcessingArticle] = useState("");
  const [rowError, setRowError] = useState("");
  const [selectedCharacteristics, setSelectedCharacteristics] = useState(null);
  const tabbedArticles = useMemo(() => {
    return articles.filter((article) => (
      articleTab === "excluded"
        ? article.article_type === "excluded_study"
        : article.article_type !== "excluded_study"
    ));
  }, [articles, articleTab]);
  const rows = useMemo(() => {
    const copy = [...tabbedArticles];
    if (sortByOutcome) {
      copy.sort((a, b) => Number(a.outcome_id || 0) - Number(b.outcome_id || 0) || String(a.article_id).localeCompare(String(b.article_id)));
    }
    return copy;
  }, [tabbedArticles, sortByOutcome]);

  const processPmid = async (article) => {
    const pmid = String(pmidInputs[article.article_id] || article.pmid || "").trim();
    if (!pmid) {
      setRowError(`Enter a PMID for ${article.article_id}.`);
      return;
    }
    setProcessingArticle(article.article_id);
    setRowError("");
    try {
      await onProcessPmid(article.article_id, pmid);
      setPmidInputs((current) => ({ ...current, [article.article_id]: "" }));
    } catch (err) {
      setRowError(err.message);
    } finally {
      setProcessingArticle("");
    }
  };

  const markManualFailed = async (article) => {
    setProcessingArticle(article.article_id);
    setRowError("");
    try {
      await onManualFailed(article.article_id);
    } catch (err) {
      setRowError(err.message);
    } finally {
      setProcessingArticle("");
    }
  };

  const renderCi = (article) => (
    article.confidence_interval_begin && article.confidence_interval_end ? (
      <>
        {article.confidence_interval_begin} to {article.confidence_interval_end}
        {article.confidence_interval_percentage ? ` (${article.confidence_interval_percentage}%)` : ""}
      </>
    ) : (
      <span className="muted">Missing</span>
    )
  );

  const renderPmid = (article) => (
    <div className="pmidCell">
      {article.manual_extraction_failed ? (
        <span className="muted">Manual extract failed</span>
      ) : (
        <LinkOut href={article.pubmed_url}>{article.pmid || "PMID"}</LinkOut>
      )}
      <div className="pmidControl">
        <input
          value={pmidInputs[article.article_id] || ""}
          onChange={(event) => setPmidInputs((current) => ({ ...current, [article.article_id]: event.target.value }))}
          placeholder="PMID"
          inputMode="numeric"
        />
        <button
          className="smallButton"
          disabled={processingArticle === article.article_id}
          onClick={() => processPmid(article)}
        >
          {processingArticle === article.article_id ? <RefreshCw size={14} className="spin" /> : null}
          Process PMID
        </button>
        <button
          className="smallButton warningButton"
          disabled={processingArticle === article.article_id || Boolean(article.pmid)}
          onClick={() => markManualFailed(article)}
        >
          <AlertTriangle size={14} />
          Manual extract failed
        </button>
      </div>
    </div>
  );

  const renderFiles = (article) => (
    <div className="fileLinks">
      {article.abstract_path ? <a href={apiHref(`/api/articles/${article.article_id}/abstract`)}>Abstract</a> : <span className="muted">No abstract</span>}
      {article.full_text_path ? <a href={apiHref(`/api/articles/${article.article_id}/full-text`)}>Full text</a> : <span className="muted">No full text</span>}
    </div>
  );

  const renderEvalAnswer = (article) => (
    evaluationByArticle[article.article_id]?.answer ? (
      <>
        <Pill>{evaluationByArticle[article.article_id].answer}</Pill>{" "}
        <span className="muted">{evaluationByArticle[article.article_id].memorization_label || ""}</span>
      </>
    ) : (
      <span className="muted">No run</span>
    )
  );

  const renderCharacteristics = (article) => {
    const markdown = article.characteristics_markdown || "";
    if (!markdown) return <span className="muted">Missing</span>;
    return (
      <div className="characteristicsCell">
        <p>{markdownPreview(markdown)}</p>
        <button className="smallButton" onClick={() => setSelectedCharacteristics(article)}>View formatted</button>
      </div>
    );
  };

  const columns = [
    { id: "study", label: "Study", className: "stickyColumn", render: (article) => article.study_label || <span className="muted">Unlabeled</span> },
    { id: "article_id", label: "Article ID", render: (article) => article.article_id },
    { id: "outcome_id", label: "Outcome", render: (article) => article.outcome_id || <span className="muted">n/a</span> },
    { id: "type", label: "Type", render: (article) => article.article_type === "excluded_study" ? <Pill tone="warn">Excluded</Pill> : <Pill>Included</Pill> },
    { id: "pmid", label: "PMID", render: renderPmid },
    { id: "pmcid", label: "PMCID", render: (article) => <LinkOut href={article.pmc_url}>{article.pmcid || "PMC"}</LinkOut> },
    { id: "files", label: "Files", render: renderFiles },
    { id: "citation", label: "Citation", className: "titleCell", render: (article) => article.citation || <span className="muted">Missing</span> },
    { id: "title", label: "Title", className: "titleCell", render: (article) => article.title || <span className="muted">Missing</span> },
    { id: "effect_measure", label: "Effect measure", render: (article) => article.effect_measure || <span className="muted">Missing</span> },
    { id: "unit_of_measure", label: "unit", render: (article) => article.unit_of_measure || <span className="muted">Missing</span> },
    { id: "polarity_of_measure", label: "polarity", render: (article) => article.polarity_of_measure || <span className="muted">Missing</span> },
    { id: "comparator_effect_measure", label: "comparator effect", render: (article) => article.comparator_effect_measure || article.line_of_no_effect || <span className="muted">Missing</span> },
    { id: "effect_estimate", label: "Effect estimate", render: (article) => article.effect_estimate || <span className="muted">Missing</span> },
    { id: "ci", label: "CI", render: renderCi },
    { id: "sample_size", label: "sample size", render: (article) => article.sample_size || <span className="muted">Missing</span> },
    {
      id: "wald_z_category",
      label: "z category",
      render: (article) => article.wald_z_category ? <Pill>{article.wald_z_category}</Pill> : <span className="muted">{article.wald_z_error ? "Unclassified" : "Missing"}</span>,
    },
    { id: "wald_z", label: "wald-Z", render: (article) => formatNumber(article.wald_z) || <span className="muted">{article.wald_z_error || "Missing"}</span> },
    { id: "characteristics", label: "characteristics", className: "titleCell", render: renderCharacteristics },
    { id: "eval_context_answer", label: "eval context answer", render: renderEvalAnswer },
    { id: "reason_for_exclusion", label: "exclusion reason", className: "titleCell", render: (article) => article.reason_for_exclusion || <span className="muted">n/a</span> },
    { id: "match_status", label: "match", render: (article) => article.match_status || <span className="muted">Not matched</span> },
  ];
  const viewColumns = {
    full: columns.map((column) => column.id),
    results: ["study", "article_id", "outcome_id", "type", "effect_measure", "unit_of_measure", "polarity_of_measure", "comparator_effect_measure", "effect_estimate", "ci", "sample_size", "wald_z_category", "wald_z", "eval_context_answer", "match_status"],
    characteristics: ["study", "article_id", "outcome_id", "type", "characteristics", "eval_context_answer", "reason_for_exclusion", "match_status"],
    citation: ["study", "article_id", "outcome_id", "type", "pmid", "pmcid", "files", "citation", "title", "match_status"],
  };
  const visibleColumns = columns.filter((column) => viewColumns[viewMode].includes(column.id));
  const includedCount = articles.filter((article) => article.article_type !== "excluded_study").length;
  const excludedCount = articles.filter((article) => article.article_type === "excluded_study").length;

  return (
    <>
      <div className="sectionHeader">
        <h2>Associated Articles</h2>
        <div className="articleControls">
          <div className="segmentedControl" role="tablist" aria-label="Article type">
            <button className={articleTab === "included" ? "active" : ""} onClick={() => setArticleTab("included")}>Included ({includedCount})</button>
            <button className={articleTab === "excluded" ? "active" : ""} onClick={() => setArticleTab("excluded")}>Excluded ({excludedCount})</button>
          </div>
          <div className="segmentedControl" aria-label="Article table view">
            <button className={viewMode === "full" ? "active" : ""} onClick={() => setViewMode("full")}>Full</button>
            <button className={viewMode === "results" ? "active" : ""} onClick={() => setViewMode("results")}>Results</button>
            <button className={viewMode === "characteristics" ? "active" : ""} onClick={() => setViewMode("characteristics")}>Characteristics</button>
            <button className={viewMode === "citation" ? "active" : ""} onClick={() => setViewMode("citation")}>Citation</button>
          </div>
          <label className="checkControl">
            <input type="checkbox" checked={sortByOutcome} onChange={(event) => setSortByOutcome(event.target.checked)} />
            Sort by outcome
          </label>
        </div>
      </div>
      <div className="tableWrap compact articlesTableWrap">
        <table className="articlesTable">
          <thead>
            <tr>
              {visibleColumns.map((column) => (
                <th className={column.className || ""} key={column.id}>{column.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((article) => (
              <tr key={article.article_id}>
                {visibleColumns.map((column) => (
                  <td className={column.className || ""} key={column.id}>{column.render(article)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {rowError && <div className="error compactError">{rowError}</div>}
        {rows.length === 0 && <div className="empty">No {articleTab} articles have been extracted for this review.</div>}
      </div>
      {selectedCharacteristics && (
        <div className="modalBackdrop" role="presentation" onClick={() => setSelectedCharacteristics(null)}>
          <div className="modalPanel" role="dialog" aria-modal="true" aria-label="Study characteristics" onClick={(event) => event.stopPropagation()}>
            <div className="modalHeader">
              <div>
                <h2>{selectedCharacteristics.study_label || selectedCharacteristics.article_id}</h2>
                <p>Study characteristics</p>
              </div>
              <button className="smallButton" onClick={() => setSelectedCharacteristics(null)}>Close</button>
            </div>
            <MarkdownView markdown={selectedCharacteristics.characteristics_markdown || ""} />
          </div>
        </div>
      )}
    </>
  );
}

const WALD_Z_CATEGORIES = ["COM-Z2+", "COM-Z1", "COM-Z0", "INT-Z0", "INT-Z1", "INT-Z2+"];
const PARAMETRIC_ANSWERS = ["y", "n", "m"];

function answerValue(value) {
  const answer = String(value || "").trim().toLowerCase();
  return PARAMETRIC_ANSWERS.includes(answer) ? answer : "";
}

function evaluationRows(run) {
  const rows = [];
  for (const outcome of run?.outcomes || []) {
    const parametricAnswer = answerValue(outcome.parametric?.answer);
    for (const context of outcome.contexts || []) {
      rows.push({
        articleId: context.article_id,
        answer: answerValue(context.answer),
        parametricAnswer,
        waldZCategory: context.wald_z_category || "",
        waldZ: context.wald_z,
      });
    }
  }
  return rows;
}

function accuracySummary(rows, selectedCategories, selectedParametric) {
  const selected = rows.filter((row) => selectedCategories.includes(row.waldZCategory) && row.parametricAnswer === selectedParametric && row.answer);
  const correct = selected.filter((row) => row.answer === "m").length;
  return {
    correct,
    total: selected.length,
    accuracy: selected.length ? correct / selected.length : null,
  };
}

function EvaluationAccuracyExplorer({ run }) {
  const rows = useMemo(() => evaluationRows(run), [run]);
  const [selectedCategories, setSelectedCategories] = useState(WALD_Z_CATEGORIES);
  const [selectedParametric, setSelectedParametric] = useState("m");
  const summary = useMemo(
    () => accuracySummary(rows, selectedCategories, selectedParametric),
    [rows, selectedCategories, selectedParametric],
  );
  const chartData = useMemo(() => {
    return PARAMETRIC_ANSWERS.map((parametricAnswer) => ({
      parametricAnswer,
      bars: WALD_Z_CATEGORIES.map((category) => ({
        category,
        ...accuracySummary(rows, [category], parametricAnswer),
      })),
    }));
  }, [rows]);

  const toggleCategory = (category) => {
    setSelectedCategories((current) => (
      current.includes(category)
        ? current.filter((item) => item !== category)
        : [...current, category]
    ));
  };

  if (!run) return <div className="empty">No evaluation data available.</div>;

  return (
    <>
      <section className="accuracyExplorer">
        <div className="filterPanel">
          <div>
            <h2>Accuracy Filters</h2>
            <p>Accuracy is the share of contextual answers equal to m.</p>
          </div>
          <div className="filterGrid">
            <fieldset>
              <legend>Wald-Z Category</legend>
              <div className="categoryChecks">
                {WALD_Z_CATEGORIES.map((category) => (
                  <label className="checkControl" key={category}>
                    <input
                      type="checkbox"
                      checked={selectedCategories.includes(category)}
                      onChange={() => toggleCategory(category)}
                    />
                    {category}
                  </label>
                ))}
              </div>
            </fieldset>
            <label className="selectControl">
              Parametric Answer
              <select value={selectedParametric} onChange={(event) => setSelectedParametric(event.target.value)}>
                {PARAMETRIC_ANSWERS.map((answer) => (
                  <option value={answer} key={answer}>{answer}</option>
                ))}
              </select>
            </label>
          </div>
        </div>

        <div className="accuracyResult">
          <span>Filtered Accuracy</span>
          <strong>{formatRate(summary.accuracy)}</strong>
          <p>{summary.correct} m answers / {summary.total} contextual answers</p>
        </div>
      </section>

      <section className="chartGrid">
        {chartData.map((chart) => (
          <div className="chartPanel" key={chart.parametricAnswer}>
            <h2>Parametric {chart.parametricAnswer}</h2>
            <div className="barChart">
              {chart.bars.map((bar) => (
                <div className="barRow" key={bar.category}>
                  <span className="barLabel">{bar.category}</span>
                  <div className="barTrack">
                    <div className="barFill" style={{ width: `${Math.max(0, Number(bar.accuracy || 0) * 100)}%` }} />
                  </div>
                  <span className="barValue">{formatRate(bar.accuracy)}</span>
                  <span className="barCount">{bar.correct}/{bar.total}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </section>
    </>
  );
}

function MessageLog({ messages }) {
  if (!messages?.length) return null;
  return (
    <div className="messageStack">
      {messages.map((message, index) => (
        <div className="messageBlock" key={index}>
          <div className="messageRole">{message.role || `message ${index + 1}`}</div>
          <pre>{message.content || ""}</pre>
        </div>
      ))}
    </div>
  );
}

function MultiturnTranscript({ run }) {
  const rows = [];
  for (const outcome of run?.outcomes || []) {
    for (const context of outcome.contexts || []) {
      if (context.detail_exposure_type === "multiturn_char" || context.multiturn?.turns?.length) {
        rows.push({ outcome, context });
      }
    }
  }

  if (!rows.length) return null;

  return (
    <section className="transcriptPanel">
      <div className="sectionHeader transcriptHeader">
        <div>
          <h2>Multiturn Conversation Logs</h2>
          <p>Exact saved prompts and outputs for the evaluated model and gatekeeper classifier.</p>
        </div>
        <Pill>{rows.length} contexts</Pill>
      </div>
      <div className="transcriptList">
        {rows.map(({ outcome, context }, index) => (
          <details className="transcriptItem" key={`${outcome.pmid}-${outcome.outcome_id}-${context.article_id}-${index}`}>
            <summary>
              <span>Q{outcome.outcome_id} · {outcome.review_id || outcome.pmid} · {context.article_id}</span>
              <span>
                <Pill tone={context.answer === "m" ? "neutral" : "warn"}>{context.answer || "error"}</Pill>
              </span>
            </summary>
            <div className="transcriptMeta">
              <div><strong>Question</strong><p>{outcome.question}</p></div>
              <div><strong>Study</strong><p>{context.title || context.citation || context.article_id}</p></div>
              {context.error ? <div><strong>Error</strong><p>{context.error}</p></div> : null}
            </div>
            {(context.multiturn?.turns || []).map((turn) => (
              <div className="turnLog" key={turn.round}>
                <h3>Evaluated LLM Turn {turn.round}</h3>
                <h4>Prompt</h4>
                <MessageLog messages={turn.llm_request?.messages || []} />
                <h4>Output</h4>
                <pre>{turn.raw_response || ""}</pre>
                {turn.gatekeeper_responses?.length ? (
                  <div className="gatekeeperLogs">
                    <h3>Gatekeeper</h3>
                    {turn.gatekeeper_responses.map((response, responseIndex) => (
                      <details className="gatekeeperItem" key={`${turn.round}-${responseIndex}`}>
                        <summary>
                          <span>{response.study_id}: {response.classification}</span>
                          <span>{response.revealed_section || "no reveal"}</span>
                        </summary>
                        <div className="messageBlock">
                          <div className="messageRole">follow-up question</div>
                          <pre>{response.question || ""}</pre>
                        </div>
                        <h4>Gatekeeper Prompt</h4>
                        <MessageLog messages={response.gatekeeper?.messages || []} />
                        {response.gatekeeper?.raw_response ? (
                          <>
                            <h4>Gatekeeper Output</h4>
                            <pre>{response.gatekeeper.raw_response}</pre>
                          </>
                        ) : null}
                        {response.gatekeeper?.error ? (
                          <div className="error compactError">{response.gatekeeper.error}</div>
                        ) : null}
                        <h4>Response Sent To Evaluated LLM</h4>
                        <pre>{response.response || ""}</pre>
                      </details>
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
          </details>
        ))}
      </div>
    </section>
  );
}

function ExtractionPanel({ title, kind, value, onChange, onSubmit, busy, performed, rawText, editing, onEdit }) {
  if (performed && !editing) {
    return (
      <div className="extractPanel">
        <div className="extractPanelHeader">
          <h2>{title}</h2>
          <Pill>Extracted</Pill>
        </div>
        <button className="primaryButton" disabled={Boolean(busy)} onClick={() => onEdit(kind, rawText || "")}>
          <FileText size={16} /> Edit extraction
        </button>
      </div>
    );
  }

  return (
    <div className="extractPanel">
      <div className="extractPanelHeader">
        <h2>{title}</h2>
        {editing ? <Pill tone="warn">Editing</Pill> : null}
      </div>
      <textarea value={value} onChange={(event) => onChange(event.target.value)} />
      <button className="primaryButton" disabled={busy === kind} onClick={() => onSubmit(kind)}>
        {busy === kind ? <RefreshCw size={16} className="spin" /> : <FileText size={16} />} {editing ? "Submit edit" : title}
      </button>
    </div>
  );
}

function EvaluationsView() {
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState("");
  const [run, setRun] = useState(null);
  const [error, setError] = useState("");

  const loadRuns = () => {
    fetchJson("/api/evaluations")
      .then((data) => {
        const items = data.evaluations || [];
        setRuns(items);
        setSelected((current) => current || items[0]?.filename || "");
      })
      .catch((err) => setError(err.message));
  };

  useEffect(() => {
    loadRuns();
    const timer = window.setInterval(loadRuns, 4000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!selected) {
      setRun(null);
      return;
    }
    const loadSelectedRun = () => {
      fetchJson(`/api/evaluations/${encodeURIComponent(selected)}`)
        .then(setRun)
        .catch((err) => setError(err.message));
    };
    loadSelectedRun();
    const timer = window.setInterval(loadSelectedRun, 4000);
    return () => window.clearInterval(timer);
  }, [selected]);

  return (
    <>
      {error && <div className="error">{error}</div>}
      <div className="sectionHeader">
        <h2>Evaluations</h2>
        <select value={selected} onChange={(event) => setSelected(event.target.value)}>
          <option value="">Select run</option>
          {runs.map((item) => (
            <option value={item.filename} key={item.filename}>{item.filename}</option>
          ))}
        </select>
      </div>
      {run ? (
        <>
          <section className="detailHeader">
            <div>
              <h2>{run.metadata?.run_id || run.task}: {run.metadata?.model}</h2>
              <p>
                {run.metadata?.provider} · {run.metadata?.created_at} · {selected}
                {run.metadata?.status ? ` · ${run.metadata.status}` : ""}
                {run.metadata?.total_outcomes ? ` · ${run.metadata.completed_outcomes || 0}/${run.metadata.total_outcomes} outcomes` : ""}
              </p>
            </div>
          </section>
          <EvaluationAccuracyExplorer run={run} />
          <MultiturnTranscript run={run} />
        </>
      ) : (
        <div className="empty">No evaluation runs found.</div>
      )}
    </>
  );
}

function ReviewDetail({ reviewId, onBack, onReviewUpdated }) {
  const [payload, setPayload] = useState(null);
  const [sofText, setSofText] = useState("");
  const [studiesText, setStudiesText] = useState("");
  const [characteristicsText, setCharacteristicsText] = useState("");
  const [excludedText, setExcludedText] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [evaluationRuns, setEvaluationRuns] = useState([]);
  const [selectedEvaluation, setSelectedEvaluation] = useState("");
  const [reviewEvaluation, setReviewEvaluation] = useState(null);
  const [editModes, setEditModes] = useState({});
  const [documentFiles, setDocumentFiles] = useState([]);
  const [documentInputKey, setDocumentInputKey] = useState(0);

  const load = () => {
    setError("");
    fetchJson(`/api/reviews/${encodeURIComponent(reviewId)}`)
      .then(setPayload)
      .catch((err) => setError(err.message));
  };

  useEffect(load, [reviewId]);

  useEffect(() => {
    fetchJson("/api/evaluations")
      .then((data) => setEvaluationRuns(data.evaluations || []))
      .catch(() => setEvaluationRuns([]));
  }, []);

  useEffect(() => {
    if (!selectedEvaluation) {
      setReviewEvaluation(null);
      return;
    }
    fetchJson(`/api/reviews/${encodeURIComponent(reviewId)}/evaluations/${encodeURIComponent(selectedEvaluation)}`)
      .then(setReviewEvaluation)
      .catch((err) => setError(err.message));
  }, [reviewId, selectedEvaluation]);

  const submit = async (kind) => {
    setBusy(kind);
    setError("");
    setMessage("");
    try {
      const config = {
        sof: { path: "extract-sof", text: sofText, success: (result) => result.message || "SoF extracted." },
        studies: { path: "extract-studies", text: studiesText, success: (result) => `Studies extracted. Added ${result.article_count || 0} articles.` },
        characteristics: { path: "extract-characteristics", text: characteristicsText, success: (result) => `Characteristics extracted. Updated ${result.updated_article_count || 0} articles.` },
        excluded: { path: "extract-excluded", text: excludedText, success: (result) => `Excluded studies extracted. Added ${result.article_count || 0} articles.` },
      }[kind];
      const result = await fetchJson(`/api/reviews/${encodeURIComponent(reviewId)}/${config.path}`, {
        method: "POST",
        body: JSON.stringify({ text: config.text }),
      });
      setPayload(result.review ? result : { ...payload, ...result });
      if (result.review) onReviewUpdated(result.review);
      setEditModes((current) => ({ ...current, [kind]: false }));
      setMessage(config.success(result));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  };

  const editExtraction = (kind, rawText) => {
    const setters = {
      sof: setSofText,
      studies: setStudiesText,
      characteristics: setCharacteristicsText,
      excluded: setExcludedText,
    };
    setters[kind](rawText);
    setEditModes((current) => ({ ...current, [kind]: true }));
  };

  const deleteExtractions = async () => {
    if (!window.confirm("Delete all extracted outcomes, study rows, notes, and pasted extraction text for this review?")) {
      return;
    }
    setBusy("delete-extractions");
    setError("");
    setMessage("");
    try {
      const result = await fetchJson(`/api/reviews/${encodeURIComponent(reviewId)}/extractions`, {
        method: "DELETE",
      });
      setPayload(result);
      if (result.review) onReviewUpdated(result.review);
      setSofText("");
      setStudiesText("");
      setCharacteristicsText("");
      setExcludedText("");
      setEditModes({});
      setMessage(result.message || `Deleted ${result.deleted_outcome_count || 0} outcomes and ${result.deleted_article_count || 0} articles.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  };

  const uploadDocuments = async () => {
    if (!documentFiles.length) {
      setError("Choose at least one document to upload.");
      return;
    }
    setBusy("documents");
    setError("");
    setMessage("");
    try {
      const formData = new FormData();
      for (const file of documentFiles) {
        formData.append("files", file);
      }
      const response = await fetch(apiHref(`/api/reviews/${encodeURIComponent(reviewId)}/documents`), {
        method: "POST",
        body: formData,
      });
      const text = await response.text();
      const result = text ? JSON.parse(text) : {};
      if (!response.ok) {
        throw new Error(result.detail || `Request failed: ${response.status}`);
      }
      setPayload(result);
      if (result.review) onReviewUpdated(result.review);
      setDocumentFiles([]);
      setDocumentInputKey((current) => current + 1);
      setMessage(`Saved ${result.saved_document_count || 0} documents for this review.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  };

  const processArticlePmid = async (articleId, pmid) => {
    const result = await fetchJson(`/api/articles/${encodeURIComponent(articleId)}/process-pmid`, {
      method: "POST",
      body: JSON.stringify({ pmid }),
    });
    setPayload(result.review ? result : { ...payload, articles: articles.map((article) => (article.article_id === articleId ? result.article : article)) });
    if (result.review) onReviewUpdated(result.review);
    setMessage(`Processed PMID ${pmid} for ${articleId}.`);
  };

  const markArticleManualFailed = async (articleId) => {
    const result = await fetchJson(`/api/articles/${encodeURIComponent(articleId)}/manual-extraction-failed`, {
      method: "POST",
    });
    setPayload(result.review ? result : { ...payload, articles: articles.map((article) => (article.article_id === articleId ? result.article : article)) });
    if (result.review) onReviewUpdated(result.review);
    setMessage(`Marked manual extraction failed for ${articleId}.`);
  };

  if (!payload && !error) return <div className="empty">Loading review...</div>;
  const review = payload?.review;
  const outcomes = payload?.outcomes || [];
  const articles = payload?.articles || [];
  const savedDocuments = review?.saved_documents || [];
  const savedDocumentCount = savedDocuments.length;
  const extractionPerformed = {
    sof: Boolean(review?.sof_extracted_at || review?.extraction_result),
    studies: Boolean(review?.studies_extracted_at),
    characteristics: Boolean(review?.characteristics_extracted_at),
    excluded: Boolean(review?.excluded_extracted_at),
  };
  const evaluationByOutcome = {};
  const evaluationByArticle = {};
  for (const outcome of reviewEvaluation?.outcomes || []) {
    evaluationByOutcome[`${outcome.pmid}::${outcome.outcome_id}`] = outcome;
    for (const context of outcome.contexts || []) {
      evaluationByArticle[context.article_id] = context;
    }
  }

  return (
    <>
      <button className="iconButton" onClick={onBack}>
        <ArrowLeft size={18} /> Reviews
      </button>
      {error && <div className="error">{error}</div>}
      {message && <div className="notice">{message}</div>}
      {review && (
        <>
          <section className="detailHeader">
            <div>
              <h2>{review.review_id}: {review.title}</h2>
              <p>{review.pmid} · {review.year || "Year unknown"} · {review.journal || "Journal unknown"}</p>
            </div>
            <div className="headerActions">
              <LinkOut href={review.pmc_url}>PMC entry</LinkOut>
              <a className="buttonLink" href={apiHref(`/api/reviews/${encodeURIComponent(review.review_id || review.pmid)}/pdf`)}>
                <Download size={16} /> Download PDF
              </a>
            </div>
          </section>
          <ReviewMetadata review={review} />
          <section className="documentPanel">
            <div>
              <h2>Saved Documents</h2>
              <p>{savedDocumentCount} saved</p>
            </div>
            <div className="documentControls">
              <input key={documentInputKey} type="file" multiple onChange={(event) => setDocumentFiles(Array.from(event.target.files || []))} />
              <button className="primaryButton" disabled={busy === "documents"} onClick={uploadDocuments}>
                {busy === "documents" ? <RefreshCw size={16} className="spin" /> : <Upload size={16} />} Upload
              </button>
            </div>
            {savedDocumentCount > 0 && (
              <div className="savedDocumentLinks">
                {savedDocuments.map((document, index) => (
                  <a
                    className="buttonLink"
                    href={apiHref(`/api/reviews/${encodeURIComponent(review.review_id || review.pmid)}/documents/${index}`)}
                    key={`${document.path || document.filename || "document"}-${index}`}
                  >
                    <Download size={16} /> {document.filename || `Document ${index + 1}`}
                  </a>
                ))}
              </div>
            )}
          </section>
          <section className="extractGrid">
            <ExtractionPanel title="Extract SoF" kind="sof" value={sofText} onChange={setSofText} onSubmit={submit} busy={busy} performed={extractionPerformed.sof} rawText={review.sof_raw_extraction_text} editing={Boolean(editModes.sof)} onEdit={editExtraction} />
            <ExtractionPanel title="Extract Studies" kind="studies" value={studiesText} onChange={setStudiesText} onSubmit={submit} busy={busy} performed={extractionPerformed.studies} rawText={review.studies_raw_extraction_text} editing={Boolean(editModes.studies)} onEdit={editExtraction} />
            <ExtractionPanel title="Extract Characteristics" kind="characteristics" value={characteristicsText} onChange={setCharacteristicsText} onSubmit={submit} busy={busy} performed={extractionPerformed.characteristics} rawText={review.characteristics_raw_extraction_text} editing={Boolean(editModes.characteristics)} onEdit={editExtraction} />
            <ExtractionPanel title="Extract Excluded" kind="excluded" value={excludedText} onChange={setExcludedText} onSubmit={submit} busy={busy} performed={extractionPerformed.excluded} rawText={review.excluded_raw_extraction_text} editing={Boolean(editModes.excluded)} onEdit={editExtraction} />
          </section>
          <section className="resetPanel">
            <div>
              <h2>Extractions</h2>
              <p>Delete extracted outcomes, study rows, notes, and pasted extraction text for this review.</p>
            </div>
            <button className="dangerButton" disabled={Boolean(busy)} onClick={deleteExtractions}>
              {busy === "delete-extractions" ? <RefreshCw size={16} className="spin" /> : <Trash2 size={16} />} Delete extractions
            </button>
          </section>
          <OverallNotes review={review} />
          <section className="evalSelector">
            <div>
              <h2>Evaluation Run</h2>
              <p>{reviewEvaluation ? `${reviewEvaluation.metadata?.model || ""} · ${selectedEvaluation}` : "Select a run to show evaluation answers for this CSR."}</p>
            </div>
            <select value={selectedEvaluation} onChange={(event) => setSelectedEvaluation(event.target.value)}>
              <option value="">No evaluation selected</option>
              {evaluationRuns.map((item) => (
                <option value={item.filename} key={item.filename}>{item.filename}</option>
              ))}
            </select>
          </section>
          {reviewEvaluation && <EvaluationAccuracyExplorer run={reviewEvaluation} />}
          <div className="sectionHeader"><h2>Extracted Outcomes</h2></div>
          <OutcomeTable outcomes={outcomes} evaluationByOutcome={evaluationByOutcome} />
          <ArticlesTable articles={articles} onProcessPmid={processArticlePmid} onManualFailed={markArticleManualFailed} evaluationByArticle={evaluationByArticle} />
        </>
      )}
    </>
  );
}

export default function App() {
  const [reviews, setReviews] = useState([]);
  const [selectedReview, setSelectedReview] = useState("");
  const [activeTab, setActiveTab] = useState("reviews");
  const [error, setError] = useState("");

  useEffect(() => {
    fetchJson("/api/reviews")
      .then((data) => setReviews(data.reviews || []))
      .catch((err) => setError(err.message));
  }, []);

  const updateReview = (updatedReview) => {
    setReviews((current) =>
      current.map((review) => (String(review.pmid) === String(updatedReview.pmid) ? { ...review, ...updatedReview } : review)),
    );
  };

  return (
    <main>
      <header className="appHeader">
        <div>
          <h1>Grade Inconsistency</h1>
          <p>Manual extraction workflow for 2025 open-access Cochrane reviews</p>
        </div>
      </header>
      <nav className="tabs">
        <button className={activeTab === "reviews" ? "active" : ""} onClick={() => setActiveTab("reviews")}>Reviews</button>
        <button className={activeTab === "evaluations" ? "active" : ""} onClick={() => { setSelectedReview(""); setActiveTab("evaluations"); }}>
          <BarChart3 size={16} /> Evaluations
        </button>
      </nav>
      {error && <div className="error">{error}</div>}
      {!error && activeTab === "reviews" && selectedReview && <ReviewDetail reviewId={selectedReview} onBack={() => setSelectedReview("")} onReviewUpdated={updateReview} />}
      {!error && activeTab === "reviews" && !selectedReview && <ReviewsView reviews={reviews} onOpen={setSelectedReview} />}
      {!error && activeTab === "evaluations" && <EvaluationsView />}
    </main>
  );
}
