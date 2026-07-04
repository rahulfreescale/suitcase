"""Live streaming over Redis pub/sub — the channel between workers and the API.

The result store (jobs:result:*) holds the FINAL answer, durably, for polling.
But a user watching a 2-50s request wants to see progress *as it happens*:
"researching... writing... <tokens streaming in>". That needs a live push, not a
poll.

Design (why pub/sub, not a direct worker->API socket):
  - The worker doesn't know which API instance holds the user's SSE connection
    (there are many behind a load balancer). It publishes to a per-job CHANNEL
    without knowing who listens.
  - Whichever API instance holds that user's connection SUBSCRIBES to the channel
    and relays messages to the browser. Neither side knows about the other; both
    only know Redis. That keeps workers stateless and lets APIs/workers scale
    independently.

Channel: stream:<job_id>. Messages are small JSON events:
  {"type":"status","stage":"researching"}
  {"type":"token","text":"Based"}
  {"type":"done","answer":"...full text..."}
  {"type":"error","error":"..."}

Pub/sub is ephemeral (delivered to whoever is subscribed *now*, then gone) — so
the worker ALSO writes the final answer to the durable result store, letting a
user who disconnected still poll /result for the complete answer.
"""
from __future__ import annotations
import json
from app.config import get_settings

_s = get_settings()
_client = None


def _redis():
    global _client
    if _client is None:
        import redis
        _client = redis.Redis.from_url(_s.redis_url, decode_responses=True)
        _client.ping()
    return _client


def _channel(job_id: str) -> str:
    return f"stream:{job_id}"


# ---- worker side: publish events ---------------------------------------------
def publish(job_id: str, event: dict) -> None:
    """Publish one streaming event to this job's channel. Best-effort (never
    let a streaming hiccup break the actual job)."""
    try:
        _redis().publish(_channel(job_id), json.dumps(event))
    except Exception as e:
        print(f"[stream] publish skipped: {e}")


def publish_status(job_id: str, stage: str) -> None:
    publish(job_id, {"type": "status", "stage": stage})


def publish_token(job_id: str, text: str) -> None:
    publish(job_id, {"type": "token", "text": text})


def publish_done(job_id: str, answer: str, **extra) -> None:
    publish(job_id, {"type": "done", "answer": answer, **extra})


def publish_clarification(job_id: str, question: str) -> None:
    publish(job_id, {"type": "clarification", "question": question})


def publish_error(job_id: str, error: str) -> None:
    publish(job_id, {"type": "error", "error": error})


# ---- API side: subscribe and yield events ------------------------------------
def subscribe(job_id: str, timeout_s: int = 120):
    """Generator yielding event dicts for a job until a terminal event
    (done/error) or timeout. The API turns each yielded event into an SSE line.

    Uses its OWN Redis connection + pubsub (pub/sub connections are stateful and
    must not be shared with the command client)."""
    import redis
    r = redis.Redis.from_url(_s.redis_url, decode_responses=True)
    ps = r.pubsub()
    ps.subscribe(_channel(job_id))
    try:
        # get_message with a timeout lets us bound how long we wait overall.
        waited = 0.0
        poll = 0.5
        while waited < timeout_s:
            msg = ps.get_message(ignore_subscribe_messages=True, timeout=poll)
            if msg is None:
                waited += poll
                continue
            waited = 0.0  # reset idle timer on any activity
            try:
                event = json.loads(msg["data"])
            except Exception:
                continue
            yield event
            if event.get("type") in ("done", "error"):
                return
        yield {"type": "error", "error": "stream timeout"}
    finally:
        try:
            ps.unsubscribe(_channel(job_id))
            ps.close()
        except Exception:
            pass
