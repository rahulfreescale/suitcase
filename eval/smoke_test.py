"""End-to-end smoke test — verify every feature still works after the travel swap.

Hits the running sync /ask endpoint with representative queries for each capability
we built, and reports PASS/FAIL with a short reason. One command, whole-system check.

Prereq: API running (make run). No worker needed (uses the synchronous /ask).
    python -m eval.smoke_test
"""
from __future__ import annotations
import json, time, urllib.request, uuid

BASE = "http://localhost:8080"


def ask(query, thread_id=None, session_id=None, user_id=None):
    body = {"query": query}
    if thread_id: body["thread_id"] = thread_id
    if session_id: body["session_id"] = session_id
    if user_id: body["user_id"] = user_id
    req = urllib.request.Request(f"{BASE}/ask", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
    out["_latency"] = round(time.time() - t0, 2)
    return out


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  — {detail}" if detail else ""))
    return cond


def main():
    results = []

    # 1. Basic RAG — grounded, cited answer from a destination guide
    r = ask("What are the best neighborhoods in Lisbon?")
    ans = r.get("answer", "").lower()
    results.append(check("RAG (guide retrieval)",
        r.get("type") == "answer" and ("alfama" in ans or "baixa" in ans or "bairro" in ans),
        f"{r['_latency']}s · {len(r.get('citations',[]))} citations"))

    # 2. Text-to-SQL — returns structured rows
    r = ask("Show me hotels in Lisbon rated 4 or above under $200 a night")
    ans = r.get("answer", "").lower()
    results.append(check("Text-to-SQL (stays table)",
        r.get("type") == "answer" and "sql" in (r.get("sources_used") or []) or "lisbon" in ans,
        f"{r['_latency']}s · sources={r.get('sources_used')}"))

    # 3. Constraint SQL on the NEW enriched columns
    r = ask("Show me family-friendly, wheelchair-accessible hotels in Lisbon")
    results.append(check("Constraint SQL (enriched columns)",
        r.get("type") == "answer",
        f"{r['_latency']}s · sources={r.get('sources_used')}"))

    # 4. Clarification — a vague query should ask which city
    r = ask("What's it like there?")
    results.append(check("Clarification (vague query)",
        r.get("type") == "clarification",
        f"asked: {str(r.get('question'))[:60]}"))

    # 5. Memory / reference resolution — within one thread, 'it' -> the city
    tid = str(uuid.uuid4()); sid = "smoke-mem"
    ask("What are the best neighborhoods in Lisbon?", thread_id=tid, session_id=sid, user_id="smoke")
    r = ask("Is it walkable there?", thread_id=tid, session_id=sid, user_id="smoke")
    ans = r.get("answer", "").lower()
    results.append(check("Memory (reference resolution)",
        r.get("type") == "answer" and "lisbon" in ans,
        f"{r['_latency']}s · resolved 'there'->Lisbon: {'lisbon' in ans}"))

    # 6. Semantic cache — same question twice; second should be much faster
    q = "What are the top things to do in Tokyo?"
    r1 = ask(q); r2 = ask(q)
    results.append(check("Semantic cache (repeat hit)",
        r2["_latency"] < r1["_latency"] * 0.6 or r2["_latency"] < 1.0,
        f"first={r1['_latency']}s second={r2['_latency']}s"))

    # 7. Grounding refusal — out-of-corpus question should be declined
    r = ask("What is the visa policy for visiting Antarctica?")
    ans = r.get("answer", "").lower()
    refused = any(w in ans for w in ["couldn't find", "can't answer", "don't have",
                                     "not able", "no information", "cannot",
                                     "insufficient", "not contain", "no relevant",
                                     "not enough", "unable to answer", "only cover"])
    results.append(check("Grounding refusal (out-of-scope)",
        r.get("type") == "clarification" or refused,
        f"{r['_latency']}s"))

    # 8. Accessibility RAG — the new guide sections
    r = ask("Is Lisbon good for travelling with a toddler?")
    ans = r.get("answer", "").lower()
    results.append(check("Accessibility/family RAG (new guide section)",
        r.get("type") == "answer" and ("stroller" in ans or "family" in ans or "hill" in ans),
        f"{r['_latency']}s"))

    print("\n" + "=" * 50)
    passed = sum(1 for x in results if x)
    print(f"  {passed}/{len(results)} checks passed")
    if passed == len(results):
        print("  ✅ travel system verified end-to-end")
    else:
        print("  ⚠️  some checks failed — see above")


if __name__ == "__main__":
    main()
