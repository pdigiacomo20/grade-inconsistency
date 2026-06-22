import React, { useEffect, useMemo, useState } from "react";
import { ArrowLeft, Download, ExternalLink, FileText, RefreshCw, Search } from "lucide-react";

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
  const filtered = useMemo(() => {
    const needle = normalize(query);
    return reviews.filter((review) => {
      if (hideProtocols && review.is_protocol_only) return false;
      return [review.review_id, review.pmid, review.title, review.year, review.journal, review.status].some((value) =>
        normalize(value).includes(needle),
      );
    });
  }, [reviews, query, hideProtocols]);

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

function OutcomeTable({ outcomes }) {
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
            <th>Certainty</th>
            <th>Forest Plot</th>
            <th>Agreeing Articles</th>
            <th>Opposing Articles</th>
            <th>Downgrade Reasoning</th>
          </tr>
        </thead>
        <tbody>
          {outcomes.map((outcome) => (
            <tr key={outcome.outcome_id}>
              <td>{outcome.outcome_id}</td>
              <td>{outcome.sof_table}</td>
              <td>{outcome.row}</td>
              <td>{outcome.question}</td>
              <td>{outcome.consensus_answer}</td>
              <td>{outcome.certainty}</td>
              <td>{outcome.forest_plot_title || <span className="muted">Pending</span>}</td>
              <td>{(outcome.agreeing_articles || []).join(", ") || <span className="muted">None</span>}</td>
              <td>{(outcome.opposing_articles || []).join(", ") || <span className="muted">None</span>}</td>
              <td>{outcome.downgrade_reasoning}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {outcomes.length === 0 && <div className="empty">No extracted inconsistency outcomes.</div>}
    </div>
  );
}

function ArticlesTable({ articles }) {
  const [sortByOutcome, setSortByOutcome] = useState(true);
  const rows = useMemo(() => {
    const copy = [...articles];
    if (sortByOutcome) {
      copy.sort((a, b) => Number(a.outcome_id || 0) - Number(b.outcome_id || 0) || String(a.article_id).localeCompare(String(b.article_id)));
    }
    return copy;
  }, [articles, sortByOutcome]);

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
              <th>Citation</th>
              <th>PMID</th>
              <th>PMCID</th>
              <th>Files</th>
              <th>Match</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((article) => (
              <tr key={article.article_id}>
                <td>{article.article_id}</td>
                <td>{article.outcome_id}</td>
                <td>{article.stance}</td>
                <td>{article.study_label || <span className="muted">Unlabeled</span>}</td>
                <td className="titleCell">{article.citation}</td>
                <td>
                  <LinkOut href={article.pubmed_url}>{article.pmid || "PMID"}</LinkOut>
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
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="empty">No articles have been extracted for this review.</div>}
      </div>
    </>
  );
}

function ReviewDetail({ reviewId, onBack }) {
  const [payload, setPayload] = useState(null);
  const [sofText, setSofText] = useState("");
  const [agreeText, setAgreeText] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const load = () => {
    setError("");
    fetchJson(`/api/reviews/${encodeURIComponent(reviewId)}`)
      .then(setPayload)
      .catch((err) => setError(err.message));
  };

  useEffect(load, [reviewId]);

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
      setMessage(kind === "sof" ? result.message || "SoF extracted." : `Agree/Oppose extracted. Added ${result.article_count || 0} articles.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  };

  if (!payload && !error) return <div className="empty">Loading review...</div>;
  const review = payload?.review;
  const outcomes = payload?.outcomes || [];
  const articles = payload?.articles || [];

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
          <div className="sectionHeader"><h2>Extracted Outcomes</h2></div>
          <OutcomeTable outcomes={outcomes} />
          <ArticlesTable articles={articles} />
        </>
      )}
    </>
  );
}

export default function App() {
  const [reviews, setReviews] = useState([]);
  const [selectedReview, setSelectedReview] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    fetchJson("/api/reviews")
      .then((data) => setReviews(data.reviews || []))
      .catch((err) => setError(err.message));
  }, []);

  return (
    <main>
      <header className="appHeader">
        <div>
          <h1>Grade Inconsistency</h1>
          <p>Manual extraction workflow for 2025 open-access Cochrane reviews</p>
        </div>
      </header>
      {error && <div className="error">{error}</div>}
      {!error && selectedReview && <ReviewDetail reviewId={selectedReview} onBack={() => setSelectedReview("")} />}
      {!error && !selectedReview && <ReviewsView reviews={reviews} onOpen={setSelectedReview} />}
    </main>
  );
}
