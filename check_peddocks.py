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


def send_email(subject, html):
    """Send via SendGrid. Raises on any failure so CI turns red."""
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    missing = [k for k in ("SENDGRID_API_KEY", "FROM_EMAIL", "TO_EMAIL")
               if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing environment variable(s): {', '.join(missing)}")

    mail = Mail(
        from_email=os.environ["FROM_EMAIL"],
        to_emails=os.environ["TO_EMAIL"],
        subject=subject,
        html_content=html,
    )
    try:
        response = SendGridAPIClient(os.environ["SENDGRID_API_KEY"]).send(mail)
    except Exception as exc:
        hint = ""
        if "401" in str(exc):
            hint = ("\n  -> The SENDGRID_API_KEY is invalid, revoked, or expired. Make a new"
                    "\n     key with 'Mail Send' permission at https://app.sendgrid.com/settings/api_keys"
                    "\n     then run: gh secret set SENDGRID_API_KEY")
        elif "403" in str(exc):
            hint = ("\n  -> SendGrid refused the sender. Verify FROM_EMAIL as a Single Sender"
                    "\n     at https://app.sendgrid.com/settings/sender_auth")
        raise RuntimeError(f"SendGrid send failed: {exc}{hint}") from exc
    if not 200 <= response.status_code < 300:
        raise RuntimeError(
            f"SendGrid rejected the message: HTTP {response.status_code} {response.body}"
        )
    print(f"Email sent to {os.environ['TO_EMAIL']}! Status code: {response.status_code}")


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
        print("Sending test email...")
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
