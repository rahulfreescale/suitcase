#!/usr/bin/env python3
"""Geocode every place in the accessibility banks and cache lat/lng in the CSV.

WHY: the itinerary map needs a coordinate per place. Place *names* live in the
banks; this fills in `lat`/`lng` columns so the plan can render pins and order
each day's stops geographically. It's a ONE-TIME enrichment run at ingest, not a
live dependency - once a place has coordinates they're cached in the CSV forever.

DESIGN:
  - Uses Nominatim (OpenStreetMap), free, no API key. Queried as "{place}, {city}"
    so "Colosseum" resolves to the Rome one, not some other Colosseum.
  - Polite: 1 request/second (Nominatim's usage policy), a real User-Agent.
  - Idempotent: rows that already have lat/lng are skipped, so re-running only
    fills gaps (new cities, previously-failed lookups).
  - Safe: a failed lookup leaves lat/lng blank (the map just omits that pin) and
    is retried on the next run; it never crashes the batch.

RUN:  python -m scripts.geocode_banks              (all banks)
      python -m scripts.geocode_banks Rome Paris    (specific cities)
      python -m scripts.geocode_banks --force       (re-geocode even filled rows)

Requires network access (this talks to nominatim.openstreetmap.org).
"""
from __future__ import annotations
import csv
import sys
import time
import json
import urllib.parse
import urllib.request
from pathlib import Path

BANK_DIR = Path("data/banks")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "suitcase-trip-planner/1.0 (portfolio project; geocode-once)"
FIELDNAMES = ["city", "place", "is_famous", "wheelchair", "toddler",
              "senior", "confidence", "note", "source", "lat", "lng"]
RATE_SECONDS = 1.1   # be a good citizen; Nominatim asks for <= 1 req/sec


def _clean_place(name: str) -> str:
    """Strip parentheticals / qualifiers / compound joins so the geocoder gets a
    single base landmark. 'Roman Forum & Palatine Hill' -> 'Roman Forum',
    'Vatican Museums & Sistine Chapel' -> 'Vatican Museums',
    'Roman Forum (accessible path)' -> 'Roman Forum'."""
    n = (name or "").split("(")[0]
    for sep in (" & ", " + ", " and ", " - ", " \u2013 ", " / ", ","):
        n = n.split(sep)[0]
    return n.strip()


def _query(q: str) -> tuple[float, float] | None:
    """One Nominatim call. Returns (lat, lng) as floats, or None. Never raises."""
    params = urllib.parse.urlencode({"q": q, "format": "json", "limit": 1})
    req = urllib.request.Request(f"{NOMINATIM}?{params}",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data:
            return (float(data[0]["lat"]), float(data[0]["lon"]))
    except Exception as e:
        print(f"    ! geocode error for '{q}': {e}")
    return None


def _km_apart(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lng) points."""
    import math
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# Max distance a place can be from the city center before we treat the geocode as
# a wrong-match. Generous (150km) so legit day-trips still pass - Versailles from
# Paris (~20km), Machu Picchu from Cusco (~75km), Cape Point from Cape Town (~60km)
# - while the bare-name fallback grabbing a same-named place on another continent
# (Plaka -> New York, Big Buddha -> Paris) is rejected.
_MAX_KM_FROM_CITY = 150.0


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _safe_km(row: dict, center: tuple[float, float]) -> float:
    """Distance of a row's stored coords from center, or 0 if unparseable."""
    try:
        return _km_apart((float(row["lat"]), float(row["lng"])), center)
    except (ValueError, KeyError, TypeError):
        return 0.0


def _center_from_rows(rows: list[dict]) -> tuple[float, float] | None:
    """Derive the city center from the MEDIAN of already-geocoded place coords.

    This is deliberately NOT a geocode of the city name - 'Athens'/'Santa Cruz'
    alone hit the same same-name trap we're guarding against (Athens GA, Santa
    Cruz Bolivia), which would then flag every correct place as off-city. The
    median of the places themselves is trap-proof and needs no extra lookup;
    the median (not mean) shrugs off a few outlier wrong-matches mixed in.
    """
    lats, lngs = [], []
    for r in rows:
        lat, lng = (r.get("lat") or "").strip(), (r.get("lng") or "").strip()
        if lat and lng:
            try:
                lats.append(float(lat)); lngs.append(float(lng))
            except ValueError:
                pass
    if len(lats) < 3:            # too few points to trust a center
        return None
    return (_median(lats), _median(lngs))


# A few famous places whose bank name differs from the name OSM indexes them
# under (English vs local, or a common alternate). Only needed for the stubborn
# ones the generic variants miss; keyed by a lowercase substring of the bank name.
_ALIASES = {
    "uffizi": ["Galleria degli Uffizi"],
    "big buddha": ["Tian Tan Buddha"],
    "citadel of saladin": ["Cairo Citadel", "Saladin Citadel"],
    "stiklal": ["Istiklal Avenue, Istanbul"],
    "national museum of anthropology": ["Museo Nacional de Antropologia"],
    "san blas": ["San Blas, Cusco"],
    "old port waterfront": ["Old Port of Montreal"],
    "han river parks": ["Yeouido Hangang Park"],
    "french quarter": ["Hanoi Opera House"],
    "crystal palace gardens": ["Jardins do Palacio de Cristal"],
    "melbourne cbd laneways": ["Hosier Lane"],
    "griffith park hikes": ["Griffith Park"],
    "south bank riverside": ["South Bank, London"],
    "seine river quays": ["Quai de la Tournelle"],
    "akshardham temple": ["Swaminarayan Akshardham, Delhi"],
    "raohe street": ["Raohe Street Night Market, Taipei"],
    "marina bay sands skypark": ["Marina Bay Sands"],
    "ferry building marketplace": ["Ferry Building, San Francisco"],
    "wadi musa": ["Wadi Musa"],
    "monastery (ad deir": ["Ad Deir, Petra"],
}


def _variants(place: str) -> list[str]:
    """Candidate names to try, best-first, to catch English/local name mismatches.

    'Big Buddha (Tian Tan) & Ngong Ping' -> ['Big Buddha', 'Tian Tan', ...] - the
    parenthetical often holds the more-geocodable local name. Plus curated aliases
    for stubborn famous landmarks.
    """
    out = []
    base = _clean_place(place)
    if base:
        out.append(base)
    # content inside parentheses, e.g. "(Tian Tan)" -> "Tian Tan", split on & too
    import re
    for m in re.findall(r"\(([^)]*)\)", place or ""):
        for part in re.split(r"\s*&\s*|\s*/\s*", m):
            part = part.strip()
            if part and part.lower() not in ("david", "guernica") and len(part) > 3:
                out.append(part)
    # curated aliases
    low = (place or "").lower()
    for key, aliases in _ALIASES.items():
        if key in low:
            out.extend(aliases)
    # de-dupe preserving order
    seen, uniq = set(), []
    for q in out:
        if q.lower() not in seen:
            seen.add(q.lower()); uniq.append(q)
    return uniq


def geocode(place: str, city: str,
            center: tuple[float, float] | None = None) -> tuple[str, str] | None:
    """Return (lat, lng) as strings, or None on miss/error. Never raises.

    Tries multiple name variants (base name, parenthetical content, curated
    aliases), each first city-scoped then bare, validating every candidate against
    the city center: a result more than ~150km away is rejected as a wrong match,
    so we never place a Cairo mosque in Syria. Blank-but-correct beats wrong.
    """
    def _accept(hit):
        if not hit:
            return None
        if center and _km_apart(hit, center) > _MAX_KM_FROM_CITY:
            return None
        return (str(hit[0]), str(hit[1]))

    first = True
    for variant in _variants(place):
        if not first:
            time.sleep(RATE_SECONDS)
        first = False
        hit = _accept(_query(f"{variant}, {city}"))
        if hit:
            return hit
        time.sleep(RATE_SECONDS)
        hit = _accept(_query(variant))
        if hit:
            return hit
    return None


def _bank_files(cities: list[str]) -> list[Path]:
    if cities:
        out = []
        for c in cities:
            p = BANK_DIR / f"{c.replace(' ', '_')}_accessibility.csv"
            (out.append(p) if p.exists()
             else print(f"  (no bank for '{c}' at {p})"))
        return out
    return sorted(BANK_DIR.glob("*_accessibility.csv"))


def process(path: Path, force: bool, revalidate: bool = False) -> tuple[int, int, int]:
    """Geocode one bank file in place. Returns (filled, skipped, failed).

    revalidate: check rows that ALREADY have coords against the city center and
    blank out any that are implausibly far (wrong matches written by an earlier
    run), so this run re-geocodes them correctly.
    """
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return (0, 0, 0)

    city = rows[0].get("city") or path.stem.replace("_accessibility", "").replace("_", " ")

    if revalidate:
        # Center = median of the currently-stored coords. Robust even with a few
        # bad rows mixed in (median ignores outliers). Only clear a row if the
        # MAJORITY agree on a center AND this row is a far outlier from it - so we
        # never nuke a whole city because the center lookup itself went wrong.
        center = _center_from_rows(rows)
        if center:
            outliers = [r for r in rows
                        if (r.get("lat") or "").strip() and (r.get("lng") or "").strip()
                        and _safe_km(r, center) > _MAX_KM_FROM_CITY]
            have = sum(1 for r in rows if (r.get("lat") or "").strip())
            # Safety valve: if "most" rows look off, the center is probably the
            # thing that's wrong, not the rows - so don't clear anything.
            if have and len(outliers) <= max(1, have // 2):
                for r in outliers:
                    print(f"    ~ {r['place']:<34} {r['lat']},{r['lng']} is off-city -> clearing")
                    r["lat"], r["lng"] = "", ""
            elif outliers:
                print(f"    (skipping revalidation for {city}: {len(outliers)}/{have} "
                      f"look off - center is likely unreliable, leaving as-is)")

    center = _center_from_rows(rows)   # (re)compute after any clearing

    filled = skipped = failed = 0
    for r in rows:
        has_coords = (r.get("lat") or "").strip() and (r.get("lng") or "").strip()
        if has_coords and not force:
            skipped += 1
            continue
        coords = geocode(r.get("place", ""), r.get("city", ""), center=center)
        if coords:
            r["lat"], r["lng"] = coords
            filled += 1
            print(f"    \u2713 {r['place']:<34} {coords[0]:>9}, {coords[1]}")
        else:
            r.setdefault("lat", ""); r.setdefault("lng", "")
            failed += 1
            print(f"    \u2717 {r['place']:<34} (no result - will retry next run)")
        time.sleep(RATE_SECONDS)

    # rewrite with the (possibly new) lat/lng columns, preserving column order
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r.setdefault("lat", ""); r.setdefault("lng", "")
            w.writerow(r)
    return (filled, skipped, failed)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    revalidate = "--revalidate" in sys.argv
    files = _bank_files(args)
    if not files:
        print("No bank files found.")
        return 1

    mode = " (FORCE)" if force else " (REVALIDATE)" if revalidate else ""
    print(f"Geocoding {len(files)} bank file(s){mode}...")
    tot_filled = tot_skipped = tot_failed = 0
    for path in files:
        city = path.stem.replace("_accessibility", "").replace("_", " ")
        print(f"\n\u25b6 {city}")
        f_, s_, x_ = process(path, force, revalidate)
        tot_filled += f_; tot_skipped += s_; tot_failed += x_
        print(f"  {f_} filled, {s_} already had coords, {x_} failed")

    print("\n" + "=" * 50)
    print(f"DONE: {tot_filled} geocoded, {tot_skipped} skipped, {tot_failed} failed")
    if tot_failed:
        print(f"  {tot_failed} places had no result - re-run to retry just those.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
