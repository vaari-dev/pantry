"""The shop's website, through real Chrome: the site refuses headless browsers and data-centre IPs."""

import contextlib
import re
import subprocess
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import structlog
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, Request, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .apimap import Recorder
from .catalog import QUANTITY, Product, is_size, quantities, rupees, squash

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
CART_API = {("PUT", "/cfs/api/v1/cart"), ("POST", "/cfs/api/v1/cart/create")}
ORDER_API = ("POST", "/api/v3/order/")
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
STRUCK = """const struck = el => [...el.querySelectorAll('*')]
  .filter(e => getComputedStyle(e).textDecorationLine.includes('line-through'))
  .map(e => e.innerText.replace(/\\s/g, ''));"""
CARDS_JS = (
    "() => {"
    + STRUCK
    + """
  return [...document.querySelectorAll('a[data-testid=product-card]')].map(a => ({
    url: a.getAttribute('href'),
    image: (a.querySelector('img') || {}).currentSrc || '',
    struck: struck(a),
    lines: a.innerText.split('\\n').map(s => s.trim()).filter(Boolean)}));
}"""
)
CART_JS = (
    "() => {"
    + STRUCK
    + """
  const cart = document.querySelector('[data-testid=cart-screen]');
  const items = [...cart.querySelectorAll('[data-testid$=cart-qty]')].map(q => {
    let row = q;  // largest ancestor holding only this item's stepper
    while (row.parentElement && row.parentElement !== cart &&
           row.parentElement.querySelectorAll('[data-testid$=cart-qty]').length === 1) row = row.parentElement;
    const lines = row.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
    return {qty: parseInt(q.innerText, 10), lines, struck: struck(row)};
  });
  const boxes = [...cart.querySelectorAll('input[type=checkbox]')].map(i => ({checked: i.checked,
    label: (i.closest('label') || i.parentElement.parentElement).innerText.replace(/\\s+/g, ' ')}));
  return {text: cart.innerText, items, boxes};
}"""
)
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
    problems: list[str] = field(default_factory=list)
    cod: bool = False
    prices: dict[Product, str] = field(default_factory=dict)  # unit price paid
    capped: dict[Product, int] = field(default_factory=dict)  # the most one order may have


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


def _shows_size(size: str, page: str) -> bool:
    """A card's "1 pc (0.35 g)" can be "0.35g" on the page: weights or volumes decide, else counts."""
    if re.search(rf"(?<!\w){re.escape(squash(size))}(?!\w)", squash(page)):
        return True
    wanted = quantities(size)
    measures = {q for q in wanted if q[1] not in ("pc", "pack")} or wanted
    return measures <= quantities(page)


def _card(lines: list[str]) -> tuple[str, str] | None:
    """(name, size): the name is the line above the size, as some cards put a brand above the name."""
    body = [line for line in lines if not re.fullmatch(r"ADD|₹\s?[\d,.]+|[\d,.]+|OFF|₹", line)]
    if len(body) < 2:
        return None
    if at := next((i for i, line in enumerate(body) if i and is_size(line)), None):
        return body[at - 1], body[at]
    log.info("card without a size", lines=len(body))
    return max(body, key=len), ""


def _core(name: str) -> str:
    """Without the "| tagline" that pages and cart rows drop."""
    return squash(name.split("|")[0])


def _rupees(paise: int) -> str:
    return f"₹{paise // 100}" if paise % 100 == 0 else f"₹{paise / 100:.2f}"


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


def _cart_view(data: dict) -> dict:
    """Never the reply's address or wallet parts."""
    items = []
    for store in data.get("storeLevelCartProducts") or []:
        for p in store.get("cartProducts") or []:
            sp = p["storeProduct"]
            items.append(
                [
                    sp["productVariant"]["id"],
                    p["quantity"],
                    sp["discountedSellingPrice"],
                    sp["availableQuantity"],
                    sp["isActive"],
                ]
            )
    return {
        "items": items,
        "oos": len(data.get("oosProducts") or []),
        "item_total": data.get("itemTotalAmount"),
        "to_pay": data.get("toPay"),
    }


def _order_view(data: dict) -> dict:
    return {
        "created": data.get("isOrderCreated") is True,
        "status": data.get("status"),
        "failure": data.get("failureReason"),
        "item_total": data.get("itemTotalAmount"),
        "wallet_used": data.get("walletBalanceUsed"),
    }


def _read(req: Request, view: Callable[[dict], dict]) -> dict:
    try:
        return view(req.response().json())
    except Exception as e:  # its shape is the shop's to change: evidence, never a failure
        return {"unreadable": type(e).__name__}


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
        self._cart_req: Request | None = None  # the shop app's latest cart call, unread
        self._cart: dict | None = None
        self._cart_at = 0.0
        self._order_req: Request | None = None
        self._order: dict | None = None
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
        call = (req.method, urlsplit(req.url).path)
        if call in CART_API:
            self._cart_req, self._cart_at = req, time.monotonic()
        elif call == ORDER_API:
            self._order_req = req

    def _cart_api(self) -> dict | None:
        """The latest cart reply. Read before every navigation, which discards the page's reply bodies."""
        if self._cart_req:
            self._cart, self._cart_req = _read(self._cart_req, _cart_view), None
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
        cards = self.page.evaluate(CARDS_JS)
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
            if not price:
                log.info("card without a price", lines=lines)
            image = "" if "placeholder" in c["image"].lower() else c["image"].replace("f-auto", "f-jpg")
            found.append(Product(*card, c["url"], price, image, mrp))
            if len(found) == limit:
                break
        return found

    def add(self, product: Product, qty: int) -> str | None:
        """None on success, else why not, for people."""
        self.goto(product.url)
        title, headings = self.page.title(), self.page.locator("h1").all_inner_texts()
        if not any(squash(t).startswith(_core(product.name)) for t in [title, *headings]):
            log.info("listing changed", product=product.name, title=title, headings=headings)
            return "the shop changed this product"
        if not _shows_size(product.size, body := self.page.locator("body").inner_text()):
            sizes = sorted({m.group() for m in QUANTITY.finditer(body)})[:20]
            log.info("size not on the page", product=product.name, size=product.size, page_sizes=sizes)
            return "the size you picked isn't sold any more"
        add = self.page.locator(PLUS, has_text="Add to Cart")
        if not add.count():
            return "out of stock"
        badge = self.page.locator(CART_COUNT).first
        before = _text(badge)
        if not self._tap(add.first, lambda: self._wait(add.first, 5, "hidden")):
            return "the shop didn't take it into the cart"
        self._changed(badge, before, 5)  # + taps are judged by the count: the add's own change first
        for n in range(1, qty):
            if not self._one_more(badge):
                log.info("quantity not raised", product=product.name, qty=n, asked=qty)
                break
        log.info("added", product=product.name, qty=qty, cart_count=_text(badge) or None)
        return None

    def _one_more(self, badge: Locator) -> bool:
        """False: the shop's per-order limit, or lost taps; the cart check tells which."""
        before = _text(badge)
        # 8 s: a slow tap tapped again would add two
        return self._tap(self.page.locator(PLUS).first, lambda: self._changed(badge, before, 8))

    def _tap(self, button: Locator, took: Callable[[], bool]) -> bool:
        """Twice at most: a tap on a page still loading can be lost."""
        for _ in range(2):
            button.evaluate("b => b.click()")  # the sticky search bar overlaps it for mouse clicks
            if took():
                return True
        return False

    def _until(self, done: Callable[[], bool], seconds: float) -> bool:
        for _ in range(int(seconds * 4)):
            if done():
                return True
            self.page.wait_for_timeout(250)
        return done()

    def _open_cart(self) -> dict:
        opened = time.monotonic()
        self.page.locator("[data-testid=cart-btn]").first.click()
        cart = self.page.locator(CART).first
        if not self._wait(cart, TIMEOUT_MS // 1000):
            raise ShopError("the cart did not open")
        self._until(lambda: self._cart_at >= opened, 5)  # opening it asks the shop for the cart
        loaded = self._wait(cart.locator("[data-testid$=cart-qty]").or_(cart.get_by_text("Item Total")).first, 10)
        read = self.page.evaluate(CART_JS)
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
        if not self._until(lambda: _text(locator) != before, seconds):
            return False
        self.page.wait_for_timeout(500)  # the next tap must not land mid-update
        return True

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

    def _match_by_id(self, expected: dict[Product, int], cart: dict, item_total: float | None, result: Cart) -> bool:
        """By the shop app's own cart reply, if it agrees with the page. False: not used."""
        api = self._cart_api() or {}
        rows = {r[0]: r for r in api.get("items", []) if r[4] and r[3] > 0}
        agrees = (
            item_total is not None
            and api.get("item_total") == round(item_total * 100) == sum(r[1] * r[2] for r in rows.values())
            and sorted(r[1] for r in rows.values()) == sorted(i["qty"] for i in cart["items"])
            and all(p.variant in rows for p in expected)
        )
        if not agrees:
            log.info("cart reply not used")
            return False
        for product, want in expected.items():
            if (row := rows.pop(product.variant, None)) is None:
                result.problems.append(f"{product.name} did not make it into the cart")
                continue
            _, got, paise, available, _ = row
            if got < want and available <= got:
                log.info("quantity capped", product=product.name, asked=want, allowed=got)
                result.capped[product] = got
            elif got != want:
                result.problems.append(f"{product.name}: {got} in the cart, expected {want}")
                continue
            result.prices[product] = _rupees(paise)
        result.problems += ["an item the bot did not add is in the cart"] * len(rows)
        return True

    def _match_by_name(self, expected: dict[Product, int], cart: dict, result: Cart) -> None:
        seen = set()
        for item in cart["items"]:
            names = {_core(line) for line in item["lines"]}
            product = next((p for p in expected if p not in seen and _core(p.name) in names), None)
            if product is None:
                result.problems.append(f"unexpected item in the cart: {' '.join(item['lines'][:2])}")
            elif item["qty"] != expected[product]:
                seen.add(product)
                result.problems.append(f"{product.name}: {item['qty']} in the cart, expected {expected[product]}")
            else:
                seen.add(product)
                if item["qty"] == 1 and (price := _prices(item["lines"], item["struck"])[0]):
                    result.prices[product] = price
        result.problems += [f"{p.name} did not make it into the cart" for p in expected.keys() - seen]

    def review_cart(self, expected: dict[Product, int], rules: Rules) -> Cart:
        """Pays from the wallet if it covers the whole bill, else cash on delivery. Checks every rule."""
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
        if not self._match_by_id(expected, cart, item_total, result):
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
        self._await_order()

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
        self._order_req = None  # an earlier tap's reply is not this order's
        pay.click()
        log.info("tapped", button="pay on delivery")
        self._await_order()
        return None

    def _await_order(self) -> None:
        """Reads the shop's reply to Place Order before anything navigates."""
        if not self._until(lambda: self._order_req is not None, 30):
            log.info("no order reply")
            return
        self._order = _read(self._order_req, _order_view)
        log.info("order reply", **self._order)
        self.page.wait_for_timeout(10_000)  # "Confirming your order...": let the page finish

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
        """By the shop's reply to Place Order; unless it says created, by the Orders page."""
        if self._order and self._order.get("created"):
            return True
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
                lines = [line for line in body.split("\n") if re.search(r"₹|Order|Placed|Delivered|Arriv", line)]
                log.info("no order on the orders page", at=self._at(), lines=lines[:12])
            self.page.wait_for_timeout(5000)
        return False
