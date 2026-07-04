"""A tiny process-local registry mapping thread_id -> token callback.

Why this exists: the writer node needs a function to call for each token it
streams. But that function CANNOT live in the graph state — LangGraph's
checkpointer serializes state between nodes, and a function/lambda isn't
serializable (it would crash or be silently dropped). So instead of putting the
callback in state, we register it here by thread_id before the run and look it
up inside the writer. The registry is in-process (each worker has its own),
which is exactly right: the worker running a given thread is the one that
registered its sink.
"""
from __future__ import annotations

_sinks: dict[str, object] = {}


def register(thread_id: str, on_token) -> None:
    if thread_id and on_token is not None:
        _sinks[thread_id] = on_token


def get(thread_id: str):
    return _sinks.get(thread_id)


def clear(thread_id: str) -> None:
    _sinks.pop(thread_id, None)
