# Suitcase — a constraint-faithful, agentic-RAG trip planner

Suitcase plans trips that **respect hard constraints** — wheelchair access, budget, dietary needs — that mainstream AI trip planners quietly ignore. Ask most tools for a "wheelchair-friendly Rome trip" and they cheerfully include the Spanish Steps (which are, literally, stairs). Suitcase leaves them out — and **tells you why**.

It's an agentic Retrieval-Augmented Generation system: a multi-agent reasoning loop for open questions, and a constraint-faithful planning pipeline that rates every candidate place against your hard requirements before it lands on your itinerary. It's grounded in a 26-city guide corpus, cites its sources, and is honest about the edges of what it knows.

Built as a portfolio piece to demonstrate production-shaped agentic-RAG: retrieval, multi-agent orchestration, evaluation, tracing, and a one-command AWS deployment behind real authentication.

---

## The demo

**Plan mode** — enter a trip with constraints. The input is a boarding pass; the plan is the itinerary.

![The planner input](./screenshots/input.png)

**The result** — a wheelchair-friendly, 2-day Rome itinerary. Each placed activity is rated and cited. Below the plan is the section that makes the system trustworthy: **"Popular spots I left out — and why."** Famous, must-see places are *correctly refused* when they don't fit the constraint, each with a sourced reason.

![The rendered plan with honest refusals](./screenshots/plan.png)

> A short screen recording of the full flow — login, planning, and the editable drag-and-drop itinerary — is in [`screenshots/demo.mov`](./screenshots/demo.mov).

Notice what the plan does that others don't:
- **Spanish Steps -> TOUGH** (the steps are stairs; you can enjoy the view from the base)
- **Trevi Fountain -> TOUGH**, marked *must-see* — small viewing space, steps to the basin
- **Roman Forum -> TOUGH** — uneven gravel and cobbles
- Placed instead: Colosseum, Pantheon, Vatican Museums, St. Peter's — all rated **GOOD** for wheelchair access, with citations

This is the north star: **respecting a hard constraint even when it means dropping a landmark, and being honest about the trade-off.**

---

## How it thinks

Two modes share one grounded retrieval stack.

![The agent pipelines](./architecture-agents.svg)

**Ask mode** is a multi-agent loop (built on LangGraph): `clarify -> planner -> researcher -> reflection -> writer`. The planner decides whether it needs to retrieve more; reflection checks whether the evidence is sufficient and loops back to re-plan if not. The writer only produces a cited answer once the loop is satisfied. The looping — deciding to gather more, reflecting on sufficiency — is what makes it agentic rather than a single call.

**Plan mode** is a constraint-faithful pipeline: `extract requirements -> retrieve -> rate each place -> assemble`.

The heart of it is the **two-layer rater**:
- **Code owns the hard constraints.** Wheelchair access and budget are decided deterministically from a structured *accessibility bank* — a per-city table of sourced ratings (EXCELLENT / GOOD / TOUGH / FAIL / UNKNOWN) with confidence levels. A hard FAIL is a locked wall; no amount of fluent LLM prose can put a stairs-only landmark back on the plan.
- **The LLM refines the soft constraints** (toddler-friendly, pace) *within* the lines code already drew.

Confidence feeds the lock: a HIGH-confidence FAIL is a true wall; a LOW-confidence FAIL softens to "TOUGH — verify," so an unverified guess never slams a door. The system is exactly as strict as its evidence justifies.

When a city is genuinely hard for the given constraints, the assembler leaves day-slots **honestly empty** with a note, rather than padding the plan with places that don't fit.

---

## How it runs

Deployed to AWS from a single command (`./deploy.sh up` / `down`), fully defined in Terraform.

![AWS architecture](./architecture-aws.svg)

- **Compute:** the API and a separate worker pool run on **ECS Fargate**, behind an **Application Load Balancer**.
- **Retrieval:** the 26-city corpus is embedded and indexed in **Amazon OpenSearch** (vector search + semantic cache).
- **State & queue:** **DynamoDB** holds the agent trail; **RDS Postgres** is the LangGraph checkpointer; **Redis on ElastiCache** is the job queue (async path) and session memory.
- **Auth:** **Amazon Cognito** issues JWTs; the app verifies them in a FastAPI dependency. Auth is enforced in the cloud and bypassed in local dev.
- **Secrets & cost:** API keys live in **Secrets Manager**, injected at runtime (never in the image); a **budget alarm** emails at 50% / 90% of a cap.
- **Tracing:** every request and LLM call is traced to **Langfuse** (cost, latency, the reasoning trail).

The store abstraction is the point: moving from local Docker (OpenSearch, Postgres, Redis containers) to managed AWS services was a **config change, not a code change** — the app connects to the same interfaces either way.

---

## Engineering decisions worth defending

These are the choices an interviewer tends to probe, and the honest reasoning behind each.

**RAG describes; a structured bank decides.** Retrieval tells you what a place *is* (narrative, from the guides). It can't be trusted to decide whether a place *passes a hard rule* — an LLM optimizing for a fluent answer will include the famous landmark. So hard constraints are decided by code against a lockable table, not by the model. This split is the whole reliability story.

**ALB, not API Gateway.** The app is a persistent Fargate container with a Server-Sent-Events streaming endpoint. API Gateway's request timeouts and buffering fight SSE, and it'd add a hop on top of the ALB anyway. ALB is the right fit for a long-running container; API Gateway is for Lambda / managed-API features this app doesn't need.

**Redis queue, not SQS.** The queue lives in Redis (on ElastiCache) because the same store also does session memory *and* pub/sub for live token streaming — which SQS can't do — and it kept local/cloud parity. SQS would be the more AWS-native choice for a pure job queue (dead-letter queues, visibility timeouts); noted as a production upgrade.

**Auth verified in-app, not at the edge.** Cognito issues tokens; the FastAPI app verifies the JWT signature (JWKS fetch, RS256, issuer/audience/expiry). This keeps SSE streaming intact and needs no HTTPS/cert/domain for the API. It's also the more transferable pattern — the same verification works in any framework.

---

## Limitations & what's next

Honest about what this is: a **portfolio-grade prototype**, not a product.

- **The data is the moat, and it's thin.** The accessibility ratings are hand-researched for 26 cities from firsthand wheelchair-traveler accounts and official venue pages. For out-of-corpus cities the system falls back to guide-derived, then model-knowledge ratings (clearly flagged, LOW confidence). A real product's next step is a **correction/feedback loop** — users correcting bad ratings, which compounds into trustworthy data over time. That flywheel is the actual product hypothesis.
- **Auth is CLI-and-in-app tested, not production-hardened.** Login works (Cognito -> token -> verified in-app). The natural next step is the full hosted-UI browser flow over HTTPS (needs an ACM cert + domain).
- **Multi-tenancy is not built.** The system is single-tenant. Serving real users would need data isolation at the store layer (user-partitioned DynamoDB, Postgres row-level security, namespaced cache) — designed, not yet implemented.
- **Freshness.** Access details change (a lift breaks, a ramp is added). Production would need `last_verified` timestamps and a re-check cadence.

---

## Stack

**Orchestration:** LangGraph · Python · FastAPI
**LLMs:** Anthropic (reasoning) + OpenAI (embeddings), dispatched via LiteLLM with provider fallback
**Retrieval:** OpenSearch (k-NN vectors + semantic cache), 26-city guide corpus
**State/queue:** DynamoDB · RDS Postgres · Redis
**Infra:** Terraform · ECS Fargate · ALB · Cognito · Secrets Manager · ElastiCache
**Observability:** Langfuse (tracing) · reference-free eval on live traffic

---

*Suitcase is an independent portfolio project. It is advisory, not a booking tool, and covers activities only.*
