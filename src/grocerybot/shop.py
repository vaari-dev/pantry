"""The shop's website, through real Chrome: the site refuses headless browsers and data-centre IPs."""

import contextlib
import re
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import structlog
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, Request, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .apimap import Recorder
from .catalog import QUANTITY, Product, is_size, quantities, rupees

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
LOGIN_EXPIRED = "the shop login has expired; the account's owner can /setup again"
CART_COUNT = "[data-testid=cart-items-number]"
CART = "[data-testid=cart-screen]"
CART_API = "/cfs/api/v1/cart"
REMOVE = re.compile(r"^\s*(remove|delete)(\s+item)?\s*$", re.I)
STOCK = re.compile(r"out of stock|sold out|unavailable|back in stock", re.I)
# The cart shows the address and wallet balance: only known words are logged.
LOGGABLE = re.compile(rf"^(item total|to pay|remove|delete)$|{STOCK.pattern}", re.I)
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

    def over(self, total: float) -> str | None:
        return (
            f"₹{total:g} is more than what is left of today's limit (₹{self.budget:g})" if total > self.budget else None
        )


@dataclass
class Cart:
    total: float | None = None
    item_total: float = 0
    items: list[tuple[str, int]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    cod: bool = False
    prices: dict[str, str] = field(default_factory=dict)  # name -> unit price paid, for /basket
    capped: dict[str, int] = field(default_factory=dict)  # name -> the most the shop lets one order have


def _money(text: str) -> float:
    return float(text.replace(",", ""))


def _amount(text: str, label: str) -> float | None:
    """The bill line's amount, after any struck-out one."""
    m = re.search(label + r"\s*\n(?:-?₹[\d,.]+\s*\n)?-?₹([\d,.]+)", text)
    return _money(m.group(1)) if m else None


def _owed(cart: dict) -> bool:
    return (_amount(cart["text"], "To Pay") or 0) > 0


def _loggable(text: str) -> list[str]:
    return [line for line in map(str.strip, text.split("\n")) if LOGGABLE.search(line)][:40]


def _text(locator: Locator) -> str:
    try:
        return locator.inner_text(timeout=2000) if locator.count() else ""
    except PlaywrightTimeout:
        return ""


def _squash(text: str) -> str:
    return " ".join(text.split()).lower()


def _shows_size(size: str, page: str) -> bool:
    """Whether the page offers the size the item was picked at. A card's "1 pc (0.35 g)" may show on
    the page as "0.35g", so the weights or volumes are compared, else the counts. A size with no
    quantity in it can't tell one pack from another, so it doesn't stop the add."""
    if re.search(rf"(?<!\w){re.escape(_squash(size))}(?!\w)", _squash(page)):
        return True
    wanted = quantities(size)
    measures = {q for q in wanted if q[1] not in ("pc", "pack")} or wanted
    return measures <= quantities(page)


def _card(lines: list[str]) -> tuple[str, str] | None:
    """(name, size) from a search card's lines. Found by the size's shape, not by position: some
    cards have a brand line above the name ("Elle 18", "Elle 18 Eyedrama Kajal Bold Black", "1 pc (0.35 g)")."""
    body = [line for line in lines if not re.fullmatch(r"ADD|₹\s?[\d,.]+|[\d,.]+|OFF|₹", line)]
    at = next((i for i, line in enumerate(body) if i and is_size(line)), None)
    if at is not None:
        return body[at - 1], body[at]
    if body:  # no size shown: the longest line is the name, never a brand
        log.info("card without a size", lines=len(body))
        return max(body, key=len), ""
    return None


def _core(name: str) -> str:
    """Pages and cart rows drop a long name's "| tagline"."""
    return _squash(name.split("|")[0])


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
    if not mrp and len(amounts) > 1 and (rupees(amounts[1]) or 0) > (rupees(price) or 0):
        mrp = amounts[1]  # no strike styling found: cards list the price, then the original
    return price, mrp


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
        self._carts: list[tuple[float, Request]] = []  # the shop app's own cart calls, newest last
        self._cart: dict | None = None
        self._cart_at = 0.0
        self.page.on("requestfinished", self._seen)  # not "response": a navigation can cut its body off forever
        self.page.set_default_timeout(TIMEOUT_MS)
        return self

    def __exit__(self, exc_type, exc, _):
        try:
            if exc_type:
                self._log_stop(exc)
            try:  # a failed save must not replace the outcome, e.g. an unconfirmed order
                if self.state_file is not None:
                    self._ctx.storage_state(path=str(self.state_file))
                    self.state_file.chmod(0o600)
                if self.recorder:
                    self.recorder.write(self.page, self._ctx)
            except Exception:
                log.warning("browser state not saved", exc_info=True)
        finally:
            self._browser.close()
            self._pw.stop()
        if isinstance(exc, PlaywrightError):  # whatever page step it was, the user gets a reason
            raise ShopError("the shop's page stopped responding; try again in a minute") from exc

    def _seen(self, req: Request) -> None:
        """Keeps it, unread: reading the body here, inside Playwright's event, is not safe."""
        if req.method == "PUT" and urlsplit(req.url).path == CART_API:
            self._carts.append((time.monotonic(), req))

    def _cart_api(self) -> dict | None:
        """The latest cart reply, as far as the bot reads it: never its address or wallet parts.
        Read before every navigation, which discards the bodies of the page's replies."""
        if self._carts:
            self._cart_at, req = self._carts[-1]
            self._carts.clear()
            try:
                data = req.response().json()
                rows = [p for s in data.get("storeLevelCartProducts") or [] for p in s.get("cartProducts") or []]
                self._cart = {
                    "items": [
                        [
                            p["storeProduct"]["productVariant"]["id"],
                            p["quantity"],
                            p["storeProduct"]["discountedSellingPrice"],
                            p["storeProduct"]["availableQuantity"],
                            p["storeProduct"]["isActive"],
                        ]
                        for p in rows
                    ],
                    "oos": len(data.get("oosProducts") or []),
                    "item_total": data.get("itemTotalAmount"),
                    "to_pay": data.get("toPay"),
                }
            except Exception as e:  # its shape is the shop's to change: evidence, never a failure
                self._cart = {"unreadable": type(e).__name__}
        # age: a reply older than the page's last change is not a disagreement
        return self._cart and {**self._cart, "age": round(time.monotonic() - self._cart_at, 1)}

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
            self.page.screenshot(path=str(self.debug_dir / name), timeout=10_000)
            return name
        except Exception:  # never mask the real error
            log.warning("screenshot failed", exc_info=True)
            return None

    def _log_stop(self, exc: BaseException) -> None:
        """Not str(exc): Playwright's call log holds URLs."""
        steps = [f.name for f in traceback.extract_tb(exc.__traceback__) if f.filename == __file__]
        try:  # never mask the real error
            log.warning(
                "browser stopped",
                at=self._at(),
                error=type(exc).__name__,
                step=steps[-1] if steps else None,
                screenshot=self.screenshot("error"),
            )
        except Exception:
            log.warning("browser stop not logged", exc_info=True)

    def _at(self) -> str:
        return urlsplit(self.page.url).path  # never the query: searches hold the user's words

    def goto(self, url: str) -> None:
        """Two tries; a failed one checks the home connection outside Chrome, to tell the two apart."""
        url = url if url.startswith("http") else self.base + url
        self._cart_api()
        for attempt in (1, 2):
            start = time.monotonic()
            try:
                resp = self.page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS // 2)
                break
            except PlaywrightError as e:
                # Not str(e): it holds the URL, and a search's query is the user's words.
                reason = m.group() if (m := re.search(r"net::\w+|Timeout", str(e))) else type(e).__name__
                home = self._home_reaches_shop()
                log.info("page failed", to=urlsplit(url).path, reason=reason, attempt=attempt, home_ok=home)
                if attempt == 2:
                    raise ShopError(
                        "the home connection (exit node) is not reaching the shop"
                        if home is False
                        else "the shop's page did not load; try again in a minute"
                    ) from e
                with contextlib.suppress(PlaywrightError):  # the stalled load would abort the retry
                    self.page.goto("about:blank", timeout=5000)
        status = resp.status if resp else None
        log.info(
            "page loaded", at=self._at(), status=status, attempt=attempt, seconds=round(time.monotonic() - start, 1)
        )
        if status in (403, 429):
            raise ShopError(f"the shop refused the connection (HTTP {status})")
        self.page.wait_for_timeout(2000)

    def _home_reaches_shop(self) -> bool | None:
        """None without an exit node."""
        if not self.proxy:
            return None
        socks = self.proxy.replace("socks5://", "socks5h://")  # the exit node resolves names too
        curl = ["curl", "-s", "-o", "/dev/null", "--max-time", "10", "-x", socks, self.base]
        try:
            return subprocess.run(curl, check=False).returncode == 0  # noqa: S603  config, not input
        except OSError:  # no curl
            return None

    def check_session(self, rules: Rules) -> str:
        if not self.page.url.startswith(self.base):
            self.goto("/")
        addr = self.page.locator(ADDRESS).first
        shown, logged_out = self._wait(addr, 20), self._logged_out()
        text = addr.inner_text() if shown else ""
        ok = rules.address_ok(text)
        log.info("session checked", logged_in=not logged_out, address_shown=shown, address_ok=ok)
        if logged_out:  # checked even with an address showing: logged-out pages can show one
            raise ShopError(LOGIN_EXPIRED)
        if not shown:
            raise ShopError("could not read the delivery address")
        if not ok:
            raise ShopError(
                f"the shop is not set to deliver to '{rules.address_label}'", "the shop is set to another address"
            )
        return text

    def _logged_out(self) -> bool:
        return bool(self.page.locator(LOGGED_OUT).filter(visible=True).count())

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
            if not (card := _card(lines)):
                continue
            seen.add(c["url"])
            price, mrp = _prices(lines, c["struck"])
            image = "" if "placeholder" in c["image"].lower() else c["image"].replace("f-auto", "f-jpg")
            found.append(Product(*card, c["url"], price, image, mrp))
            if len(found) == limit:
                break
        return found

    def add(self, product: Product, qty: int) -> str | None:
        """None on success, else why it was skipped."""
        self.goto(product.url)
        body = self.page.locator("body").inner_text()
        if not (
            _squash(title := self.page.title()).startswith(_core(product.name)) or _core(product.name) in _squash(body)
        ):
            headings = self.page.locator("h1").all_inner_texts()
            log.info("listing changed", product=product.name, title=title, headings=headings)
            return "listing changed"
        if not _shows_size(product.size, body):
            sizes = sorted({m.group() for m in QUANTITY.finditer(body)})[:20]
            log.info("size not on the page", product=product.name, size=product.size, page_sizes=sizes)
            return "size changed"
        add = self.page.locator(PLUS, has_text="Add to Cart")
        if not add.count():
            return "out of stock"
        for _ in range(2):  # a tap on a page still loading can be lost
            add.first.evaluate("b => b.click()")  # the sticky search bar overlaps it for mouse clicks
            if self._wait(add.first, 5, "hidden"):
                break
        else:
            return "the shop did not take it"
        for _ in range(qty - 1):
            self.page.locator(PLUS).first.evaluate("b => b.click()")
            self.page.wait_for_timeout(1000)
        badge = self.page.locator(CART_COUNT)
        log.info("added", product=product.name, qty=qty, cart_count=badge.first.inner_text() if badge.count() else None)
        return None

    def _open_cart(self) -> dict:
        self.page.locator("[data-testid=cart-btn]").first.click()
        cart = self.page.locator(CART).first
        if not self._wait(cart, TIMEOUT_MS // 1000):
            raise ShopError("the cart did not open")
        loaded = self._wait(cart.locator("[data-testid$=cart-qty]").or_(cart.get_by_text("Item Total")).first, 10)
        read = self.page.evaluate("""() => {
          const cart = document.querySelector('[data-testid=cart-screen]');
          const items = [...cart.querySelectorAll('[data-testid$=cart-qty]')].map(q => {
            let row = q;  // largest ancestor holding only this item's stepper
            while (row.parentElement && row.parentElement !== cart &&
                   row.parentElement.querySelectorAll('[data-testid$=cart-qty]').length === 1) row = row.parentElement;
            const lines = row.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
            const struck = [...row.querySelectorAll('*')].filter(e => getComputedStyle(e).textDecorationLine
              .includes('line-through')).map(e => e.innerText.replace(/\\s/g, ''));
            return {qty: parseInt(q.innerText, 10), lines, struck};
          });
          const boxes = [...cart.querySelectorAll('input[type=checkbox]')].map(i => ({checked: i.checked,
            label: (i.closest('label') || i.parentElement.parentElement).innerText.replace(/\\s+/g, ' ')}));
          return {text: cart.innerText, items, boxes};
        }""")
        text = read["text"]
        log.info(
            "cart read",
            loaded=loaded,
            quantities=[i["qty"] for i in read["items"]],
            item_total=_amount(text, "Item Total"),
            to_pay=_amount(text, "To Pay"),
            stock=STOCK.findall(text),
            api=self._cart_api(),
        )
        return read

    def empty_cart(self) -> None:
        """Raises if anything is left: an out of stock row has no minus button, and comes back in stock."""
        badge = self.page.locator(CART_COUNT).first
        if not badge.count():
            return
        self._open_cart()
        cart = self.page.locator(CART).first
        tap = cart.locator("[data-testid$=minus-btn]").or_(cart.get_by_role("button", name=REMOVE)).first
        taps = 0
        while tap.count() and taps < 100:
            before = _text(cart)
            try:
                tap.click(timeout=10_000)
            except PlaywrightTimeout:
                break
            taps += 1
            if not self._changed(cart, before):
                break
        left = _text(cart)
        # The count, not the cart's words: an emptied cart can say "All items are out of stock".
        stuck = not self._wait(badge, 10, "hidden")
        log.info("cart emptied", taps=taps, stuck=stuck, lines=_loggable(left))
        if stuck:
            self.screenshot("cart-stuck")
            raise ShopError("the shop's cart has items the bot can't remove, maybe out of stock; empty it in the app")
        self.page.keyboard.press("Escape")

    def _changed(self, locator: Locator, before: str, seconds: int = 15) -> bool:
        for _ in range(seconds * 4):
            self.page.wait_for_timeout(250)
            if _text(locator) != before:
                self.page.wait_for_timeout(500)  # the next tap must not land mid-update
                return True
        return False

    def _pay_with(self, wallet: bool) -> dict:
        """Sets the wallet box, unticks reward coins; returns the cart as it then is."""
        cart = self._open_cart()
        for i, box in enumerate(cart["boxes"]):
            is_wallet = bool(WALLET.search(box["label"]))
            if (is_wallet or COINS.search(box["label"])) and box["checked"] != (wallet and is_wallet):
                log.info("box tapped", wallet=is_wallet, was=box["checked"])
                self.page.locator(f"{CART} input[type=checkbox]").nth(i).click(force=True)
                self.page.wait_for_timeout(1500)
                cart = self._open_cart()
        return cart

    def _api_rows(self, cart: dict, item_total: float | None) -> dict[str, list] | None:
        """The shop app's cart reply by product id, when it shows the cart the page shows; else None."""
        api = self._cart_api()
        if not api or "items" not in api:
            return None
        live = [r for r in api["items"] if r[4] and r[3] > 0]
        if (
            item_total is None
            or sorted(r[1] for r in live) != sorted(i["qty"] for i in cart["items"])
            or (api["item_total"] != round(item_total * 100))
        ):
            log.info("cart reply not used", reason="differs from the page")
            return None
        return {r[0]: r for r in live}

    def _match_by_id(self, expected: dict[str, int], ids: dict[str, str], rows: dict[str, list], result: Cart) -> None:
        for name, want in expected.items():
            if (row := rows.pop(ids[name], None)) is None:
                result.problems.append(f"{name} did not make it into the cart")
                continue
            got, available = row[1], row[3]
            if got < want and available <= got:  # the shop's limit per order, not a lost tap
                log.info("quantity capped", product=name, asked=want, allowed=got)
                result.capped[name] = got
            elif got != want:
                result.problems.append(f"{name}: {got} in the cart, expected {want}")
                continue
            result.items.append((name, got))
            result.prices[name] = f"₹{row[2] / 100:g}"
        result.problems += ["an item the bot did not add is in the cart"] * len(rows)

    def _match_by_name(self, expected: dict[str, int], cart: dict, result: Cart) -> None:
        seen = set()
        for item in cart["items"]:
            name = next((n for n in expected for line in item["lines"] if _core(line) == _core(n)), None)
            if name is None:
                result.problems.append(f"unexpected item in the cart: {' '.join(item['lines'][:2])}")
            elif item["qty"] != expected[name]:
                result.problems.append(f"{name}: {item['qty']} in the cart, expected {expected[name]}")
            else:
                seen.add(name)
                result.items.append((name, item["qty"]))
                # The price paid, not the struck-through original: /basket shows these.
                if item["qty"] == 1 and (price := _prices(item["lines"], item["struck"])[0]):
                    result.prices[name] = price
        result.problems += [f"{n} did not make it into the cart" for n in expected.keys() - seen]

    def review_cart(self, expected: dict[str, int], rules: Rules, ids: dict[str, str] | None = None) -> Cart:
        """Pays from the wallet if it covers the whole bill, else cash on delivery. Checks every rule.
        ids: name -> the shop's product id, to check the items by the shop app's own cart reply."""
        cart, result = self._pay_with(wallet=True), Cart()
        if _owed(cart) and _owed(self._open_cart()):  # twice: the bill can lag behind the wallet tick
            cart, result.cod = self._pay_with(wallet=False), True
        if not cart["items"]:
            log.info("cart has no items", lines=_loggable(cart["text"]))
            if self._logged_out():  # mid-run: the shop took the adds into a guest cart
                raise ShopError(LOGIN_EXPIRED)
            result.problems.append("the cart is empty: the shop did not take the items")
            return result
        text = cart["text"]
        if not rules.address_ok(text):
            result.problems.append("the cart is not delivering to your saved address (/settings)")  # logged: no label
        item_total = _amount(text, "Item Total")
        rows = self._api_rows(cart, item_total) if ids and expected.keys() <= ids.keys() else None
        if rows is not None:
            self._match_by_id(expected, ids, rows, result)
        else:
            self._match_by_name(expected, cart, result)
        for box in cart["boxes"]:
            if COINS.search(box["label"]) and box["checked"]:
                result.problems.append("reward coins are selected")
            if WALLET.search(box["label"]) and box["checked"] == result.cod:
                result.problems.append("the wallet is still selected" if result.cod else "the wallet is not selected")

        to_pay = _amount(text, "To Pay")
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
        elif over := rules.over(result.total):
            result.problems.append(over)
        return result

    def place(self) -> None:
        self.page.locator(f"{CART} button", has_text="Place Order").click()
        log.info("tapped", button="Place Order")
        self.page.wait_for_timeout(3000)
        self._skip_upsell("Place Order")
        self.page.wait_for_timeout(15_000)  # "Confirming your order..."

    def has_pay_button(self, total: float) -> bool:
        """Taps nothing."""
        if self._pay_button(PAY, total, 5):
            return True
        labels = self.page.locator(f"{CART} button").all_inner_texts()
        log.info("no pay button", labels=[t for t in labels if re.search(r"₹|pay|order", t, re.I)])
        return False

    def place_cod(self, total: float) -> str | None:
        """None once "Pay ₹total on delivery" is tapped, else where it stopped."""
        if not (button := self._pay_button(PAY, total, 5)):
            return f"the cart's Pay ₹{total:g} button went away"
        label = " ".join(button.inner_text().split())
        button.click()
        log.info("tapped", button=label)
        self.page.wait_for_timeout(3000)
        self._skip_upsell(label)
        option = self.page.get_by_text(COD).filter(visible=True).first
        if not self._wait(option, 20):
            return "cash on delivery is not offered for this order"
        if not (pay := self._pay_button(PAY_COD, total, 3)):
            option.click()  # its button shows once it is selected
            log.info("tapped", button="cash on delivery")
            if not (pay := self._pay_button(PAY_COD, total, 10)):
                return f"no Pay ₹{total:g} on delivery button"
        pay.click()
        log.info("tapped", button="pay on delivery")
        self.page.wait_for_timeout(15_000)  # "Confirming your order..."
        return None

    def _pay_button(self, pattern: re.Pattern, total: float, seconds: int) -> Locator | None:
        """Only a button for exactly ₹total."""
        button = self.page.get_by_role("button", name=pattern).filter(visible=True).first
        if not self._wait(button, seconds):
            return None
        label = " ".join(button.inner_text().split())
        m = pattern.search(label)
        if not (m and _money(m.group(1)) == total):
            log.info("pay button amount differs", button=label, total=total)
            return None
        return button

    def _wait(self, locator: Locator, seconds: int, state: str = "visible") -> bool:
        try:
            locator.wait_for(state=state, timeout=seconds * 1000)
            return True
        except PlaywrightTimeout:
            return False

    def _skip_upsell(self, label: str) -> None:
        """Gets past the "Missed something?" pop-up, adding nothing, by its button labelled like the one tapped."""
        if self.page.get_by_text("Missed something?", exact=True).count():
            log.info("upsell shown", tapping=label)
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
                log.info("latest order", amount=_money(m.group(1)), placed=placed.isoformat(), total=total)
                if _money(m.group(1)) == total and placed >= since - timedelta(minutes=1):
                    return True
            else:
                log.info("no order on the orders page")
            self.page.wait_for_timeout(5000)
        return False
