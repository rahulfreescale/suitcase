"""Pick the structured-store backend from config."""
from app.config import get_settings

_s = get_settings()


def run_structured_select(sql: str):
    if _s.structured_backend == "athena":
        from app.stores import structured_athena as backend
    else:
        from app.stores import structured_local as backend
    return backend.run_select(sql)
