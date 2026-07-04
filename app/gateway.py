"""Model gateway — decides WHICH model handles each call, and tracks it.

Layering:
  agent nodes  ->  gateway (route + A/B + tag)  ->  llm.py (call + fallback)

llm.py already gives us "call a model chain with retries and provider fallback."
The gateway adds the decision layer on top:

  1. ROUTING — pick the model chain by TASK. Cheap, structured steps (clarify,
     plan, reflect) don't need a frontier model; the final answer (write) does.
     This cuts cost and latency without hurting answer quality.

  2. A/B TESTING — deterministically assign each user to a variant by
     hash(user_id), so a user always gets the same variant (no flip-flopping
     mid-conversation). A variant can override the model for a task, letting us
     compare e.g. "Haiku-write" vs "Sonnet-write" on live traffic.

  3. TAGGING — record the task, chosen model, and variant on the trace, so the
     existing Langfuse dashboards can be sliced by variant to pick a winner.

Everything still flows through llm.py, so provider fallback is preserved: the
gateway chooses the PRIMARY chain, llm.py handles failure by moving down it.
"""
from __future__ import annotations
import hashlib
from app.config import get_settings
from app import llm
from app import experiments
from app.observability import add_trace_tags

_s = get_settings()

# --- Routing table: task -> ordered model chain -------------------------------
# Falls back to the configured default chain for any unlisted task. The fast
# model handles the lightweight structured steps; the default (stronger) chain
# handles research synthesis and the final written answer.
def _routes() -> dict[str, list[str]]:
    fast = [_s.llm_fast_model] + _s.model_chain          # fast, but fall back to strong
    strong = _s.model_chain
    return {
        "clarify": fast,
        "plan": fast,
        "reflect": fast,
        "research": strong,
        "write": strong,
    }


# Experiment name per task (an experiment on a task is stored under this name).
def _exp_name(task: str) -> str:
    return f"{task}-experiment"


def _resolve(task: str, user_id: str | None) -> tuple[list[str], str | None, dict]:
    """Pick the model chain + any prompt override for a task, applying an A/B
    experiment if one is running for this task. Returns (chain, variant, payload).

    payload may carry:
      - "model_chain": [...]  -> overrides the routed model chain
      - "prompt_key": "..."   -> node uses an alternate prompt (prompt experiment)
    The store is agnostic; the gateway/nodes interpret the payload.
    """
    variant, payload = experiments.get_variant(_exp_name(task), user_id)
    payload = payload or {}
    if payload.get("model_chain"):
        chain = payload["model_chain"]
    else:
        chain = _routes().get(task, _s.model_chain)
    try:
        tags = [f"task:{task}", f"model:{chain[0]}"]
        if variant:
            tags.append(f"variant:{variant}")
            if payload.get("prompt_key"):
                tags.append(f"prompt:{payload['prompt_key']}")
        add_trace_tags(tags)
    except Exception:
        pass
    return chain, variant, payload


# --- Public API: same shapes as llm.py, but task-aware ------------------------
def chat(task: str, messages: list[dict], user_id: str | None = None, **kw) -> str:
    chain, _, _ = _resolve(task, user_id)
    return llm.chat(messages, model_chain=chain, **kw)


def chat_json(task: str, messages: list[dict], user_id: str | None = None, **kw):
    chain, _, _ = _resolve(task, user_id)
    return llm.chat_json(messages, model_chain=chain, **kw)


def chat_stream(task: str, messages: list[dict], on_token=None,
                user_id: str | None = None, **kw) -> str:
    chain, _, _ = _resolve(task, user_id)
    return llm.chat_stream(messages, on_token=on_token, model_chain=chain, **kw)


def variant_payload(task: str, user_id: str | None) -> dict:
    """Expose the variant payload so a node can pick an alternate prompt
    (prompt experiment). Returns {} if no experiment/variant applies."""
    _, _, payload = _resolve(task, user_id)
    return payload or {}
