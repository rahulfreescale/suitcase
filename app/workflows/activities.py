"""Activities — the only place a workflow touches the outside world.

Temporal best practice: workflow code is pure orchestration and must be
deterministic, so ALL I/O (LLM calls, PDF rendering, network, email) lives in
activities. Temporal records each activity's result and retries it on failure.

These two activities are thin wrappers over code the app already has — they
replicate exactly what the synchronous /email_itinerary endpoint does, just
split into a 'build' step and a 'send' step so a human decision can sit between
them.

PDF bytes are carried as base64 because Temporal serializes activity
inputs/outputs (JSON by default); raw bytes don't round-trip cleanly.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

from temporalio import activity


@dataclass
class BuildResult:
    """What the build activity returns: the destination label + the PDF as b64."""
    destination: str
    pdf_b64: str


@activity.defn
async def build_pdf_activity(query: str, user_id: str | None) -> BuildResult:
    """Build the dossier + itinerary and render the PDF.

    Mirrors the /email_itinerary build step: build_dossier, fall back to a plain
    plan only if the dossier has no itinerary, then render_pdf.
    """
    # Imported inside the activity so the worker only pays the import cost when a
    # build actually runs, and so workflow-sandbox import rules never see these.
    from app.agents.dossier_graph import build_dossier
    from app.agents.plan_pipeline import plan_trip
    from app.services.pdf_builder import render_pdf

    dossier = build_dossier(query, user_id=user_id)
    # dossier carries itinerary + sections (prose + access_services) + meta.
    plan = {} if dossier.get("itinerary") else plan_trip(query, user_id=user_id)
    destination = (
        (dossier.get("meta") or {}).get("destination")
        or (plan.get("contract") or {}).get("destination")
        or "your trip"
    )
    pdf_bytes = render_pdf(plan, dossier)
    return BuildResult(destination=destination,
                       pdf_b64=base64.b64encode(pdf_bytes).decode("ascii"))


@activity.defn
async def send_activity(recipient: str, destination: str, pdf_b64: str) -> dict:
    """Send the built PDF to the (already-validated) recipient.

    This is the privileged action. It still goes through the least-privilege
    sender: send_itinerary_pdf can ONLY send a fixed-format itinerary PDF to one
    recipient, and it re-validates the address itself. confirm=True here means
    'the workflow has cleared the human-in-the-loop gate' — the approval signal
    already arrived before this activity is scheduled.
    """
    from app.services.email_sender import send_itinerary_pdf

    pdf_bytes = base64.b64decode(pdf_b64)
    result = send_itinerary_pdf(recipient, destination, pdf_bytes, confirm=True)
    return {"status": result.get("status"), "recipient": result.get("recipient")}
