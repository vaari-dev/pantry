"""The shop's website, through real Chrome: the site refuses headless browsers and data-centre IPs."""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import structlog
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .apimap import Recorder
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
WALLET = re.compile(r"\bCash\b(?! on [Dd]elivery)")  # the prepaid wallet
COINS = re.compile(r"\bCoins\b")  # reward coins: never used
# Wallet short: the cart's pay button opens Payment Options; "Pay by Cash / UPI on delivery" shows "Pay ₹N on delivery".
PAY = re.compile(r"^\s*(?:click to )?pay ₹\s?([\d,.]+)(?: on delivery)?\s*$", re.I)
COD = re.compile(r"^\s*pay by cash\b.*on delivery\s*$", re.I)
PAY_COD = re.compile(r"^\s*pay ₹\s?([\d,.]+) on delivery\s*$", re.I)
LOGGED_OUT = "[data-testid=login-btn]"
CART_COUNT = "[data-testid=cart-items-number]"
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
log = structlog.get_logger(__name__)


class ShopError(Exception):
    """Its text is told to the user; `logged` is the same without their address in it."""

    def __init__(self, text: str, logged: str | None = None):
        super().__init__(text)
        self.logged = logged or text


@dataclass(frozen=True)
class Rules:
    address_label: str
    address_match: str
    budget: float

    def address_ok(self, text: str) -> bool:
        return text.startswith(self.address_label) and self.address_match in text


@dataclass
class Cart:
    total: float | None = None
    item_total: float = 0
    items: list[tuple[str, int]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    cod: bool = False


def _money(text: str) -> float:
    return float(text.replace(",", ""))


def _amount(text: str, label: str) -> float | None:
    """The bill line's amount, after any struck-out one."""
    m = re.search(label + r"\s*\n(?:-?₹[\d,.]+\s*\n)?-?₹([\d,.]+)", text)
    return _money(m.group(1)) if m else None


def _owed(cart: dict) -> bool:
    return (_amount(cart["text"], "To Pay") or 0) > 0


def _squash(text: str) -> str:
    return " ".join(text.split()).lower()


def _prices(lines: list[str], struck: list[str]) -> tuple[str, str]:
    """(price, original price) from a card's lines; struck are the texts shown crossed out."""
    amounts = [
        line.replace(" ", "")
        for i, line in enumerate(lines)
        if re.fullmatch(r"₹\s?[\d,.]+", line) and lines[i + 1 : i + 2] != ["OFF"]  # not a "₹15 OFF" badge
    ]
    struck = {s.replace(" ", "") for s in struck}
    price = next((a for a in amounts if a not in struck), "")
    mrp = next((a for a in amounts if a in struck), "")
    if not mrp and len(amounts) > 1 and _rupees(amounts[1]) > _rupees(price):
        mrp = amounts[1]  # no strike styling found: cards list the price, then the original
    return price, mrp


def _rupees(amount: str) -> float:
    try:
        return float(amount.lstrip("₹").replace(",", ""))
    except ValueError:
        return 0


class Browser:
    """One Chrome session. Loads and saves state_file (None: neither); fresh: starts logged out;
    record: the API map to add to."""

    def __init__(
        self,
        base_url: str,
        state_file: Path | None,
        proxy: str | None,
        debug_dir: Path,
        images: bool = False,
        fresh: bool = False,
        record: Path | None = None,
    ):
        self.base = base_url.rstrip("/")
        self.state_file, self.proxy, self.debug_dir, self.fresh = state_file, proxy, debug_dir, fresh
        self.skip_types = SKIP_TYPES if images else SKIP_TYPES | {"image"}
        self.recorder = Recorder(record) if record else None

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
        if self.recorder:
            self.recorder.attach(self._ctx)
        self.page: Page = self._ctx.new_page()
        self.page.set_default_timeout(TIMEOUT_MS)
        return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type:
                self._log_stop()
            if self.state_file is not None:
                self._ctx.storage_state(path=str(self.state_file))
                self.state_file.chmod(0o600)
            if self.recorder:
                self.recorder.write(self.page, self._ctx)
        finally:
            self._browser.close()
            self._pw.stop()

    def _filter(self, route: Route):
        req = route.request
        if req.resource_type in self.skip_types or any(s in req.url for s in SKIP_URLS):
            return route.abort()
        return route.continue_()

    def screenshot(self, tag: str) -> str | None:
        """Failures only: screens show the address."""
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{tag}.png"
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(self.debug_dir / name))
            return name
        except Exception:  # never mask the real error
            log.warning("screenshot failed", exc_info=True)
            return None

    def _log_stop(self) -> None:
        try:  # never mask the real error
            log.warning("browser stopped", at=self._at(), screenshot=self.screenshot("error"))
        except Exception:
            log.warning("browser stop not logged", exc_info=True)

    def _at(self) -> str:
        return urlsplit(self.page.url).path  # never the query: searches hold the user's words

    def goto(self, url: str) -> None:
        start, url = time.monotonic(), url if url.startswith("http") else self.base + url
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeout as e:
            log.info("page timed out", to=urlsplit(url).path)
            raise ShopError("the shop did not load; is the home connection (exit node) up?") from e
        status = resp.status if resp else None
        log.info("page loaded", at=self._at(), status=status, seconds=round(time.monotonic() - start, 1))
        if status in (403, 429):
            raise ShopError(f"the shop refused the connection (HTTP {status})")
        self.page.wait_for_timeout(2000)

    def check_session(self, rules: Rules) -> str:
        if not self.page.url.startswith(self.base):
            self.goto("/")
        addr = self.page.locator(ADDRESS).first
        shown, logged_out = self._wait(addr, 20), bool(self.page.locator(LOGGED_OUT).filter(visible=True).count())
        text = addr.inner_text() if shown else ""
        ok = rules.address_ok(text)
        log.info("session checked", logged_in=not logged_out, address_shown=shown, address_ok=ok)
        if logged_out:  # checked even with an address showing: logged-out pages can show one
            raise ShopError("the shop login has expired; the account's owner can /setup again")
        if not shown:
            raise ShopError("could not read the delivery address")
        if not ok:
            raise ShopError(
                f"the shop is not set to deliver to '{rules.address_label}'", "the shop is set to another address"
            )
        return text

    def request_otp(self, phone: str) -> None:
        self.goto("/")
        self.page.locator(LOGGED_OUT).first.click()
        field = self.page.locator(PHONE_FIELD).filter(visible=True).first
        if not self._wait(field, 20):
            inputs = self.page.locator("input:visible").evaluate_all(
                "l => l.map(i => [i.type, i.name, i.inputMode, i.autocomplete, i.maxLength].join(' '))"
            )
            log.info("no phone field", inputs=inputs)
            raise ShopError("the shop's login form did not open")
        field.fill(phone)
        self.page.wait_for_timeout(500)
        button = self.page.get_by_role("button", name=CONTINUE).filter(visible=True)
        if button.count():
            button.first.click()
        else:
            field.press("Enter")
        if not self._wait(self.page.locator(OTP_FIELD).filter(visible=True).first, 20):
            raise ShopError("the shop did not ask for a code; is the number right?")

    def enter_otp(self, code: str) -> None:
        """One field or one box per digit: typing works for both."""
        self.page.locator(OTP_FIELD).filter(visible=True).first.click()
        self.page.keyboard.type(code, delay=150)
        self.page.wait_for_timeout(1000)
        button = self.page.get_by_role("button", name=VERIFY).filter(visible=True)
        if button.count():
            button.first.click()
        if not (
            self._wait(self.page.locator(LOGGED_OUT).first, 30, "detached")
            and self._wait(self.page.locator(ADDRESS).first, 20)
        ):
            raise ShopError("the shop did not accept that code")
        self.page.wait_for_timeout(3000)

    def delivery_address(self) -> str:
        text = self.page.locator(ADDRESS).first.inner_text().strip()
        return "" if text.startswith("Select") else text

    def saved_addresses(self) -> list[str]:
        """Opens the address picker and lists its saved addresses, in order; pick one with pick_address."""
        if not self.open_picker():
            return []
        self.page.wait_for_timeout(1500)  # the list fills in after the picker shows
        try:
            found = self.page.evaluate(SAVED_ADDRESSES)
        except PlaywrightError:  # setup falls back to naming the address
            log.warning("could not read saved addresses", exc_info=True)
            found = []
        log.info("saved addresses read", saved=len(found))
        if not found:
            self.close_picker()
        return found

    def open_picker(self) -> bool:
        """Right after login the page can ignore clicks for a while: click until the picker shows."""
        picker = self.page.locator(f":is({MODAL}):visible").first
        for _ in range(3):
            self.page.locator(PICKER_BUTTON).first.click()
            if self._wait(picker, 8):
                return True
        return False

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
            raise ShopError(
                f"there is no saved address called '{label}'; add it in the shop's app first",
                "no saved address by that name",
            )
        choice.first.click(timeout=15_000)
        self.page.wait_for_timeout(3000)
        self.goto("/")

    def public_ip(self) -> str:
        self.goto("https://api.ipify.org")
        return self.page.locator("body").inner_text().strip()

    def search(self, query: str, limit: int = 4) -> list[Product]:
        self.goto(f"/search?query={quote(query)}")
        if not self._wait(self.page.locator("a[data-testid=product-card]").first, 15):
            return []
        self.page.wait_for_timeout(1500)
        cards = self.page.evaluate("""() => [...document.querySelectorAll('a[data-testid=product-card]')].map(a => ({
            url: a.getAttribute('href'),
            image: (a.querySelector('img') || {}).currentSrc || '',
            struck: [...a.querySelectorAll('*')].filter(e => getComputedStyle(e).textDecorationLine
                .includes('line-through')).map(e => e.innerText.replace(/\\s/g, '')),
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
            price, mrp = _prices(lines, c["struck"])
            image = "" if "placeholder" in c["image"].lower() else c["image"].replace("f-auto", "f-jpg")
            found.append(Product(body[0], body[1], c["url"], price, image, mrp))
            if len(found) == limit:
                break
        return found

    def add(self, product: Product, qty: int) -> str | None:
        """None on success, else why it was skipped."""
        self.goto(product.url)
        # Titles drop the "| tagline" of long names, so compare the core name.
        if not _squash(title := self.page.title()).startswith(_squash(product.name.split("|")[0])):
            headings = self.page.locator("h1").all_inner_texts()
            log.info("listing changed", product=product.name, title=title, headings=headings)
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
        badge = self.page.locator(CART_COUNT)
        log.info("added", product=product.name, qty=qty, cart_count=badge.first.inner_text() if badge.count() else None)
        return None

    def _open_cart(self) -> dict:
        self.page.locator("[data-testid=cart-btn]").first.click()
        if not self._wait(self.page.locator("[data-testid=cart-screen]").first, TIMEOUT_MS // 1000):
            raise ShopError("the cart did not open")
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
        if not self.page.locator(CART_COUNT).count():
            return
        self._open_cart()
        minus = self.page.locator("[data-testid=cart-screen] [data-testid$=minus-btn]")
        while minus.count():
            minus.first.click()
            self.page.wait_for_timeout(700)
        self.page.keyboard.press("Escape")

    def _pay_with(self, wallet: bool) -> dict:
        """Sets the wallet box, unticks reward coins; returns the cart as it then is."""
        cart = self._open_cart()
        for i, box in enumerate(cart["boxes"]):
            is_wallet = bool(WALLET.search(box["label"]))
            if (is_wallet or COINS.search(box["label"])) and box["checked"] != (wallet and is_wallet):
                self.page.locator("[data-testid=cart-screen] input[type=checkbox]").nth(i).click(force=True)
                self.page.wait_for_timeout(1500)
                cart = self._open_cart()
        return cart

    def review_cart(self, expected: dict[str, int], rules: Rules) -> Cart:
        """Pays from the wallet if it covers the whole bill, else cash on delivery. Checks every rule."""
        cart, result, seen = self._pay_with(wallet=True), Cart(), set()
        if _owed(cart) and _owed(self._open_cart()):  # twice: the bill can lag behind the wallet tick
            cart, result.cod = self._pay_with(wallet=False), True
        if not cart["items"]:
            result.problems.append("the cart is empty: the shop did not take the items")
            return result
        text = cart["text"]
        if not rules.address_ok(text):
            result.problems.append("the cart is not delivering to your saved address (/settings)")  # logged: no label
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
            if WALLET.search(box["label"]) and box["checked"] == result.cod:
                result.problems.append("the wallet is still selected" if result.cod else "the wallet is not selected")

        item_total, to_pay = _amount(text, "Item Total"), _amount(text, "To Pay")
        if item_total is not None:
            result.item_total = item_total
        else:
            result.problems.append("could not read the item total")
        if result.cod:
            result.total = to_pay
        else:
            if to_pay != 0:
                result.problems.append("the wallet does not cover the whole bill")
            result.total = _amount(text, r"[^\n]*\bCash")
        if result.total is None:
            result.problems.append("could not read the amount to pay")
        elif result.total > rules.budget:
            result.problems.append(f"₹{result.total:g} is more than what is left of today's limit (₹{rules.budget:g})")
        return result

    def place(self) -> None:
        self.page.locator("[data-testid=cart-screen] button", has_text="Place Order").click()
        self.page.wait_for_timeout(3000)
        self._skip_upsell("Place Order")
        self.page.wait_for_timeout(15_000)  # "Confirming your order..."

    def has_pay_button(self, total: float) -> bool:
        """Taps nothing."""
        if self._pay_button(PAY, total, 5):
            return True
        labels = self.page.locator("[data-testid=cart-screen] button").all_inner_texts()
        log.info("no pay button", labels=[t for t in labels if re.search(r"₹|pay|order", t, re.I)])
        return False

    def place_cod(self, total: float) -> str | None:
        """None once "Pay ₹total on delivery" is tapped, else where it stopped."""
        if not (button := self._pay_button(PAY, total, 5)):
            return f"the cart's Pay ₹{total:g} button went away"
        label = " ".join(button.inner_text().split())
        button.click()
        self.page.wait_for_timeout(3000)
        self._skip_upsell(label)
        option = self.page.get_by_text(COD).filter(visible=True).first
        if not self._wait(option, 20):
            return "cash on delivery is not offered for this order"
        if not (pay := self._pay_button(PAY_COD, total, 3)):
            option.click()  # its button shows once it is selected
            if not (pay := self._pay_button(PAY_COD, total, 10)):
                return f"no Pay ₹{total:g} on delivery button"
        pay.click()
        self.page.wait_for_timeout(15_000)  # "Confirming your order..."
        return None

    def _pay_button(self, pattern: re.Pattern, total: float, seconds: int) -> Locator | None:
        """Only a button for exactly ₹total."""
        button = self.page.get_by_role("button", name=pattern).filter(visible=True).first
        if not self._wait(button, seconds):
            return None
        m = pattern.search(" ".join(button.inner_text().split()))
        return button if m and _money(m.group(1)) == total else None

    def _wait(self, locator: Locator, seconds: int, state: str = "visible") -> bool:
        try:
            locator.wait_for(state=state, timeout=seconds * 1000)
            return True
        except PlaywrightTimeout:
            return False

    def _skip_upsell(self, label: str) -> None:
        """Gets past the "Missed something?" pop-up, adding nothing, by its button labelled like the one tapped."""
        if self.page.get_by_text("Missed something?", exact=True).count():
            self.page.evaluate(
                """label => {
                const pop = [...document.querySelectorAll('[role=dialog], div')].find(d =>
                    d.innerText.startsWith('Missed something?') && d.querySelector('button'));
                [...pop.querySelectorAll('button')].find(b => b.innerText.trim() === label)?.click();
            }""",
                label,
            )
            self.page.wait_for_timeout(3000)

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
