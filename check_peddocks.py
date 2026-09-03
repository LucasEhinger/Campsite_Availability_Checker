"""Check Peddocks Island (Boston Harbor Islands SP) for open yurts / tent sites.

Reads the ReserveAmerica 2-week availability matrix for the campground and
reports any target date where a site is bookable ("A"). Emails via SendGrid
when something opens up.

Usage:
    python check_peddocks.py                      # check the target dates below
    python check_peddocks.py --dry-run            # never send email (local testing)
    python check_peddocks.py --dates 2026-09-19 2026-10-03
"""

import argparse
import datetime
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

# One night each, both Saturdays.
TARGET_DATES = ["2026-09-19", "2026-09-26"]
TARGET_LOOP = "Peddocks Island"
# Y## are the yurts, P## the tent sites -- we want both.
TARGET_TYPES = {"Yurt", "Tent"}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def send_availability_email(subject, message):
    """Send via SendGrid, matching check_brightangel.py's setup."""
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    mail = Mail(
        from_email=os.environ["FROM_EMAIL"],
        to_emails=os.environ["TO_EMAIL"],
        subject=subject,
        html_content=message,
    )
    try:
        sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
        response = sg.send(mail)
        print(f"Email sent! Status code: {response.status_code}")
    except Exception as e:
        print(f"Error sending email: {e}")


def site_type(site_name, icon_src):
    """Yurts are Y01-Y10 (cabin icon); tent sites are P01-P06 (tent icon)."""
    if site_name.upper().startswith("Y") or "cabin" in icon_src or "yurt" in icon_src:
        return "Yurt"
    if site_name.upper().startswith("P") or "tent" in icon_src:
        return "Tent"
    return "Other"


def scrape_window(page, start_date):
    """Load the matrix starting at start_date; return {(site, date): status}.

    The grid shows 14 nights. Column order gives the date, and for bookable
    cells we confirm it against the arvdate in the booking link.
    """
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
                        help="print results, never send email")
    args = parser.parse_args()

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
    header = f"{'Site':6} {'Type':6} " + " ".join(d.strftime("%m/%d") for d in targets)
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
        print(f"{site:6} {kind:6} " + "     ".join(f"{c:1}" for c in cells))

    print("\nA = available, R = reserved, X = not available/closed\n")

    if not hits:
        print("No yurts or tent sites available on target dates.")
        return 0

    lines = "".join(
        f"<li><strong>{site}</strong> ({kind}) &mdash; {date:%A, %B %-d, %Y} (1 night)</li>"
        for site, kind, date in hits
    )
    print("AVAILABILITY FOUND:")
    for site, kind, date in hits:
        print(f"  {site} ({kind}) on {date}")

    if args.dry_run:
        print("\n[dry run] Email not sent.")
        return 0

    send_availability_email(
        "Peddocks Island Campsite Available!",
        f"<strong>Peddocks Island has open sites!</strong><ul>{lines}</ul>"
        f"<a href='{DETAILS_URL}'>Book here</a>",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
