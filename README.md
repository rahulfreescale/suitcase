# Agentic RAG — AWS-faithful reference implementation

A runnable, scaled-down rebuild of the **PRINCE** architecture from the
Thoughtworks/Bayer case study ([martinfowler.com](https://martinfowler.com/articles/reliable-llm-bayer.html)):
an agentic Retrieval-Augmented Generation system that answers questions over a
corpus of (synthetic) preclinical study reports, with the same five-node
LangGraph workflow, three reflection loops, hybrid retrieval, Text-to-SQL,
state persistence, model fallbacks, observability, and evaluation.

> This is a **learning scaffold**, not a turnkey product. It's built to be read,
> run, and extended. The sample data is synthetic. See `BUILD_AND_DEPLOY.md` for
> the step-by-step path from local to AWS.

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

```bash
make install                  # pip install -r requirements.txt
cp .env.example .env          # defaults are wired for local dev
make up                       # opensearch + dynamodb-local + postgres + minio
make index                    # create OpenSearch index + DynamoDB table
make ingest                   # extract → chunk → embed → index sample reports
make health                   # confirm every dependency is reachable
make run                      # FastAPI on http://localhost:8080
```

You need model credentials for the embed/LLM calls — set Bedrock (AWS) **or**
OpenAI keys in `.env`. See `BUILD_AND_DEPLOY.md` step 3.

Then open <http://localhost:8080> and ask:
> "Were any clinical findings observed in study T123456-2: piloerection, ataxia, loose faeces?"

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

## Cost & safety notes

- Bedrock, OpenSearch Service, RDS, and Athena all bill while running. Tear down
  with `terraform destroy` / `make down`.
- The Text-to-SQL tool blocks anything but `SELECT`, but you should still scope
  the IAM/DB role to read-only as defence in depth.
- All sample study data is fabricated for the demo.
