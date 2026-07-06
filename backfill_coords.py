"""One-time backfill: add lat/lng to existing city banks that were built without
coordinates (the lazy-build path historically didn't geocode). Run once:

    python backfill_coords.py            # fix every bank missing coords
    python backfill_coords.py Venice     # fix just one city

Idempotent: banks that already have coords are skipped. Rows that already have
lat/lng are left alone; only blanks are filled. Uses Nominatim (be gentle — it
sleeps between lookups to respect the public rate limit).
"""
import csv
import sys
import time
from pathlib import Path
from app.tools import travel_data as td

BANK_DIR = Path("data/banks")
FIELDS = ["city", "place", "is_famous", "wheelchair", "toddler", "senior",
          "confidence", "note", "source", "lat", "lng"]


def backfill_city(path: Path) -> int:
    rows = list(csv.DictReader(open(path)))
    if not rows:
        return 0
    city = rows[0].get("city", path.stem.replace("_accessibility", ""))
    filled = 0
    for r in rows:
        if (r.get("lat") or "").strip() and (r.get("lng") or "").strip():
            continue  # already has coords
        coords = td.geocode_place(r.get("place", ""), city)
        if coords:
            r["lat"], r["lng"] = coords[0], coords[1]
            filled += 1
            print(f"  {r['place']:35s} -> {coords[0]:.4f}, {coords[1]:.4f}")
        else:
            print(f"  {r['place']:35s} -> (no match)")
        time.sleep(1.1)  # Nominatim public rate limit: ~1 req/sec
    # rewrite with the full field set (adds lat/lng columns if they were absent)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return filled


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    files = sorted(BANK_DIR.glob("*_accessibility.csv"))
    for path in files:
        city = path.stem.replace("_accessibility", "").replace("_", " ")
        if only and only.lower() not in city.lower():
            continue
        header = open(path).readline()
        if "lat,lng" in header and only is None:
            # already has the columns; still may have blank rows, so only skip
            # in bulk mode if we can't tell — cheap check: does row 2 have coords?
            lines = open(path).read().splitlines()
            if len(lines) > 1 and lines[1].rstrip().endswith(tuple("0123456789")):
                print(f"SKIP {city} (already geocoded)")
                continue
        print(f"\n=== {city} ===")
        n = backfill_city(path)
        print(f"  filled {n} places")


if __name__ == "__main__":
    main()
