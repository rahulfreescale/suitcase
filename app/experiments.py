"""Experiment store — the abstract "which variant for this user?" seam.

WHY THIS EXISTS (the Level-2 decoupling):
  The gateway must not hardcode what experiments are running. If it did, starting
  or stopping an experiment would mean editing code and redeploying. Instead the
  experiment definition lives OUTSIDE the code — here, in Redis — so it can be
  changed at runtime (start / stop / adjust split) with zero deploy, and a bad
  variant can be killed INSTANTLY.

THE SEAM:
  Everything goes through get_variant(experiment, user_id). Today it reads from
  Redis. To move to a real Level-3 system (Statsig, LaunchDarkly for models/config;
  Langfuse for prompts) you swap ONLY the body of this function — the gateway and
  nodes never change. That is the whole point of the abstraction.

WHAT A VARIANT CARRIES (agnostic):
  A variant's payload is an opaque dict. It can hold a model override, a prompt
  key, a top_k, a boolean — anything. The store doesn't care what's being varied;
  it just assigns users to variants and returns the payload. That's why this works
  for BOTH the model experiment and the prompt experiment.

Experiment config shape stored at redis key  exp:<name> :
  {
    "task": "write",
    "variants": {
      "A": {"share": 50, "payload": {"model_chain": ["...sonnet..."]}},
      "B": {"share": 50, "payload": {"model_chain": ["...haiku..."]}}
    }
  }
Assignment is deterministic: hash(name:user_id) % 100 against cumulative shares,
so a given user always lands in the same variant (no mid-conversation flip).
"""
from __future__ import annotations
import json
import hashlib
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


def _key(name: str) -> str:
    return f"exp:{name}"


# --- admin: manage experiments at runtime (no deploy) -------------------------
def set_experiment(name: str, task: str, variants: dict) -> None:
    """Start/replace an experiment. variants = {"A": {"share":50,"payload":{...}}, ...}."""
    cfg = {"task": task, "variants": variants}
    _redis().set(_key(name), json.dumps(cfg))


def stop_experiment(name: str) -> None:
    """Kill switch — delete the experiment; everyone reverts to routed defaults."""
    _redis().delete(_key(name))


def get_experiment(name: str) -> dict | None:
    raw = _redis().get(_key(name))
    return json.loads(raw) if raw else None


def list_experiments() -> list[dict]:
    out = []
    for k in _redis().scan_iter("exp:*"):
        raw = _redis().get(k)
        if raw:
            d = json.loads(raw)
            d["name"] = k.split(":", 1)[1]
            out.append(d)
    return out


# --- the seam the gateway calls ----------------------------------------------
def _bucket(name: str, user_id: str) -> int:
    return int(hashlib.sha1(f"{name}:{user_id}".encode()).hexdigest(), 16) % 100


def get_variant(name: str, user_id: str | None) -> tuple[str | None, dict | None]:
    """Return (variant_name, payload) for this user in the named experiment, or
    (None, None) if no experiment is running or no user id. Deterministic per
    user. THIS is the swappable seam — replace the body to use Statsig /
    LaunchDarkly / Langfuse instead of Redis."""
    if not user_id:
        return None, None
    cfg = get_experiment(name)
    if not cfg:
        return None, None
    variants = cfg.get("variants", {})
    b = _bucket(name, user_id)
    cum = 0
    for vname, v in variants.items():
        cum += v.get("share", 0)
        if b < cum:
            return vname, v.get("payload", {})
    return None, None
