"""Langfuse v3 wiring. No-op if keys are absent, so the app always runs.

Uses the Langfuse v3 SDK (OpenTelemetry-based):
  - `observe()`        -> decorator that traces each workflow node
  - `request_trace()`  -> one root span per /ask, so a request is ONE trace
  - `generation()`     -> wraps a single LLM call (model, input/output, usage)

Everything is defensive: if the SDK is missing or an API differs, tracing
silently turns off and the application keeps working.
"""
import os
from contextlib import contextmanager
from app.config import get_settings

_s = get_settings()
_enabled = bool(_s.langfuse_public_key and _s.langfuse_secret_key)
_client = None
_observe = None

if _enabled:
    # v3 reads these from the OS environment when the client is constructed.
    os.environ["LANGFUSE_PUBLIC_KEY"] = _s.langfuse_public_key
    os.environ["LANGFUSE_SECRET_KEY"] = _s.langfuse_secret_key
    os.environ["LANGFUSE_HOST"] = _s.langfuse_host
    try:
        from langfuse import Langfuse, observe as _observe  # v3 top-level API
        _client = Langfuse()           # configures the OTEL exporter to your project
        print(f"[observability] Langfuse v3 tracing on -> {_s.langfuse_host}")
    except Exception as e:             # SDK missing/incompatible -> tracing off
        print(f"[observability] Langfuse disabled ({e}); app runs without tracing")
        _enabled = False


def observe(name: str | None = None):
    """Decorator that traces a workflow node when Langfuse is configured."""
    def deco(fn):
        if not _enabled or _observe is None:
            return fn
        return _observe(name=name or fn.__name__)(fn)
    return deco


@contextmanager
def request_trace(name: str, user_input, session_id=None, user_id=None, tags=None):
    """Open one root span per request so all node/LLM spans nest into ONE trace.

    session_id groups multiple requests from one conversation/session; user_id
    attributes traffic to a user. Both make Langfuse filterable per-session and
    per-user — the backbone of multi-user production observability.
    """
    if not _enabled or _client is None:
        yield None
        return
    cm = gen = None
    try:
        cm = _client.start_as_current_span(name=name, input=user_input)
        gen = cm.__enter__()
        # Attach identity/metadata to the trace (v3 API; defensive).
        try:
            trace_kwargs = {}
            if session_id: trace_kwargs["session_id"] = session_id
            if user_id: trace_kwargs["user_id"] = user_id
            if tags: trace_kwargs["tags"] = list(tags)
            if trace_kwargs:
                _client.update_current_trace(**trace_kwargs)
        except Exception:
            pass
    except Exception:
        yield None
        return
    try:
        yield gen
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


def add_trace_tags(tags):
    """Append tags to the current trace (used by the gateway to record the
    task / model / A-B variant so dashboards can be sliced by them)."""
    if not _enabled or _client is None:
        return
    try:
        _client.update_current_trace(tags=list(tags))
    except Exception:
        pass


@contextmanager
def generation(name: str, model: str, prompt):
    """Wrap a single LLM call as a Langfuse generation (model, IO, usage)."""
    if not _enabled or _client is None:
        yield None
        return
    cm = gen = None
    try:
        cm = _client.start_as_current_generation(name=name, model=model, input=prompt)
        gen = cm.__enter__()
    except Exception:
        yield None
        return
    try:
        yield gen
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


def flush():
    """Flush buffered traces so they appear promptly (call after each request)."""
    if _enabled and _client is not None:
        try:
            _client.flush()
        except Exception:
            pass


def current_trace_id():
    """Return the active trace id (inside a span), or None if tracing is off."""
    if not _enabled or _client is None:
        return None
    try:
        return _client.get_current_trace_id()
    except Exception:
        return None


def score(trace_id, name, value, comment=None):
    """Attach a numeric score to a specific trace. Defensive: never raises."""
    if not _enabled or _client is None or not trace_id:
        return
    try:
        _client.create_score(trace_id=trace_id, name=name,
                             value=float(value), comment=comment)
    except Exception:
        try:  # older/newer signature fallback
            _client.score(trace_id=trace_id, name=name,
                          value=float(value), comment=comment)
        except Exception:
            pass
