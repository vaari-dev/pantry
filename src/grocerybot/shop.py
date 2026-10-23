"""Everything that touches the shop's website, through a real (non-headless) Chrome on a virtual screen.

The site refuses headless browsers and data-centre IPs, so Chrome runs normally under Xvfb and,
on a server, sends its traffic through the home connection (a Tailscale exit node, see
docker/entrypoint.sh). Products are opened by their saved links, so no feed, ads or upsells are
browsed; analytics/ad scripts and media are not even loaded.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Page, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .catalog import Product

TIMEOUT_MS = 90_000  # pages are slower through the home exit node
CHROME_ARGS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--no-first-run",
    "--mute-audio",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-default-apps",
]
SKIP_TYPES = frozenset({"media", "font"})
SKIP_URLS = (
    "googletagmanager.com",
    "google-analytics.com",
    "doubleclick.net",
    "facebook.",
    "google.com/ccm",
    "google.com/rmkt",
    "google.com/pagead",
    "google.co.in/ads",
    "faro.",  # the site's own telemetry and event beacons
    "publish-events",
    "~partytown",
    "/proxy/",  # third-party scripts the site relays through itself
)
WALLET = re.compile(r"\bCash\b")  # the prepaid wallet: the only way the bot pays
COINS = re.compile(r"\bCoins\b")  # reward coins: never used
LOGGED_OUT = "[data-testid=login-btn]"
PLUS = "button[aria-label='Increase quantity by one']:not([data-hidden=true])"
log = logging.getLogger(__name__)


class ShopError(Exception):
    """Stops an order; the message is safe to show to the family."""


@dataclass(frozen=True)
class Rules:
    address_label: str
    address_match: str
    min_order: float
    budget: float  # what is left of today's cap


@dataclass
class Cart:
    total: float | None = None  # paid from the wallet
    items: list[tuple[str, int]] = field(default_factory=list)  # product name, quantity
    problems: list[str] = field(default_factory=list)


def _money(text: str) -> float:
    return float(text.replace(",", ""))


def _squash(text: str) -> str:
    return " ".join(text.split()).lower()


class Browser:
    """One Chrome session with the saved shop login. Use as a context manager."""

    def __init__(self, base_url: str, state_file: Path, proxy: str | None, debug_dir: Path, images: bool = False):
        self.base = base_url.rstrip("/")
        self.state_file, self.proxy, self.debug_dir = state_file, proxy, debug_dir
        self.skip_types = SKIP_TYPES if images else SKIP_TYPES | {"image"}

    def __enter__(self) -> "Browser":
        if not self.state_file.exists():
            raise ShopError("not logged in to the shop yet (see README: Shop login)")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=False, args=CHROME_ARGS, proxy={"server": self.proxy} if self.proxy else None
        )
        self._ctx = self._browser.new_context(
            storage_state=str(self.state_file), viewport={"width": 1024, "height": 700}
        )
        self._ctx.route("**/*", self._filter)
        self.page: Page = self._ctx.new_page()
        self.page.set_default_timeout(TIMEOUT_MS)
        return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type:
                self.screenshot("error")
            self._ctx.storage_state(path=str(self.state_file))  # keep refreshed cookies
            self.state_file.chmod(0o600)
        finally:
            self._browser.close()
            self._pw.stop()

    def _filter(self, route: Route):
        req = route.request
        if req.resource_type in self.skip_types or any(s in req.url for s in SKIP_URLS):
            return route.abort()
        return route.continue_()

    def screenshot(self, tag: str) -> None:
        """Only for failures (screens show the address); old ones are pruned by the bot."""
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(self.debug_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{tag}.png"))
        except Exception:  # never mask the real error
            log.warning("screenshot failed", exc_info=True)

    def goto(self, url: str) -> None:
        try:
            resp = self.page.goto(url if url.startswith("http") else self.base + url, wait_until="domcontentloaded")
        except PlaywrightTimeout as e:
            raise ShopError("the shop did not load; is the home connection (exit node) up?") from e
        if resp is not None and resp.status in (403, 429):
            raise ShopError(f"the shop refused the connection (HTTP {resp.status})")
        self.page.wait_for_timeout(2000)

    # ------------------------------------------------------------- session

    def check_session(self, rules: Rules) -> str:
        """Logged in and delivering to the family address. Works on any page of the site."""
        if not self.page.url.startswith(self.base):
            self.goto("/")
        addr = self.page.locator("[data-testid=user-address]").first
        try:
            addr.wait_for(timeout=20_000)
        except PlaywrightTimeout:
            if self.page.locator(LOGGED_OUT).count():
                raise ShopError("the shop login has expired") from None
            raise ShopError("could not read the delivery address") from None
        text = addr.inner_text()
        if not text.startswith(rules.address_label) or rules.address_match not in text:
            raise ShopError(f"the shop is not set to deliver to '{rules.address_label}'")
        return text

    def public_ip(self) -> str:
        self.goto("https://api.ipify.org")
        return self.page.locator("body").inner_text().strip()

    # ------------------------------------------------------------- search

    def search(self, query: str, limit: int = 4) -> list[Product]:
        """Top in-stock, non-sponsored results, with photo, size and price."""
        self.goto(f"/search?query={quote(query)}")
        try:
            self.page.wait_for_selector("a[data-testid=product-card]", timeout=15_000)
        except PlaywrightTimeout:
            return []
        self.page.wait_for_timeout(1500)  # let card images resolve
        cards = self.page.evaluate("""() => [...document.querySelectorAll('a[data-testid=product-card]')].map(a => ({
            url: a.getAttribute('href'),
            image: (a.querySelector('img') || {}).currentSrc || '',
            lines: a.innerText.split('\\n').map(s => s.trim()).filter(Boolean)}))""")
        found, seen = [], set()
        for c in cards:
            lines = c["lines"]
            if (
                c["url"] in seen
                or {"Ad", "Sponsored"} & set(lines)
                or any(re.fullmatch(r"(?i)sold out|notify( me)?|out of stock", line) for line in lines)
            ):
                continue
            body = [line for line in lines if not re.fullmatch(r"ADD|₹\s?[\d,.]+|[\d,.]+|OFF|₹", line)]
            if len(body) < 2:
                continue
            seen.add(c["url"])
            price = next((line.replace(" ", "") for line in lines if re.fullmatch(r"₹\s?[\d,.]+", line)), "")
            image = "" if "placeholder" in c["image"].lower() else c["image"].replace("f-auto", "f-jpg")
            found.append(Product(body[0], body[1], c["url"], price, image))
            if len(found) == limit:
                break
        return found

    # ------------------------------------------------------------- cart

    def add(self, product: Product, qty: int) -> str | None:
        """Add a product from its own page. Returns None on success, else why it was skipped."""
        self.goto(product.url)
        # Titles drop the "| tagline" of long names, so compare the core name.
        if not _squash(self.page.title()).startswith(_squash(product.name.split("|")[0])):
            return "listing changed"
        if _squash(product.size) not in _squash(self.page.locator("body").inner_text()):
            return "size changed"
        add = self.page.locator(PLUS, has_text="Add to Cart")
        if not add.count():
            return "out of stock"
        add.first.evaluate("b => b.click()")  # the sticky search bar overlaps it for mouse clicks
        self.page.wait_for_timeout(1500)
        for _ in range(qty - 1):
            self.page.locator(PLUS).first.evaluate("b => b.click()")
            self.page.wait_for_timeout(1000)
        return None

    def _open_cart(self) -> dict:
        self.page.locator("[data-testid=cart-btn]").first.click()
        self.page.wait_for_selector("[data-testid=cart-screen]")
        self.page.wait_for_timeout(1500)
        return self.page.evaluate("""() => {
          const cart = document.querySelector('[data-testid=cart-screen]');
          const items = [...cart.querySelectorAll('[data-testid$=cart-qty]')].map(q => {
            let row = q;  // largest ancestor holding only this item's stepper
            while (row.parentElement && row.parentElement !== cart &&
                   row.parentElement.querySelectorAll('[data-testid$=cart-qty]').length === 1) row = row.parentElement;
            const lines = row.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
            return {qty: parseInt(q.innerText, 10), lines};
          });
          const boxes = [...cart.querySelectorAll('input[type=checkbox]')].map(i => ({checked: i.checked,
            label: (i.closest('label') || i.parentElement.parentElement).innerText.replace(/\\s+/g, ' ')}));
          return {text: cart.innerText, items, boxes};
        }""")

    def empty_cart(self) -> None:
        if not self.page.locator("[data-testid=cart-items-number]").count():
            return
        self._open_cart()
        minus = self.page.locator("[data-testid=cart-screen] [data-testid$=minus-btn]")
        while minus.count():
            minus.first.click()
            self.page.wait_for_timeout(700)
        self.page.keyboard.press("Escape")

    def review_cart(self, expected: dict[str, int], rules: Rules) -> Cart:
        """Open the cart, pay only from the wallet, and check every rule. Never adds anything."""
        cart = self._open_cart()
        for i, box in enumerate(cart["boxes"]):  # wallet on, reward coins off
            want = bool(WALLET.search(box["label"]))
            if (want or COINS.search(box["label"])) and box["checked"] != want:
                self.page.locator("[data-testid=cart-screen] input[type=checkbox]").nth(i).click(force=True)
                self.page.wait_for_timeout(1500)
                cart = self._open_cart()

        text, result, seen = cart["text"], Cart(), set()
        if not text.startswith(rules.address_label) or rules.address_match not in text:
            result.problems.append(f"the cart is not delivering to '{rules.address_label}'")
        for item in cart["items"]:
            name = next((n for n in expected for line in item["lines"] if _squash(line) == _squash(n)), None)
            if name is None:
                result.problems.append(f"unexpected item in the cart: {' '.join(item['lines'][:2])}")
            elif item["qty"] != expected[name]:
                result.problems.append(f"{name}: {item['qty']} in the cart, expected {expected[name]}")
            else:
                seen.add(name)
                result.items.append((name, item["qty"]))
        result.problems += [f"{n} did not make it into the cart" for n in expected.keys() - seen]
        for box in cart["boxes"]:
            if COINS.search(box["label"]) and box["checked"]:
                result.problems.append("reward coins are selected")
            if WALLET.search(box["label"]) and not box["checked"]:
                result.problems.append("the wallet is not selected")

        amount = lambda label: re.search(label + r"\s*\n(?:-?₹[\d,.]+\s*\n)?-?₹([\d,.]+)", text)  # noqa: E731
        item_total, to_pay, cash = amount("Item Total"), amount("To Pay"), amount(r"[^\n]*\bCash")
        if not item_total or _money(item_total.group(1)) < rules.min_order:
            result.problems.append(f"orders under ₹{rules.min_order:g} get a delivery fee")
        if not to_pay or _money(to_pay.group(1)) != 0:
            result.problems.append("the wallet does not cover the whole bill")
        result.total = _money(cash.group(1)) if cash else None
        if result.total is None:
            result.problems.append("could not read the amount paid from the wallet")
        elif result.total > rules.budget:
            result.problems.append(f"₹{result.total:g} is more than what is left of today's limit (₹{rules.budget:g})")
        return result

    def place(self) -> None:
        """Tap Place Order; confirm the "Missed something?" upsell pop-up without adding anything."""
        self.page.locator("[data-testid=cart-screen] button", has_text="Place Order").click()
        self.page.wait_for_timeout(3000)
        if self.page.get_by_text("Missed something?", exact=True).count():
            self.page.evaluate("""() => {
                const pop = [...document.querySelectorAll('[role=dialog], div')].find(d =>
                    d.innerText.startsWith('Missed something?') && d.querySelector('button'));
                [...pop.querySelectorAll('button')].find(b => b.innerText.trim() === 'Place Order').click();
            }""")
        self.page.wait_for_timeout(15_000)  # "Confirming your order..."

    def confirm(self, total: float, since: datetime) -> bool:
        """The newest order on the Orders page has this total and was placed after `since`."""
        for _ in range(3):
            self.goto("/account/orders")
            body = self.page.locator("body").inner_text().split("Log Out", 1)[-1]
            m = re.search(r"Order [^\n]+?\s+₹([\d,.]+)\s+Placed at (\d+)\w* (\w+ \d{4}), (\d+:\d+ [ap]m)", body)
            if m:
                placed = datetime.strptime(f"{m.group(2)} {m.group(3)} {m.group(4)}", "%d %b %Y %I:%M %p")
                if _money(m.group(1)) == total and placed >= since - timedelta(minutes=1):
                    return True
            self.page.wait_for_timeout(5000)
        return False
