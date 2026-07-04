"""Stage 2: generate a metadata filter to shrink the search space.

Few-shot prompted; emits a small dict of equality filters that map to
OpenSearch term filters (e.g. {"city": "Lisbon"}).
"""
from app.llm import chat_json

_FIELDS = ["city", "country", "region"]
_PROMPT = """You build metadata filters for a travel destination-guide search index.
Allowed fields: {fields}. Extract only filters explicitly implied by the query.
Return ONLY a JSON object (may be empty). Examples:
Q: "things to do in Lisbon"                 -> {{"city": "Lisbon"}}
Q: "guides for cities in Asia"              -> {{"region": "Asia"}}
Q: "general travel tips"                     -> {{}}

Query: {q}"""


def generate_filter(query: str) -> dict:
    try:
        out = chat_json([{"role": "user",
                          "content": _PROMPT.format(fields=_FIELDS, q=query)}])
        return {k: v for k, v in out.items() if k in _FIELDS and v}
    except Exception:
        return {}
