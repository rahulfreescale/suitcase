"""FastAPI surface for the agentic RAG system."""
import uuid
import json
from pathlib import Path
from fastapi import FastAPI, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from app.auth import current_user
from app.agents.graph import graph_with_checkpointer, run_with_memory
from app.stores.appstate_dynamo import get_trail
from app.stores.interactions import log_interaction
from app.stores.jobs import enqueue, get_result, queue_depth
from app.eval_utils import contexts_from_state
from app.observability import request_trace, flush

app = FastAPI(title="Suitcase — Agentic RAG")
_UI_PATH = Path(__file__).parent.parent / "ui" / "index.html"


class AskRequest(BaseModel):
    query: str
    thread_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None


@app.get("/", response_class=HTMLResponse)
def home():
    # Read fresh each request so UI edits appear on refresh (no server restart).
    return _UI_PATH.read_text()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config")
def config():
    # non-secret values the browser login needs; empty locally (auth off)
    from app.config import get_settings
    s = get_settings()
    return {
        "auth_enabled": s.deploy_profile == "aws" and bool(s.cognito_client_id),
        "cognito_client_id": s.cognito_client_id,
        "region": s.aws_region,
    }


@app.post("/ask")
def ask(req: AskRequest, user_id: str = Depends(current_user)):
    thread_id = req.thread_id or str(uuid.uuid4())
    # Route through run_with_memory so the synchronous path gets the SAME
    # behavior as the distributed worker: short/long-term memory, reference
    # resolution ("that city" -> the city), and the semantic cache. Calling
    # graph.invoke() directly would bypass all of that.
    with request_trace("ask", req.query):
        with graph_with_checkpointer() as graph:
            final = run_with_memory(graph, req.query, thread_id,
                                    session_id=req.session_id, user_id=user_id)
    flush()
    if final.get("needs_clarification"):
        return JSONResponse({"thread_id": thread_id, "type": "clarification",
                             "question": final.get("clarification_question")})
    answer = final.get("answer", "")
    # Record this interaction so the daily live-traffic eval can score it later.
    log_interaction(thread_id, req.query, answer, contexts_from_state(final))
    return JSONResponse({
        "thread_id": thread_id, "type": "answer",
        "answer": answer,
        "citations": final.get("citations", []),
        "sources_used": final.get("sources", []),
    })


class PlanRequest(BaseModel):
    query: str
    user_id: str | None = None


class ConverseRequest(BaseModel):
    message: str
    contract: dict | None = None
    session_id: str | None = None
    asked: int = 0
    user_id: str | None = None


@app.post("/plan_converse")
def plan_converse(req: ConverseRequest, user_id: str = Depends(current_user)):
    """One turn of the planning conversation. Returns {action: ask|plan, message,
    contract, asked, request}. When action=='plan', `request` is the self-contained
    planning string to feed /dossier_stream to build the trip."""
    from app.agents.plan_conversation import converse, build_request_from_contract
    result = converse(req.message, req.contract, session_id=req.session_id,
                      user_id=user_id, asked=req.asked)
    if result["action"] == "plan":
        result["request"] = build_request_from_contract(result["contract"])
    return JSONResponse(result)


@app.post("/plan")
def plan(req: PlanRequest, user_id: str = Depends(current_user)):
    """Constraint-faithful trip planning.

    Separate from /ask (the Q&A graph): this runs the plan_trip pipeline —
    extract a typed constraint contract -> retrieve + bank-seed activities ->
    rate each against the contract (hard facts locked) -> assemble a rated
    day-by-day itinerary plus a "skipped, and why" list. Returns the full
    structured result so the UI can render day cards, per-constraint pills,
    day-fit %, and the skipped section.
    """
    from app.agents.plan_pipeline import plan_trip
    with request_trace("plan", req.query):
        result = plan_trip(req.query, user_id=user_id)
    flush()
    return JSONResponse(result)


@app.post("/dossier")
def dossier(req: PlanRequest, user_id: str = Depends(current_user)):
    """Multi-agent professional trip dossier.

    Runs the constraint-faithful itinerary (the spine) and then a team of
    specialist agents (sense-of-place, logistics, dining, seasonal weather,
    practical prep) in parallel, an auditor that enforces quality, coherence and
    accessibility-consistency (nothing left-out may sneak back in), and a writer
    that composes the premium concierge-voice dossier. Returns the structured
    dossier object; the UI renders the Style-C view and reuses the itinerary +
    map from the same structure.
    """
    from app.agents.dossier_graph import build_dossier
    with request_trace("dossier", req.query):
        result = build_dossier(req.query, user_id=user_id)
    flush()
    return JSONResponse(result)


@app.post("/dossier_stream")
def dossier_stream(req: PlanRequest, user_id: str = Depends(current_user)):
    """Streaming version of /dossier: emits Server-Sent Events as each agent in
    the multi-agent pipeline completes, so the UI can show a live progress
    tracker, then a final event with the composed Travel Brief. Same result as
    /dossier, just streamed stage-by-stage.
    """
    from app.agents.dossier_graph import build_dossier_stream

    def event_source():
        with request_trace("dossier_stream", req.query):
            for ev in build_dossier_stream(req.query, user_id=user_id):
                yield f"data: {json.dumps(ev)}\n\n"
        flush()

    return StreamingResponse(event_source(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


class FeedbackRequest(BaseModel):
    place: str
    city: str | None = None
    constraint: str | None = "wheelchair"
    current_label: str | None = None
    proposed_label: str | None = None
    source: str = "user_explicit"   # user_explicit | user_implicit | auto_review
    direction: str = "disagree"      # agree | disagree
    note: str | None = ""


@app.post("/feedback")
def feedback(req: FeedbackRequest, user_id: str = Depends(current_user)):
    """Capture a correction on a rating — the raw signal for the feedback loop.

    Two implicit/explicit sources feed the same store: a 👍/👎 on a rated place
    (explicit), or dragging a "left out" place back into the plan (implicit
    'you were too harsh'). Corrections are recorded as PENDING and never touch
    the bank directly — a human-reviewed sync job weighs and promotes them,
    adjusting confidence. Logging never blocks the caller.
    """
    from app.stores import corrections
    item = corrections.log_correction(
        place=req.place, city=req.city or "", constraint=req.constraint or "wheelchair",
        current_label=req.current_label, proposed_label=req.proposed_label,
        source=req.source, direction=req.direction,
        note=req.note or "", user_id=user_id or "anon")
    return {"ok": True, "status": item.get("status", "pending")}
def trail(thread_id: str):
    """The transparent step-by-step trail (intermediate steps) for a request."""
    try:
        return {"thread_id": thread_id, "steps": get_trail(thread_id)}
    except Exception as e:
        return {"thread_id": thread_id, "steps": [], "note": str(e)}


# ---- Async (distributed) surface --------------------------------------------
# /ask blocks for the full 2-50s of a request, capping concurrency. The async
# pair decouples accept from execution: /ask_async enqueues and returns a
# job_id instantly; a worker pool runs the agent; the client polls /result.
@app.post("/ask_async")
def ask_async(req: AskRequest):
    """Enqueue a job and return its id immediately (non-blocking)."""
    job_id = enqueue(req.query, session_id=req.session_id,
                     user_id=req.user_id, thread_id=req.thread_id)
    return JSONResponse({"job_id": job_id, "status": "queued",
                         "queue_depth": queue_depth()})


@app.get("/result/{job_id}")
def result(job_id: str):
    """Poll a job's status/result. status: queued|running|done|failed|unknown."""
    return JSONResponse(get_result(job_id))


# ---- Experiment admin (Level-2 runtime control, no deploy) -------------------
# Start/stop A/B experiments live. The gateway reads these from Redis per request,
# so changes take effect immediately and a bad variant can be killed instantly.
class ExperimentRequest(BaseModel):
    name: str                      # e.g. "write-experiment"
    task: str                      # e.g. "write"
    variants: dict                 # {"A":{"share":50,"payload":{...}}, "B":{...}}


@app.post("/admin/experiment")
def start_experiment(req: ExperimentRequest):
    from app import experiments
    experiments.set_experiment(req.name, req.task, req.variants)
    return {"status": "started", "name": req.name,
            "experiment": experiments.get_experiment(req.name)}


@app.delete("/admin/experiment/{name}")
def kill_experiment(name: str):
    from app import experiments
    experiments.stop_experiment(name)
    return {"status": "stopped", "name": name}


@app.get("/admin/experiments")
def get_experiments():
    from app import experiments
    return {"experiments": experiments.list_experiments()}


# ---- Streaming (SSE relay over Redis pub/sub) --------------------------------
# The worker publishes live events (status stages, answer tokens) to the Redis
# channel stream:<job_id>. This endpoint enqueues the job, then SUBSCRIBES to
# that channel and relays each event to the browser as Server-Sent Events. The
# API holds the connection; the (stateless) worker just publishes — so any API
# instance can serve any job and workers never hold user connections.
@app.post("/ask_stream")
def ask_stream(req: AskRequest):
    from app.stores.streaming import subscribe
    job_id = enqueue(req.query, session_id=req.session_id,
                     user_id=req.user_id, thread_id=req.thread_id)

    def event_source():
        # Tell the client its job_id first (so it can also poll /result if the
        # connection drops).
        yield f"event: job\ndata: {json.dumps({'job_id': job_id})}\n\n"
        for event in subscribe(job_id, timeout_s=120):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---- Voice (push-to-talk) ---------------------------------------------------
# The agent is interface-agnostic: /voice/transcribe turns recorded audio into
# text (Whisper), the client feeds that text to /ask_stream like any typed query,
# and /voice/speak turns the final answer text into audio (OpenAI TTS). No change
# to the agent itself.
from fastapi import UploadFile, File, Form
from fastapi.responses import Response


@app.post("/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(...)):
    """Audio in -> transcript out."""
    from app.voice import transcribe
    data = await audio.read()
    text = transcribe(data, filename=audio.filename or "speech.webm")
    return JSONResponse({"text": text})


class SpeakRequest(BaseModel):
    text: str


@app.post("/voice/speak")
def voice_speak(req: SpeakRequest):
    """Answer text in -> spoken audio (mp3), streamed as it's generated.

    StreamingResponse forwards each chunk from OpenAI TTS the moment it arrives,
    so the client starts playing after ~1s instead of waiting for the whole file.
    """
    from app.voice import synthesize_stream
    return StreamingResponse(synthesize_stream(req.text), media_type="audio/mpeg")


@app.get("/voice/speak")
def voice_speak_get(text: str):
    """GET variant so a browser <audio> element can stream directly from a URL
    (progressive playback). Same streaming TTS as the POST endpoint."""
    from app.voice import synthesize_stream
    return StreamingResponse(synthesize_stream(text), media_type="audio/mpeg")


class EmailRequest(BaseModel):
    recipient: str                    # single validated email address
    query: str                        # the trip request, to rebuild plan + dossier
    confirm: bool = False             # human-in-the-loop: must be True to send


@app.post("/email_itinerary")
def email_itinerary(req: EmailRequest, user_id: str = Depends(current_user)):
    """Send a trip itinerary to a single recipient — a PRIVILEGED action.

    Security controls on this endpoint:
      - Human-in-the-loop: refuses unless req.confirm is True (the UI shows a
        confirmation step; Stage 2 replaces this with a Temporal approval signal).
      - Least privilege: delegates to email_sender.send_itinerary, which can ONLY
        send a fixed-format itinerary to ONE validated recipient — no general
        send-arbitrary-email capability exists.
      - Tool validation: the recipient address is validated (single, well-formed,
        no header-injection) before any send.
      - Privilege separation: this send capability lives here, isolated from the
        onboarding agent that reads untrusted web content — an injection in fetched
        content has no path to trigger a send.
    """
    from app.config import get_settings
    from app.services.email_sender import send_itinerary_pdf, EmailError, validate_recipient
    if not get_settings().enable_email:
        return JSONResponse({"ok": False, "error": "email feature disabled"}, status_code=403)

    # Validate the recipient up-front (tool-validation) so we fail fast before
    # doing the expensive dossier/PDF build for a bad address.
    try:
        validate_recipient(req.recipient)
    except EmailError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    if not req.confirm:
        # human-in-the-loop gate: don't build or send until the user confirms
        return JSONResponse({"ok": False, "needs_confirmation": True,
                             "message": f"Send this itinerary to {req.recipient}?"},
                            status_code=200)
    try:
        # Build the plan + full Travel Brief (dossier), then render the PDF.
        from app.agents.dossier_graph import build_dossier
        from app.agents.plan_pipeline import plan_trip
        from app.services.pdf_builder import render_pdf
        dossier = build_dossier(req.query, user_id=user_id)
        # dossier carries itinerary + sections (prose + access_services) + meta.
        # Fall back to a plain plan only if the dossier has no itinerary.
        plan = {} if dossier.get("itinerary") else plan_trip(req.query, user_id=user_id)
        dest = ((dossier.get("meta") or {}).get("destination")
                or (plan.get("contract") or {}).get("destination") or "your trip")
        pdf = render_pdf(plan, dossier)
        result = send_itinerary_pdf(req.recipient, dest, pdf, confirm=True)
        return {"ok": True, "status": result.get("status"), "recipient": result.get("recipient")}
    except EmailError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"could not build/send: {type(e).__name__}"},
                            status_code=500)


# ============================================================================
# EMAIL-APPROVAL WORKFLOW ENDPOINTS  — append to app/api/main.py
# ============================================================================
# Two channels through the Temporal server:
#   submit:  POST /email_workflow/start           (the Share button)
#   status:  GET  /email_workflow/{id}            (the user's page polls this)
#   list:    GET  /admin/pending                  (the admin page loads this)
#   signal:  POST /admin/email_workflow/{id}/approve  and  /reject
#
# The /admin/pending list is served from a small Redis index (app/stores/
# pending_index.py) for instant reads; the authoritative per-workflow state
# comes from Temporal's `status` query, which we merge in.
#
# Everything is wrapped so that if Temporal isn't running (server down, or the
# worker isn't up), the endpoints return a clean 503 instead of a 500 — nothing
# breaks when you're not running the Temporal path.

import uuid as _uuid
from pydantic import BaseModel


class EmailWorkflowRequest(BaseModel):
    query: str                    # the trip request, to build the plan + PDF
    recipient: str                # single validated email address


def _temporal_unavailable(detail: str):
    return JSONResponse({"ok": False, "error": f"workflow engine unavailable: {detail}"},
                        status_code=503)


@app.post("/email_workflow/start")
async def email_workflow_start(req: EmailWorkflowRequest,
                               user_id: str = Depends(current_user)):
    """Submit an email-approval workflow — the durable version of the Share action.

    Validates the recipient (fail fast), starts the workflow on Temporal (which
    builds the PDF then parks awaiting approval), records it in the pending index,
    and returns immediately with the workflow id. No waiting for the build.
    """
    from app.config import get_settings
    from app.services.email_sender import EmailError, validate_recipient
    if not get_settings().enable_email:
        return JSONResponse({"ok": False, "error": "email feature disabled"},
                            status_code=403)
    try:
        validate_recipient(req.recipient)
    except EmailError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    workflow_id = "email-" + _uuid.uuid4().hex[:12]
    try:
        from app.workflows.common import TASK_QUEUE, get_client
        from app.workflows.email_approval import (EmailApprovalWorkflow,
                                                  EmailApprovalInput)
        client = await get_client()
        await client.start_workflow(
            EmailApprovalWorkflow.run,
            EmailApprovalInput(query=req.query, recipient=req.recipient,
                               user_id=user_id),
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    except Exception as e:  # noqa: BLE001
        return _temporal_unavailable(type(e).__name__)

    # record in the fast index so the admin page can list it
    try:
        from app.stores.pending_index import add_pending
        add_pending(workflow_id, req.recipient, req.query)
    except Exception:  # noqa: BLE001
        pass  # index is best-effort; the workflow still exists in Temporal

    return {"ok": True, "workflow_id": workflow_id, "state": "pending_approval"}


@app.get("/email_workflow/{workflow_id}")
async def email_workflow_status(workflow_id: str,
                                user_id: str = Depends(current_user)):
    """Live status of one workflow (building / pending_approval / sent / ...)."""
    try:
        from app.workflows.common import get_client
        from app.workflows.email_approval import EmailApprovalWorkflow
        client = await get_client()
        handle = client.get_workflow_handle(workflow_id)
        status = await handle.query(EmailApprovalWorkflow.status)
    except Exception as e:  # noqa: BLE001
        return _temporal_unavailable(type(e).__name__)
    return {"ok": True, "workflow_id": workflow_id, **status}


@app.get("/admin/pending")
async def admin_pending(user_id: str = Depends(current_user)):
    """List workflows awaiting admin action, enriched with live Temporal state.

    Reads the Redis index for the in-flight ids (instant), then queries each
    workflow's `status` for the authoritative state. Terminal-state workflows
    are pruned from the index as we notice them, so the list stays clean.
    """
    from app.stores.pending_index import list_pending, remove_pending
    records = list_pending()
    if not records:
        return {"ok": True, "pending": []}

    try:
        from app.workflows.common import get_client
        from app.workflows.email_approval import EmailApprovalWorkflow
        client = await get_client()
    except Exception as e:  # noqa: BLE001
        # Temporal down — return the raw index without live state rather than 503,
        # so the admin page still shows *something*.
        return {"ok": True, "pending": records, "note": "live state unavailable"}

    TERMINAL = {"sent", "rejected", "expired", "send_failed"}
    out = []
    for rec in records:
        wid = rec.get("workflow_id")
        state = "unknown"
        destination = ""
        try:
            handle = client.get_workflow_handle(wid)
            st = await handle.query(EmailApprovalWorkflow.status)
            state = st.get("state", "unknown")
            destination = st.get("destination", "")
        except Exception:  # noqa: BLE001
            state = "unknown"
        if state in TERMINAL:
            remove_pending(wid)      # prune finished ones
            continue
        out.append({**rec, "state": state, "destination": destination})
    return {"ok": True, "pending": out}


@app.post("/admin/email_workflow/{workflow_id}/approve")
async def admin_approve(workflow_id: str, user_id: str = Depends(current_user)):
    """Send the approve signal to a parked workflow — it wakes and sends."""
    return await _signal_workflow(workflow_id, "approve")


@app.post("/admin/email_workflow/{workflow_id}/reject")
async def admin_reject(workflow_id: str, user_id: str = Depends(current_user)):
    """Send the reject signal to a parked workflow — it exits without sending."""
    return await _signal_workflow(workflow_id, "reject")


async def _signal_workflow(workflow_id: str, which: str):
    try:
        from app.workflows.common import get_client
        from app.workflows.email_approval import EmailApprovalWorkflow
        client = await get_client()
        handle = client.get_workflow_handle(workflow_id)
        sig = (EmailApprovalWorkflow.approve if which == "approve"
               else EmailApprovalWorkflow.reject)
        await handle.signal(sig)
    except Exception as e:  # noqa: BLE001
        return _temporal_unavailable(type(e).__name__)
    # remove from the pending index (best-effort; admin_pending also prunes)
    try:
        from app.stores.pending_index import remove_pending
        remove_pending(workflow_id)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "workflow_id": workflow_id, "signal": which}

