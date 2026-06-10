# Grade Inconsistency

Pipeline and browser UI for indexing Cochrane systematic reviews, their Summary of Findings outcomes, and whether each outcome was downgraded for inconsistency.

## Data Model

The pipeline writes two DynamoDB tables:

- `reviews`
  - Primary key: `pmid`
  - Columns include `title`, `year`, `journal`, `pmcid`, `full_text_url`, `status`, `summary_tables`, and `indexed_at`.
- `outcomes`
  - Partition key: `pmid`
  - Sort key: `outcome_id`
  - One item per Summary of Findings row.
  - Columns include `question`, `consensus_answer`, `inconsistency`, `subgroup_differences`, `certainty`, `inconsistency_reason`, `downgrade_categories`, `footnote_labels`, and `footnotes`.

`inconsistency` is `1` when the GRADE footnotes for the outcome identify an inconsistency downgrade. `subgroup_differences` is `1` when the extracted footnotes indicate subgroup-related differences and the row was not downgraded for inconsistency.

## Setup

Install Python dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start local DynamoDB:

```bash
docker compose up -d dynamodb
```

Create a config file:

```bash
cp config.example.yml config.yml
```

For local DynamoDB, keep:

```yaml
dynamodb_endpoint_url: http://localhost:8000
aws_region: us-west-2
reviews_table: reviews
outcomes_table: outcomes
```

For AWS DynamoDB, set `dynamodb_endpoint_url` to `null` and make sure your normal AWS credentials are available in the environment.

## Run Ingestion

The ingestion command uses the PubMed and PMC lookup methods in `grade_inconsistency.py`. Before parsing a review, it checks the `reviews` table for the PMID and skips already indexed reviews unless `force_reprocess: true` is set in the YAML config.

```bash
python -m pipeline.ingest --config config.yml
```

Important config fields:

- `limit`: number of PubMed records to inspect.
- `pause_seconds`: delay between PMC article fetches.
- `create_tables`: create DynamoDB tables if missing.
- `force_reprocess`: replace stored review/outcome rows even if the PMID already exists.

## Run API

```bash
uvicorn pipeline.api:app --host 0.0.0.0 --port 8080
```

Optional API environment variables:

```bash
export DYNAMODB_ENDPOINT_URL=http://localhost:8000
export AWS_REGION=us-west-2
export REVIEWS_TABLE=reviews
export OUTCOMES_TABLE=outcomes
```

API routes:

- `GET /api/reviews`
- `GET /api/reviews/{pmid}`
- `GET /api/outcomes`

## Run Frontend

In a separate terminal:

```bash
cd frontend
npm install
npx npm start
```

The frontend defaults to `http://localhost:8080` for the API. To use another API URL:

```bash
cd frontend
VITE_API_BASE=http://localhost:8080 npx npm start
```

The app has:

- A searchable systematic review list showing PMID, title, publication year, journal, and full-text link.
- A review detail view showing all indexed outcomes for a selected review.
- An outcomes-only view sorted by systematic review PMID, with links to the source review.

## Run Remotely Over SSH

If the app is running on a server and you want to view it from your laptop, forward both the frontend and backend ports. From your laptop:

```bash
ssh -L 5174:localhost:5174 -L 8080:localhost:8080 pd@10.0.0.193
```

On the server, start the API so it accepts the forwarded frontend origin:

```bash
cd ~/grade-inconsistency/grade-inconsistency
source .venv/bin/activate

CORS_ALLOW_ORIGINS=http://localhost:5174,http://localhost:5173,http://localhost:3000 \
uvicorn pipeline.api:app --host 127.0.0.1 --port 8080
```

In another server terminal, start the frontend on the forwarded port:

```bash
cd ~/grade-inconsistency/grade-inconsistency/frontend

VITE_API_BASE=http://localhost:8080 \
npx vite --host 127.0.0.1 --port 5174 --strictPort
```

Then open this URL on your laptop:

```text
http://localhost:5174
```

To run the API and frontend in the background on the server:

```bash
cd ~/grade-inconsistency/grade-inconsistency

setsid -f bash -lc 'cd ~/grade-inconsistency/grade-inconsistency && CORS_ALLOW_ORIGINS=http://localhost:5174,http://localhost:5173,http://localhost:3000 .venv/bin/uvicorn pipeline.api:app --host 127.0.0.1 --port 8080 >>/tmp/grade-inconsistency-api.log 2>&1'

setsid -f bash -lc 'cd ~/grade-inconsistency/grade-inconsistency/frontend && VITE_API_BASE=http://localhost:8080 npx vite --host 127.0.0.1 --port 5174 --strictPort >>/tmp/grade-inconsistency-frontend.log 2>&1'
```

Useful checks on the server:

```bash
curl http://127.0.0.1:8080/api/reviews
curl http://127.0.0.1:5174/
ss -ltnp | grep -E ':(5174|8080)\b'
tail -f /tmp/grade-inconsistency-api.log
tail -f /tmp/grade-inconsistency-frontend.log
```

To stop the remote dev servers:

```bash
pkill -f 'uvicorn pipeline.api:app'
pkill -f 'vite --host 127.0.0.1 --port 5174'
```

## Existing Parser CLI

The original standalone summary script remains available:

```bash
python grade_inconsistency.py --limit 50 --output-dir .
```
