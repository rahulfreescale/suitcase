"""Embedding helper (Bedrock Titan / OpenAI / any litellm-supported model)."""
import litellm
from app.config import get_settings

_s = get_settings()


def embed(texts: list[str]) -> list[list[float]]:
    resp = litellm.embedding(model=_s.embed_model, input=texts)
    # litellm normalises to OpenAI shape: {"data": [{"embedding": [...]}, ...]}
    return [row["embedding"] for row in resp["data"]]


def embed_one(text: str) -> list[float]:
    return embed([text])[0]
