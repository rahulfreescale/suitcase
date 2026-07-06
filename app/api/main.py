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
