"""Save a shop login for the bot (README: Shop login).

    SHOP_URL=https://... python scripts/login.py session.json

Log in (phone + OTP) in the window; it closes once logged in.
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
        print("Log in in the browser window...")
        address = page.locator("[data-testid=user-address]").first
        logged_out = page.locator("[data-testid=login-btn]")
        while logged_out.count() or not address.count() or address.inner_text().startswith("Select Location"):
            time.sleep(2)
        time.sleep(3)
        ctx.storage_state(path=out)
        browser.close()
    os.chmod(out, 0o600)
    print(f"Saved {out}. Treat it as a password; delete it after uploading.")


if __name__ == "__main__":
    main()
