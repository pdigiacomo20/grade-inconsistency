# Grade Inconsistency

Browser workflow for semi-automated extraction from 2025 open-access Cochrane systematic reviews.

## What It Does

1. Ingests PubMed records for open-access 2025 Cochrane systematic reviews into DynamoDB.
2. Assigns each review a stable ID: `CSR_0001`, `CSR_0002`, and so on.
3. Marks protocol-only reviews in the `reviews` table so the frontend can filter them out.
4. Lets a user open each review detail page, download the review PDF when PMC exposes one, and paste GPT browser outputs into two extraction boxes.
5. Parses `Extract SoF` first and stores inconsistency-downgraded outcomes in `outcomes`.
6. Parses `Extract Agree Oppose` second, stores agreeing/opposing article IDs on each outcome, and inserts article rows as `ART_00001`, `ART_00002`, and so on.
7. Attempts PubMed/PMC enrichment for every extracted article citation, saving abstracts and available full text as `.txt` files outside git.

## DynamoDB Tables

`reviews`

- Primary key: `pmid`
- Important columns: `review_id`, `title`, `year`, `journal`, `pmcid`, `pmc_url`, `pubmed_url`, `is_protocol_only`, `status`

`outcomes`

- Partition key: `pmid`
- Sort key: `outcome_id`
- Important columns: `review_id`, `sof_table`, `row`, `question`, `consensus_answer`, `certainty`, `downgrade_reasoning`, `forest_plot_title`, `effect_measure`, `line_of_no_effect`, `agreeing_articles`, `opposing_articles`

`articles`

- Primary key: `article_id`
- Important columns: `review_id`, `review_pmid`, `outcome_id`, `stance`, `study_label`, `effect_measure`, `effect_estimate`, `confidence_interval_begin`, `confidence_interval_end`, `confidence_interval_percentage`, `line_of_no_effect`, `citation`, `pmid`, `pmcid`, `abstract_path`, `full_text_path`, `pubmed_query`, `match_status`

The app intentionally inserts a new article row for every pasted citation. It does not deduplicate citations.

## Setup

```bash
cd ~/grade-inconsistency/grade-inconsistency
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config.example.yml config.yml
```

Set `OPENAI_API_KEY` in `.env`. The key is used to identify PMIDs for extracted agreeing/opposing article citations with GPT web search before PubMed/PMC metadata and text are fetched.

Start local DynamoDB:

```bash
docker compose up -d dynamodb
```

For local DynamoDB, keep:

```yaml
dynamodb_endpoint_url: http://localhost:8000
aws_region: us-west-2
reviews_table: reviews
outcomes_table: outcomes
articles_table: articles
```

For AWS DynamoDB, set `dynamodb_endpoint_url: null` and provide normal AWS credentials in the environment.

## Ingest Reviews

This command only populates/updates `reviews`. It does not extract outcomes.

```bash
python -m pipeline.ingest --config config.yml
```

Useful config fields:

- `limit`: number of PubMed results to inspect.
- `pause_seconds`: delay after PMC article fetches.
- `create_tables`: create missing DynamoDB tables.
- `force_reprocess`: refresh existing review metadata while preserving existing `review_id`.
- `abstract_text_dir`: where article abstracts are saved.
- `full_text_dir`: where PMC full text `.txt` files are saved.

## Run Backend

```bash
cd ~/grade-inconsistency/grade-inconsistency
source .venv/bin/activate
uvicorn pipeline.api:app --host 127.0.0.1 --port 8080
```

The API loads `.env` automatically.

Main routes:

- `GET /api/reviews`
- `GET /api/reviews/{CSR_ID}`
- `GET /api/reviews/{CSR_ID}/pdf`
- `POST /api/reviews/{CSR_ID}/extract-sof`
- `POST /api/reviews/{CSR_ID}/extract-agree-oppose`
- `GET /api/outcomes`
- `GET /api/articles/{ART_ID}/abstract`
- `GET /api/articles/{ART_ID}/full-text`

The PDF endpoint returns `Content-Disposition: attachment; filename="CSR_XXXX.pdf"`. Browser security does not allow a web app to force `~/Downloads/CSR`; configure the browser download location to that folder if needed.

## Run Frontend

```bash
cd ~/grade-inconsistency/grade-inconsistency/frontend
npm install
npm run dev:remote
```

Open:

```text
http://localhost:5174
```

The Vite dev server proxies `/api` to `http://127.0.0.1:8080`.

## Frontend Workflow

1. Use the Reviews table. Keep `Hide protocols only` checked to remove protocol-only rows.
2. Click the `CSR_XXXX` link to open the detail view.
3. Click `PMC entry` to inspect the article in a new tab, or `Download PDF` to download `CSR_XXXX.pdf` if PMC exposes a PDF.
4. Use GPT in a separate browser with the downloaded Cochrane PDF and the `Extract SoF` prompt.
5. Paste the GPT output into `Extract SoF` and click `Extract SoF`.
6. Use GPT with the `Extract Agree Oppose` prompt.
7. Paste the GPT output into `Extract Agree Oppose` and click `Extract Agree/Oppose`.
8. Review extracted outcomes and associated articles below the input boxes.

The backend rejects `Extract Agree Oppose` if `Extract SoF` has not already produced matching outcome rows. The `Extract Agree Oppose` output must include `Effect measure` and `Line of no effect` for each matched outcome, plus `Effect estimate`, `Confidence interval begin`, `Confidence interval end`, and `Confidence interval percentage` for each listed study.

## Run Remotely Over SSH

Forward the frontend port from your laptop:

```bash
ssh -L 5174:localhost:5174 pd@10.0.0.193
```

On the server, start the API:

```bash
cd ~/grade-inconsistency/grade-inconsistency
source .venv/bin/activate
uvicorn pipeline.api:app --host 127.0.0.1 --port 8080
```

In another server terminal, start the frontend:

```bash
cd ~/grade-inconsistency/grade-inconsistency/frontend
npm run dev:remote
```

Open this URL on your laptop:

```text
http://localhost:5174
```

To run both in the background on the server:

```bash
cd ~/grade-inconsistency/grade-inconsistency
setsid -f bash -lc 'cd ~/grade-inconsistency/grade-inconsistency && .venv/bin/uvicorn pipeline.api:app --host 127.0.0.1 --port 8080 >>/tmp/grade-inconsistency-api.log 2>&1'
setsid -f bash -lc 'cd ~/grade-inconsistency/grade-inconsistency/frontend && npm run dev:remote >>/tmp/grade-inconsistency-frontend.log 2>&1'
```

Useful checks:

```bash
curl http://127.0.0.1:8080/api/reviews
curl http://127.0.0.1:5174/
ss -ltnp | grep -E ':(5174|8080)\b'
tail -f /tmp/grade-inconsistency-api.log
tail -f /tmp/grade-inconsistency-frontend.log
```

Stop background servers:

```bash
pkill -f 'uvicorn pipeline.api:app'
pkill -f 'npm run dev:remote'
pkill -f 'vite --host 127.0.0.1 --port 5174'
```
