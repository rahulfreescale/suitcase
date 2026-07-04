"""Job queue + result store for distributed execution (Redis-backed).

The synchronous API blocks one process for the full 2-50s of a request, so it
caps at a handful of concurrent users. This decouples the two halves:

  API   -> enqueue a job, return a job_id INSTANTLY (no blocking)
  Worker-> pop a job, run the (unchanged) LangGraph agent, write the result back

Because both the queue AND the session memory live in shared Redis, workers are
STATELESS — any worker can serve any session. That is what lets you scale by
simply running more workers.

Local: Redis (already running for memory).  AWS: swap the queue for SQS and the
result store for DynamoDB/ElastiCache — same interface, config change only.
"""
from __future__ import annotations
import json
import time
import uuid
from app.config import get_settings

_s = get_settings()
_QUEUE_KEY = "jobs:queue"                 # a Redis LIST used as a FIFO queue
_RESULT_PREFIX = "jobs:result:"           # one key per job holding its status/answer
_RESULT_TTL_S = 60 * 60                    # results expire after an hour

_client = None


def _redis():
    global _client
    if _client is not None:
        return _client
    import redis
    _client = redis.Redis.from_url(_s.redis_url, decode_responses=True)
    _client.ping()
    return _client


def _result_key(job_id: str) -> str:
    return f"{_RESULT_PREFIX}{job_id}"


# ---- API side: enqueue + read result -----------------------------------------
def enqueue(query: str, session_id: str | None = None,
            user_id: str | None = None, thread_id: str | None = None) -> str:
    """Push a job and return its id immediately. Non-blocking."""
    r = _redis()
    job_id = str(uuid.uuid4())
    job = {"job_id": job_id, "query": query,
           "session_id": session_id or "", "user_id": user_id or "",
           "thread_id": thread_id or str(uuid.uuid4()),
           "enqueued_at": time.time()}
    # Mark queued FIRST (so a poll right after enqueue sees a valid status),
    # then push onto the queue for a worker to pick up.
    r.set(_result_key(job_id), json.dumps({"status": "queued"}), ex=_RESULT_TTL_S)
    r.rpush(_QUEUE_KEY, json.dumps(job))
    return job_id


def get_result(job_id: str) -> dict:
    """Return the current status/result for a job. Never blocks."""
    r = _redis()
    raw = r.get(_result_key(job_id))
    if raw is None:
        return {"status": "unknown"}      # expired or never existed
    return json.loads(raw)


def queue_depth() -> int:
    """How many jobs are waiting — the signal you autoscale workers on."""
    return _redis().llen(_QUEUE_KEY)


# ---- Worker side: pop + write result -----------------------------------------
def dequeue(block_timeout_s: int = 5) -> dict | None:
    """Pop the next job, blocking up to block_timeout_s. None if idle."""
    r = _redis()
    popped = r.blpop(_QUEUE_KEY, timeout=block_timeout_s)   # (key, value) or None
    if not popped:
        return None
    return json.loads(popped[1])


def mark_running(job_id: str) -> None:
    r = _redis()
    r.set(_result_key(job_id), json.dumps({"status": "running"}), ex=_RESULT_TTL_S)


def mark_done(job_id: str, result: dict) -> None:
    r = _redis()
    payload = {"status": "done", **result}
    r.set(_result_key(job_id), json.dumps(payload), ex=_RESULT_TTL_S)


def mark_failed(job_id: str, error: str) -> None:
    r = _redis()
    r.set(_result_key(job_id), json.dumps({"status": "failed", "error": error}),
          ex=_RESULT_TTL_S)
