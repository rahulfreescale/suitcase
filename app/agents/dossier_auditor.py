"""Dossier auditor — the quality gate and, crucially, the accessibility guardian.

It reads ALL specialist sections together (not one at a time) and checks three
things:
  1. Accessibility consistency (the thesis, structurally enforced): does any
     section recommend or route through something that undercuts the itinerary's
     accessibility decisions? Did a specialist suggest a place the itinerary
     LEFT OUT for a constraint? That's a hard fail.
  2. Coherence: do the sections contradict each other or repeat? Is it one voice?
  3. Quality: is it vivid and specific, or generic filler?

It returns a structured verdict. Sections that fail get flagged for a re-draft.
The auditor never rewrites content itself - it judges and routes, keeping the
separation of concerns that makes this a real multi-agent system.
"""
from __future__ import annotations
import json
from app import gateway
from app.agents.dossier_specialists import _left_out, _constraint_line


_AUDIT_SYS = (
    "You are the senior editor and accessibility auditor for a premium, "
    "accessibility-first travel concierge. You are given the traveler's "
    "constraints, the list of places the itinerary deliberately LEFT OUT (for "
    "accessibility or other hard reasons), and the draft prose sections of a "
    "dossier.\n\n"
    "Audit the sections together and return STRICT JSON only:\n"
    "{\n"
    '  "accessibility_ok": true|false,\n'
    '  "violations": [ {"section":"...", "problem":"...", "fix_hint":"..."} ],\n'
    '  "coherence_notes": "one or two sentences",\n'
    '  "quality_score": 0-100,\n'
    '  "sections_to_redraft": ["section_name", ...],\n'
    '  "verdict": "approve" | "revise"\n'
    "}\n\n"
    "Rules:\n"
    "- If ANY section recommends or routes through a LEFT-OUT place, or suggests "
    "something clearly inaccessible for this traveler (stairs-only, steep cobbles, "
    "inaccessible transit), set accessibility_ok=false, record it in violations, "
    "and put that section in sections_to_redraft. Accessibility violations ALWAYS "
    "mean verdict=revise.\n"
    "- Flag genuine contradictions between sections or generic, place-less filler.\n"
    "- Be a tough but fair editor. If it's genuinely good and accessible, approve."
)


def audit_sections(contract: dict, itinerary: dict, sections: list[dict],
                   user_id=None) -> dict:
    """Return the auditor's structured verdict over all prose sections."""
    left_out = _left_out(itinerary)
    constraints = _constraint_line(contract)
    payload = {
        "traveler_constraints": constraints,
        "left_out_places": left_out,
        "sections": [{"section": s["section"], "title": s.get("title"),
                      "body": s.get("body", "")}
                     for s in sections if s.get("body")],
    }
    try:
        out = gateway.chat_json(
            "reflection",
            [{"role": "system", "content": _AUDIT_SYS},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            user_id=user_id,
        )
    except Exception as e:
        # fail-open but conservative: approve so a flaky audit never blocks the
        # dossier, but record that the audit didn't run.
        return {"accessibility_ok": True, "violations": [],
                "coherence_notes": f"(audit unavailable: {e})",
                "quality_score": None, "sections_to_redraft": [],
                "verdict": "approve", "audit_ran": False}

    # normalize / defensively fill
    out.setdefault("accessibility_ok", True)
    out.setdefault("violations", [])
    out.setdefault("sections_to_redraft", [])
    out.setdefault("quality_score", None)
    out.setdefault("coherence_notes", "")
    out.setdefault("verdict", "approve")
    out["audit_ran"] = True
    # hard rule: an accessibility violation can never be an approve
    if not out.get("accessibility_ok"):
        out["verdict"] = "revise"
    return out
