import json, urllib.request
from app.config import get_settings

key = get_settings().ors_api_key
print("key present:", bool(key), "| length:", len(key) if key else 0)

body = json.dumps({"coordinates": [[12.4922, 41.8902], [12.4769, 41.8986]]}).encode()

# try a few URL variants to find the one that works
urls = [
    "https://api.openrouteservice.org/v2/directions/wheelchair/geojson",
    "https://api.openrouteservice.org/v2/directions/wheelchair",
    "https://api.openrouteservice.org/v2/directions/foot-walking/geojson",
]
for url in urls:
    try:
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": key,
            "Content-Type": "application/json",
            "Accept": "application/json, application/geo+json",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        # show what structure came back
        keys = list(d.keys())
        print(f"OK  {url}\n     top-level keys: {keys}")
        break
    except urllib.error.HTTPError as e:
        errbody = e.read().decode()[:300]
        print(f"{e.code} {url}\n     body: {errbody}")
    except Exception as e:
        print(f"ERR {url}: {e}")
