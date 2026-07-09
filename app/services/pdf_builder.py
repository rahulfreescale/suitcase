"""Render a branded, professional trip itinerary + Travel Brief to PDF.

Uses WeasyPrint (HTML/CSS -> PDF) so the layout is designed in HTML — full-width
hero images, cover page, accessibility notes, and the Travel Brief (good-to-know
+ accessibility services + excluded places).

WeasyPrint needs system libraries (Pango, Cairo, GDK-PixBuf):
  macOS:   brew install weasyprint        (or: brew install pango gdk-pixbuf libffi)
  Debian:  apt-get install -y libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 libffi-dev
  pip:     pip install weasyprint

Run standalone to verify your install renders correctly:
  python -m app.services.pdf_builder        # writes /tmp/suitcase_sample.pdf
"""
from __future__ import annotations
import html as _html


def _esc(s) -> str:
    return _html.escape(str(s or ""))


_PILL_CLASS = {"EXCELLENT": "EXCELLENT", "GOOD": "GOOD",
               "TOUGH": "TOUGH", "FAIL": "FAIL", "UNKNOWN": "TOUGH"}

_SLOTS = ("morning", "afternoon", "evening")


def _place_card(slot: str, block: dict) -> str:
    name = _esc(block.get("name_hint"))
    label = ((block.get("overall") or {}).get("label")) or ""
    pill_cls = _PILL_CLASS.get(str(label).upper(), "TOUGH")
    # best note across constraints (prefer wheelchair, then any real citation)
    per = block.get("per_constraint") or {}
    note = ""
    for k in ("wheelchair", "toddler-friendly", "senior-friendly", "budget"):
        c = (per.get(k) or {}).get("citation") or ""
        if c and len(c) > 12 and not c.lower().startswith("no relevant"):
            note = c
            break
    img = block.get("image_url") or ""
    hero = (f'<img class="hero" src="{_esc(img)}" alt="{name}"/>' if img
            else '<div class="hero hero-empty"></div>')
    pills = f'<span class="pill {pill_cls}">wheelchair · {_esc(label)}</span>' if label else ""
    note_html = (f'<div class="note">{_esc(note)}</div>' if note else "")
    return (f'<div class="place">{hero}<div class="place-body">'
            f'<div class="place-top"><span class="slot">{_esc(slot)}</span></div>'
            f'<div class="place-name">{name}</div>'
            f'<div class="pills">{pills}</div>{note_html}</div></div>')


def _day_section(day: dict) -> str:
    num = _esc(day.get("day"))
    blocks = day.get("blocks") or {}
    cards = "".join(_place_card(s, blocks[s]) for s in _SLOTS if blocks.get(s))
    if not cards:
        return ""
    return (f'<div class="day"><div class="day-head">'
            f'<span class="day-num">Day {num}</span>'
            f'<span class="day-title">Your day</span></div>{cards}</div>')


def _access_block(ab: dict) -> str:
    if not ab or not ab.get("items"):
        return ""
    items = "".join(
        f'<div class="ab-item"><div class="ab-k">{_esc(i.get("label"))}</div>'
        f'<div class="ab-v">{_esc(i.get("value"))}</div></div>'
        for i in ab.get("items", []))
    note = _esc(ab.get("note") or "")
    return (f'<div class="access-block"><div class="ab-head">♿ '
            f'{_esc(ab.get("title") or "Accessibility Services")}</div>'
            f'<div class="ab-grid">{items}</div>'
            f'<div class="ab-note">{note}</div></div>')


def _brief_sections(sections: list) -> str:
    out = []
    for s in (sections or []):
        title = s.get("title") or ""
        body = s.get("body") or ""
        if not body or s.get("section") == "access_services":
            continue
        # crude md->text: strip markdown emphasis; keep paragraphs
        para = "".join(f"<p>{_esc(p.strip())}</p>"
                       for p in str(body).split("\n\n") if p.strip())
        out.append(f'<div class="brief-sec"><h3 class="brief-h">{_esc(title)}</h3>{para}</div>')
    return "".join(out)


def _excluded(skipped: list) -> str:
    if not skipped:
        return ""
    rows = []
    for s in skipped:
        tag = ((s.get("overall") or {}).get("label")) or "TOUGH"
        tag = str(tag).upper()
        tag_txt = "NOT SURE" if tag == "UNKNOWN" else tag
        star = ('<span class="star">must-see</span>' if s.get("is_famous") else "")
        why = _esc(s.get("reason") or "")
        cls = " unsure" if tag == "UNKNOWN" else ""
        rows.append(
            f'<div class="skip{cls}"><div class="sktag {tag}">{_esc(tag_txt)}</div>'
            f'<div class="skbody"><div class="skname">{_esc(s.get("name_hint"))}{star}</div>'
            f'<div class="skwhy">{why}</div></div></div>')
    return ('<h3 class="brief-h" style="margin-top:6px">Popular spots we left out '
            '— and why</h3><p class="brief-sub">Well-known places that don\'t fit '
            'the accessibility constraints you gave — shown so you can see they '
            'were skipped for access reasons, not overlooked.</p>' + "".join(rows))


def _split_dossier_sections(sections: list):
    """Real dossier packs everything in `sections`: an itinerary section, prose
    sections (with `body`), and an access_services section (with `items`).
    Split them for the PDF."""
    access = {}
    prose = []
    for s in (sections or []):
        if s.get("section") == "access_services" or s.get("items"):
            access = s
        elif s.get("body"):
            prose.append(s)
    return access, prose


def build_itinerary_html(plan: dict, dossier: dict | None = None) -> str:
    """Assemble the full PDF HTML. Accepts a dossier (the real multi-agent shape:
    itinerary + sections[itinerary|prose|access_services] + meta/_chips/_travelers)
    and/or a plan_trip result (contract + itinerary + chips) as fallback."""
    d = dossier or {}
    plan = plan or {}
    contract = plan.get("contract") or {}
    meta = d.get("meta") or {}

    dest = (meta.get("destination") or contract.get("destination") or "Your trip")
    itin = d.get("itinerary") or plan.get("itinerary") or {}
    days_n = (meta.get("days") or contract.get("trip_length_days")
              or len(itin.get("days") or []))

    trav = (d.get("_travelers") or contract.get("travelers") or [])
    who = "Traveler"
    if any((t or {}).get("mobility") == "wheelchair" for t in trav):
        who = "Wheelchair traveler"
    elif any((t or {}).get("type") == "toddler" for t in trav):
        who = "Family with a toddler"

    chips = (d.get("_chips", {}) or {}).get("detected") or \
            (plan.get("chips", {}) or {}).get("detected") or []
    chip_html = "".join(f'<span class="chip">{_esc(c.replace("_"," "))}</span>' for c in chips)

    days_html = "".join(_day_section(dd) for dd in (itin.get("days") or []))
    skipped_html = _excluded(itin.get("skipped") or [])

    # Travel Brief: split the dossier's sections into access-block + prose.
    access_block, prose_sections = _split_dossier_sections(d.get("sections") or [])
    ab_html = _access_block(access_block)
    brief_html = _brief_sections(prose_sections)
    brief_block = ""
    if ab_html or brief_html or skipped_html:
        brief_block = (f'<div class="day"><div class="day-head">'
                       f'<span class="day-num">Travel Brief</span>'
                       f'<span class="day-title">Good to know</span></div>'
                       f'{ab_html}{brief_html}{skipped_html}</div>')

    return _TEMPLATE.format(
        dest=_esc(dest), days_n=_esc(days_n), who=_esc(who),
        chips=chip_html, days=days_html, brief=brief_block)


def _ensure_lib_path():
    """WeasyPrint needs Pango/Cairo/GObject. On macOS (Apple Silicon) Homebrew
    installs to /opt/homebrew/lib, which Python's loader doesn't search by
    default — setting DYLD_FALLBACK_LIBRARY_PATH at runtime is too late (dyld
    reads it at process start). Instead we preload the core dylibs by absolute
    path via ctypes, which DOES work at runtime and lets WeasyPrint resolve the
    rest. No-op on Linux/Docker where the libs are already on the loader path."""
    import sys, os, ctypes, glob
    if sys.platform != "darwin":
        return
    libdirs = [d for d in ("/opt/homebrew/lib", "/usr/local/lib") if os.path.isdir(d)]
    # preload the foundational libs in dependency order; ignore if absent
    for stem in ("libgobject-2.0", "libpango-1.0", "libpangocairo-1.0",
                 "libcairo", "libgdk_pixbuf-2.0", "libfontconfig", "libharfbuzz"):
        for d in libdirs:
            hits = sorted(glob.glob(os.path.join(d, stem + "*.dylib")))
            if hits:
                try:
                    ctypes.CDLL(hits[0])
                except OSError:
                    pass
                break


def _img_url_fetcher(url):
    """WeasyPrint URL fetcher with a real User-Agent + timeout, so Wikimedia
    serves images (its default fetcher gets a 403) and a slow/blocked image
    degrades gracefully instead of blanking the whole PDF."""
    import urllib.request, base64
    from weasyprint import default_url_fetcher
    if url.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Suitcase/1.0 (accessibility travel planner)"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read()
                ctype = r.headers.get("Content-Type", "image/jpeg")
            return {"string": data, "mime_type": ctype.split(";")[0]}
        except Exception:
            px = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
                "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
            return {"string": px, "mime_type": "image/png"}
    return default_url_fetcher(url)


def render_pdf(plan: dict, dossier: dict | None = None) -> bytes:
    """Render the itinerary+brief to PDF bytes via WeasyPrint."""
    _ensure_lib_path()
    from weasyprint import HTML
    doc_html = build_itinerary_html(plan, dossier)
    return HTML(string=doc_html, url_fetcher=_img_url_fetcher).write_pdf()


# ---- the HTML/CSS template (the approved design) --------------------------
_TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>
<style>
  @page {{ size: A4; margin: 0; }}
  * {{ box-sizing: border-box; }}
  html,body {{ margin:0; padding:0; background:#EDE9E0; color:#141C26;
    font-family:'Helvetica Neue',Arial,sans-serif; line-height:1.5; }}
  .cover {{ background:#1B2A4A; color:#fff; padding:54px 60px 46px; position:relative; }}
  .cover .stripe {{ position:absolute; top:0; left:0; right:0; height:10px;
    background:repeating-linear-gradient(90deg,#D8452B 0 22px,#fff 22px 30px); }}
  .brand {{ font-weight:800; font-size:34px; letter-spacing:-.02em; color:#fff; margin-top:8px; }}
  .brand .s {{ color:#D8452B; }}
  .brand-tag {{ font-size:10px; letter-spacing:.18em; text-transform:uppercase; color:#A9B4C9; margin-top:4px; }}
  .cover h1 {{ font-weight:800; font-size:44px; line-height:1.05; margin:30px 0 8px; }}
  .cover .meta {{ margin-top:16px; }}
  .cover .meta .m {{ display:inline-block; margin-right:26px; font-size:11px; letter-spacing:.08em; color:#C6CEDC; }}
  .cover .meta .m b {{ display:block; color:#fff; font-size:15px; margin-top:3px; }}
  .cover .chips {{ margin-top:22px; }}
  .cover .chip {{ display:inline-block; font-size:10px; letter-spacing:.1em; text-transform:uppercase;
    background:rgba(255,255,255,.12); border:1px solid rgba(255,255,255,.22); color:#fff;
    padding:6px 12px; border-radius:100px; margin-right:8px; }}
  .day {{ padding:38px 60px 8px; }}
  .day-head {{ border-bottom:2px solid #1B2A4A; padding-bottom:10px; margin-bottom:26px; }}
  .day-num {{ font-weight:800; font-size:13px; letter-spacing:.14em; text-transform:uppercase; color:#D8452B; margin-right:14px; }}
  .day-title {{ font-weight:700; font-size:24px; color:#1B2A4A; }}
  .place {{ background:#fff; border:1px solid #DFDCD3; border-radius:14px; overflow:hidden;
    margin-bottom:26px; page-break-inside:avoid; }}
  .hero {{ width:100%; height:240px; object-fit:cover; display:block; background:#E6E2D8; }}
  .hero-empty {{ height:8px; background:#D8452B; }}
  .place-body {{ padding:22px 26px 24px; }}
  .slot {{ font-size:9.5px; letter-spacing:.16em; text-transform:uppercase; color:#A7ADB5; }}
  .place-name {{ font-weight:700; font-size:22px; color:#1B2A4A; margin:2px 0 12px; }}
  .pills {{ margin-bottom:14px; }}
  .pill {{ display:inline-block; font-size:10px; letter-spacing:.06em; padding:5px 11px; border-radius:100px; }}
  .pill.EXCELLENT,.pill.GOOD {{ background:#E4F1EC; color:#12856A; }}
  .pill.TOUGH {{ background:#F6ECD6; color:#B8862B; }}
  .pill.FAIL {{ background:#F7E4DF; color:#D8452B; }}
  .note {{ font-size:13.5px; line-height:1.6; color:#4A4A44; border-left:3px solid #DFDCD3; padding-left:14px; }}
  .access-block {{ background:#FCF4EE; border:1px dashed #D8452B; border-radius:12px; padding:22px 24px;
    margin-bottom:26px; page-break-inside:avoid; }}
  .ab-head {{ font-weight:700; font-size:16px; color:#D8452B; margin-bottom:16px; }}
  .ab-item {{ display:inline-block; width:46%; vertical-align:top; margin:0 2% 14px 0; }}
  .ab-k {{ font-size:9px; letter-spacing:.1em; text-transform:uppercase; color:#9A958A; margin-bottom:3px; }}
  .ab-v {{ font-size:13px; line-height:1.5; color:#141C26; }}
  .ab-note {{ margin-top:8px; font-size:11px; font-style:italic; color:#5C6773; }}
  .brief-sec {{ margin-bottom:20px; page-break-inside:avoid; }}
  .brief-h {{ font-weight:700; font-size:17px; color:#1B2A4A; margin:0 0 8px; }}
  .brief-sec p {{ font-size:13.5px; line-height:1.7; color:#4A4A44; margin:0 0 10px; }}
  .brief-sub {{ font-size:13px; color:#5C6773; line-height:1.6; margin:-8px 0 16px; }}
  .skip {{ background:#fff; border:1px solid #DFDCD3; border-radius:12px; padding:18px 20px;
    margin-bottom:14px; page-break-inside:avoid; }}
  .skip.unsure {{ border-style:dashed; background:#FBFAF6; }}
  .sktag {{ display:inline-block; font-size:9.5px; font-weight:700; letter-spacing:.08em;
    padding:6px 10px; border-radius:7px; margin-right:12px; vertical-align:top; }}
  .sktag.TOUGH {{ background:#F6ECD6; color:#B8862B; }}
  .sktag.FAIL {{ background:#F7E4DF; color:#D8452B; }}
  .sktag.UNKNOWN {{ background:#ECEEF1; color:#6B7480; }}
  .skbody {{ display:inline-block; width:85%; vertical-align:top; }}
  .skname {{ font-weight:700; font-size:16px; color:#1B2A4A; margin-bottom:5px; }}
  .star {{ font-size:9px; letter-spacing:.08em; text-transform:uppercase; color:#D8452B;
    background:#F7E4DF; padding:3px 7px; border-radius:5px; margin-left:6px; }}
  .skwhy {{ font-size:13px; line-height:1.6; color:#4A4A44; }}
  .foot {{ padding:26px 60px 40px; color:#5C6773; font-size:11px; border-top:1px dashed #DFDCD3; margin-top:14px; }}
  .fbrand {{ font-weight:800; color:#1B2A4A; font-size:14px; }}
  .fbrand .s {{ color:#D8452B; }}
  .foot p {{ margin:6px 0 0; line-height:1.6; }}
</style></head><body>
  <div class="cover"><div class="stripe"></div>
    <div class="brand">SUIT<span class="s">CASE</span></div>
    <div class="brand-tag">Accessibility-first trip planning</div>
    <h1>{dest}</h1>
    <div class="meta">
      <span class="m">DESTINATION<b>{dest}</b></span>
      <span class="m">DURATION<b>{days_n} days</b></span>
      <span class="m">PLANNED FOR<b>{who}</b></span>
    </div>
    <div class="chips">{chips}</div>
  </div>
  {days}
  {brief}
  <div class="foot"><div class="fbrand">SUIT<span class="s">CASE</span></div>
    <p>This itinerary is advisory. Accessibility details are grounded in public
    sources; confirm step-free access directly before relying on it.</p>
    <p>Generated by Suitcase — accessibility-first trip planning.</p>
  </div>
</body></html>"""


if __name__ == "__main__":
    # Standalone smoke test: render a sample PDF so you can verify WeasyPrint works.
    sample_plan = {
        "contract": {"destination": "Ljubljana", "trip_length_days": 2,
                     "travelers": [{"type": "adult", "mobility": "wheelchair"}]},
        "chips": {"detected": ["wheelchair"]},
        "itinerary": {"days": [
            {"day": 1, "blocks": {
                "morning": {"name_hint": "Ljubljana Cathedral",
                            "overall": {"label": "GOOD"},
                            "per_constraint": {"wheelchair": {"citation": "Side entrance on the southern wall; no wheelchair barriers. Accessible toilets within 500m."}},
                            "image_url": ""}}},
        ], "skipped": [
            {"name_hint": "Old Town", "is_famous": True,
             "overall": {"label": "TOUGH"},
             "reason": "Cobblestones and frequent stairs; challenging for mobility devices."}]},
    }
    sample_dossier = {
        "access_block": {"title": "Accessibility Services",
            "note": "Reference information — confirm details locally.",
            "items": [{"label": "Emergency", "value": "112 (EU-wide)"},
                      {"label": "Accessible transport", "value": "Pre-book accessible taxis a day ahead"}]},
        "sections": [{"section": "sense_of_place", "title": "The place",
                      "body": "Ljubljana rewards a slow pace along its riverside promenades."}],
    }
    pdf = render_pdf(sample_plan, sample_dossier)
    with open("/tmp/suitcase_sample.pdf", "wb") as f:
        f.write(pdf)
    print("Wrote /tmp/suitcase_sample.pdf  (%d bytes)" % len(pdf))
