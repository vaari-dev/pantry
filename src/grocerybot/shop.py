"""The shop's website, through real Chrome: the site refuses headless browsers and data-centre IPs."""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import structlog
from playwright.sync_api import Error as PlaywrightError
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
    "faro.",
    "publish-events",
    "~partytown",
    "/proxy/",
)
WALLET = re.compile(r"\bCash\b")  # the prepaid wallet: the only way the bot pays
COINS = re.compile(r"\bCoins\b")  # reward coins: never used
LOGGED_OUT = "[data-testid=login-btn]"
ADDRESS = "[data-testid=user-address]"
PHONE_FIELD = "input[type=tel], input[inputmode=numeric], input[name*=phone i]"
OTP_FIELD = "input[autocomplete=one-time-code], input[maxlength='1'], input[maxlength='4'], input[maxlength='6']"
# Playwright runs these in the browser: flags must be re flags, not inline (?i).
CONTINUE = re.compile(r"^\s*(continue|get otp|send otp|login|next)\s*$", re.I)
VERIFY = re.compile(r"^\s*(verify|continue|login|submit)", re.I)
PLUS = "button[aria-label='Increase quantity by one']:not([data-hidden=true])"
MODAL = "[id^=zui-modal], [role=dialog], [aria-modal=true]"  # the address picker; it covers the page
PICKER_BUTTON = f"button:has({ADDRESS}), {ADDRESS}"  # the header button around the address opens it
# The picker's markup has no stable ids: a saved address is the innermost clickable block of two or
# more lines with a digit or comma after its name. Tags each with data-saved-address=<n>.
SAVED_ADDRESSES = """() => {
    const scope = [...document.querySelectorAll(MODAL)].filter(el => el.checkVisibility()).pop();
    if (!scope) return [];  // never the page itself: product cards behind it look like addresses
    const header = document.querySelector('[data-testid=user-address]');
    const skip = /add (a )?new address|current location|detect my location/i;
    const lines = el => el.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
    const clickable = el => el.matches('button, a, [role=button], [tabindex]')
        || getComputedStyle(el).cursor === 'pointer';
    const found = [...scope.querySelectorAll('*')].filter(el => {
        if (!(el instanceof HTMLElement)) return false;  // svg icons have no innerText
        if (!el.checkVisibility() || !clickable(el) || header?.contains(el) || el.contains(header)) return false;
        if (el.querySelector('input') || skip.test(el.innerText)) return false;
        const l = lines(el);
        return l.length >= 2 && el.innerText.length < 400 && /[\\d,]/.test(l.slice(1).join(' '));
    });
    const cards = found.filter(el => !found.some(o => o !== el && el.contains(o)));
    document.querySelectorAll('[data-saved-address]').forEach(el => el.removeAttribute('data-saved-address'));
    return cards.map((el, n) => { el.setAttribute('data-saved-address', n); return lines(el).join('\\n'); });
}""".replace("MODAL", repr(MODAL))
# The picker's markup with its text masked like _mask; structural attribute values kept, others emptied.
MASKED_PICKER = """m => {
    const keep = new Set(['class', 'id', 'role', 'tabindex', 'type', 'data-testid', 'aria-modal']);
    const mask = s => s.replace(/\\p{L}/gu, 'x').replace(/\\p{N}/gu, '0');
    const root = ([...document.querySelectorAll(m)].pop() || document.body).cloneNode(true);
    const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walk.nextNode()) walk.currentNode.nodeValue = mask(walk.currentNode.nodeValue);
    for (const el of [root, ...root.querySelectorAll('*')])
        for (const a of el.attributes) if (!keep.has(a.name)) a.value = '';
    return root.outerHTML;
}"""
LOG_PART = 10_000  # characters; JSON escaping grows them
log = structlog.get_logger(__name__)


class ShopError(Exception):
    pass


@dataclass(frozen=True)
class Rules:
    address_label: str
    address_match: str
    budget: float


@dataclass
class Cart:
    total: float | None = None
    item_total: float = 0
    items: list[tuple[str, int]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _money(text: str) -> float:
    return float(text.replace(",", ""))


def _mask(value):
    """Keeps the shape of JSON, not its content: letters become x, digits 0; keys, booleans and nulls stay."""
    if isinstance(value, dict):
        return {k: _mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"\d", "0", re.sub(r"[^\W\d_]", "x", value))
    if isinstance(value, bool) or value is None:
        return value
    return 0


def _squash(text: str) -> str:
    return " ".join(text.split()).lower()


class Browser:
    """One Chrome session. Loads and saves state_file (None: neither); fresh: starts logged out."""

    def __init__(
        self,
        base_url: str,
        state_file: Path | None,
        proxy: str | None,
        debug_dir: Path,
        images: bool = False,
        fresh: bool = False,
    ):
        self.base = base_url.rstrip("/")
        self.state_file, self.proxy, self.debug_dir, self.fresh = state_file, proxy, debug_dir, fresh
        self.skip_types = SKIP_TYPES if images else SKIP_TYPES | {"image"}

    def __enter__(self) -> "Browser":
        stored = self.state_file is not None and not self.fresh
        if stored and not self.state_file.exists():
            raise ShopError("this account is not logged in to the shop; its owner can /setup again")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=False, args=CHROME_ARGS, proxy={"server": self.proxy} if self.proxy else None
        )
        self._ctx = self._browser.new_context(
            storage_state=str(self.state_file) if stored else None, viewport={"width": 1024, "height": 700}
        )
        self._ctx.route("**/*", self._filter)
        self.page: Page = self._ctx.new_page()
        self.page.set_default_timeout(TIMEOUT_MS)
        return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type:
                self.screenshot("error")
            if self.state_file is not None:
                self._ctx.storage_state(path=str(self.state_file))
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
        """Failures only: screens show the address."""
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

    def check_session(self, rules: Rules) -> str:
        if not self.page.url.startswith(self.base):
            self.goto("/")
        addr = self.page.locator(ADDRESS).first
        try:
            addr.wait_for(timeout=20_000)
        except PlaywrightTimeout:
            if self.page.locator(LOGGED_OUT).count():
                raise ShopError("the shop login has expired; the account's owner can /setup again") from None
            raise ShopError("could not read the delivery address") from None
        text = addr.inner_text()
        if not text.startswith(rules.address_label) or rules.address_match not in text:
            raise ShopError(f"the shop is not set to deliver to '{rules.address_label}'")
        return text

    def request_otp(self, phone: str) -> None:
        self.goto("/")
        self.page.locator(LOGGED_OUT).first.click()
        field = self.page.locator(PHONE_FIELD).filter(visible=True).first
        try:
            field.wait_for(timeout=20_000)
        except PlaywrightTimeout:
            raise ShopError("the shop's login form did not open") from None
        field.fill(phone)
        self.page.wait_for_timeout(500)
        button = self.page.get_by_role("button", name=CONTINUE).filter(visible=True)
        if button.count():
            button.first.click()
        else:
            field.press("Enter")
        try:
            self.page.locator(OTP_FIELD).filter(visible=True).first.wait_for(timeout=20_000)
        except PlaywrightTimeout:
            raise ShopError("the shop did not ask for a code; is the number right?") from None

    def enter_otp(self, code: str) -> None:
        """One field or one box per digit: typing works for both."""
        self.page.locator(OTP_FIELD).filter(visible=True).first.click()
        self.page.keyboard.type(code, delay=150)
        self.page.wait_for_timeout(1000)
        button = self.page.get_by_role("button", name=VERIFY).filter(visible=True)
        if button.count():
            button.first.click()
        try:
            self.page.locator(LOGGED_OUT).first.wait_for(state="detached", timeout=30_000)
            self.page.locator(ADDRESS).first.wait_for(timeout=20_000)
        except PlaywrightTimeout:
            raise ShopError("the shop did not accept that code") from None
        self.page.wait_for_timeout(3000)

    def delivery_address(self) -> str:
        """'' when none is chosen."""
        text = self.page.locator(ADDRESS).first.inner_text().strip()
        return "" if text.startswith("Select") else text

    def saved_addresses(self) -> list[str]:
        """Opens the address picker and lists its saved addresses, in order; pick one with pick_address."""
        replies = []

        def record(response):
            if "address" in response.url.split("?")[0].lower() and "json" in response.headers.get("content-type", ""):
                replies.append(response)

        self.page.on("response", record)
        try:
            opened = self.open_picker()
            self.page.wait_for_timeout(1500)  # the list fills in after the picker shows
        finally:
            self.page.remove_listener("response", record)
        if logging.getLogger(__name__).isEnabledFor(logging.DEBUG):
            self._log_picker(replies)
        found = []
        if opened:
            try:
                found = self.page.evaluate(SAVED_ADDRESSES)
            except PlaywrightError:  # setup falls back to naming the address
                log.warning("could not read saved addresses", exc_info=True)
        log.info("address picker", opened=opened, saved=len(found), replies=len(replies))
        if opened and not found:
            self.close_picker()
        return found

    def open_picker(self) -> bool:
        """Right after login the page can ignore clicks for a while: click until the picker shows."""
        picker = self.page.locator(f":is({MODAL}):visible").first
        for _ in range(3):
            self.page.locator(PICKER_BUTTON).first.click()
            try:
                picker.wait_for(timeout=8_000)
                return True
            except PlaywrightTimeout:
                pass
        return False

    def _log_picker(self, replies: list) -> None:
        """LOG_LEVEL=DEBUG: the shape of the picker and of the shop's address replies, to build the address
        reader against. Masked first (letters x, digits 0; markup and keys kept), so no address is logged.
        In parts: Docker splits log lines over 16 KB."""
        try:
            html = self.page.evaluate(MASKED_PICKER, MODAL)
            api = []
            for r in replies:
                try:
                    api.append({"path": _mask(r.url.split("?")[0]), "status": r.status, "body": _mask(r.json())})
                except Exception:  # redirects and aborted requests have no body
                    log.debug("address reply unreadable")
            for kind, text in (("html", html), ("api", json.dumps(api))):
                parts = [text[i : i + LOG_PART] for i in range(0, len(text), LOG_PART)] or [""]
                for n, part in enumerate(parts, 1):
                    log.debug("address picker", kind=kind, part=n, parts=len(parts), text=part)
        except Exception:  # only a diagnostic: never stop setup over it
            log.warning("could not log the address picker", exc_info=True)

    def pick_address(self, n: int) -> str:
        """The nth of saved_addresses(); returns the delivery address the shop now shows."""
        self.page.locator(f"[data-saved-address='{n}']").first.click()
        self.page.wait_for_timeout(3000)
        self.goto("/")
        if not (address := self.delivery_address()):
            raise ShopError("the shop didn't switch to that address")
        return address

    def close_picker(self) -> None:
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(1000)

    def choose_address(self, label: str) -> None:
        addr = self.page.locator(ADDRESS).first
        if addr.inner_text().startswith(label):
            return
        if not self.open_picker():
            raise ShopError("the shop's address list didn't open")
        self.page.wait_for_timeout(1500)
        # Only inside the picker: the same name on the page behind it can't be clicked.
        choice = self.page.locator(MODAL).last.get_by_text(label, exact=True).filter(visible=True)
        if not choice.count():
            self.close_picker()
            raise ShopError(f"there is no saved address called '{label}'; add it in the shop's app first")
        choice.first.click(timeout=15_000)
        self.page.wait_for_timeout(3000)
        self.goto("/")

    def public_ip(self) -> str:
        self.goto("https://api.ipify.org")
        return self.page.locator("body").inner_text().strip()

    def search(self, query: str, limit: int = 4) -> list[Product]:
        self.goto(f"/search?query={quote(query)}")
        try:
            self.page.wait_for_selector("a[data-testid=product-card]", timeout=15_000)
        except PlaywrightTimeout:
            return []
        self.page.wait_for_timeout(1500)
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

    def add(self, product: Product, qty: int) -> str | None:
        """None on success, else why it was skipped."""
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
        """Selects the wallet, never reward coins, and checks every rule."""
        cart = self._open_cart()
        for i, box in enumerate(cart["boxes"]):
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
        if item_total:
            result.item_total = _money(item_total.group(1))
        else:
            result.problems.append("could not read the item total")
        if not to_pay or _money(to_pay.group(1)) != 0:
            result.problems.append("the wallet does not cover the whole bill")
        result.total = _money(cash.group(1)) if cash else None
        if result.total is None:
            result.problems.append("could not read the amount paid from the wallet")
        elif result.total > rules.budget:
            result.problems.append(f"₹{result.total:g} is more than what is left of today's limit (₹{rules.budget:g})")
        return result

    def place(self) -> None:
        """Also confirms the "Missed something?" upsell pop-up, adding nothing."""
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
