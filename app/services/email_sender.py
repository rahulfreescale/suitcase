"""Least-privilege email sender for trip itineraries.

This module does EXACTLY ONE THING: send a formatted itinerary to a single,
validated recipient. It deliberately exposes no general "send arbitrary email"
capability — the only public function takes an itinerary and one recipient and
sends a fixed-format message. That narrow surface is the least-privilege
control: even if something upstream is compromised, this tool can't be coerced
into spamming, exfiltrating data, or emailing arbitrary content.

Security properties:
  - Recipient is validated (single, well-formed address) before any send.
  - The body is built from the itinerary by THIS code, not passed in freely,
    so injected content can't become an arbitrary outbound message.
  - Sending is gated behind an explicit confirmation upstream (human-in-the-loop);
    this module refuses to send unless confirm=True is passed.
"""
from __future__ import annotations
import re
import html

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_MAX_RECIPIENT_LEN = 254


class EmailError(Exception):
    pass


def validate_recipient(addr: str) -> str:
    """Validate & normalize a single recipient address. Raises EmailError.
    Rejects multiple addresses, malformed ones, and anything suspicious — this
    is the tool-validation layer for the email action."""
    a = (addr or "").strip()
    if not a:
        raise EmailError("no recipient address given")
    if len(a) > _MAX_RECIPIENT_LEN:
        raise EmailError("recipient address too long")
    # reject attempts to send to multiple recipients (comma/semicolon/space lists)
    if any(sep in a for sep in (",", ";", " ", "\n", "\r", "\t", "<", ">")):
        raise EmailError("only a single, plain email address is allowed")
    if not _EMAIL_RE.match(a):
        raise EmailError("recipient is not a valid email address")
    return a


def _itinerary_to_html(destination: str, days: list) -> str:
    """Build the email body from the itinerary IN CODE — the model/user never
    supplies raw HTML, so injected content can't become the message body."""
    esc = lambda s: html.escape(str(s or ""))
    parts = [f"<h2>Your trip to {esc(destination)}</h2>"]
    for d in (days or []):
        parts.append(f"<h3>Day {esc(d.get('day'))}</h3><ul>")
        for slot in ("morning", "afternoon", "evening"):
            b = (d.get("blocks") or {}).get(slot)
            if b:
                name = esc(b.get("name_hint"))
                lbl = esc(((b.get("overall") or {}).get("label")) or "")
                parts.append(f"<li><b>{esc(slot)}:</b> {name} "
                             f"<i>({lbl})</i></li>")
        parts.append("</ul>")
    parts.append("<p style='color:#888;font-size:12px'>Sent from Suitcase — "
                 "an accessibility-first trip planner. Advisory only.</p>")
    return "\n".join(parts)


def send_itinerary(recipient: str, destination: str, days: list,
                   confirm: bool = False) -> dict:
    """Send ONE itinerary to ONE validated recipient. Refuses unless confirm=True
    (the human-in-the-loop gate). Returns {ok, status, recipient} or raises."""
    if not confirm:
        # Hard refusal: no send without explicit confirmation. This is where the
        # human-in-the-loop / durable-workflow approval hooks in.
        raise EmailError("send not confirmed — confirmation is required before sending")

    to_addr = validate_recipient(recipient)      # tool-validation
    body_html = _itinerary_to_html(destination, days)
    subject = f"Your accessible trip to {destination}"

    from app.config import get_settings
    s = get_settings()
    key = getattr(s, "sendgrid_api_key", None)
    from_addr = getattr(s, "email_from", None)

    if not key or not from_addr:
        # No creds configured — don't pretend to send. Honest failure.
        raise EmailError("email is not configured (missing SENDGRID_API_KEY / EMAIL_FROM)")

    # Minimal SendGrid v3 API call via urllib (no extra dependency needed).
    import json, urllib.request, urllib.error
    payload = {
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from": {"email": from_addr},
        "subject": subject,
        "content": [{"type": "text/html", "value": body_html}],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        raise EmailError(f"email provider rejected the send (HTTP {e.code})")
    except Exception as e:
        raise EmailError(f"email send failed: {type(e).__name__}")

    return {"ok": True, "status": "sent", "recipient": to_addr, "http": code}


def send_itinerary_pdf(recipient: str, destination: str, pdf_bytes: bytes,
                       confirm: bool = False) -> dict:
    """Send ONE itinerary as a PDF ATTACHMENT to ONE validated recipient.

    Same controls as send_itinerary: refuses unless confirm=True (human-in-the-
    loop), validates the recipient (tool-validation), and the message body is
    built in code — the only free input is the recipient, and it's validated."""
    if not confirm:
        raise EmailError("send not confirmed — confirmation is required before sending")
    to_addr = validate_recipient(recipient)
    if not pdf_bytes:
        raise EmailError("no PDF to send")

    import base64
    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    subject = f"Your accessible trip to {destination}"
    body_html = (f"<p>Your Suitcase itinerary for <b>{destination}</b> is attached "
                 f"as a PDF.</p><p style='color:#888;font-size:12px'>Advisory only — "
                 f"confirm step-free access before you rely on it.</p>")
    safe_name = "".join(c for c in destination if c.isalnum() or c in " -_").strip() or "trip"

    from app.config import get_settings
    s = get_settings()
    key = getattr(s, "sendgrid_api_key", None)
    from_addr = getattr(s, "email_from", None)
    if not key or not from_addr:
        raise EmailError("email is not configured (missing SENDGRID_API_KEY / EMAIL_FROM)")

    import json, urllib.request, urllib.error
    payload = {
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from": {"email": from_addr},
        "subject": subject,
        "content": [{"type": "text/html", "value": body_html}],
        "attachments": [{
            "content": b64,
            "type": "application/pdf",
            "filename": f"Suitcase - {safe_name}.pdf",
            "disposition": "attachment",
        }],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        raise EmailError(f"email provider rejected the send (HTTP {e.code})")
    except Exception as e:
        raise EmailError(f"email send failed: {type(e).__name__}")
    return {"ok": True, "status": "sent", "recipient": to_addr, "http": code}
