"""Node 5 - Writer: synthesise a grounded, cited answer (with draft reflection)."""
from app.agents.state import AgentState
from app import gateway
from app.observability import observe
from app.stores.appstate_dynamo import log_step

_WRITE = """Answer the question using ONLY the evidence. Ground every claim and
cite sources inline as [n] matching the citation numbers. If evidence is
insufficient, say so plainly. Honour any requested formatting.

Question: {q}

Evidence:
{evidence}

Citations available: {cites}"""

_REVIEW = """You are finalizing the answer the user will see. Check the draft against
the question: if a part of the question is unanswered or a claim is missing its [n]
citation, fix it; otherwise keep the draft as-is.

Output ONLY the final answer text — exactly what the user should read. Do NOT
describe your review, do NOT add preamble, and do NOT write meta-comments like
"the draft is complete" or "no corrections needed". Return the answer itself.

Question: {q}

Draft:
{draft}"""

# Alternate prompt for the prompt A/B experiment: same task, but instruct a
# terser, lead-with-the-answer style. Selected when the running experiment's
# variant payload sets prompt_key="concise".
_REVIEW_CONCISE = """You are finalizing the answer the user will see. Make it as
concise as possible while keeping every [n] citation. Lead with the direct answer
in the first sentence, then only essential supporting detail. No preamble, no
meta-comments.

Output ONLY the final answer text.

Question: {q}

Draft:
{draft}"""

_REVIEW_VARIANTS = {"concise": _REVIEW_CONCISE}


def _format_evidence(state: AgentState) -> str:
    blocks = []
    for e in state.get("evidence", []):
        if e.get("tool") == "rag" and e.get("context"):
            blocks.append(e["context"])
        elif e.get("tool") == "sql" and e.get("rows"):
            blocks.append("Structured results:\n" + str(e["rows"][:20]))
    return "\n\n".join(blocks) or "(no evidence retrieved)"


@observe(name="writer")
def writer(state: AgentState) -> AgentState:
    # Grounding-refusal guardrail: if retrieval never cleared the relevance gate,
    # refuse instead of letting the model improvise an ungrounded answer.
    used_rag = any(e.get("tool") == "rag" for e in state.get("evidence", []))
    if used_rag and not state.get("grounded", False):
        msg = ("I couldn't find information about that in the available destination "
               "guides, so I can't answer it from the provided data.")
        log_step(state["thread_id"], "05_writer",
                 {"node": "writer", "refused": True, "reason": "ungrounded"})
        return {**state, "answer": msg}

    evidence = _format_evidence(state)
    cites = [f'[{c["n"]}] {c.get("city")} guide p.{c.get("page")}'
             for c in state.get("citations", [])]
    draft = gateway.chat("write", [{"role": "user", "content": _WRITE.format(
        q=state["clarified_query"], evidence=evidence, cites=cites)}],
        user_id=state.get("user_id"))
    # Draft reflection: one lightweight completeness pass. This produces the
    # user-facing answer, so we STREAM it token-by-token if a token sink was
    # provided (set by the worker when serving a streaming request).
    on_token = None
    try:
        from app.agents.token_sink import get as _get_sink
        on_token = _get_sink(state["thread_id"])
    except Exception:
        on_token = None
    # Prompt experiment: if the running 'write' experiment assigns this user a
    # variant carrying prompt_key, use that alternate review prompt.
    payload = gateway.variant_payload("write", state.get("user_id"))
    review_tmpl = _REVIEW_VARIANTS.get(payload.get("prompt_key"), _REVIEW)
    final = gateway.chat_stream("write",
        [{"role": "user", "content": review_tmpl.format(
            q=state["clarified_query"], draft=draft)}],
        on_token=on_token, user_id=state.get("user_id"))
    log_step(state["thread_id"], "05_writer", {"node": "writer", "chars": len(final)})
    return {**state, "answer": final}
