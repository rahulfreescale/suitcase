"""Security test for the trip-sharing email feature (a PRIVILEGED action).

Proves the controls that make sending safe:
  1. Recipient validation (tool-validation): single, well-formed address only;
     rejects multi-recipient lists and email-header-injection attempts.
  2. Human-in-the-loop: send_itinerary refuses unless confirm=True.
  3. Least privilege: the body is built in code from the itinerary, so injected
     content can't become an arbitrary outbound message.

Usage:  python3 -m eval.test_email_security
No API key, no network needed (we never reach the actual send).
"""
from app.services.email_sender import validate_recipient, send_itinerary, EmailError


def _check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    return cond


def main():
    print("== Email feature security ==\n")
    ok = True

    print("recipient validation (tool-validation):")
    for good in ("alex@example.com", "a.b+tag@mail.co.uk"):
        try:
            validate_recipient(good); ok &= _check(f"accepts valid {good}", True)
        except EmailError:
            ok &= _check(f"accepts valid {good}", False)
    for bad, why in [
        ("x@y.com, z@evil.com", "multi-recipient list"),
        ("a@b.com; b@c.com", "semicolon list"),
        ("a@b.com\nBcc: evil@x.com", "header injection"),
        ("notanemail", "malformed"),
        ("<script>@x.com", "html/junk"),
        ("", "empty"),
    ]:
        try:
            validate_recipient(bad); ok &= _check(f"rejects {why}", False)
        except EmailError:
            ok &= _check(f"rejects {why}", True)

    print("\nhuman-in-the-loop gate:")
    try:
        send_itinerary("alex@example.com", "Rome", [], confirm=False)
        ok &= _check("refuses to send without confirm=True", False)
    except EmailError as e:
        ok &= _check("refuses to send without confirm=True", "confirm" in str(e).lower())

    print("\nleast-privilege (body built in code, not passed in):")
    # send_itinerary takes days (structured data), NOT a raw html/body string —
    # there's no parameter through which arbitrary message content can be injected.
    import inspect
    sig = inspect.signature(send_itinerary)
    params = set(sig.parameters)
    ok &= _check("no raw body/html parameter exists",
                 not ({"body", "html", "message", "content"} & params))
    ok &= _check("only structured itinerary inputs",
                 {"recipient", "destination", "days", "confirm"}.issubset(params))

    print("\n" + ("ALL PASS — email action is validated, gated, least-privilege."
                  if ok else "SOME FAILED — review above."))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
