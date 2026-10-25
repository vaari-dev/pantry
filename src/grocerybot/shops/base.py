"""What every shop's driver shares: one real Chrome session (the sites refuse headless browsers and
data-centre IPs), page loads, login, the address picker, search cards and the rules a cart must meet.

A driver subclasses Browser, sets its selectors, and implements the steps orders.order runs, in order:
check_session, empty_cart, add, review_cart, ready (everything before the order is irreversible),
place (the irreversible tap) and confirm.
"""

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
from playwright.sync_api import Locator, Page, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..apimap import Recorder
from ..catalog import QUANTITY, Product, is_size, quantities, rupees, squash

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
LOGIN_EXPIRED = "the shop login has expired; the account's owner can /setup again"
PHONE_FIELD = "input[type=tel], input[inputmode=numeric], input[name*=phone i]"
OTP_FIELD = (
    "input[autocomplete=one-time-code], input[data-test-id=otp-input], "
    "input[maxlength='1'], input[maxlength='4'], input[maxlength='6']"
)
# Playwright runs these in the browser: flags must be re flags, not inline (?i).
CONTINUE = re.compile(r"^\s*(continue|get otp|send otp|login|next)\s*$", re.I)
VERIFY = re.compile(r"^\s*(verify|continue|login|submit)", re.I)
UNLISTED = re.compile(r"(?i)sold out|notify( me)?|out of stock")
STRUCK = """const struck = el => [...el.querySelectorAll('*')]
  .filter(e => getComputedStyle(e).textDecorationLine.includes('line-through'))
  .map(e => e.innerText.replace(/\\s/g, ''));"""
log = structlog.get_logger(__name__)


class ShopError(Exception):
    """Its text is told to the user; `logged` is the same without their address in it."""

    def __init__(self, text: str, logged: str | None = None):
        super().__init__(text)
        self.logged = logged or text


class NotPlaced(ShopError):
    """From place, before it tapped anything that could order."""


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


def money(text: str) -> float:
    return float(text.replace(",", ""))


def text_of(locator: Locator) -> str:
    try:
        return locator.inner_text(timeout=2000) if locator.count() else ""
    except PlaywrightTimeout:
        return ""


def shows_size(size: str, page: str) -> bool:
    """A card's "1 pc (0.35 g)" can be "0.35g" on the page: weights or volumes decide, else counts."""
    if re.search(rf"(?<!\w){re.escape(squash(size))}(?!\w)", squash(page)):
        return True
    wanted = quantities(size)
    measures = {q for q in wanted if q[1] not in ("pc", "pack")} or wanted
    return measures <= quantities(page)


def card(lines: list[str]) -> tuple[str, str] | None:
    """(name, size): the name is the line above the size, as some cards put a brand above the name."""
    body = [line for line in lines if not re.fullmatch(r"ADD|₹\s?[\d,.]+|[\d,.]+|OFF|₹", line)]
    if len(body) < 2:
        return None
    if at := next((i for i, line in enumerate(body) if i and is_size(line)), None):
        return body[at - 1], body[at]
    log.info("card without a size", lines=len(body))
    return max(body, key=len), ""


def core(name: str) -> str:
    """Without the "| tagline" that pages and cart rows drop."""
    return squash(name.split("|")[0])


def prices(lines: list[str], struck: list[str]) -> tuple[str, str]:
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


def match_by_name(expected: dict[Product, int], rows: list[dict], result: Cart, capped: dict[str, int]) -> None:
    """Cart rows ({lines, qty, struck}) against what was added; capped: variant -> the most the shop allowed."""
    seen = set()
    for row in rows:
        names = {core(line) for line in row["lines"]}
        product = next((p for p in expected if p not in seen and core(p.name) in names), None)
        if product is None:
            result.problems.append(f"unexpected item in the cart: {' '.join(row['lines'][:2])}")
            continue
        want, got = expected[product], row["qty"]
        if got < want and capped.get(product.variant) == got:
            log.info("quantity capped", product=product.name, asked=want, allowed=got)
            result.capped[product] = got
        elif got != want:
            result.problems.append(f"{product.name}: {got} in the cart, expected {want}")
            continue
        seen.add(product)
        if got == 1 and (price := prices(row["lines"], row["struck"])[0]):
            result.prices[product] = price
    result.problems += [f"{p.name} did not make it into the cart" for p in expected.keys() - seen]


class Browser:
    """One Chrome session. Loads and saves state_file (None: neither); fresh: starts logged out;
    record: the API map to add to.

    A shop sets NAME, TRIAL (offered to the admin only, placing off until turned on), PAYS, and
    the selectors below."""

    NAME = "the shop"
    TRIAL = False
    PAYS = ""
    ADDRESS = ""  # the delivery address the page shows
    PICKER_BUTTON = ""  # opens the address picker
    PICKER = ""  # shows once it is open
    SAVED_ADDRESSES = ""  # JS: tags each saved address with data-saved-address=<n>, returns their texts
    SEARCH = ""  # path, with {query}
    CARD = ""  # a search result
    CARDS_JS = ""  # JS: [{url, id, image, struck, lines}] of the results
    ORDERS = "/account/orders"

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

    def __enter__(self):
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
        self._opened()
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

    def _opened(self) -> None:
        """The page is open: a shop can start watching its replies here."""

    def _leaving(self) -> None:
        """Before every navigation, which discards the bodies of the page's replies."""

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
        here = Path(__file__).parent
        steps = [f.name for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).parent == here]
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
        self._leaving()
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

    def _wait(self, locator: Locator, seconds: int, state: str = "visible") -> bool:
        try:
            locator.wait_for(state=state, timeout=seconds * 1000)
            return True
        except PlaywrightTimeout:
            return False

    def _changed(self, locator: Locator, before: str, seconds: int = 15) -> bool:
        for _ in range(seconds * 4):
            self.page.wait_for_timeout(250)
            if text_of(locator) != before:
                self.page.wait_for_timeout(500)  # the next tap must not land mid-update
                return True
        return False

    def _tap_away(self, cart: Locator, tap: Locator) -> int:
        """Taps until tap is gone or stops changing the cart; how many taps."""
        taps = 0
        while tap.count() and taps < 100:
            before = text_of(cart)
            try:
                tap.click(timeout=10_000)
            except PlaywrightTimeout:
                break
            taps += 1
            if not self._changed(cart, before):
                break
        return taps

    def _latest_order(self) -> tuple[float, datetime] | None:
        """(amount, placed at) of the newest order on the orders page."""
        raise NotImplementedError

    def confirm(self, total: float, since: datetime) -> bool:
        """Whether the orders page shows an order of total, placed since."""
        for _ in range(3):
            self.goto(self.ORDERS)
            if latest := self._latest_order():
                amount, placed = latest
                log.info("latest order", amount=amount, placed=placed.isoformat(), total=total)
                if amount == total and placed >= since - timedelta(minutes=1):
                    return True
            else:
                log.info("no order on the orders page")
            self.page.wait_for_timeout(5000)
        return False

    def public_ip(self) -> str:
        self.goto("https://api.ipify.org")
        return self.page.locator("body").inner_text().strip()

    # Session and login

    def _logged_out(self) -> bool:
        raise NotImplementedError

    def _open_login(self) -> None:
        raise NotImplementedError

    def _logged_in(self) -> bool:
        """Waits for the login to land."""
        raise NotImplementedError

    def _address(self, shown: str) -> str:
        """The delivery address as "Label\\naddress", from what the page shows; '' if none is set."""
        return shown.strip()

    def delivery_address(self) -> str:
        return self._address(text_of(self.page.locator(self.ADDRESS).first))

    def check_session(self, rules: Rules) -> str:
        if not self.page.url.startswith(self.base):
            self.goto("/")
        shown = self._wait(self.page.locator(self.ADDRESS).first, 20)
        logged_out = self._logged_out()
        text = self.delivery_address() if shown else ""
        ok = rules.address_ok(text)
        log.info("session checked", logged_in=not logged_out, address_shown=bool(text), address_ok=ok)
        if logged_out:  # checked even with an address showing: logged-out pages can show one
            raise ShopError(LOGIN_EXPIRED)
        if not text:
            raise ShopError("could not read the delivery address")
        if not ok:
            raise ShopError(
                f"the shop is not set to deliver to '{rules.address_label}'", "the shop is set to another address"
            )
        return text

    def request_otp(self, phone: str) -> None:
        self.goto("/")
        self._open_login()
        phone_field = self.page.locator(PHONE_FIELD).filter(visible=True).first
        if not self._wait(phone_field, 20):
            inputs = self.page.locator("input:visible").evaluate_all(
                "l => l.map(i => [i.type, i.name, i.inputMode, i.autocomplete, i.maxLength].join(' '))"
            )
            log.info("no phone field", inputs=inputs)
            raise ShopError("the shop's login form did not open")
        phone_field.fill(phone)
        self.page.wait_for_timeout(500)
        button = self.page.get_by_role("button", name=CONTINUE).filter(visible=True)
        if button.count():
            button.first.click()
        else:
            phone_field.press("Enter")
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
        if not self._logged_in():
            raise ShopError("the shop did not accept that code")
        self.page.wait_for_timeout(3000)

    # The address picker

    def open_picker(self) -> bool:
        """Right after login the page can ignore clicks for a while: click until the picker shows."""
        picker = self.page.locator(self.PICKER).first
        for _ in range(3):
            self.page.locator(self.PICKER_BUTTON).first.click()
            if self._wait(picker, 8):
                return True
        return False

    def close_picker(self) -> None:
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(1000)

    def saved_addresses(self) -> list[str]:
        """Opens the address picker and lists its saved addresses, in order; pick one with pick_address."""
        if not self.open_picker():
            return []
        self.page.wait_for_timeout(1500)  # the list fills in after the picker shows
        try:
            found = self.page.evaluate(self.SAVED_ADDRESSES)
        except PlaywrightError:  # setup falls back to naming the address
            log.warning("could not read saved addresses", exc_info=True)
            found = []
        log.info("saved addresses read", saved=len(found))
        if not found:
            self.close_picker()
        return found

    def pick_address(self, n: int) -> str:
        """The nth of saved_addresses(); returns the delivery address the shop now shows."""
        self.page.locator(f"[data-saved-address='{n}']").first.click()
        self.page.wait_for_timeout(3000)
        self.goto("/")
        if not (address := self.delivery_address()):
            raise ShopError("the shop didn't switch to that address")
        return address

    # Search

    def _url(self, found: dict, name: str) -> str:
        """A search result's product URL, ending in the shop's id for the product."""
        return found["url"]

    def _image(self, src: str) -> str:
        """A search result's image as Telegram can show it."""
        return src

    def search(self, query: str, limit: int = 4) -> list[Product]:
        self.goto(self.SEARCH.format(query=quote(query)))
        if not self._wait(self.page.locator(self.CARD).first, 15):
            return []
        self.page.wait_for_timeout(1500)
        found, seen = [], set()
        for c in self.page.evaluate(self.CARDS_JS):
            lines = c["lines"]
            if {"Ad", "Sponsored"} & set(lines) or any(UNLISTED.fullmatch(line) for line in lines):
                continue
            if not (named := card(lines)):
                continue
            url = self._url(c, named[0])
            if url in seen:
                continue
            seen.add(url)
            price, mrp = prices(lines, c["struck"])
            image = "" if "placeholder" in c["image"].lower() else self._image(c["image"])
            found.append(Product(*named, url, price, image, mrp))
            if len(found) == limit:
                break
        return found

    def _size_shown(self, product: Product, text: str) -> bool:
        if shows_size(product.size, text):
            return True
        sizes = sorted({m.group() for m in QUANTITY.finditer(text)})[:20]
        log.info("size not on the page", product=product.name, size=product.size, page_sizes=sizes)
        return False
