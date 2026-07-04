"""Unified, OpenAI-compatible chat layer with provider fallbacks.

A single endpoint over many providers: a single endpoint over many providers, retrying a
model a few times before falling back to an alternative provider/model.
"""
from __future__ import annotations
import json
import litellm
from app.config import get_settings
from app.observability import generation  # Langfuse v3 generation spans

_s = get_settings()
litellm.drop_params = True  # ignore params a given provider doesn't support


def chat(messages: list[dict], model_chain: list[str] | None = None,
         temperature: float = 0.0, max_tokens: int = 1500) -> str:
    """Try each model in the chain (with retries) until one succeeds."""
    chain = model_chain or _s.model_chain
    last_err: Exception | None = None
    for model in chain:
        try:
            with generation("llm", model, messages) as gen:
                resp = litellm.completion(
                    model=model, messages=messages,
                    temperature=temperature, max_tokens=max_tokens,
                    num_retries=_s.llm_num_retries,
                    timeout=_s.llm_timeout_s,        # fail fast instead of hanging
                )
                content = resp["choices"][0]["message"]["content"]
                if gen is not None:
                    try:
                        u = getattr(resp, "usage", None)
                        usage = {"input": getattr(u, "prompt_tokens", None),
                                 "output": getattr(u, "completion_tokens", None)} if u else None
                        gen.update(output=content, usage_details=usage)
                    except Exception:
                        pass
            return content
        except Exception as e:  # provider/model failure -> fall back
            last_err = e
            continue
    raise RuntimeError(f"All models failed: {chain}") from last_err


def chat_stream(messages: list[dict], on_token=None, model_chain: list[str] | None = None,
                temperature: float = 0.0, max_tokens: int = 1500) -> str:
    """Like chat(), but streams tokens as they're generated.

    Calls on_token(text) for each delta as the model produces it (so the caller
    can publish tokens live), and still returns the complete assembled string.
    Falls back through the model chain like chat(). If a provider doesn't
    support streaming, litellm still yields one final chunk — so this degrades
    gracefully to "whole answer at the end" rather than breaking.
    """
    chain = model_chain or _s.model_chain
    last_err: Exception | None = None
    for model in chain:
        try:
            with generation("llm-stream", model, messages) as gen:
                parts = []
                stream = litellm.completion(
                    model=model, messages=messages,
                    temperature=temperature, max_tokens=max_tokens,
                    num_retries=_s.llm_num_retries, timeout=_s.llm_timeout_s,
                    stream=True,
                )
                for chunk in stream:
                    try:
                        delta = chunk["choices"][0]["delta"].get("content")
                    except Exception:
                        delta = None
                    if delta:
                        parts.append(delta)
                        if on_token is not None:
                            try:
                                on_token(delta)
                            except Exception:
                                pass
                content = "".join(parts)
                if gen is not None:
                    try:
                        gen.update(output=content)
                    except Exception:
                        pass
            return content
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All models failed (stream): {chain}") from last_err


def chat_json(messages: list[dict], **kw) -> dict | list:
    """Chat call that must return JSON. Strips code fences defensively."""
    raw = chat(messages, **kw).strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        raw = raw[4:].strip() if raw.lower().startswith("json") else raw.strip()
    return json.loads(raw)
