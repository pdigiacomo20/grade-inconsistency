import React, { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowLeft, BarChart3, Download, ExternalLink, FileText, RefreshCw, Search } from "lucide-react";

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
  const data = text ? JSON.parse(text) : {};
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
    { label: "Agree/Oppose overall notes", value: review?.agree_oppose_overall_notes },
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
            <th>MC Answer</th>
            <th>Eval Parametric</th>
            <th>Certainty</th>
            <th>Forest Plot</th>
            <th>Effect Measure</th>
            <th>Line of No Effect</th>
            <th>Agreeing Articles</th>
            <th>Opposing Articles</th>
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
              <td>{outcome.mc_answer || <span className="muted">Missing</span>}</td>
              <td>{evalOutcome.parametric?.answer || <span className="muted">No run</span>}</td>
              <td>{outcome.certainty}</td>
              <td>{outcome.forest_plot_title || <span className="muted">Pending</span>}</td>
              <td>{outcome.effect_measure || <span className="muted">Pending</span>}</td>
              <td>{outcome.line_of_no_effect || <span className="muted">Pending</span>}</td>
              <td>{(outcome.agreeing_articles || []).join(", ") || <span className="muted">None</span>}</td>
              <td>{(outcome.opposing_articles || []).join(", ") || <span className="muted">None</span>}</td>
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
  const [pmidInputs, setPmidInputs] = useState({});
  const [processingArticle, setProcessingArticle] = useState("");
  const [rowError, setRowError] = useState("");
  const rows = useMemo(() => {
    const copy = [...articles];
    if (sortByOutcome) {
      copy.sort((a, b) => Number(a.outcome_id || 0) - Number(b.outcome_id || 0) || String(a.article_id).localeCompare(String(b.article_id)));
    }
    return copy;
  }, [articles, sortByOutcome]);

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

  return (
    <>
      <div className="sectionHeader">
        <h2>Associated Articles</h2>
        <label className="checkControl">
          <input type="checkbox" checked={sortByOutcome} onChange={(event) => setSortByOutcome(event.target.checked)} />
          Sort by outcome
        </label>
      </div>
      <div className="tableWrap compact">
        <table>
          <thead>
            <tr>
              <th>Article ID</th>
              <th>Outcome</th>
              <th>Stance</th>
              <th>Study</th>
              <th>Effect Measure</th>
              <th>Effect Estimate</th>
              <th>CI</th>
              <th>Line of No Effect</th>
              <th>Z Category</th>
              <th>Wald Z</th>
              <th>Title</th>
              <th>Citation</th>
              <th>PMID</th>
              <th>PMCID</th>
              <th>Files</th>
              <th>Match</th>
              <th>Eval Context Answer</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((article) => (
              <tr key={article.article_id}>
                <td>{article.article_id}</td>
                <td>{article.outcome_id}</td>
                <td>{article.stance}</td>
                <td>{article.study_label || <span className="muted">Unlabeled</span>}</td>
                <td>{article.effect_measure || <span className="muted">Missing</span>}</td>
                <td>{article.effect_estimate || <span className="muted">Missing</span>}</td>
                <td>
                  {article.confidence_interval_begin && article.confidence_interval_end ? (
                    <>
                      {article.confidence_interval_begin} to {article.confidence_interval_end}
                      {article.confidence_interval_percentage ? ` (${article.confidence_interval_percentage}%)` : ""}
                    </>
                  ) : (
                    <span className="muted">Missing</span>
                  )}
                </td>
                <td>{article.line_of_no_effect || <span className="muted">Missing</span>}</td>
                <td>
                  {article.wald_z_category ? (
                    <Pill>{article.wald_z_category}</Pill>
                  ) : (
                    <span className="muted">{article.wald_z_error ? "Unclassified" : "Missing"}</span>
                  )}
                </td>
                <td>{formatNumber(article.wald_z) || <span className="muted">{article.wald_z_error || "Missing"}</span>}</td>
                <td className="titleCell">{article.title || <span className="muted">Missing</span>}</td>
                <td className="titleCell">{article.citation}</td>
                <td>
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
                </td>
                <td>
                  <LinkOut href={article.pmc_url}>{article.pmcid || "PMC"}</LinkOut>
                </td>
                <td>
                  <div className="fileLinks">
                    {article.abstract_path ? <a href={apiHref(`/api/articles/${article.article_id}/abstract`)}>Abstract</a> : <span className="muted">No abstract</span>}
                    {article.full_text_path ? <a href={apiHref(`/api/articles/${article.article_id}/full-text`)}>Full text</a> : <span className="muted">No full text</span>}
                  </div>
                </td>
                <td>{article.match_status || <span className="muted">Not matched</span>}</td>
                <td>
                  {evaluationByArticle[article.article_id]?.answer ? (
                    <>
                      <Pill>{evaluationByArticle[article.article_id].answer}</Pill>{" "}
                      <span className="muted">{evaluationByArticle[article.article_id].memorization_label || ""}</span>
                    </>
                  ) : (
                    <span className="muted">No run</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rowError && <div className="error compactError">{rowError}</div>}
        {rows.length === 0 && <div className="empty">No articles have been extracted for this review.</div>}
      </div>
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

function EvaluationsView() {
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState("");
  const [run, setRun] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetchJson("/api/evaluations")
      .then((data) => {
        const items = data.evaluations || [];
        setRuns(items);
        if (items[0]) setSelected(items[0].filename);
      })
      .catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    if (!selected) {
      setRun(null);
      return;
    }
    fetchJson(`/api/evaluations/${encodeURIComponent(selected)}`)
      .then(setRun)
      .catch((err) => setError(err.message));
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
              <p>{run.metadata?.provider} · {run.metadata?.created_at} · {selected}</p>
            </div>
          </section>
          <EvaluationAccuracyExplorer run={run} />
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
  const [agreeText, setAgreeText] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [evaluationRuns, setEvaluationRuns] = useState([]);
  const [selectedEvaluation, setSelectedEvaluation] = useState("");
  const [reviewEvaluation, setReviewEvaluation] = useState(null);

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
      const path = kind === "sof" ? "extract-sof" : "extract-agree-oppose";
      const text = kind === "sof" ? sofText : agreeText;
      const result = await fetchJson(`/api/reviews/${encodeURIComponent(reviewId)}/${path}`, {
        method: "POST",
        body: JSON.stringify({ text }),
      });
      setPayload(result.review ? result : { ...payload, ...result });
      if (result.review) onReviewUpdated(result.review);
      setMessage(kind === "sof" ? result.message || "SoF extracted." : `Agree/Oppose extracted. Added ${result.article_count || 0} articles.`);
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
          <section className="extractGrid">
            <div className="extractPanel">
              <h2>Extract SoF</h2>
              <textarea value={sofText} onChange={(event) => setSofText(event.target.value)} />
              <button className="primaryButton" disabled={busy === "sof"} onClick={() => submit("sof")}>
                {busy === "sof" ? <RefreshCw size={16} className="spin" /> : <FileText size={16} />} Extract SoF
              </button>
            </div>
            <div className="extractPanel">
              <h2>Extract Agree Oppose</h2>
              <textarea value={agreeText} onChange={(event) => setAgreeText(event.target.value)} />
              <button className="primaryButton" disabled={busy === "agree"} onClick={() => submit("agree")}>
                {busy === "agree" ? <RefreshCw size={16} className="spin" /> : <FileText size={16} />} Extract Agree/Oppose
              </button>
            </div>
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
