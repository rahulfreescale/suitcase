"""Verify token streaming is REAL (tokens arrive spread over time) vs FAKE
(all at once). Run this, then ask a specific-study question in the browser.
"""
import redis, time, json
from app.config import get_settings

r = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
ps = r.pubsub(); ps.psubscribe('stream:*')
print("listening — now ask a specific-study question in the browser...")
first = last = None; n = 0
for m in ps.listen():
    if m['type'] != 'pmessage':
        continue
    ev = json.loads(m['data'])
    now = time.time()
    if ev.get('type') == 'token':
        if first is None:
            first = now
        gap = now - last if last else 0
        last = now; n += 1
        print(f"token {n:3d}  +{gap:5.2f}s   {ev['text'][:40]!r}")
    elif ev.get('type') == 'done':
        span = (last - first) if first else 0
        print(f"--- {n} tokens spread over {span:.2f}s ---")
        print("REAL if span is several seconds & gaps vary; FAKE if span ~0.")
        break
