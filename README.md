# Suitcase — Agentic RAG trip-planning assistant

A production-minded **agentic Retrieval-Augmented Generation** system that plans
trips: it answers open-ended travel questions over a corpus of destination guides
and a structured table of flights and stays, using a multi-agent LangGraph
workflow with hybrid retrieval, Text-to-SQL, reflection loops, conversation
memory, semantic caching, streaming, distributed workers, a model gateway with
A/B routing, full observability, and evaluation.

> A **learning-grade, runnable** system built to be read, run, and extended.
> All sample data is synthetic. See `BUILD_AND_DEPLOY.md` for the step-by-step
> path from local Docker to AWS.

---

## The architecture

```
        ┌──────────────┐
 user → │  FastAPI /ask │
        └──────┬───────┘
               ▼
   ┌───────────────────────── LangGraph workflow (Postgres checkpointer) ──────────────────────────┐
   │  clarify ─► plan ──rag/sql──► researcher ──► plan        (process-reflection loop)             │
   │              │                                                                                  │
   │              └─reflect──► reflection ──insufficient──► plan   (data-reflection loop)            │
   │                              │                                                                  │
   │                           sufficient ──► writer ──► END       (draft reflection inside writer)  │
   └──────────────┬───────────────────────────────┬────────────────────────────┬───────────────────┘
                  ▼                                ▼                            ▼
        OpenSearch (hybrid kNN+kw)      Athena over S3 (Text-to-SQL)   DynamoDB (step trail)
                                                                       + Langfuse traces / RAGAS eval
```

### The five nodes

| Node | File | Reflection role |
|------|------|-----------------|
| Clarify intent | `app/agents/clarify.py` | fail-fast on ambiguity; recommend sources |
| Think & Plan | `app/agents/plan.py` | **process reflection** — right path / right tool? |
| Researcher | `app/agents/researcher.py` | run RAG and/or Text-to-SQL |
| Reflection | `app/agents/reflection.py` | **data reflection** — enough evidence? |
| Writer | `app/agents/writer.py` | **draft reflection** — complete & cited? |

### The RAG pipeline (`app/tools/rag_tool.py`)

`keywords → metadata filter → query expansion (×5) → weighted hybrid search
(0.7 semantic / 0.3 keyword, ~20 candidates) → cross-encoder rerank (top 7) →
grounded answer + citations` — each stage is its own module under `app/retrieval/`.

### The Text-to-SQL tool (`app/tools/sql_tool.py`)

Schema-aware generation with few-shot examples, **SELECT-only validation**
(via `sqlglot`), an always-included identifier column rule, a row cap, and a
self-correcting retry loop (up to 3 attempts).

---

## Demo ↔ production mapping

| In this repo (local)        | Production (AWS)                  | Same code path? |
|-----------------------------|----------------------------------|-----------------|
| OpenSearch (Docker)         | Amazon OpenSearch Service        | ✅ (config only) |
| DuckDB over `studies.csv`   | Athena + Glue over S3 (Parquet)  | adapter swap (`STRUCTURED_BACKEND`) |
| DynamoDB-Local              | Amazon DynamoDB                  | ✅ (endpoint only) |
| Postgres (Docker)           | Amazon RDS for PostgreSQL        | ✅ (DSN only) |
| litellm → any provider      | Amazon Bedrock (+ fallbacks)     | ✅ (model strings) |
| MinIO                       | Amazon S3                        | ✅ (endpoint only) |
| Langfuse (Docker)           | Langfuse Cloud or self-host      | ✅ (keys only) |

The only piece without a true local equivalent is **Athena**; the `local`
structured backend runs the same SQL through DuckDB so the path works end-to-end
without AWS. Keep generated SQL ANSI-ish and it ports to Trino/Athena.

---

## Quickstart (local, ~10 min)

> **Python 3.11 or 3.12 required.** The ML dependencies (torch, ragas, langchain)
> don't have wheels for 3.13/3.14 yet. If `make install` fails with a
> `psycopg-binary` / `cp314` error, you're on too-new a Python — recreate the
> venv with `make venv PY=python3.12`.

```bash
make venv PY=python3.12       # create the venv with a supported Python
source .venv/bin/activate     # Windows: .venv\Scripts\activate
make install                  # python -m pip install -r requirements.txt
cp .env.example .env          # defaults are wired for local dev
make up                       # opensearch + dynamodb-local + postgres + minio
make index                    # create OpenSearch index + DynamoDB tables
make ingest                   # extract → chunk → embed → index sample reports
make health                   # confirm every dependency is reachable
make run                      # FastAPI on http://localhost:8080
```

You need model credentials for the embed/LLM calls — set Bedrock (AWS) **or**
OpenAI keys in `.env`. See `BUILD_AND_DEPLOY.md` step 3.

Then open <http://localhost:8080> and ask:
> "Were any clinical findings observed in study T123456-2: piloerection, ataxia, loose faeces?"

---

## Does running locally use AWS?

Short answer: **the databases don't, but the AI model does (by default).**

| Component | Running locally | Touches AWS? |
|-----------|-----------------|--------------|
| OpenSearch (vector store) | Docker container | No |
| DynamoDB (state + interactions) | DynamoDB **Local** emulator (`localhost:8000`) | No |
| Postgres (checkpointer) | Docker container | No |
| Structured store (Text-to-SQL) | DuckDB over a local CSV | No |
| S3 data lake | MinIO container | No |
| **Embeddings + LLM** | called via litellm | **Yes — AWS Bedrock by default** |

So every database is 100% local and free. The only thing leaving your machine is
the model calls. You have four choices for that:

1. **Anthropic + OpenAI** (recommended if you have both keys — **no AWS needed**):
   Claude does the reasoning, OpenAI does embeddings. Just set `ANTHROPIC_API_KEY`
   and `OPENAI_API_KEY` in `.env` (the file ships with this preset). Note: Anthropic
   has **no embeddings API**, so embeddings must come from OpenAI (or another provider).
2. **AWS Bedrock** — the "AWS-faithful" default; real, billable AWS with model access.
3. **OpenAI only** — one key for both reasoning and embeddings.
4. **Fully offline (Ollama)** — run the models on your own machine; **nothing leaves it**:
   ```bash
   brew install ollama && ollama serve            # in one terminal
   ollama pull llama3.1 && ollama pull nomic-embed-text
   # then uncomment the Ollama block in .env (EMBED_DIM=768) and recreate the index
   ```

When you later deploy to AWS (Track B in the guide), the databases *also* move to
their managed AWS equivalents — that's when the rest of the stack becomes cloud.
(The model provider is independent: you can deploy on AWS but still call Anthropic
+ OpenAI directly if you prefer.)

---

## Repo layout

```
app/
  agents/      LangGraph nodes + graph wiring (the workflow)
  retrieval/   the 5 RAG stages (keywords, filters, expansion, hybrid, rerank)
  tools/       rag_tool + sql_tool
  stores/      OpenSearch / Athena / DuckDB / DynamoDB adapters
  llm.py       unified model layer with provider fallbacks
  embeddings.py
  observability.py   Langfuse wiring (no-op without keys)
  api/main.py  FastAPI + /ask + /trail
  ui/index.html
ingest/        extract → chunk → embed → index + load structured rows
eval/          RAGAS dataset evaluation
infra/         OpenSearch mapping, Glue DDL, DynamoDB def, Terraform starter
scripts/       index/table creation, health check
```

## Live traffic & the daily batch

The repo ships **two** evaluation modes, matching the source case study:

| Mode | When it runs | Needs reference answers? | Command |
|------|--------------|--------------------------|---------|
| Dataset eval | on significant change (workflow/prompts/models) | yes (`eval/dataset.jsonl`) | `make eval` |
| **Live-traffic eval** | **daily, as a batch job** | **no** | `make eval-live` |

Every answered `/ask` request is recorded to a DynamoDB interaction log
(`app/stores/interactions.py`) with the question, answer, and retrieved
contexts. The daily batch reads the last day's interactions, samples some, and
scores them with reference-free RAGAS metrics (faithfulness, answer relevancy),
saves a daily summary, and can push scores to Langfuse / CloudWatch and alert if
faithfulness drops.

To see it work without real users, generate traffic first:

```bash
make run                 # in one terminal
make simulate            # in another — sends ~30 varied requests
make eval-live           # score yesterday/today's traffic
```

`eval/simulate_traffic.py` produces a realistic mix (document questions,
counting questions, vague ones that trigger clarify/reflection, multi-step ones)
and can paraphrase them with `--paraphrase` for more variety. It can hit a
running server (`--mode http`) or run the graph in-process (`--mode inprocess`).

Scheduling: locally use cron with `scripts/run_daily_batch.sh`; on AWS use the
EventBridge Scheduler → ECS task skeleton in `infra/terraform/scheduled_eval.tf`.

## Cost & safety notes

- Bedrock, OpenSearch Service, RDS, and Athena all bill while running. Tear down
  with `terraform destroy` / `make down`.
- The Text-to-SQL tool blocks anything but `SELECT`, but you should still scope
  the IAM/DB role to read-only as defence in depth.
- All sample study data is fabricated for the demo.
