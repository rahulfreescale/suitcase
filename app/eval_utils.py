"""Shared helpers for turning workflow state into evaluation inputs."""


def contexts_from_state(state: dict) -> list[str]:
    """Flatten retrieved evidence (RAG passages + SQL rows) into context strings."""
    contexts: list[str] = []
    for e in state.get("evidence", []):
        if e.get("context"):
            contexts.append(e["context"])
        for row in (e.get("rows") or []):
            contexts.append(str(row))
    return contexts or ["(none)"]
