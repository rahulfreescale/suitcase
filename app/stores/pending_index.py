"""A tiny Redis index of email-approval workflows awaiting admin action.

Why this exists (design choice 'b'): the admin page needs to list every request
currently awaiting approval. We *could* ask Temporal's visibility API to list
running workflows, but that has an indexing lag on the dev server. Instead, when
a workflow is started we drop a small record into a Redis set, and remove it once
the workflow reaches a terminal state (sent / rejected / expired). The admin page
reads this set — instant, and it reuses the Redis we already run.

The record is intentionally minimal (id, recipient, query, when). The
authoritative state always comes from Temporal's `status` query; this index is
just a fast "which workflow ids are in flight" list.

Mirrors the lazy, defensive Redis pattern used elsewhere (memory.py / jobs.py):
if Redis is unavailable, every function no-ops gracefully.
"""
from __future__ import annotations

import json
import time

from app.config import get_settings

_SET_KEY = "email:pending"
MAX_AGE_SECONDS = 3600   # prune index entries older than 1h                 # a Redis HASH: workflow_id -> record json
_client = None


def _redis():
    """Return a Redis client, or None if unavailable (index then no-ops)."""
    global _client
    if _client is not None:
        return _client
    try:
        import redis
        _client = redis.Redis.from_url(get_settings().redis_url,
                                       decode_responses=True)
        _client.ping()
    except Exception as e:  # noqa: BLE001
        print(f"[pending_index] Redis unavailable, index disabled: {e}")
        _client = None
    return _client


def add_pending(workflow_id: str, recipient: str, query: str) -> None:
    """Record a newly-started workflow as pending."""
    r = _redis()
    if r is None:
        return
    record = {
        "workflow_id": workflow_id,
        "recipient": recipient,
        "query": query,
        "requested_at": int(time.time()),
    }
    try:
        r.hset(_SET_KEY, workflow_id, json.dumps(record))
    except Exception as e:  # noqa: BLE001
        print(f"[pending_index] add failed: {e}")


def remove_pending(workflow_id: str) -> None:
    """Drop a workflow from the index once it reaches a terminal state."""
    r = _redis()
    if r is None:
        return
    try:
        r.hdel(_SET_KEY, workflow_id)
    except Exception as e:  # noqa: BLE001
        print(f"[pending_index] remove failed: {e}")


def list_pending() -> list[dict]:
    """Return the recorded pending workflows (most recent first).

    Entries older than MAX_AGE_SECONDS are pruned on read — a dead/abandoned
    workflow can't linger in the index forever and slow the admin list.

    NOTE: this returns the lightweight index records only. The caller enriches
    each with the live Temporal `status` query for the authoritative state.
    """
    r = _redis()
    if r is None:
        return []
    try:
        raw = r.hgetall(_SET_KEY) or {}
    except Exception as e:  # noqa: BLE001
        print(f"[pending_index] list failed: {e}")
        return []
    now = int(time.time())
    out = []
    for _id, blob in raw.items():
        try:
            rec = json.loads(blob)
        except Exception:  # noqa: BLE001
            r.hdel(_SET_KEY, _id)   # unparseable — drop it
            continue
        if now - int(rec.get("requested_at", now)) > MAX_AGE_SECONDS:
            r.hdel(_SET_KEY, _id)   # too old — prune
            continue
        out.append(rec)
    out.sort(key=lambda x: x.get("requested_at", 0), reverse=True)
    return out
