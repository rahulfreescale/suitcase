# Build & Deploy — step by step

This guide is written for the setup you're running:

> **Your environment:** Apple-Silicon Mac · Homebrew · `uv` + Python 3.12 ·
> models via **Anthropic + OpenAI** (no AWS for the model layer) ·
> **Langfuse Cloud** for tracing · databases in local Docker.

There are two tracks. **Track A** runs the whole system on your Mac — that's where
you are now. **Track B** promotes it to AWS managed services later, and even then
your models can stay on Anthropic + OpenAI (the provider is independent of where
the app runs).

Alternatives (AWS Bedrock, OpenAI-only, fully-offline Ollama) are noted in small
print where relevant, but the main path is yours.

---

## Track A — Local (your setup)

### Step 0 · Prerequisites (Mac, Apple Silicon)
Install the tools with Homebrew:
```bash
brew install uv docker        # uv = Python + package manager; Docker for the databases
```
- **Docker Desktop memory:** Settings → Resources → **Memory ≥ 6 GB**. OpenSearch
  alone wants ~2 GB; too little RAM is the #1 reason it fails to boot. (Lightweight
  alternative: `brew install colima && colima start --memory 6`.)
- **Keys you'll need:** an **Anthropic API key** and an **OpenAI API key**. That's
  it — no AWS account required for local.
- All container images (OpenSearch, Postgres, DynamoDB-Local, MinIO) have arm64
  builds and run natively on M-series chips.

### Step 1 · Create the environment and install
`uv` downloads Python 3.12 for you (your system Python is too new — 3.13/3.14 don't
have wheels for parts of the ML stack yet). Run these **one line at a time**:
```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
cp .env.example .env
```
Right after activating, confirm the version:
```bash
python --version          # must print 3.12.x
```
> If you ever see `externally-managed-environment`, your venv isn't active —
> run `source .venv/bin/activate` first. That error can't happen inside a venv.
> The first reranker call also downloads `bge-reranker-large` (~1.3 GB); to skip
> it while setting up, set `RERANK_BACKEND=none` in `.env`.

### Step 2 · Start the databases
```bash
make up                   # opensearch:9200, dynamodb:8000, postgres:5432, minio:9000
docker compose --profile core ps     # all should be healthy
```
These run entirely on your Mac — no AWS, no cost. (The DynamoDB one is Amazon's
*local emulator*, not the real service.)

### Step 3 · Add your model keys
Open `.env`. The Anthropic + OpenAI preset is already the active block — just fill
in the two keys:
```ini
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
LLM_MODEL_CHAIN=anthropic/claude-3-5-sonnet-20241022,gpt-4o
LLM_FAST_MODEL=anthropic/claude-3-5-haiku-20241022
EMBED_MODEL=text-embedding-3-small
EMBED_DIM=1536
```
**Why both providers:** Claude does all the reasoning; OpenAI does the embeddings,
because **Anthropic has no embeddings endpoint**. You also get real cross-provider
fallback for free — if Claude is unavailable, the chain falls back to GPT-4o. Swap
the model strings for whatever your accounts have access to.

<sub>Other options, if you ever want them: **Bedrock** — set `LLM_MODEL_CHAIN=bedrock/...` + AWS creds; **OpenAI-only** — `gpt-4o` for both, `EMBED_DIM=1536`; **fully offline** — Ollama block in `.env`, `EMBED_DIM=768`. Changing `EMBED_DIM` means you must recreate the OpenSearch index.</sub>

### Step 4 · Create the stores and load data
```bash
make index                # OpenSearch index + DynamoDB tables (idempotent)
make ingest               # extract → chunk → embed → index the sample reports
make health               # every check should print "ok"
```
`make health` makes a tiny live Claude call, so it confirms your Anthropic key
works before you go further.

### Step 5 · Run it
```bash
make run                  # http://localhost:8080
```
Open the page and try both tools:
- Document question (RAG): *"Were piloerection and ataxia observed in study T123456-2?"*
- Counting question (SQL): *"How many studies were done on rats?"*
- Vague question (watch it clarify/loop): *"any safety concerns?"*

### Step 6 · Turn on tracing (Langfuse Cloud)
You're using the hosted version — no local container needed.
1. Make a free project at <https://cloud.langfuse.com> and copy its keys.
2. In `.env`:
   ```ini
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=https://cloud.langfuse.com      # use https://us.cloud.langfuse.com for a US project
   ```
3. Restart `make run`. Every LLM call now traces to the cloud dashboard.

> Match `LANGFUSE_HOST` to your project's **region** (EU vs US) or the keys are
> rejected. Note that prompts, answers, and retrieved context are sent to Langfuse
> as part of traces — fine for this synthetic data; weigh it before pointing at
> anything real.

### Step 7 · Dataset evaluation (run on change)
```bash
make eval                 # scores eval/dataset.jsonl with RAGAS
```
RAGAS runs locally but grades using an LLM — so this makes Claude/OpenAI calls too.
Run it whenever you change the workflow, prompts, or models, and treat a score drop
as a regression.

### Step 8 · Simulate traffic and the daily batch
The other half of evaluation is **live-traffic eval**: scoring real production
queries daily, with no reference answers. To see it locally, manufacture traffic,
then score it.
```bash
# terminal 1
make run
# terminal 2
make simulate             # ~30 mixed requests (or: --n 60 --paraphrase)
make eval-live            # reference-free scoring of the last day's traffic
```
Each `/ask` is logged to local DynamoDB; the batch samples the day, scores with
RAGAS (faithfulness + answer relevancy), saves a summary, pushes scores to Langfuse
Cloud, and warns if faithfulness drops. Schedule it locally with cron:
```bash
crontab -e
# 0 2 * * *  cd /path/to/suitcase && ./scripts/run_daily_batch.sh >> eval.log 2>&1
```

---

## Track B — Deploy to AWS (when you're ready)

Your **databases** move to managed AWS services; your **models stay on Anthropic +
OpenAI** unless you decide otherwise. The app code doesn't change — you repoint each
store via `.env`, one at a time, re-running `make health` after each.

| On your Mac | becomes, on AWS |
|-------------|-----------------|
| OpenSearch (Docker) | Amazon OpenSearch Service |
| DuckDB over a CSV | Athena querying files in S3 |
| DynamoDB Local | Amazon DynamoDB |
| Postgres (Docker) | Amazon RDS |
| *(models: Anthropic + OpenAI — unchanged)* | *(or switch to Bedrock if you prefer)* |

### Step 1 · Provision with Terraform
```bash
cd infra/terraform
terraform init
terraform apply           # creates S3, DynamoDB, OpenSearch, RDS — review the plan; it costs money
```
The skeleton omits VPC/security hardening on purpose; add it for anything beyond a
sandbox. Run `terraform destroy` when you're done experimenting.

### Step 2 · Repoint the stores in `.env`
```ini
OPENSEARCH_HOST=search-suitcase-...es.amazonaws.com
OPENSEARCH_PORT=443
OPENSEARCH_USE_SSL=true
OPENSEARCH_USE_AWS_AUTH=true
STRUCTURED_BACKEND=athena
DYNAMODB_ENDPOINT=                 # blank = real AWS
POSTGRES_DSN=postgresql://suitcase:<pw>@<rds-endpoint>:5432/suitcase
```
Keep your `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` as-is. (Going through Bedrock
instead would only need AWS creds and `bedrock/...` model strings.)

### Step 3 · Load data into the cloud
- Re-run `make index` and `make ingest` against the managed OpenSearch.
- For Athena: upload the studies data to S3, then create the table with
  `infra/glue_table.sql` (fix the `LOCATION`).

### Step 4 · Run the app on AWS
```bash
docker build -t suitcase .
# push to Amazon ECR, then run on ECS Fargate behind a load balancer
```
Give the task role least privilege: read/write its DynamoDB tables, search its
OpenSearch domain, Athena read queries + Glue + read-only S3, RDS connectivity.
Your provider keys live as task secrets (Secrets Manager), not in the image.

### Step 5 · Eval & tracing in prod
- **Dataset eval on change** in CI: `python -m eval.run_ragas`.
- **Live-traffic eval daily:** real `/ask` traffic is logged automatically;
  schedule the batch with the EventBridge → ECS skeleton in
  `infra/terraform/scheduled_eval.tf` (runs `python -m eval.live_traffic_eval`).
  Set `DEPLOY_PROFILE=aws` to also push scores to CloudWatch and alarm on drops.
- Langfuse stays on Cloud — same keys.

---

## Going further
- **Drop torch:** swap the local cross-encoder reranker for a hosted one (Cohere /
  Bedrock rerank) to remove `sentence-transformers` + `torch` from the install.
- **Domain sub-agents:** split the single Researcher into per-domain specialists.
- **Native hybrid scoring:** move the 0.7/0.3 blend into an OpenSearch search pipeline.
- **NER metadata enrichment:** confidence-scored auto-fixes to the structured store.

---

## Troubleshooting (your environment)

| Symptom | Fix |
|---------|-----|
| `python3.12: No such file or directory` | use `uv venv --python 3.12 .venv` (uv fetches it), or `brew install python@3.12` |
| `externally-managed-environment` | you're not in a venv; `source .venv/bin/activate` first |
| `psycopg-binary` not found / `cp314` wheels | venv is on Python 3.13/3.14; recreate with `--python 3.12` |
| stuck at a `quote>` prompt | you pasted a `#` comment with an apostrophe; press **Ctrl + C**, paste commands only |
| OpenSearch won't start / exits | Docker needs more memory — give it ≥ 6 GB in Docker Desktop |
| `connection refused` on `make health` | a container is still booting; wait, retry |
| `dimension mismatch` on the index | `EMBED_DIM` ≠ index mapping; recreate the index after fixing it |
| `AuthenticationError` from Anthropic/OpenAI | key missing/typo in `.env`; keys load from `.env` automatically on startup |
| Langfuse keys rejected | wrong region host — match EU (`cloud.langfuse.com`) vs US (`us.cloud.langfuse.com`) |
