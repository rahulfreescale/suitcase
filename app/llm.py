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


def chat_tools(messages: list[dict], tools: list[dict], tool_registry: dict,
               model_chain: list[str] | None = None, temperature: float = 0.0,
               max_tokens: int = 1500, max_iters: int = 6,
               on_step=None) -> dict:
    """Agentic tool-calling loop. The MODEL decides which tools to call and when
    to stop — this is what makes a caller a true 'agent' rather than a workflow.

    - `tools`: OpenAI-style tool schemas (list of {"type":"function","function":{...}})
    - `tool_registry`: {tool_name: python_callable} the loop invokes when the model
      asks for a tool. The callable receives the model's args as kwargs and returns
      any JSON-serializable result.
    - `on_step(kind, name, payload)`: optional callback for observability/UI — fired
      as 'tool_call' (model requested a tool) and 'tool_result' (we ran it).

    Returns {"content": <final text>, "trace": [ {name,args,result}, ... ],
             "iters": <n>}. The trace is the evidence of real agentic tool use.

    The loop: send messages+tools -> if model returns tool_calls, execute them,
    append results, repeat -> when model returns plain content, that's the answer.
    Falls back through the model chain like chat() if a provider errors.
    """
    chain = model_chain or _s.model_chain
    last_err: Exception | None = None
    for model in chain:
        try:
            convo = list(messages)
            trace = []
            for _i in range(max_iters):
                # Wrap each tool-loop call in a Langfuse generation span so the
                # tools offered AND the tool the model requests are traced —
                # essential for production debugging of agent behaviour.
                with generation("agent-tool-call", model, convo) as gen:
                    resp = litellm.completion(
                        model=model, messages=convo, tools=tools,
                        tool_choice="auto", temperature=temperature,
                        max_tokens=max_tokens, num_retries=_s.llm_num_retries,
                        timeout=_s.llm_timeout_s,
                    )
                    msg = resp["choices"][0]["message"]
                    tool_calls = msg.get("tool_calls") or []
                    if gen is not None:
                        try:
                            # log what tools were offered + what the model chose
                            chose = [{"name": tc["function"]["name"],
                                      "args": tc["function"].get("arguments")}
                                     for tc in tool_calls]
                            u = getattr(resp, "usage", None)
                            usage = {"input": getattr(u, "prompt_tokens", None),
                                     "output": getattr(u, "completion_tokens", None)} if u else None
                            gen.update(
                                output={"tool_calls_requested": chose,
                                        "content": msg.get("content")},
                                usage_details=usage,
                                metadata={"tools_offered": [
                                    t.get("function", t).get("name") for t in tools],
                                    "iteration": _i})
                        except Exception:
                            pass
                if not tool_calls:
                    # model is done — plain content is the final answer
                    return {"content": msg.get("content") or "", "trace": trace,
                            "iters": _i}
                # record the assistant turn that requested tools
                convo.append({"role": "assistant", "content": msg.get("content"),
                              "tool_calls": tool_calls})
                # execute each requested tool and feed results back
                for tc in tool_calls:
                    fn = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                    except Exception:
                        args = {}
                    if on_step:
                        try: on_step("tool_call", fn, args)
                        except Exception: pass
                    impl = tool_registry.get(fn)
                    if impl is None:
                        result = {"error": f"unknown tool {fn}"}
                    else:
                        try:
                            result = impl(**args)
                        except Exception as e:
                            result = {"error": f"{type(e).__name__}: {e}"}
                    trace.append({"name": fn, "args": args, "result": result})
                    if on_step:
                        try: on_step("tool_result", fn, result)
                        except Exception: pass
                    convo.append({"role": "tool", "tool_call_id": tc.get("id"),
                                  "name": fn, "content": json.dumps(result)[:4000]})
            # ran out of iterations — ask for a final answer without tools
            convo.append({"role": "user",
                          "content": "Summarize your findings now. Do not call more tools."})
            final = litellm.completion(model=model, messages=convo,
                                       temperature=temperature, max_tokens=max_tokens,
                                       num_retries=_s.llm_num_retries, timeout=_s.llm_timeout_s)
            return {"content": final["choices"][0]["message"].get("content") or "",
                    "trace": trace, "iters": max_iters}
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All models failed (tools): {chain}") from last_err


def chat_json(messages: list[dict], **kw) -> dict | list:
    """Chat call that must return JSON. Strips code fences defensively."""
    raw = chat(messages, **kw).strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        raw = raw[4:].strip() if raw.lower().startswith("json") else raw.strip()
    return json.loads(raw)
