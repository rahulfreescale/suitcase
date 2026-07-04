"""A worker: pull jobs from the queue, run the agent, write results back.

Run several of these (in separate terminals, or as separate containers/tasks)
to process requests concurrently:

  python -m app.worker            # start one worker
  # ...in other terminals, start more; they share the queue

Each worker opens the LangGraph graph ONCE (expensive Postgres checkpointer
setup) and reuses it across many jobs — a real efficiency win over the old
per-request setup in the synchronous API. Workers are stateless: session memory
lives in shared Redis, so any worker can serve any session.
"""
from __future__ import annotations
import os
import time
import uuid

# Tag worker traffic distinctly in Langfuse.
os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", "worker")

from app.agents.graph import graph_with_checkpointer, run_with_memory
from app.stores.jobs import dequeue, mark_running, mark_done, mark_failed
from app.stores import streaming as stream
from app.stores.interactions import log_interaction
from app.eval_utils import contexts_from_state
from app.observability import request_trace, flush as lf_flush


def _handle(graph, job: dict) -> None:
    job_id = job["job_id"]
    mark_running(job_id)
    query = job["query"]
    session_id = job.get("session_id") or None
    user_id = job.get("user_id") or None
    thread_id = job.get("thread_id") or str(uuid.uuid4())
    t0 = time.time()
    stream.publish_status(job_id, "started")
    try:
        with request_trace("ask", query, session_id=session_id, user_id=user_id,
                           tags=["worker"]):
            # Real token streaming: the writer node calls on_token(delta) for each
            # token as the LLM generates it, and we publish it immediately. Status
            # events come from on_stage as each node completes.
            final = run_with_memory(graph, query, thread_id,
                                    session_id=session_id, user_id=user_id,
                                    on_stage=lambda name: stream.publish_status(job_id, name),
                                    on_token=lambda text: stream.publish_token(job_id, text))
        dt = time.time() - t0
        if final.get("needs_clarification"):
            q = final.get("clarification_question")
            stream.publish_clarification(job_id, q)
            mark_done(job_id, {"type": "clarification",
                               "question": q,
                               "thread_id": thread_id, "latency_s": round(dt, 2)})
        else:
            answer = final.get("answer", "")
            try:
                log_interaction(thread_id, query, answer, contexts_from_state(final))
            except Exception:
                pass
            # Tokens already streamed live from the writer node during the run.
            # Just send the terminal done event (with citations) + store result.
            stream.publish_done(job_id, answer,
                                citations=final.get("citations", []),
                                latency_s=round(dt, 2))
            mark_done(job_id, {"type": "answer", "answer": answer,
                               "citations": final.get("citations", []),
                               "sources_used": final.get("sources", []),
                               "thread_id": thread_id, "latency_s": round(dt, 2)})
        print(f"[worker {WORKER_ID}] job {job_id[:8]} done in {dt:4.1f}s")
    except Exception as e:
        stream.publish_error(job_id, f"{type(e).__name__}: {e}")
        mark_failed(job_id, f"{type(e).__name__}: {e}")
        print(f"[worker {WORKER_ID}] job {job_id[:8]} FAILED: {e}")
    finally:
        lf_flush()


WORKER_ID = os.environ.get("WORKER_ID") or uuid.uuid4().hex[:4]


def main():
    print(f"[worker {WORKER_ID}] starting; waiting for jobs...")
    # Open the graph ONCE and reuse it for every job this worker handles.
    with graph_with_checkpointer() as graph:
        idle = 0
        while True:
            job = dequeue(block_timeout_s=5)
            if job is None:
                idle += 1
                if idle % 12 == 0:      # ~every minute of idle
                    print(f"[worker {WORKER_ID}] idle, still listening...")
                continue
            idle = 0
            _handle(graph, job)


if __name__ == "__main__":
    main()
