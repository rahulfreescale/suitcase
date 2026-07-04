"""Demo: live streaming over SSE.

Fires a question at /ask_stream and prints each event AS IT ARRIVES — status
stages first ("started", "clarify", "researcher", "writing"), then the answer in
chunks, then "done". Contrast with /ask_async + /result, which shows nothing
until the whole 2-50s request finishes.

Setup (separate terminals):
  make up
  make run                                   # API on :8080
  WORKER_ID=1 python -m app.worker           # at least one worker

Then:
  python -m eval.demo_stream "Were piloerection and ataxia seen in study T123456-2?"
"""
from __future__ import annotations
import sys
import json
import time
import urllib.request

BASE = "http://localhost:8080"


def main():
    q = sys.argv[1] if len(sys.argv) > 1 else \
        "Were piloerection and ataxia observed in study T123456-2?"
    body = json.dumps({"query": q, "session_id": "sess-stream",
                       "user_id": "user_stream"}).encode()
    req = urllib.request.Request(f"{BASE}/ask_stream", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    print(f"Q: {q}\n" + "-" * 70)
    t0 = time.time()
    answer_parts = []
    with urllib.request.urlopen(req) as resp:
        for raw in resp:                       # SSE lines arrive as produced
            line = raw.decode().strip()
            if not line or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                event = json.loads(line[5:].strip())
                dt = time.time() - t0
                et = event.get("type")
                if et == "status":
                    print(f"[{dt:5.1f}s] STAGE: {event['stage']}")
                elif et == "token":
                    answer_parts.append(event["text"])
                    sys.stdout.write(event["text"])
                    sys.stdout.flush()
                elif et == "done":
                    print(f"\n[{dt:5.1f}s] DONE (latency={event.get('latency_s')}s)")
                elif et == "error":
                    print(f"\n[{dt:5.1f}s] ERROR: {event.get('error')}")
                elif "job_id" in event:
                    print(f"[{dt:5.1f}s] job_id={event['job_id'][:8]}")
    print("-" * 70)
    print("Notice: stages appeared DURING the slow pipeline, and the answer "
          "streamed in — the user never stared at a frozen screen.")


if __name__ == "__main__":
    main()
