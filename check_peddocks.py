"""Check Peddocks Island (Boston Harbor Islands SP) for open yurts / tent sites.

Reads the ReserveAmerica 2-week availability matrix for the campground and
reports any target date where a site is bookable ("A"). Emails via SendGrid
when something opens up, remembering what it already reported so the same
opening is not emailed twice.

Usage:
    python check_peddocks.py                      # check the target dates below
    python check_peddocks.py --dry-run            # print results, never email
    python check_peddocks.py --test-email         # send a test email, then exit
    python check_peddocks.py --dates 2026-09-19 2026-10-03
    python check_peddocks.py --reset-state        # forget what was already sent
"""

import argparse
import datetime
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

CONTRACT_CODE = "MA"
PARK_ID = "32603"

MATRIX_URL = (
    "https://massdcrcamping.reserveamerica.com/camping/boston-harbor-islands-sp/r/"
    "campsiteCalendar.do?page=matrix&calarvdate={date}"
    f"&contractCode={CONTRACT_CODE}&parkId={PARK_ID}"
)
DETAILS_URL = (
    "https://massdcrcamping.reserveamerica.com/camping/boston-harbor-islands-sp/r/"
    f"campgroundDetails.do?contractCode={CONTRACT_CODE}&parkId={PARK_ID}#sr_a"
)

# One night each. Peddocks only offers Thu/Fri/Sat nights -- Sun-Wed always
# show X (closed), so a Sunday target can never turn available.
TARGET_DATES = [
    "2026-09-11",  # Fri
    "2026-09-12",  # Sat
    "2026-09-13",  # Sun -- closed night, watched anyway
    "2026-09-19",  # Sat
    "2026-09-26",  # Sat
]
TARGET_LOOP = "Peddocks Island"
TARGET_TYPES = {"Yurt", "Tent"}

# Remembers which (site, date) openings were already emailed, so a site that
# stays open for hours only notifies once. Persisted across CI runs by the
# actions/cache step in .github/workflows/check_peddocks.yml.
DEFAULT_STATE_FILE = ".peddocks_state.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def mail_backend():
    """SMTP wins when configured; SendGrid stays as a fallback."""
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"):
        return "smtp"
    if os.environ.get("SENDGRID_API_KEY"):
        return "sendgrid"
    return None


def describe_email_env():
    """Report the shape of the mail secrets without revealing them.

    Log output is readable by anyone who can see the repo, so this prints
    only lengths and shape checks -- never the values themselves.
    """
    backend = mail_backend()
    print(f"Mail backend: {backend or 'NONE CONFIGURED'}")
    names = (["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "TO_EMAIL"]
             if backend == "smtp"
             else ["SENDGRID_API_KEY", "FROM_EMAIL", "TO_EMAIL"])
    for name in names:
        raw = os.environ.get(name)
        if not raw:
            default = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587"}.get(name)
            print(f"  {name}: not set" + (f" (defaulting to {default})" if default else ""))
            continue
        notes = []
        if raw != raw.strip():
            notes.append("HAS SURROUNDING WHITESPACE (likely the problem)")
        if name == "SMTP_PASS":
            stripped = raw.replace(" ", "").strip()
            notes.append(f"{len(stripped)} chars once spaces are removed")
            if len(stripped) != 16:
                notes.append("NOTE: a Gmail app password is 16 characters")
        elif name == "SENDGRID_API_KEY":
            notes.append("starts with 'SG.'" if raw.startswith("SG.")
                         else "does NOT start with 'SG.'")
        elif name in ("FROM_EMAIL", "TO_EMAIL", "SMTP_USER"):
            notes.append("looks like an address" if "@" in raw and "." in raw.split("@")[-1]
                         else "does NOT look like an email address")
        else:
            notes.append(raw)
        print(f"  {name}: {len(raw)} chars, " + ", ".join(notes))


def probe_sendgrid_key():
    """Ask SendGrid what this key can do -- distinguishes a dead key from a
    live key that merely lacks Mail Send permission."""
    import urllib.error
    import urllib.request

    key = os.environ.get("SENDGRID_API_KEY", "").strip()
    if not key or mail_backend() != "sendgrid":
        return
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/scopes",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        scopes = payload.get("scopes", [])
        print(f"  key probe: HTTP {resp.status} -- key is VALID, {len(scopes)} scope(s)")
        print(f"  mail.send permission: {'YES' if 'mail.send' in scopes else 'NO -- this is the problem'}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        print(f"  key probe: HTTP {exc.code} -- {detail or '(empty body)'}")
    except Exception as exc:
        print(f"  key probe: could not reach SendGrid ({exc})")


def _send_via_smtp(subject, html):
    """Send through any SMTP provider. Defaults to Gmail."""
    import smtplib
    import ssl
    from email.message import EmailMessage

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.environ.get("SMTP_PORT", "587").strip() or 587)
    user = os.environ["SMTP_USER"].strip()
    # Google displays app passwords in groups of four; the spaces are cosmetic.
    password = os.environ["SMTP_PASS"].replace(" ", "").strip()
    recipient = (os.environ.get("TO_EMAIL") or user).strip()
    # Gmail rewrites (or rejects) a From that isn't the authenticated account.
    sender = user if "gmail" in host else (os.environ.get("FROM_EMAIL") or user).strip()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content("Peddocks Island availability alert -- view in HTML.")
    message.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(user, password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            f"SMTP login rejected by {host}: {exc}\n"
            "  -> For Gmail this must be a 16-character App Password, not your\n"
            "     normal password, and 2-Step Verification must be on.\n"
            "     Create one at https://myaccount.google.com/apppasswords\n"
            "     then run: gh secret set SMTP_PASS"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"SMTP send via {host}:{port} failed: {exc}") from exc
    print(f"Email sent to {recipient} via {host}.")


def _send_via_sendgrid(subject, html):
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    mail = Mail(
        from_email=os.environ["FROM_EMAIL"],
        to_emails=os.environ["TO_EMAIL"],
        subject=subject,
        html_content=html,
    )
    try:
        response = SendGridAPIClient(os.environ["SENDGRID_API_KEY"]).send(mail)
    except Exception as exc:
        body = getattr(exc, "body", None)
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", "replace")
        if body:
            print(f"SendGrid said: {body}")
        hint = ""
        if body and "credits" in str(body).lower():
            hint = ("\n  -> The SendGrid account is out of sending credits (expired trial).\n"
                    "     Switch to SMTP by setting SMTP_USER and SMTP_PASS.")
        elif "401" in str(exc):
            hint = "\n  -> SENDGRID_API_KEY is invalid or revoked."
        elif "403" in str(exc):
            hint = "\n  -> Verify FROM_EMAIL as a Single Sender in SendGrid."
        raise RuntimeError(f"SendGrid send failed: {exc}{hint}") from exc
    if not 200 <= response.status_code < 300:
        raise RuntimeError(
            f"SendGrid rejected the message: HTTP {response.status_code} {response.body}"
        )
    print(f"Email sent to {os.environ['TO_EMAIL']}! Status code: {response.status_code}")


def send_email(subject, html):
    """Send the alert through whichever backend is configured."""
    backend = mail_backend()
    if backend == "smtp":
        return _send_via_smtp(subject, html)
    if backend == "sendgrid":
        return _send_via_sendgrid(subject, html)
    raise RuntimeError(
        "No mail backend configured. Set SMTP_USER + SMTP_PASS (recommended), "
        "or SENDGRID_API_KEY + FROM_EMAIL + TO_EMAIL."
    )


def load_state(path):
    try:
        with open(path) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"notified": {}}
    state.setdefault("notified", {})
    return state


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def site_type(site_name, icon_src):
    """Yurts are Y01-Y10 (cabin icon); tent sites are P01-P06 (tent icon)."""
    if site_name.upper().startswith("Y") or "cabin" in icon_src or "yurt" in icon_src:
        return "Yurt"
    if site_name.upper().startswith("P") or "tent" in icon_src:
        return "Tent"
    return "Other"


def scrape_window(page, start_date):
    """Load the matrix starting at start_date; return {(site, date): info}."""
    page.goto(MATRIX_URL.format(date=start_date.strftime("%m/%d/%Y")),
              timeout=60000, wait_until="domcontentloaded")
    page.wait_for_selector("#calendar .br", timeout=30000)

    grid = {}
    for row in page.locator("#calendar .br").all():
        label = row.locator(".siteListLabel")
        if not label.count():
            continue
        site = label.inner_text(timeout=5000).strip()
        loop = (row.locator(".loopName").inner_text(timeout=5000).strip()
                if row.locator(".loopName").count() else "")
        icon = row.locator(".td.sn img")
        icon_src = icon.first.get_attribute("src") or "" if icon.count() else ""

        for idx, cell in enumerate(row.locator(".td.status").all()):
            date = start_date + datetime.timedelta(days=idx)
            status = (cell.inner_text(timeout=5000) or "").strip().upper()

            link = cell.locator("a.avail")
            if link.count():
                # Authoritative date straight from the booking link.
                href = link.first.get_attribute("href") or ""
                m = re.search(r"arvdate=(\d{1,2})/(\d{1,2})/(\d{4})", href)
                if m:
                    linked = datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                    if linked != date:
                        print(f"  ! column/date mismatch for {site}: "
                              f"col={date} link={linked}; trusting link")
                        date = linked

            grid[(site, date)] = {
                "status": status,
                "loop": loop,
                "type": site_type(site, icon_src),
                "available": status == "A" or link.count() > 0,
            }
    return grid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dates", nargs="+", default=TARGET_DATES,
                        help="YYYY-MM-DD nights to check (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print results, never send email or save state")
    parser.add_argument("--test-email", action="store_true",
                        help="send a test email to prove delivery works, then exit")
    parser.add_argument("--state", default=DEFAULT_STATE_FILE,
                        help="file remembering already-sent alerts (default: %(default)s)")
    parser.add_argument("--reset-state", action="store_true",
                        help="clear remembered alerts before running")
    args = parser.parse_args()

    if args.test_email:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        describe_email_env()
        probe_sendgrid_key()
        print("\nSending test email...")
        send_email(
            "Peddocks alert test -- delivery is working",
            f"<strong>This is a test.</strong><br><br>Your Peddocks Island watcher "
            f"can reach your inbox. Sent {now}.<br><br>Real alerts will name the "
            f"site and date, and link to <a href='{DETAILS_URL}'>the booking page</a>.",
        )
        return 0

    if args.reset_state and os.path.exists(args.state):
        os.remove(args.state)
        print(f"Cleared {args.state}")

    targets = sorted(datetime.date.fromisoformat(d) for d in args.dates)

    grid = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=USER_AGENT).new_page()
        # Each load covers 14 nights, so only fetch a new window when a
        # target date isn't already on screen.
        for target in targets:
            if any(site_date == target for _, site_date in grid):
                continue
            print(f"Loading 2-week window starting {target}...")
            grid.update(scrape_window(page, target))
        browser.close()

    sites = sorted({site for site, _ in grid})
    if not sites:
        print("No sites parsed -- the page layout may have changed.")
        return 2

    print("\n=== PEDDOCKS ISLAND AVAILABILITY ===\n")
    header = f"{'Site':<6}{'Type':<6}" + "".join(f"{d:%m/%d}".rjust(7) for d in targets)
    print(header)
    print("-" * len(header))

    hits = []
    for site in sites:
        cells = []
        for target in targets:
            info = grid.get((site, target))
            cells.append(info["status"] if info else "-")
            if info and info["available"] and info["type"] in TARGET_TYPES \
                    and TARGET_LOOP.lower() in info["loop"].lower():
                hits.append((site, info["type"], target))
        info_any = next((grid[(site, t)] for t in targets if (site, t) in grid), None)
        kind = info_any["type"] if info_any else "?"
        print(f"{site:<6}{kind:<6}" + "".join(c.rjust(7) for c in cells))

    print("\nA = available, R = reserved, X = not available/closed\n")

    # --- Deduplicate against what was already emailed ---------------------
    state = load_state(args.state)
    notified = state["notified"]
    current = {f"{site}|{date.isoformat()}": (site, kind, date) for site, kind, date in hits}

    # Forget openings that are gone, so if one comes back it alerts again.
    for key in list(notified):
        if key not in current:
            del notified[key]

    fresh = [current[k] for k in current if k not in notified]

    if not hits:
        print("No yurts or tent sites available on target dates.")
    else:
        print("Currently available:")
        for key, (site, kind, date) in sorted(current.items()):
            mark = "NEW" if key not in notified else "already emailed"
            print(f"  {site} ({kind}) on {date}  [{mark}]")

    if hits and not fresh:
        print("\nNothing new since the last alert -- not emailing again.")

    if fresh:
        lines = "".join(
            f"<li><strong>{site}</strong> ({kind}) &mdash; {date:%A, %B %-d, %Y} (1 night)</li>"
            for site, kind, date in sorted(fresh, key=lambda h: (h[2], h[0]))
        )
        print(f"\nEmailing about {len(fresh)} new opening(s)...")
        if args.dry_run:
            print("[dry run] Email not sent.")
        else:
            send_email(
                "Peddocks Island Campsite Available!",
                f"<strong>Peddocks Island has open sites!</strong><ul>{lines}</ul>"
                f"<a href='{DETAILS_URL}'>Book here</a>"
                f"<br><br><small>You get one email per opening; book fast.</small>",
            )
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for key in current:
            notified.setdefault(key, now)

    if args.dry_run:
        print("[dry run] State not saved.")
    else:
        save_state(args.state, state)
        print(f"State saved to {args.state} ({len(notified)} remembered opening(s)).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail loudly so the workflow goes red
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
