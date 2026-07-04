"""Stage 1: extract keyword-search terms from the natural-language query."""
from app.llm import chat_json

_PROMPT = (
    "Extract the terms most useful for keyword search over a corpus of "
    "travel destination guides. Return ONLY a JSON array of short strings.\n\nQuery: {q}"
)


def extract_keywords(query: str) -> list[str]:
    try:
        out = chat_json([{"role": "user", "content": _PROMPT.format(q=query)}])
        return [str(x) for x in out][:12]
    except Exception:
        return [w for w in query.split() if len(w) > 3][:12]
