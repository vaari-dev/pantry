"""Log in to the shop once on your own computer and save the session for the box.

    pip install playwright && playwright install chromium
    SHOP_URL=https://... python scripts/login.py session.json
    gh secret set SHOP_SESSION --env oracle-cloud < session.json && rm session.json
    gh workflow run operate.yml -f action=set-login      # puts it on the box

A browser window opens: log in (phone number + OTP) and make sure the delivery address at the
top is the right one. The window closes by itself once you are logged in.
"""

import os
import sys
import time

from playwright.sync_api import sync_playwright


def main() -> None:
    url = os.environ.get("SHOP_URL")
    if len(sys.argv) != 2 or not url:
        sys.exit(__doc__)
    out = sys.argv[1]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(url)
        print("Log in in the browser window and pick the delivery address...")
        address = page.locator("[data-testid=user-address]").first
        logged_out = page.locator("[data-testid=login-btn]")
        while logged_out.count() or not address.count() or address.inner_text().startswith("Select Location"):
            time.sleep(2)
        time.sleep(3)  # let the session settle
        ctx.storage_state(path=out)
        browser.close()
    os.chmod(out, 0o600)
    print(f"Saved to {out}. It is your shop session: treat it like a password and delete it once uploaded.")


if __name__ == "__main__":
    main()
