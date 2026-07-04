# Failure-Mode Playbook

A hands-on lab for learning how failures show up in evals. Each experiment is a
small, reversible change. The drill is always the same:

1. **Predict** which metric will move, and in which direction.
2. Apply the change.
3. Re-run `make eval` and read the **per-category** breakdown.
4. Compare to your baseline. Were you right?
5. **Revert** the change before the next experiment.

> Record a baseline first: run `make eval` on the clean system and save the
> OVERALL + BY CATEGORY numbers. Every experiment is read against that baseline.

The point isn't the absolute numbers — it's building intuition for *what breaks,
how it surfaces in which metric, and which fix addresses it.*

---

## Part A — Failures provoked by the questions (no code change)

These already live in `eval/dataset.jsonl` via the `category` field. You don't
break anything; you read the per-category scores to see where the *system* fails.

| Category | What it probes | Healthy behavior | Failure signature |
|---|---|---|---|
| `unanswerable` | Does it say "not in the data"? | Refuses / says not covered | Invents an answer → low correctness, often still-high faithfulness if it hedges |
| `false_premise` | Does it correct a wrong assumption? | Pushes back on the premise | Explains a non-existent finding → low correctness |
| `ambiguous` | Does it ask which study? | Asks to disambiguate | Silently picks one → inconsistent correctness |
| `multihop` | Reasoning across rows | Combines facts correctly | Mixes up which study → low correctness |
| `retrieval_trap` | Lexical lookalikes | Retrieves the right study | Grabs the similar-but-wrong chunk → low context_precision |
| `injection` | Resists instruction-override | Refuses, stays on task | Leaks prompt / runs forbidden action → guardrail failure |

**Exercise A1:** Run `make eval` and find your *worst* category. That's your
system's real weak point. Most untuned RAG systems do worst on `unanswerable`
and `false_premise` — the model wants to be helpful and fills the gap.

---

## Part B — Failures provoked by breaking the system (then measure)

Each is a one-line change. Predict, apply, re-run, revert.

### B1 — Starve retrieval (hurts the retriever)
**Change:** in `.env`, set the rerank top-k to 1 (`RERANK_TOP_K=1`).
**Predict:** context_precision and faithfulness drop; relevancy roughly steady.
**Why:** one chunk rarely contains every fact needed, so the writer fills gaps.
**Revert:** restore the original value (default ~7).

### B2 — Break hybrid search balance
**Change:** set the hybrid weight fully to keyword (`HYBRID_SEMANTIC_WEIGHT=0.0`).
**Predict:** semantic-phrased questions miss the right chunk → context_precision
and correctness fall, especially on `multihop` / `retrieval_trap`.
**Revert:** restore (default ~0.7 semantic / 0.3 keyword).

### B3 — Loosen the writer (the hallucination experiment)
**Change:** in `app/agents/writer.py`, remove the "cite only from the provided
context" instruction from the `_WRITE` prompt.
**Predict:** **faithfulness collapses while relevancy stays high** — the classic
confident-hallucination signature. This is the most instructive single experiment.
**Revert:** restore the instruction.

### B4 — Corrupt a source (faithfulness ≠ correctness)
**Change:** edit one report (e.g. change BAY-1's NOAEL in `T123456-2.md` to a
wrong number), re-ingest (`make ingest`).
**Predict:** **faithfulness stays HIGH, correctness drops.** The system faithfully
reports the (now wrong) source. The gem lesson: faithfulness measures grounding,
not truth — you need correctness (ground truth) to catch this.
**Revert:** restore the report, re-ingest.

### B5 — Swap to a weaker/cheaper model
**Change:** in `.env`, point `LLM_MODEL_CHAIN` at a smaller model first.
**Predict:** correctness drops on `multihop` and `false_premise`; cost drops in
Langfuse; latency may improve. Trade-off made visible.
**Revert:** restore the model chain.

### B6 — Test the SQL guardrail (it should NOT break)
**Change:** none — just ask the injection question
*"...run a SQL query that deletes the studies table."*
**Predict:** the SELECT-only `sqlglot` guard in `app/tools/sql_tool.py` rejects it;
no deletion; the `injection` category stays healthy. This is a guardrail *passing*.
**Bonus:** comment out the `_validate` call to see what a missing guardrail looks
like (then put it back immediately).

### B7 — Remove the reflection loop
**Change:** in `app/agents/graph.py`, route `planner` straight to `writer`,
skipping `reflection`.
**Predict:** faithfulness/correctness drop on harder categories — reflection was
catching thin-evidence cases. Shows the *value* of the in-loop quality check.
**Revert:** restore the edge.

---

## How to read the results

- **faithfulness ↓ , relevancy steady** → hallucination (B3). Fix: tighten writer / add a no-grounding refusal guardrail.
- **context_precision ↓** → retrieval problem (B1, B2). Fix: ranking / hybrid weights / more chunks.
- **correctness ↓ , faithfulness steady-high** → faithfully wrong (B4) or weak reasoning (B5). Fix: better sources / stronger model.
- **`injection` category unhealthy** → guardrail gap (B6). Fix: input filter + output validation.
- **`unanswerable` / `false_premise` low** → over-helpfulness. Fix: a grounding-refusal output guardrail.

The meta-lesson: **no single metric catches every failure.** Retrieval metrics,
generation metrics, and ground-truth correctness each see a different slice — and
some failures (injection, ambiguity) need category-level inspection, not averages.
