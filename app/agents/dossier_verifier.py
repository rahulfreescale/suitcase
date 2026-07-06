"""Verifier agent — the 3rd true agent, and the honest 'quality' agent.

The plain auditor (dossier_auditor.py) judges the draft from the TEXT ALONE in a
single model call. This verifier is different: it's a TRUE AGENT that can call
tools to CHECK specific claims against ground truth before ruling. It decides
which claims are worth verifying (a named venue, a "short step-free walk", an
access assertion), calls the relevant tool, and flags only claims that fail
verification — with evidence.

It is config-controlled (settings.enable_verifier_agent, on by default) and
returns the SAME verdict shape as the plain auditor, so it slots into the exact
same revise loop without any graph change.

Why an agent and not a task: the auditor makes one judgment call in a coded
loop (a workflow evaluator). This verifier OWNS its control flow — it decides
what to verify and loops on tool results — which is what makes it an agent.
"""
from __future__ import annotations
import json
from app import gateway
from app.agents.tool_agents import TOOL_SCHEMAS, TOOL_REGISTRY
from app.observability import observe

try:
    from app.config import get_settings
    _settings = get_settings()
except Exception:
    _settings = None


_VERIFY_SYS = (
    "You are the fact-checking VERIFIER for an accessibility-first travel brief. "
    "You are given the traveler's constraints, the places in their plan (with "
    "coordinates), and the draft prose sections. Your job: catch claims that are "
    "WRONG or UNSUPPORTED by reality — especially accessibility claims, which are "
    "the highest-stakes.\n\n"
    "You have tools to check the truth:\n"
    "- accessible_places / special_needs_nearby -> does a named venue actually "
    "exist near there, and what do its real OSM access tags say?\n"
    "- route_leg -> is a claimed 'short step-free walk' actually short and "
    "step-free?\n"
    "- rest_stops -> are the claimed toilets/benches actually mapped?\n"
    "- weather / air_quality / holidays_in_window -> are stated figures real?\n\n"
    "PROCESS:\n"
    "1. Scan the sections. Pick the RISKIEST specific claims — a named venue, an "
    "access assertion ('wheelchair accessible', 'step-free'), a distance/time, a "
    "count. Don't verify generic advice ('call ahead').\n"
    "2. Call tools to check those claims. Be economical: verify the few claims "
    "that would most mislead a disabled traveler if wrong.\n"
    "3. When done, output ONLY a JSON verdict (no tool call):\n"
    '{"accessibility_ok": <bool>, "quality_score": <0-10>, '
    '"violations": [{"section":"<name>","problem":"<what tool showed>","fix_hint":"<how to fix>"}], '
    '"sections_to_redraft": ["<section names that failed a check>"]}\n'
    "A claim that CONTRADICTS a tool result is a violation. A claim you couldn't "
    "verify is NOT automatically a violation — only flag genuine contradictions "
    "or clearly invented specifics. Accessibility contradictions ALWAYS set "
    "accessibility_ok=false and go in sections_to_redraft."
)


def _placed(itinerary: dict) -> list:
    out = []
    for d in itinerary.get("days", []):
        for slot in ("morning", "afternoon", "evening"):
            b = (d.get("blocks") or {}).get(slot)
            if b and b.get("lat") is not None:
                out.append({"name": b.get("name_hint"), "lat": b["lat"], "lng": b["lng"]})
    return out


@observe(name="verifier_agent")
def verify_sections(contract: dict, itinerary: dict, sections: list[dict],
                    user_id=None) -> dict:
    """Agentic fact-check of the draft. Returns the auditor verdict shape."""
    payload = {
        "traveler_constraints": contract.get("travelers", []),
        "dietary": contract.get("dietary", []),
        "planned_places": _placed(itinerary),
        "sections": [{"section": s["section"], "title": s.get("title"),
                      "body": s.get("body", "")}
                     for s in sections if s.get("body")],
    }
    try:
        result = gateway.chat_tools(
            "reflection",
            [{"role": "system", "content": _VERIFY_SYS},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            tools=TOOL_SCHEMAS, tool_registry=TOOL_REGISTRY,
            user_id=user_id, max_iters=6)
    except Exception as e:
        # fail-open but conservative: don't block the brief on a flaky verifier
        return {"accessibility_ok": True, "violations": [], "quality_score": None,
                "sections_to_redraft": [], "_verifier_error": f"{type(e).__name__}: {e}"}

    # the final content should be the JSON verdict
    verdict = {}
    content = (result or {}).get("content", "")
    if content:
        try:
            cleaned = content.strip().replace("```json", "").replace("```", "").strip()
            verdict = json.loads(cleaned)
        except Exception:
            verdict = {}

    verdict.setdefault("accessibility_ok", True)
    verdict.setdefault("violations", [])
    verdict.setdefault("sections_to_redraft", [])
    verdict.setdefault("quality_score", None)
    # attach the trace so we can show what it checked
    verdict["_verify_trace"] = (result or {}).get("trace", [])
    return verdict


def verifier_enabled() -> bool:
    """Config gate — on by default, can be turned off."""
    if _settings is None:
        return True
    return bool(getattr(_settings, "enable_verifier_agent", True))
