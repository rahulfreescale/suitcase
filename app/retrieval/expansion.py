"""Stage 3: query expansion with a smaller, faster model (recall booster)."""
from app.llm import chat_json
from app.config import get_settings

_s = get_settings()
_PROMPT = (
    "Rewrite the question into {n} semantically equivalent variants that use "
    "synonyms and alternate phrasings a traveler might use. Keep any city names "
    "intact. Return ONLY a JSON array of strings.\n\nQuestion: {q}"
)


def expand(query: str, n: int | None = None) -> list[str]:
    n = n or _s.expansion_n
    try:
        out = chat_json(
            [{"role": "user", "content": _PROMPT.format(n=n, q=query)}],
            model_chain=[_s.llm_fast_model] + _s.model_chain,
        )
        variants = [str(x) for x in out][:n]
    except Exception:
        variants = []
    return [query] + variants  # always keep the original
