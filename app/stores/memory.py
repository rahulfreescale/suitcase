"""Conversation memory — short-term (per session) and long-term (per user).

This is DIFFERENT from the three request-scoped state stores already in the app:
  - LangGraph checkpointer (Postgres)  -> resumes ONE request's execution
  - AppState trail (DynamoDB)          -> human-facing debug trail for ONE request
  - Interactions log (DynamoDB)        -> source data for live-traffic eval

None of those remember a *conversation*. This store does:

  SHORT-TERM  (key: session_id)  a rolling window of recent (question, answer)
              turns + a running summary of older turns. Injected into the
              clarify/plan prompts so "that city" resolves to earlier context.

  LONG-TERM   (key: user_id)     durable facts about a user across all sessions
              (e.g. "usually asks about Tokyo trips"). Fetched at request
              start to personalise/contextualise.

Backend is Redis so the state is SHARED across processes — which is what lets
*any* worker serve *any* session later (stateless workers = the prerequisite for
distributed execution). The interface (load_session / save_turn /
load_user_memory / save_user_fact) is backend-agnostic: on AWS you swap Redis for
ElastiCache (same client) or DynamoDB with no caller changes.
"""
from __future__ import annotations
import json
import time
from app.config import get_settings

_s = get_settings()

# ---- Redis client (lazy, defensive) -----------------------------------------
_client = None


def _redis():
    """Return a Redis client, or None if unavailable (memory then no-ops)."""
    global _client
    if _client is not None:
        return _client
    if not _s.memory_enabled:
        return None
    try:
        import redis
        _client = redis.Redis.from_url(_s.redis_url, decode_responses=True)
        _client.ping()
    except Exception as e:
        print(f"[memory] Redis unavailable, memory disabled: {e}")
        _client = None
    return _client


def _sess_key(session_id: str) -> str:
    return f"mem:session:{session_id}"        # a Redis LIST of turn JSONs


def _summary_key(session_id: str) -> str:
    return f"mem:summary:{session_id}"        # a Redis STRING (running summary)


def _user_key(user_id: str) -> str:
    return f"mem:user:{user_id}"              # a Redis LIST of fact strings


# ---- SHORT-TERM: session window + summary ------------------------------------
def load_session(session_id: str | None) -> dict:
    """Return {"turns": [...recent...], "summary": "..."} for a session.

    turns are the last `memory_window_turns` (question, answer) pairs; summary is
    a compact recap of everything older. Safe to call with None/unknown session.
    """
    r = _redis()
    if not r or not session_id:
        return {"turns": [], "summary": ""}
    try:
        raw = r.lrange(_sess_key(session_id), -_s.memory_window_turns, -1)
        turns = [json.loads(x) for x in raw]
        summary = r.get(_summary_key(session_id)) or ""
        return {"turns": turns, "summary": summary}
    except Exception as e:
        print(f"[memory] load_session failed: {e}")
        return {"turns": [], "summary": ""}


def save_turn(session_id: str | None, question: str, answer: str) -> None:
    """Append a completed turn to the session, refresh TTL, and compact if long."""
    r = _redis()
    if not r or not session_id:
        return
    try:
        key = _sess_key(session_id)
        r.rpush(key, json.dumps({"q": question, "a": answer, "ts": int(time.time())}))
        r.expire(key, _s.memory_session_ttl_s)
        r.expire(_summary_key(session_id), _s.memory_session_ttl_s)
        # Compact: if the session has grown beyond the window, fold the oldest
        # turn into the running summary and trim the list back down.
        n = r.llen(key)
        if n > _s.memory_summarize_after:
            _compact_session(session_id)
    except Exception as e:
        print(f"[memory] save_turn failed: {e}")


def _compact_session(session_id: str) -> None:
    """Summarize the oldest turns into the running summary, keep the window."""
    r = _redis()
    if not r:
        return
    try:
        key = _sess_key(session_id)
        n = r.llen(key)
        overflow = n - _s.memory_window_turns
        if overflow <= 0:
            return
        old_raw = r.lrange(key, 0, overflow - 1)          # oldest `overflow` turns
        old = [json.loads(x) for x in old_raw]
        prev_summary = r.get(_summary_key(session_id)) or ""
        new_summary = _summarize(prev_summary, old)
        r.set(_summary_key(session_id), new_summary, ex=_s.memory_session_ttl_s)
        r.ltrim(key, overflow, -1)                          # drop the summarized turns
    except Exception as e:
        print(f"[memory] compact failed: {e}")


def _summarize(prev_summary: str, turns: list[dict]) -> str:
    """Fold older turns into a compact running summary via the fast model."""
    from app.llm import chat
    convo = "\n".join(f"User: {t['q']}\nAssistant: {t['a']}" for t in turns)
    prompt = (
        "Maintain a SHORT running summary of a conversation between a user and a "
        "travel-planning assistant. Keep only durable, reference-worthy facts "
        "(which destinations were discussed, trip dates, budget, preferences). "
        "Be concise: a few sentences.\n\n"
        f"Existing summary:\n{prev_summary or '(none yet)'}\n\n"
        f"New turns to fold in:\n{convo}\n\n"
        "Return ONLY the updated summary text.")
    try:
        return chat([{"role": "user", "content": prompt}],
                    model_chain=[_s.llm_fast_model] + _s.model_chain).strip()
    except Exception as e:
        print(f"[memory] summarize failed, keeping previous: {e}")
        return prev_summary


# ---- LONG-TERM: durable user facts -------------------------------------------
def load_user_memory(user_id: str | None) -> list[str]:
    """Return durable facts known about a user (most recent last)."""
    r = _redis()
    if not r or not user_id:
        return []
    try:
        return r.lrange(_user_key(user_id), -_s.memory_max_user_facts, -1)
    except Exception as e:
        print(f"[memory] load_user_memory failed: {e}")
        return []


def save_user_fact(user_id: str | None, fact: str) -> None:
    """Append a durable fact about a user, de-duplicated, capped."""
    r = _redis()
    if not r or not user_id or not fact:
        return
    try:
        key = _user_key(user_id)
        existing = set(r.lrange(key, 0, -1))
        if fact in existing:
            return
        r.rpush(key, fact)
        r.ltrim(key, -_s.memory_max_user_facts, -1)         # keep only the last N
    except Exception as e:
        print(f"[memory] save_user_fact failed: {e}")


def extract_and_save_user_facts(user_id: str | None, question: str, answer: str) -> list[str]:
    """Decide what (if anything) about THIS user is worth remembering long-term.

    The hard part of long-term memory isn't storing — it's *determining* what to
    store. The rule: keep only DURABLE facts about the USER (their role, focus
    areas, stated preferences), never transient facts about one question. We ask
    the fast model to make that call and return a short list — or nothing.

    Dedup and capacity are handled by save_user_fact, so repeated mentions don't
    pile up. Returns the facts that were newly considered (for logging/demo).
    """
    r = _redis()
    if not r or not user_id:
        return []
    from app.llm import chat_json
    prompt = (
        "From this single exchange, extract DURABLE facts about the USER that "
        "would still be useful in a future, unrelated conversation — e.g. their "
        "typical travel style, budget level, destinations of interest, or explicitly "
        "stated preferences. Do NOT include anything about the specific question "
        "asked, one-off details, or facts about the destinations themselves. If there is "
        "nothing durable about the user, return an empty list.\n"
        'Return ONLY JSON: {"facts": ["...", "..."]} with 0-3 short facts.\n\n'
        f"User asked: {question}\n"
        f"Assistant answered: {answer[:600]}")
    try:
        out = chat_json([{"role": "user", "content": prompt}],
                        model_chain=[_s.llm_fast_model] + _s.model_chain)
        facts = out.get("facts", []) if isinstance(out, dict) else []
        if not facts:
            # Visible when the model returned no facts OR returned an unexpected
            # shape — so an empty result is never silently mysterious.
            print(f"[memory] extraction returned no facts (raw type={type(out).__name__}, "
                  f"value={str(out)[:120]})")
    except Exception as e:
        print(f"[memory] fact extraction FAILED: {type(e).__name__}: {str(e)[:200]}")
        return []
    kept = []
    for f in facts:
        f = str(f).strip()
        if f and len(f) < 200:
            save_user_fact(user_id, f)      # dedup + cap handled inside
            kept.append(f)
    return kept


# ---- Entity detection for reference hints (used by rendering) ----------------
import re as _re

# Detects known destination cities mentioned in the conversation, so a reference
# like "that city" / "there" can be resolved to a concrete destination.
# NOTE: this is a pragmatic, corpus-specific detector (the 10 known cities). A
# general system would use a broader entity-linking step.
_CITIES = ["Lisbon", "Tokyo", "Barcelona", "Bangkok", "Reykjavik",
           "Mexico City", "Cape Town", "Queenstown", "Marrakech", "Vancouver"]
_CITY_RE = _re.compile(r"\b(" + "|".join(_re.escape(c) for c in _CITIES) + r")\b",
                       _re.IGNORECASE)


def _canonical_city(name: str) -> str:
    for c in _CITIES:
        if c.lower() == name.lower():
            return c
    return name


def _last_entities(session: dict) -> dict:
    """Scan session memory (recent turns + summary) for the most recent city
    mentioned, so a reference can be resolved to a concrete destination."""
    text_blobs = []
    for t in session.get("turns", []):
        text_blobs.append(t.get("q", ""))
        text_blobs.append(t.get("a", ""))
    text_blobs.append(session.get("summary", ""))
    city = None
    for blob in reversed(text_blobs):          # most-recent mention wins
        m = _CITY_RE.findall(blob or "")
        if m:
            city = _canonical_city(m[-1])
            break
    return {"city": city}


# ---- Prompt rendering: turn memory into a context block ----------------------
def render_memory_context(session: dict, user_facts: list[str]) -> str:
    """Format memory into a compact block to prepend to clarify/plan prompts.

    Empty string when there's nothing — so prompts are unchanged for the very
    first turn of a brand-new user (no spurious 'no history' noise).

    Includes an explicit "currently under discussion" hint naming the most recent
    city. This is the HYBRID approach: code finds the recent entity (a reliable
    lookup), and the model uses that clear pointer to resolve references like
    "that city" / "there" — general across any phrasing, without hardcoding a
    find-and-replace on the query itself.
    """
    parts = []
    if user_facts:
        parts.append("What we know about this user:\n" +
                     "\n".join(f"- {f}" for f in user_facts))
    if session.get("summary"):
        parts.append("Earlier in this conversation:\n" + session["summary"])
    if session.get("turns"):
        recent = "\n".join(f"User: {t['q']}\nAssistant: {t['a']}"
                            for t in session["turns"])
        parts.append("Most recent turns:\n" + recent)
    # Explicit entity hint — the reliable pointer the model resolves against.
    ents = _last_entities(session)
    if ents.get("city"):
        parts.append("Currently under discussion (resolve 'that city', 'it', "
                     "'there' to this unless the user clearly changes topic): "
                     + ents["city"])
    if not parts:
        return ""
    return ("Conversation memory (use it to resolve references like 'that city' "
            "and to stay consistent):\n\n" + "\n\n".join(parts) + "\n\n")


# ---- Deterministic reference resolution (opt-in weak-model fallback) ----------
# References that point back at something named earlier.
_REFERENCE_HINTS = ("that city", "that place", "the city", "this city",
                    "there", "that destination", "the same",
                    "it ", " it?", " it.", "those", "these")


def resolve_reference(query: str, session: dict) -> str:
    """If the query uses a back-reference ('that city', 'there', 'it') and the
    session memory names a concrete city, rewrite the query to be self-contained.
    Deterministic — no model call, so it works with any model.

    Returns the (possibly rewritten) query.
    """
    if not query or not session:
        return query
    ql = query.lower()
    if not any(h in ql for h in _REFERENCE_HINTS):
        return query
    ents = _last_entities(session)
    resolved = query
    if ents["city"]:
        for phrase in ("that city", "this city", "the city", "that place",
                       "that destination"):
            resolved = _re.sub(phrase, ents["city"], resolved, flags=_re.IGNORECASE)
        # "there" as a standalone location reference -> "in <city>"
        resolved = _re.sub(r"\bthere\b", f"in {ents['city']}", resolved, flags=_re.IGNORECASE)
    return resolved
