import os

SENDGRID_API_KEY = os.environ["SENDGRID_API_KEY"]
FROM_EMAIL = os.environ["FROM_EMAIL"]
TO_EMAIL = os.environ["TO_EMAIL"]

from playwright.sync_api import sync_playwright
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

def send_availability_email(subject, message):
    mail = Mail(from_email=FROM_EMAIL, to_emails=TO_EMAIL, subject=subject, html_content=message)
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(mail)
        print(f"Email sent! Status code: {response.status_code}")
    except Exception as e:
        print(f"Error sending email: {e}")


AVAILABILITY_URL = (
    "https://www.recreation.gov/permits/4675337/"
    "registration/detailed-availability?date=2026-03-19"
)
GROUP_SIZE = 2

TARGET_CAMPGROUNDS = ["CBG - Bright Angel Campground", "CIG - Havasupai Gardens Campground"]
TARGET_DATES = ["Sunday, March 22, 2026", "Monday, March 23, 2026", "Friday, March 27, 2026"]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()

    # --- Open page ---
    page.goto(AVAILABILITY_URL)

    # --- Select South Rim route ---
    page.get_by_role("button", name="Classic GC Hike - via South").click()

    # --- Open group size modal ---
    page.get_by_role("button", name="Add Group Members...").click()

    # --- Increase group size to desired value ---
    plus_button = page.get_by_role("button", name="Add Peoples")
    for _ in range(GROUP_SIZE - 1):
        plus_button.click()

    # --- Close modal ---
    page.get_by_role("button", name="Close").click()

    # --- Wait for campground rows ---
    page.wait_for_selector('[data-testid="division-availability-row"]', timeout=20000)

    # --- Extract date headers ---
    headers = page.locator("div[role='columnheader']").all()
    dates = []
    for h in headers:
        sr_only = h.locator(".rec-sr-only")
        if sr_only.count() > 0:
            dates.append(sr_only.inner_text().strip())

    # --- Prepare table data ---
    rows = page.locator('[data-testid="division-availability-row"]').all()
    table_data = []

    for row in rows:
        cells = row.locator('[data-component="GridCell"]').all()

        # Site name
        site_name = (
            cells[0].locator("span.sarsa-button-content").first.inner_text(timeout=0).strip()
        )

        # Area
        area = cells[1].inner_text(timeout=0).strip()

        # Availability cells
        availability_cells = row.locator('[data-testid="division-availability-cell"]').all()
        availability = []
        for cell in availability_cells:
            btns = cell.locator("button")
            if btns.count() > 0:
                btn = btns.first
                count = btn.inner_text(timeout=0).strip()
            else:
                count = "0"

            classes = cell.get_attribute("class") or ""
            if "available" in classes and count.isdigit():
                availability.append(count)
            else:
                availability.append("0")

        # Pad availability to match number of headers
        if len(availability) < len(dates):
            availability += ["0"] * (len(dates) - len(availability))

        table_data.append({"site_name": site_name, "area": area, "availability": availability})

    # --- Print aligned table ---
    col_widths = [35] + [10] * len(dates)  # site column + one per date
    header_row = ["Site (Area)"] + dates
    row_format = "".join(f"{{:<{w}}}" for w in col_widths)
    print("\n=== AVAILABILITY TABLE ===\n")
    print(row_format.format(*header_row))
    print("-" * sum(col_widths))

    for entry in table_data:
        row_text = [f"{entry['site_name']} ({entry['area']})"] + entry["availability"]
        # Pad row_text to prevent IndexError
        if len(row_text) < len(header_row):
            row_text += [""] * (len(header_row) - len(row_text))
        print(row_format.format(*row_text))

    # --- Check target campgrounds for availability ---
    availability_found = False
    for entry in table_data:
        if entry["site_name"] in TARGET_CAMPGROUNDS:
            for idx, date in enumerate(dates):
                if (
                    date in TARGET_DATES
                    and entry["availability"][idx].isdigit()
                    and int(entry["availability"][idx]) > 0
                ):
                    availability_found = True
                    break

    if availability_found:
        print("\nAVAILABILITY\n")
        send_availability_email(
            "Grand Canyon Campsite Available!",
            "<strong>CBG or CIG has available sites on March 22 or 23!</strong><br><br><a href='https://www.recreation.gov/permits/4675337/registration/detailed-availability?date=2026-03-19'>Check availability here</a>",
        )
    else:
        print("\nNo availability on target dates.\n")

    # input("Press Enter to close the browser...")
    browser.close()

