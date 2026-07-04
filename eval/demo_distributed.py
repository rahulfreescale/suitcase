"""Demo: distributed concurrency.

Fires N requests at the async API "all at once", then polls until every job
finishes. With the synchronous /ask, N requests would run one-after-another
(serialized by the single process). With the worker pool, they run in PARALLEL —
so total wall-clock time is roughly (N / num_workers) x per-request time, not
N x per-request time.

Setup (separate terminals):
  make up                              # redis + core services
  make run                             # the API on :8080
  WORKER_ID=1 python -m app.worker     # worker 1
  WORKER_ID=2 python -m app.worker     # worker 2
  WORKER_ID=3 python -m app.worker     # worker 3

Then:
  python -m eval.demo_distributed --n 9

Watch: 9 jobs, 3 workers -> they complete in ~3 waves, not 9 serial requests.
"""
from __future__ import annotations
import argparse
import time
import urllib.request
import json

QUERIES = [
    "Were piloerection and ataxia observed in study T123456-2?",
    "How many studies were done on rats?",
    "Summarize the cardiovascular effects of BAY-7 in dogs.",
    "Which studies lasted longer than 30 days?",
    "What clinical findings were seen at the high dose in study T123456-2?",
    "List all oral studies on compound BAY-1 with their doses.",
    "Did study T200110-4 show sustained cardiovascular effects?",
    "What species were used across the studies?",
    "Across all rat studies, were any neurological signs reported?",
]


def _post(base, path, body):
    req = urllib.request.Request(f"{base}{path}", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def _get(base, path):
    return json.loads(urllib.request.urlopen(f"{base}{path}").read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=9)
    ap.add_argument("--base", default="http://localhost:8080")
    args = ap.parse_args()

    queries = (QUERIES * ((args.n // len(QUERIES)) + 1))[:args.n]

    # 1) Fire all jobs as fast as possible (near-simultaneous submit).
    t0 = time.time()
    job_ids = []
    for q in queries:
        resp = _post(args.base, "/ask_async", {"query": q,
                     "session_id": "sess-load", "user_id": "user_load"})
        job_ids.append(resp["job_id"])
    submit_dt = time.time() - t0
    print(f"Submitted {len(job_ids)} jobs in {submit_dt:.2f}s "
          f"(each returned instantly). Now polling for results...\n")

    # 2) Poll until all done.
    pending = set(job_ids)
    done = {}
    while pending:
        for jid in list(pending):
            r = _get(args.base, f"/result/{jid}")
            if r.get("status") in ("done", "failed"):
                done[jid] = r
                pending.discard(jid)
                lat = r.get("latency_s", "?")
                kind = r.get("type") or r.get("status")
                print(f"  [{len(done)}/{len(job_ids)}] {jid[:8]} {kind} "
                      f"(worker latency {lat}s)")
        if pending:
            time.sleep(1)

    total_dt = time.time() - t0
    worker_time = sum(d.get("latency_s", 0) for d in done.values())
    print(f"\nAll {len(job_ids)} jobs done in {total_dt:.1f}s wall-clock.")
    print(f"Sum of per-job work = {worker_time:.1f}s. "
          f"If run serially that's what it WOULD have taken; "
          f"running concurrently it took {total_dt:.1f}s.")
    print(f"Effective parallelism ~= {worker_time/total_dt:.1f}x "
          f"(≈ your number of workers).")


if __name__ == "__main__":
    main()
