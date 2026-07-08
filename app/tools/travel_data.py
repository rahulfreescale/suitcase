"""External travel-data tools the dossier specialists call.

Three providers, all chosen to match the project's open-data, no-billing stance
(same spirit as using Nominatim for geocoding):

  * Open-Meteo   - weather. No API key. Gives BOTH a live forecast (when the trip
                   is within range) and seasonal norms from the historical archive
                   (for trips further out). Takes lat/lng directly - plugs into the
                   coordinates we already geocoded.
  * Open-Meteo air-quality - PM2.5/PM10 etc. Same provider, no key. Relevant to
                   travelers with respiratory sensitivity and "good day to be
                   outside?" judgments.
  * OpenRouteService - wheelchair-profile walking/wheeling routes + real distances
                   between two coordinates. Free tier, needs a key (ORS_API_KEY).
                   Honest scope: curb/surface-aware routing, NOT live transit
                   schedules (no free global accessible-transit data exists).

Design rules (same as the geocoder):
  - Every function is defensive: on any error it returns a structured "unavailable"
    result and NEVER raises into the agent / request path.
  - Network calls are short-timeout and cached in-process to avoid hammering.
  - Tools return STRUCTURED data; the specialist LLM turns it into prose. The tool
    provides the facts; the model provides the voice.
"""
from __future__ import annotations
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from functools import lru_cache
from datetime import date

from app.config import get_settings

settings = get_settings()

_UA = "suitcase-trip-planner/1.0 (portfolio; open-data)"
_TIMEOUT = 12


def _get_json(url: str, params: dict, headers: dict | None = None):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": _UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, body: dict, headers: dict):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": _UA, "Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------
# GEOCODING  (OpenStreetMap Nominatim, no key — needs a User-Agent)
# --------------------------------------------------------------------------
@lru_cache(maxsize=512)
def geocode_place(name: str, city: str = "", country: str = "") -> tuple | None:
    """Resolve a place name to (lat, lng) via Nominatim. Cached. Returns None on
    miss. Used to backfill coordinates for lazily-built city banks so weather,
    air-quality and the map work for cities that weren't hand-geocoded."""
    if not name:
        return None
    q = ", ".join(p for p in (name, city, country) if p)
    try:
        data = _get_json("https://nominatim.openstreetmap.org/search",
                         {"q": q, "format": "json", "limit": 1})
        if isinstance(data, list) and data:
            return (float(data[0]["lat"]), float(data[0]["lon"]))
    except Exception:
        pass
    # retry once without the place name qualifiers if the full query missed
    if city and name != city:
        try:
            data = _get_json("https://nominatim.openstreetmap.org/search",
                             {"q": f"{name}, {city}", "format": "json", "limit": 1})
            if isinstance(data, list) and data:
                return (float(data[0]["lat"]), float(data[0]["lon"]))
        except Exception:
            pass
    return None


@lru_cache(maxsize=256)
def geocode_city_country(city: str) -> str | None:
    """Resolve a city name to its ISO country code (e.g. 'Venice' -> 'IT') via
    Nominatim. Cached. Returns an uppercase 2-letter code, or None on miss.
    Lets country-dependent info (emergency numbers, etc.) work for lazily-built
    cities that aren't in the hardcoded city->country table."""
    if not city:
        return None
    try:
        data = _get_json("https://nominatim.openstreetmap.org/search",
                         {"q": city, "format": "json", "limit": 1,
                          "addressdetails": 1})
        if isinstance(data, list) and data:
            cc = (data[0].get("address", {}) or {}).get("country_code")
            if cc:
                return cc.upper()
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# WEATHER  (Open-Meteo, no key)
# --------------------------------------------------------------------------

# WMO weather-code -> short human label (the codes Open-Meteo returns)
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorm",
    96: "thunderstorm w/ hail", 99: "severe thunderstorm",
}


@lru_cache(maxsize=512)
def _weather_full_year(lat: float, lng: float) -> dict:
    """Seasonal range across the whole year (for trips with no dates given):
    samples winter vs summer so the traveler sees the spread, not one month.
    """
    try:
        yr = date.today().year
        data = _get_json("https://archive-api.open-meteo.com/v1/archive", {
            "latitude": lat, "longitude": lng,
            "start_date": date(yr - 2, 1, 1).isoformat(),
            "end_date": date(yr - 1, 12, 31).isoformat(),
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "auto",
        })
        d = data.get("daily", {})
        times = d.get("time", [])
        his = d.get("temperature_2m_max", [])
        los = d.get("temperature_2m_min", [])
        if not times:
            return {"kind": "unavailable", "reason": "no archive data"}
        # bucket by month, then find warmest & coolest months
        by_month = {}
        for t, hi, lo in zip(times, his, los):
            if hi is None or lo is None:
                continue
            m = int(t[5:7])
            by_month.setdefault(m, []).append((hi, lo))
        if not by_month:
            return {"kind": "unavailable", "reason": "sparse archive"}
        month_avg = {m: (sum(h for h, _ in v) / len(v), sum(l for _, l in v) / len(v))
                     for m, v in by_month.items()}
        warm_m = max(month_avg, key=lambda m: month_avg[m][0])
        cool_m = min(month_avg, key=lambda m: month_avg[m][1])
        import calendar
        warm_hi = round(month_avg[warm_m][0]); cool_lo = round(month_avg[cool_m][1])
        summary = (f"Across the year, {calendar.month_name[cool_m]} is coldest "
                   f"(lows near {cool_lo}\u00b0C) and {calendar.month_name[warm_m]} "
                   f"warmest (highs near {warm_hi}\u00b0C). Tell me your travel dates "
                   f"for a month-specific outlook. (Seasonal averages, not a forecast.)")
        return {"kind": "seasonal_year", "source": "Open-Meteo (2-yr archive)",
                "summary": summary,
                "detail": {"warm_month": calendar.month_name[warm_m], "warm_hi_c": warm_hi,
                           "cool_month": calendar.month_name[cool_m], "cool_lo_c": cool_lo}}
    except Exception as e:
        return {"kind": "unavailable", "reason": str(e)}


@lru_cache(maxsize=256)
def weather(lat: float, lng: float, start: str | None = None,
            end: str | None = None) -> dict:
    """Weather for a place. If `start` is within ~14 days, returns a LIVE forecast;
    otherwise returns SEASONAL norms from the historical archive for that month.

    Returns: {kind, source, summary, detail} or {kind:"unavailable", ...}.
    Dates are ISO 'YYYY-MM-DD' strings; if start is None, uses seasonal-for-today.
    """
    try:
        today = date.today()
        has_date = bool(start)
        target = date.fromisoformat(start) if start else today
        days_out = (target - today).days

        # No trip date given -> full-year seasonal range (not misleading current-month)
        if not has_date:
            return _weather_full_year(lat, lng)

        if 0 <= days_out <= 14:
            # live forecast window
            data = _get_json("https://api.open-meteo.com/v1/forecast", {
                "latitude": lat, "longitude": lng,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode",
                "timezone": "auto",
                "start_date": target.isoformat(),
                "end_date": (end or target.isoformat()),
            })
            d = data.get("daily", {})
            if not d.get("time"):
                return {"kind": "unavailable", "reason": "no forecast data"}
            hi = d["temperature_2m_max"][0]; lo = d["temperature_2m_min"][0]
            code = d.get("weathercode", [0])[0]
            rain = d.get("precipitation_probability_max", [None])[0]
            cond = _WMO.get(code, "mixed")
            summary = (f"Live forecast: {cond}, around {round(lo)}-{round(hi)}\u00b0C"
                       + (f", {rain}% chance of rain" if rain is not None else "") + ".")
            return {"kind": "forecast", "source": "Open-Meteo (live)",
                    "summary": summary, "detail": {"hi_c": hi, "lo_c": lo,
                    "condition": cond, "rain_pct": rain}}

        # seasonal norms: pull the same calendar month across the last 5 years
        yr = today.year
        month = target.month
        # a representative mid-month window
        s = date(yr - 5, month, 1).isoformat()
        e = date(yr - 1, month, 28).isoformat()
        data = _get_json("https://archive-api.open-meteo.com/v1/archive", {
            "latitude": lat, "longitude": lng,
            "start_date": s, "end_date": e,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "timezone": "auto",
        })
        d = data.get("daily", {})
        if not d.get("time"):
            return {"kind": "unavailable", "reason": "no archive data"}
        # average the highs/lows across all sampled days in that month
        his = [x for x in d.get("temperature_2m_max", []) if x is not None]
        los = [x for x in d.get("temperature_2m_min", []) if x is not None]
        prc = [x for x in d.get("precipitation_sum", []) if x is not None]
        if not his or not los:
            return {"kind": "unavailable", "reason": "sparse archive"}
        avg_hi = round(sum(his) / len(his)); avg_lo = round(sum(los) / len(los))
        wet_days = sum(1 for x in prc if x and x > 1.0)
        wet_frac = round(100 * wet_days / len(prc)) if prc else None
        month_name = target.strftime("%B")
        summary = (f"Typical {month_name}: highs near {avg_hi}\u00b0C, lows near "
                   f"{avg_lo}\u00b0C"
                   + (f", rain on roughly {wet_frac}% of days" if wet_frac is not None else "")
                   + " (seasonal average, not a live forecast).")
        return {"kind": "seasonal", "source": "Open-Meteo (5-yr archive)",
                "summary": summary, "detail": {"avg_hi_c": avg_hi, "avg_lo_c": avg_lo,
                "wet_day_pct": wet_frac, "month": month_name}}
    except Exception as e:
        return {"kind": "unavailable", "reason": str(e)}


# --------------------------------------------------------------------------
# AIR QUALITY  (Open-Meteo, no key)
# --------------------------------------------------------------------------

@lru_cache(maxsize=256)
def air_quality(lat: float, lng: float) -> dict:
    """Current air quality. Returns {kind, summary, detail} or unavailable.
    Relevant for travelers with respiratory sensitivity and outdoor-day judgments.
    """
    try:
        data = _get_json("https://air-quality-api.open-meteo.com/v1/air-quality", {
            "latitude": lat, "longitude": lng,
            "current": "pm2_5,pm10,european_aqi",
        })
        cur = data.get("current", {})
        aqi = cur.get("european_aqi")
        pm25 = cur.get("pm2_5")
        if aqi is None and pm25 is None:
            return {"kind": "unavailable", "reason": "no AQ data"}
        # crude band for a human note
        band = ("good" if (aqi or 0) <= 40 else "moderate" if (aqi or 0) <= 80
                else "poor")
        summary = (f"Air quality is currently {band}"
                   + (f" (EAQI {round(aqi)})" if aqi is not None else "")
                   + (f", PM2.5 {round(pm25)} \u00b5g/m\u00b3" if pm25 is not None else "") + ".")
        return {"kind": "air_quality", "source": "Open-Meteo",
                "summary": summary, "detail": {"eaqi": aqi, "pm2_5": pm25, "band": band}}
    except Exception as e:
        return {"kind": "unavailable", "reason": str(e)}


# --------------------------------------------------------------------------
# ROUTING  (OpenRouteService, wheelchair profile) - needs ORS_API_KEY
# --------------------------------------------------------------------------

def _ors_key() -> str | None:
    return getattr(settings, "ors_api_key", None) or None


@lru_cache(maxsize=256)
def route_leg(from_lat: float, from_lng: float, to_lat: float, to_lng: float,
              wheelchair: bool = True) -> dict:
    """Distance + duration for one leg. Uses the ORS wheelchair profile when the
    traveler needs step-free routing (curb/surface aware), else foot-walking.

    Returns {kind, summary, detail} or unavailable. Honest scope: this is
    walking/wheeling routing + distance, NOT live transit schedules.
    """
    key = _ors_key()
    if not key:
        return {"kind": "unavailable", "reason": "no ORS key configured"}

    def _try(profile: str):
        data = _post_json(
            f"https://api.openrouteservice.org/v2/directions/{profile}/geojson",
            {"coordinates": [[from_lng, from_lat], [to_lng, to_lat]]},
            headers={"Authorization": key, "Content-Type": "application/json",
                     "Accept": "application/json, application/geo+json"},
        )
        feat = (data.get("features") or [{}])[0]
        return ((feat.get("properties") or {}).get("summary") or {})

    try:
        want_wheelchair = bool(wheelchair)
        used_fallback = False
        try:
            seg = _try("wheelchair" if want_wheelchair else "foot-walking")
        except urllib.error.HTTPError as he:
            # ORS 2009 = no route on this profile's graph; fall back to foot-walking
            if want_wheelchair and he.code in (404, 500):
                seg = _try("foot-walking")
                used_fallback = True
            else:
                raise
        dist_m = seg.get("distance"); dur_s = seg.get("duration")
        if dist_m is None:
            return {"kind": "unavailable", "reason": "no route"}
        km = round(dist_m / 1000, 1); mins = round((dur_s or 0) / 60)
        mode = "wheeling" if (want_wheelchair and not used_fallback) else "walking"

        # graded distance guidance — what to actually DO, tuned to the traveler.
        # wheelchair/stroller/senior tire sooner, so their thresholds are lower.
        if want_wheelchair:
            near_max, mid_max = 1.0, 2.0
        else:
            near_max, mid_max = 1.5, 3.0
        if km <= near_max:
            advice = ""                                   # comfortable on foot/wheels
            band = "near"
        elif km <= mid_max:
            advice = (" \u2014 doable but a fair distance; consider a step-free bus/tram "
                      "for part of it if energy is a factor.")
            band = "mid"
        else:
            advice = (" \u2014 too far to " + ("wheel" if want_wheelchair else "walk") +
                      " comfortably; take an accessible taxi or step-free public "
                      "transit for this leg rather than covering it on foot.")
            band = "far"
        long_leg = band == "far"

        summary = (f"{km} km ({mins} min {mode})" + advice
                   + (" (Walking route shown; verify step-free access, as the "
                      "wheelchair network had a gap here.)" if used_fallback else ""))
        return {"kind": "route", "source": "OpenRouteService",
                "summary": summary, "detail": {"km": km, "minutes": mins,
                "profile": "foot-walking" if used_fallback else
                           ("wheelchair" if want_wheelchair else "foot-walking"),
                "fallback": used_fallback, "long_leg": long_leg, "band": band}}
    except Exception as e:
        return {"kind": "unavailable", "reason": str(e)}


def route_day(stops: list, wheelchair: bool = True) -> dict:
    """Route consecutive stops for a day. `stops` = [{name,lat,lng}, ...] in order.
    Returns {kind, legs:[{from,to,summary,detail}], total} or unavailable-ish.
    Skips gracefully over stops that lack coordinates.
    """
    pts = [s for s in stops if s.get("lat") is not None and s.get("lng") is not None]
    if len(pts) < 2:
        return {"kind": "route_day", "legs": [], "note": "not enough located stops"}
    legs = []
    total_km = 0.0
    for a, b in zip(pts, pts[1:]):
        leg = route_leg(a["lat"], a["lng"], b["lat"], b["lng"], wheelchair=wheelchair)
        legs.append({"from": a.get("name"), "to": b.get("name"),
                     "summary": leg.get("summary"), "detail": leg.get("detail")})
        if leg.get("detail", {}).get("km"):
            total_km += leg["detail"]["km"]
        time.sleep(0.2)  # be gentle on the free tier
    return {"kind": "route_day", "source": "OpenRouteService",
            "legs": legs, "total_km": round(total_km, 1)}


# --------------------------------------------------------------------------
# ACCESSIBLE PLACES  (OpenStreetMap via Overpass API, no key)
# --------------------------------------------------------------------------

# Overpass endpoints (fall back if one is busy). All free, no key.
_OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# what OSM 'amenity' values map to each kind we care about
_KIND_TAGS = {
    "restaurant": ["restaurant"],
    "cafe": ["cafe"],
    "food": ["restaurant", "cafe", "fast_food"],
    "bar": ["bar", "pub"],
}


# Each traveler constraint maps to (a) the OSM access filter that must hold, and
# (b) the extra tags worth returning. All parallel the wheelchair schema.
#   wheelchair -> wheelchair=yes|limited            (+ accessible toilet)
#   stroller   -> stroller=yes|limited|designated   (a stroller can get in)
#   toddler    -> changing_table / highchair present (baby-friendly venue)
#   senior     -> (soft) prefers places with seating / step-free; no single tag,
#                 so we surface bench + drinking_water via a separate rest-stop query
_CONSTRAINT_FILTER = {
    "wheelchair": {"key": "wheelchair", "values": "yes|limited",
                   "extra": ["toilets:wheelchair"]},
    "stroller":   {"key": "stroller", "values": "yes|limited|designated",
                   "extra": ["changing_table"]},
    "toddler":    {"key": "changing_table", "values": "yes",
                   "extra": ["highchair", "changing_table"]},
}


def _overpass(ql: str):
    for ep in _OVERPASS:
        try:
            body = urllib.parse.urlencode({"data": ql}).encode("utf-8")
            req = urllib.request.Request(ep, data=body,
                                         headers={"User-Agent": _UA,
                                                  "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=18) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue
    return None


@lru_cache(maxsize=256)
def accessible_places(lat: float, lng: float, kind: str = "food",
                      constraint: str = "wheelchair",
                      radius_m: int = 900, limit: int = 8) -> dict:
    """Real nearby places from OpenStreetMap, filtered for the traveler's CONSTRAINT.

    Constraint-aware: `constraint` picks which OSM access tag must hold -
    wheelchair (wheelchair=yes|limited), stroller (stroller=yes|limited|designated),
    or toddler (changing_table present). The returned records carry the tags
    relevant to that constraint, so the dining/family prose is grounded in the
    RIGHT data - a stroller trip gets stroller-navigable venues with changing
    tables, not just wheelchair ones.

    Returns {kind, constraint, places:[...], note} or honest 'sparse'/'unavailable'.
    OSM access tags are crowd-sourced - rich in some cities, thin in others - and
    we report that rather than pretend. No API key.
    """
    amenities = _KIND_TAGS.get(kind, _KIND_TAGS["food"])
    amenity_re = "|".join(amenities)
    filt = _CONSTRAINT_FILTER.get(constraint, _CONSTRAINT_FILTER["wheelchair"])
    key, values, extra = filt["key"], filt["values"], filt["extra"]

    ql = f"""
    [out:json][timeout:15];
    (
      node["amenity"~"^({amenity_re})$"]["{key}"~"^({values})$"](around:{radius_m},{lat},{lng});
      way["amenity"~"^({amenity_re})$"]["{key}"~"^({values})$"](around:{radius_m},{lat},{lng});
    );
    out center {limit * 3};
    """
    data = _overpass(ql)
    if data is None:
        return {"kind": "unavailable", "constraint": constraint,
                "reason": "overpass unreachable"}

    places = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        # build a human street address from OSM addr:* tags where present
        street = tags.get("addr:street")
        hn = tags.get("addr:housenumber")
        suburb = tags.get("addr:suburb") or tags.get("addr:city")
        addr_parts = []
        if street:
            addr_parts.append(f"{street} {hn}".strip() if hn else street)
        if suburb:
            addr_parts.append(suburb)
        address = ", ".join(addr_parts) if addr_parts else None
        rec = {"name": name, "cuisine": tags.get("cuisine"),
               "amenity": tags.get("amenity"),
               "opening_hours": tags.get("opening_hours"),
               "address": address,
               "access": tags.get(key)}
        for ex in extra:
            if tags.get(ex):
                rec[ex.replace(":", "_")] = tags.get(ex)
        places.append(rec)
        if len(places) >= limit:
            break

    label = {"wheelchair": "wheelchair-accessible", "stroller": "stroller-friendly",
             "toddler": "baby-friendly (changing table)"}.get(constraint, constraint)
    if not places:
        return {"kind": "sparse", "constraint": constraint,
                "source": "OpenStreetMap/Overpass", "places": [],
                "note": (f"OSM has little verified {label} data for eateries here; "
                         "describe suitable options generally and advise calling ahead.")}
    return {"kind": "accessible_places", "constraint": constraint,
            "source": "OpenStreetMap/Overpass", "places": places,
            "note": (f"Real OSM-tagged {label} places; tags are crowd-sourced - "
                     "still worth confirming by phone.")}


@lru_cache(maxsize=256)
def toddler_activities(lat: float, lng: float, radius_m: int = 1500, limit: int = 8) -> dict:
    """Playgrounds and indoor play centres near a point - actual toddler-friendly
    stops the dossier can suggest (rainy-day indoor play is especially useful).
    These are leisure= tags, not amenity=, so it's a separate query.
    """
    ql = f"""
    [out:json][timeout:15];
    (
      node["leisure"="playground"](around:{radius_m},{lat},{lng});
      way["leisure"="playground"](around:{radius_m},{lat},{lng});
      node["leisure"="indoor_play"](around:{radius_m},{lat},{lng});
      way["leisure"="indoor_play"](around:{radius_m},{lat},{lng});
    );
    out center {limit * 3};
    """
    data = _overpass(ql)
    if data is None:
        return {"kind": "unavailable", "reason": "overpass unreachable"}
    places = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        kind = tags.get("leisure")
        nm = tags.get("name") or ("Indoor play centre" if kind == "indoor_play" else "Playground")
        places.append({"name": nm, "type": kind,
                       "toddler": tags.get("playground:toddler") or tags.get("min_age"),
                       "opening_hours": tags.get("opening_hours")})
        if len(places) >= limit:
            break
    if not places:
        return {"kind": "sparse", "source": "OpenStreetMap/Overpass", "places": [],
                "note": "OSM has few playgrounds/indoor play mapped near here."}
    return {"kind": "toddler_activities", "source": "OpenStreetMap/Overpass",
            "places": places,
            "note": "OSM-mapped playgrounds & indoor play; indoor options are great rainy-day backups."}


# whether a place tends to suit a limited-walking senior: prefers nearby seating
@lru_cache(maxsize=256)
def rest_stops(lat: float, lng: float, radius_m: int = 500, limit: int = 10) -> dict:
    """Benches, drinking water, and public toilets near a point - the rest-stop
    infrastructure that matters for seniors / limited-walking travelers. Separate
    from accessible_places because these are street furniture, not venues.
    """
    ql = f"""
    [out:json][timeout:15];
    (
      node["amenity"="bench"](around:{radius_m},{lat},{lng});
      node["amenity"="drinking_water"](around:{radius_m},{lat},{lng});
      node["amenity"="toilets"](around:{radius_m},{lat},{lng});
    );
    out {limit};
    """
    data = _overpass(ql)
    if data is None:
        return {"kind": "unavailable", "reason": "overpass unreachable"}
    counts = {"bench": 0, "drinking_water": 0, "toilets": 0}
    for el in data.get("elements", []):
        am = el.get("tags", {}).get("amenity")
        if am in counts:
            counts[am] += 1
    total = sum(counts.values())
    if total == 0:
        return {"kind": "sparse", "source": "OpenStreetMap/Overpass", "counts": counts,
                "note": "OSM has little rest-stop data mapped near here."}
    return {"kind": "rest_stops", "source": "OpenStreetMap/Overpass", "counts": counts,
            "note": (f"Within {radius_m}m: {counts['bench']} benches, "
                     f"{counts['drinking_water']} water points, {counts['toilets']} toilets "
                     "(OSM-mapped; more may exist unmapped).")}


# --------------------------------------------------------------------------
# SPECIAL-NEEDS AMENITIES  (OpenStreetMap via Overpass, no key)
# One function, many categories — each maps a real accessibility/medical/dietary
# need to concrete OSM tags. Honest "sparse" result when OSM has nothing mapped.
# --------------------------------------------------------------------------

# category -> Overpass node filters (real OSM tags). Kept conservative: only tags
# that genuinely exist in OSM, so results are real, not wishful.
_AMENITY_QUERIES = {
    "medical": [  # pharmacies + hospitals + clinics: chronic conditions, meds
        'node["amenity"="pharmacy"]', 'node["amenity"="hospital"]',
        'node["amenity"="clinic"]', 'node["amenity"="doctors"]'],
    "quiet": [   # green/calm spaces: sensory-sensitive, autism, anxiety
        'node["leisure"="park"]', 'node["leisure"="garden"]',
        'node["amenity"="place_of_worship"]'],
    "allergen_dining": [  # eateries tagged for dietary needs
        'node["diet:gluten_free"="yes"]', 'node["diet:vegan"="yes"]',
        'node["diet:vegetarian"="yes"]'],
    "prayer": [  # prayer rooms + halal/kosher: religious observance
        'node["amenity"="place_of_worship"]',
        'node["diet:halal"="yes"]', 'node["diet:kosher"="yes"]'],
    "family": [  # changing tables + nursing: families beyond toddlers
        'node["changing_table"="yes"]', 'node["healthcare"="midwife"]'],
    "parking": [  # disabled parking: mobility, blue badge
        'node["amenity"="parking"]["capacity:disabled"]',
        'node["amenity"="parking"]["wheelchair"="yes"]'],
    "step_free_transit": [  # accessible transit access points: mobility
        'node["railway"="station"]["wheelchair"="yes"]',
        'node["highway"="bus_stop"]["wheelchair"="yes"]',
        'node["railway"="subway_entrance"]["wheelchair"="yes"]'],
}

_AMENITY_LABEL = {
    "medical": "pharmacies / hospitals / clinics",
    "quiet": "parks, gardens and quiet places",
    "allergen_dining": "allergen-friendly dining (gluten-free / vegan / vegetarian)",
    "prayer": "places of worship / halal / kosher options",
    "family": "changing tables / nursing facilities",
    "parking": "accessible (blue-badge) parking",
    "step_free_transit": "step-free transit access points",
}


def nearby_amenities(lat: float, lng: float, category: str,
                     radius_m: int = 1200, limit: int = 12) -> dict:
    """Find real OSM amenities matching a SPECIAL need near a point. Categories:
    medical, quiet, allergen_dining, prayer, family, parking, step_free_transit.
    Returns a count + a few named examples, or an honest 'sparse' when OSM has
    nothing mapped (common outside dense cities — we never invent)."""
    filters = _AMENITY_QUERIES.get(category)
    if not filters:
        return {"kind": "unavailable", "reason": f"unknown category {category}"}
    body = "".join(f"{f}(around:{radius_m},{lat},{lng});" for f in filters)
    ql = f"[out:json][timeout:15];({body});out {limit};"
    data = _overpass(ql)
    if data is None:
        return {"kind": "unavailable", "reason": "overpass unreachable"}
    names, count = [], 0
    for el in data.get("elements", []):
        count += 1
        nm = (el.get("tags", {}) or {}).get("name")
        if nm and nm not in names:
            names.append(nm)
    label = _AMENITY_LABEL.get(category, category)
    if count == 0:
        return {"kind": "sparse", "category": category,
                "source": "OpenStreetMap/Overpass", "count": 0,
                "note": f"OSM has no {label} mapped within {radius_m}m here — "
                        "worth confirming locally; absence in OSM isn't absence in reality."}
    return {"kind": "amenities", "category": category,
            "source": "OpenStreetMap/Overpass", "count": count,
            "examples": names[:6],
            "note": (f"{count} {label} mapped within {radius_m}m"
                     + (f" (e.g. {', '.join(names[:3])})" if names else "")
                     + " — OSM-tagged, crowd-sourced; confirm specifics.")}


# --------------------------------------------------------------------------
# PUBLIC HOLIDAYS & CLOSURES  (Nager.Date, no key)
# --------------------------------------------------------------------------

# City -> ISO 3166-1 alpha-2 country code, for the corpus cities. Nager.Date has
# full European coverage and varying coverage elsewhere; we report gaps honestly.
_CITY_COUNTRY = {
    # original 26
    "Amsterdam": "NL", "Bangkok": "TH", "Barcelona": "ES", "Buenos Aires": "AR",
    "Cape Town": "ZA", "Copenhagen": "DK", "Delhi": "IN", "Dubai": "AE",
    "Kyoto": "JP", "Lisbon": "PT", "Marrakech": "MA", "Mexico City": "MX",
    "Nairobi": "KE", "New York": "US", "Porto": "PT", "Prague": "CZ",
    "Queenstown": "NZ", "Reykjavik": "IS", "Rome": "IT", "San Francisco": "US",
    "Seoul": "KR", "Singapore": "SG", "Sydney": "AU", "Tokyo": "JP",
    "Vancouver": "CA", "Vienna": "AT",
    # +25
    "London": "GB", "Paris": "FR", "Berlin": "DE", "Madrid": "ES", "Athens": "GR",
    "Istanbul": "TR", "Edinburgh": "GB", "Florence": "IT", "Los Angeles": "US",
    "Chicago": "US", "Toronto": "CA", "Montreal": "CA", "Boston": "US",
    "Austin": "US", "Hong Kong": "HK", "Osaka": "JP", "Taipei": "TW",
    "Mumbai": "IN", "Jaipur": "IN", "Hanoi": "VN", "Cairo": "EG", "Petra": "JO",
    "Rio de Janeiro": "BR", "Cusco": "PE", "Melbourne": "AU",
    "Dehradun": "IN", "Bangalore": "IN", "Bengaluru": "IN", "Chennai": "IN",
    "Kolkata": "IN", "Hyderabad": "IN", "Pune": "IN", "Goa": "IN", "Agra": "IN",
    "Rishikesh": "IN", "Udaipur": "IN", "Varanasi": "IN", "Kochi": "IN",
}


def _country_for(city: str) -> str | None:
    if not city:
        return None
    # 1) hardcoded map (fast, curated) for the known corpus cities
    hit = _CITY_COUNTRY.get(city) or _CITY_COUNTRY.get(city.replace("_", " "))
    if hit:
        return hit
    # 2) fall back to Nominatim for lazily-built / out-of-map cities (e.g. Venice)
    #    so country-dependent info still resolves. Cached, so at most one lookup.
    return geocode_city_country(city.replace("_", " "))


@lru_cache(maxsize=64)
def _holidays_for_year(country: str, year: int) -> tuple:
    """Fetch public holidays for a country+year (cached). Returns tuple of
    (date_iso, name, is_public) or () on failure. Nager.Date, no key."""
    try:
        data = _get_json(f"https://date.nager.at/api/v3/publicholidays/{year}/{country}", {})
        out = []
        for h in data:
            types = h.get("types") or []
            out.append((h.get("date"), h.get("name"), "Public" in types))
        return tuple(out)
    except Exception:
        return ()


def holidays_in_window(city: str, start: str | None, end: str | None) -> dict:
    """Public holidays overlapping the trip window, with a closure caveat.

    Catches the trip-ruining case where a planned day lands on a public holiday -
    when many attractions close and transit runs a reduced schedule. Returns
    {kind, country, holidays:[{date,name}], note} or an honest 'unavailable'
    (e.g. country not covered - Nager.Date is strong in Europe, patchier elsewhere).
    """
    country = _country_for(city)
    if not country:
        return {"kind": "unavailable", "reason": f"no country mapping for {city}"}
    if not start:
        return {"kind": "no_dates", "country": country,
                "note": "No trip dates given, so holiday/closure checks were skipped."}
    try:
        from datetime import date as _d
        s = _d.fromisoformat(start)
        e = _d.fromisoformat(end) if end else s
    except Exception:
        return {"kind": "unavailable", "reason": "bad dates"}

    years = {s.year, e.year}
    hits = []
    for yr in years:
        for (dt, name, is_public) in _holidays_for_year(country, yr):
            if not dt:
                continue
            try:
                hd = _d.fromisoformat(dt)
            except Exception:
                continue
            if s <= hd <= e and is_public:
                hits.append({"date": dt, "name": name})
    if not hits:
        return {"kind": "holidays", "country": country, "holidays": [],
                "note": "No public holidays fall during the trip window."}
    hits.sort(key=lambda h: h["date"])
    return {"kind": "holidays", "country": country, "holidays": hits,
            "note": ("Public holiday(s) during the trip - expect some attractions "
                     "closed and reduced transit; verify hours for those days.")}


# --------------------------------------------------------------------------
# WIKIPEDIA  (narrative accessibility detail + place images, no key)
# Wikimedia REST API. Only ever hits en.wikipedia.org -> SSRF-safe by
# construction (no arbitrary/user-supplied URLs). Returned text is UNTRUSTED:
# callers must isolate()/sanitize() it before feeding it to a model.
# --------------------------------------------------------------------------
def _wiki_summary(place: str) -> dict:
    """Raw Wikipedia REST summary for a title. Internal; returns {} on miss."""
    title = urllib.parse.quote((place or "").strip().replace(" ", "_"))
    if not title:
        return {}
    try:
        return _get_json(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
            {}, headers={"accept": "application/json"}) or {}
    except Exception:
        return {}


# Sentences that mention these are the ones worth showing the rating agent.
_ACCESS_KEYWORDS = (
    "accessib", "wheelchair", "step-free", "step free", "stair", "steps",
    "ramp", "lift", "elevator", "escalator", "disabled", "mobility",
    "entrance", "cobble", "uneven", "terrain", "slope", "level access",
    "toilet", "restroom", "facilities", "hill", "climb",
)


def _wiki_full_extract(title: str, lang_site: str = "en.wikipedia.org") -> str:
    """Full plain-text article extract via the MediaWiki Query API (not the
    short summary). Returns '' on miss. lang_site lets us hit wikivoyage too."""
    t = (title or "").strip()
    if not t:
        return ""
    try:
        data = _get_json(
            f"https://{lang_site}/w/api.php",
            {"action": "query", "prop": "extracts", "explaintext": "1",
             "redirects": "1", "format": "json", "titles": t})
        pages = ((data or {}).get("query", {}) or {}).get("pages", {}) or {}
        for _pid, page in pages.items():
            ex = page.get("extract") or ""
            if ex:
                return ex
    except Exception:
        pass
    return ""


def _accessibility_sentences(text: str, limit: int = 6) -> str:
    """Pull the sentences most likely to describe accessibility, so we feed the
    agent the relevant part of a long article rather than the whole thing."""
    if not text:
        return ""
    import re as _re
    sentences = _re.split(r"(?<=[.!?])\s+", text)
    hits = [s.strip() for s in sentences
            if any(k in s.lower() for k in _ACCESS_KEYWORDS)]
    if hits:
        return " ".join(hits[:limit])[:1500]
    # no explicit accessibility sentences — fall back to the first bit of the
    # article so the agent at least has context (still marked untrusted upstream)
    return " ".join(sentences[:3])[:800]


@lru_cache(maxsize=512)
def wiki_accessibility_notes(place: str, city: str = "") -> dict:
    """Narrative accessibility detail about a place (step-free entrances, lifts,
    stairs, terrain) that OSM tags don't capture. Strategy:
      1. Full Wikipedia article -> pull the accessibility-relevant sentences.
      2. If Wikipedia is thin, try Wikivoyage (travel-focused, more likely to
         mention access) for the place, then the city.
    Returns {place, text, source_url}. `text` is UNTRUSTED external content —
    the caller MUST isolate() it before putting it in a prompt. Empty on miss."""
    # 1. Wikipedia full article, accessibility sentences
    full = _wiki_full_extract(place, "en.wikipedia.org")
    notes = _accessibility_sentences(full)
    source = f"https://en.wikipedia.org/wiki/{urllib.parse.quote((place or '').replace(' ', '_'))}"

    # 2. If nothing useful, try Wikivoyage for the place, then the city page.
    if not notes:
        wv = _wiki_full_extract(place, "en.wikivoyage.org") or \
             (_wiki_full_extract(city, "en.wikivoyage.org") if city else "")
        wv_notes = _accessibility_sentences(wv)
        if wv_notes:
            notes = wv_notes
            src_title = place if _wiki_full_extract(place, "en.wikivoyage.org") else city
            source = f"https://en.wikivoyage.org/wiki/{urllib.parse.quote((src_title or '').replace(' ', '_'))}"

    return {"place": place, "text": notes, "source_url": source if notes else ""}


@lru_cache(maxsize=512)
def wiki_place_image(place: str, city: str = "") -> str:
    """Canonical image URL for a place from Wikipedia (Wikimedia Commons,
    CC/PD licensed). Returns '' if none found."""
    data = _wiki_summary(place)
    return (((data.get("thumbnail") or {}).get("source", ""))
            or ((data.get("originalimage") or {}).get("source", "")))
