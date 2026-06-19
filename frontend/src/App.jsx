import React, { useEffect, useMemo, useState } from "react";
import { ArrowLeft, ExternalLink, FileText, ListFilter, Search } from "lucide-react";

const API_BASE = import.meta.env.VITE_API_BASE || "";

async function fetchJson(path) {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json();
}

function normalize(value) {
  return String(value || "").toLowerCase();
}

function BoolPill({ value }) {
  return <span className={value ? "pill pillYes" : "pill"}>{value ? "1" : "0"}</span>;
}

function FullTextLink({ href }) {
  if (!href) return <span className="muted">Unavailable</span>;
  return (
    <a href={href} target="_blank" rel="noreferrer" className="linkIcon">
      Full text <ExternalLink size={14} />
    </a>
  );
}

function apiHref(path) {
  return `${API_BASE}${path}`;
}

function StudyLinks({ studies }) {
  if (!studies || studies.length === 0) return <span className="muted">None parsed</span>;
  return (
    <div className="studyLinks">
      {studies.map((study) => (
        <a
          key={study.study_id}
          href={apiHref(`/api/studies/${encodeURIComponent(study.study_id)}`)}
          target="_blank"
          rel="noreferrer"
          className={`studyLink ${study.link_status === "pmc_full_text" ? "studyPmc" : study.link_status === "pubmed_abstract" ? "studyAbstract" : "studyMetadata"}`}
          title={study.title || study.label}
        >
          {study.label || study.title || study.study_id}
        </a>
      ))}
    </div>
  );
}

function ForestPlotLink({ href }) {
  if (!href) return <span className="muted">Unavailable</span>;
  return (
    <a href={apiHref(href)} target="_blank" rel="noreferrer" className="linkIcon">
      Forest plot <ExternalLink size={14} />
    </a>
  );
}

function OutcomeTable({ outcomes, includeReview }) {
  return (
    <div className="tableWrap">
      <table>
        <thead>
          <tr>
            {includeReview && <th>PMID</th>}
            {includeReview && <th>Review</th>}
            <th>ID</th>
            <th>Outcome</th>
            <th>Question</th>
            <th>Consensus Answer</th>
            <th>Inconsistency</th>
            <th>Subgroup Differences</th>
            <th>Forest Plot</th>
            <th>Agreeing Studies</th>
            <th>Opposing Studies</th>
            <th>Reason</th>
            <th>Certainty</th>
            <th>Table</th>
          </tr>
        </thead>
        <tbody>
          {outcomes.map((outcome) => (
            <tr key={`${outcome.pmid}-${outcome.outcome_id}`}>
              {includeReview && (
                <td>
                  <a className="pmidLink" href={outcome.full_text_url || "#"} target="_blank" rel="noreferrer">
                    {outcome.pmid}
                  </a>
                </td>
              )}
              {includeReview && <td className="titleCell">{outcome.review_title}</td>}
              <td>{outcome.outcome_id}</td>
              <td>{outcome.outcome}</td>
              <td>{outcome.question}</td>
              <td>{outcome.consensus_answer}</td>
              <td><BoolPill value={outcome.inconsistency} /></td>
              <td><BoolPill value={outcome.subgroup_differences} /></td>
              <td><ForestPlotLink href={outcome.forest_plot_url} /></td>
              <td><StudyLinks studies={outcome.agreeing_study_refs} /></td>
              <td><StudyLinks studies={outcome.opposing_study_refs} /></td>
              <td>{outcome.inconsistency_reason || <span className="muted">None parsed</span>}</td>
              <td>{outcome.certainty}</td>
              <td>{outcome.table_title}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {outcomes.length === 0 && <div className="empty">No outcomes found.</div>}
    </div>
  );
}

function ReviewsView({ reviews, query, setQuery, onSelect }) {
  const filtered = useMemo(() => {
    const needle = normalize(query);
    return reviews.filter((review) =>
      [review.pmid, review.title, review.year, review.journal, review.status].some((value) =>
        normalize(value).includes(needle),
      ),
    );
  }, [reviews, query]);

  return (
    <>
      <div className="toolbar">
        <div className="searchBox">
          <Search size={18} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search reviews" />
        </div>
      </div>
      <div className="tableWrap">
        <table>
          <thead>
            <tr>
              <th>PMID</th>
              <th>Title</th>
              <th>Year</th>
              <th>Journal</th>
              <th>Full Text</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((review) => (
              <tr key={review.pmid} onClick={() => onSelect(review.pmid)} className="clickableRow">
                <td className="pmidLink">{review.pmid}</td>
                <td className="titleCell">{review.title}</td>
                <td>{review.year}</td>
                <td>{review.journal}</td>
                <td onClick={(event) => event.stopPropagation()}><FullTextLink href={review.full_text_url} /></td>
                <td>{review.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {filtered.length === 0 && <div className="empty">No reviews match the search.</div>}
      </div>
    </>
  );
}

function ReviewDetail({ pmid, onBack }) {
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    setPayload(null);
    setError("");
    fetchJson(`/api/reviews/${pmid}`)
      .then(setPayload)
      .catch((err) => setError(err.message));
  }, [pmid]);

  if (error) return <div className="error">{error}</div>;
  if (!payload) return <div className="empty">Loading review...</div>;

  const { review, outcomes } = payload;
  return (
    <>
      <button className="iconButton" onClick={onBack} title="Back to reviews">
        <ArrowLeft size={18} /> Reviews
      </button>
      <section className="detailHeader">
        <div>
          <h2>{review.title}</h2>
          <p>{review.pmid} · {review.year || "Year unknown"} · {review.journal || "Journal unknown"}</p>
        </div>
        <FullTextLink href={review.full_text_url} />
      </section>
      <OutcomeTable outcomes={outcomes} includeReview={false} />
    </>
  );
}

function OutcomesView() {
  const [outcomes, setOutcomes] = useState([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    fetchJson("/api/outcomes")
      .then((data) => setOutcomes(data.outcomes || []))
      .catch((err) => setError(err.message));
  }, []);

  const filtered = useMemo(() => {
    const needle = normalize(query);
    return outcomes.filter((outcome) =>
      [
        outcome.pmid,
        outcome.review_title,
        outcome.outcome,
        outcome.question,
        outcome.consensus_answer,
        outcome.inconsistency_reason,
        ...(outcome.agreeing_study_refs || []).map((study) => study.label || study.title),
        ...(outcome.opposing_study_refs || []).map((study) => study.label || study.title),
      ].some((value) => normalize(value).includes(needle)),
    );
  }, [outcomes, query]);

  if (error) return <div className="error">{error}</div>;
  return (
    <>
      <div className="toolbar">
        <div className="searchBox">
          <Search size={18} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search outcomes" />
        </div>
      </div>
      <OutcomeTable outcomes={filtered} includeReview />
    </>
  );
}

export default function App() {
  const [reviews, setReviews] = useState([]);
  const [selectedPmid, setSelectedPmid] = useState("");
  const [view, setView] = useState("reviews");
  const [query, setQuery] = useState("");
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
          <p>Cochrane Summary of Findings outcomes indexed from DynamoDB</p>
        </div>
        <nav>
          <button className={view === "reviews" ? "tab active" : "tab"} onClick={() => { setView("reviews"); setSelectedPmid(""); }}>
            <FileText size={17} /> Reviews
          </button>
          <button className={view === "outcomes" ? "tab active" : "tab"} onClick={() => { setView("outcomes"); setSelectedPmid(""); }}>
            <ListFilter size={17} /> Outcomes
          </button>
        </nav>
      </header>

      {error && <div className="error">{error}</div>}
      {!error && selectedPmid && <ReviewDetail pmid={selectedPmid} onBack={() => setSelectedPmid("")} />}
      {!error && !selectedPmid && view === "reviews" && (
        <ReviewsView reviews={reviews} query={query} setQuery={setQuery} onSelect={setSelectedPmid} />
      )}
      {!error && !selectedPmid && view === "outcomes" && <OutcomesView />}
    </main>
  );
}
