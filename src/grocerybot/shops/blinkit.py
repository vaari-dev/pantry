"""Blinkit, on trial: its markup as a public Playwright project (blinkit-mcp, Sep 2026) drives it,
not yet seen by this bot in production. Every read that finds nothing is a problem, so the order
stops; logs and the API map gather what the next step needs.

Cash on delivery only: the bot never picks a wallet, card or UPI here.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import quote

import structlog
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..catalog import Product
from .base import LOGIN_EXPIRED, STRUCK, Browser, Cart, Rules, ShopError, match_by_name, money, text_of

LOGIN_WAIT_MS = 30_000
AUTH_JS = """() => { try { return !!JSON.parse(localStorage.getItem('auth') || '{}').accessToken; }
                     catch (e) { return false; } }"""
LOCATION = "[class*=LocationBar__Container]"
ADDRESS_ITEM = "[class*=AddressItemWrapper]"
LOGIN = "[class*=ProfileButton__Container]"
CARD = "div[role=button][id]"
PLUS, MINUS = ".icon-plus", ".icon-minus"
CAP = "Sorry, you can't add more of this item"
CART_BUTTON = "[class*=CartButton__Button], [class*=CartButton__Container]"
CART = "[class*=CartWrapper__CartContainer], [class*=CartDrawer], [class*=CartSidebar], .cart-modal-rn"
PROCEED = re.compile(r"^\s*proceed", re.I)
PAYMENT = "#payment_widget"
SELECTED = "[aria-checked=true], [aria-selected=true], [aria-pressed=true], input:checked"
SELECTED_JS = f"el => el.matches({SELECTED!r}) || !!el.querySelector({SELECTED!r})"
STATES_JS = """el => [el, ...el.querySelectorAll('*')].flatMap(e => [...e.attributes]
    .filter(a => a.name.startsWith('aria-') && /^(true|false)$/.test(a.value)).map(a => a.name + '=' + a.value))"""
PAY_NOW = re.compile(r"^\s*pay now\s*$", re.I)
ORDER_CARD = "div[class*=BffOrderHistory] > div > div"
CLOSED = re.compile(r"sorry, can't take your order|currently unavailable|store is closed", re.I)
NOT_ADDRESS = re.compile(r"(?i)^delivery in\b|\bminutes?$")
AMOUNT = re.compile(r"₹\s?([\d,]+(?:\.\d+)?)")
PLACED = re.compile(r"(\d{1,2}) ([A-Z][a-z]{2}),? (?:(\d{4}),? )?(\d{1,2}:\d{2} ?[ap]m)", re.I)
LINES = "el => el.innerText.split('\\n').map(s => s.trim()).filter(Boolean)"
CART_JS = f"""() => {{ {STRUCK}
  const lines = {LINES};
  const cart = [...document.querySelectorAll({CART!r})].find(el => el.checkVisibility());
  if (!cart) return null;
  const rows = [...cart.querySelectorAll('[class*=DefaultProductCard__Container], [class*=CartProduct__Container]')]
    .map(r => {{ const l = lines(r), n = l.filter(s => /^\\d+$/.test(s)).pop();
                return {{lines: l, qty: n ? parseInt(n, 10) : 0, struck: struck(r)}}; }});
  const bill = [...cart.querySelectorAll('[class*=BillCard__BillItemContainer]')].map(lines);
  const strips = [...cart.querySelectorAll('[class*=ListStrip__Heading]')]
    .map(h => [h, h.parentElement.querySelector('[class*=ListStrip__SubHeading]')]
      .filter(Boolean).map(e => e.innerText.trim()).join('\\n'));
  return {{text: cart.innerText, rows, bill, strips}};
}}"""
log = structlog.get_logger(__name__)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _bill(bill: list[list[str]], label: str) -> float | None:
    """The amount of the first bill line whose label matches: the last one shown, after any struck one."""
    for lines in bill:
        if lines and re.match(label, lines[0], re.I):
            if any(line.upper() == "FREE" for line in lines[1:]):
                return 0.0
            amounts = [money(m.group(1)) for line in lines[1:] if (m := AMOUNT.fullmatch(line))]
            return amounts[-1] if amounts else None
    return None


def _placed(text: str, now: datetime) -> datetime | None:
    """ "25 Oct, 3:44 pm": the year is this one, or last year's for a date ahead of today."""
    if not (m := PLACED.search(text)):
        return None
    day, month, year, at = m.groups()
    placed = datetime.strptime(f"{day} {month} {year or now.year} {at.replace(' ', '')}", "%d %b %Y %I:%M%p")
    return placed.replace(year=placed.year - 1) if not year and placed > now + timedelta(days=1) else placed


class Blinkit(Browser):
    NAME = "Blinkit"
    TRIAL = True
    PAYS = "cash on delivery"
    ADDRESS = LOCATION
    PICKER_BUTTON = LOCATION
    PICKER = ADDRESS_ITEM
    SAVED_ADDRESSES = f"""() => {{
    const lines = {LINES};
    document.querySelectorAll('[data-saved-address]').forEach(el => el.removeAttribute('data-saved-address'));
    return [...document.querySelectorAll({ADDRESS_ITEM!r})].filter(el => el.checkVisibility())
      .map((el, n) => {{ el.setAttribute('data-saved-address', n); return lines(el).join('\\n'); }});
}}"""
    SEARCH = "/s/?q={query}"
    CARD = CARD
    CARDS_JS = f"""() => {{ {STRUCK}
  const found = [...document.querySelectorAll({CARD!r})]
    .filter(el => /(^|\\n)ADD(\\n|$)/.test(el.innerText) && el.innerText.includes('₹'));
  return found.filter(el => !found.some(o => o !== el && el.contains(o))).map(el => ({{
    id: el.id, image: (el.querySelector('img') || {{}}).currentSrc || '', struck: struck(el), lines: ({LINES})(el)}}));
}}"""

    def __enter__(self):
        super().__enter__()
        self._capped: dict[str, int] = {}  # variant -> the quantity the shop stopped at
        return self

    def _url(self, found: dict, name: str) -> str:
        return f"/prn/{_slug(name)}/prid/{found['id']}"

    def _logged_out(self) -> bool:
        return not self.page.evaluate(AUTH_JS)

    def _open_login(self) -> None:
        if self.page.get_by_text("Detect my location").filter(visible=True).count():
            self.page.keyboard.press("Escape")  # the location prompt a fresh visit opens covers the header
        self.page.get_by_text("Login", exact=True).filter(visible=True).or_(self.page.locator(LOGIN)).first.click()

    def _logged_in(self) -> bool:
        try:
            self.page.wait_for_function(AUTH_JS, timeout=LOGIN_WAIT_MS)
        except PlaywrightTimeout:
            return False
        return self._wait(self.page.locator(LOCATION).first, 20)

    def _address(self, shown: str) -> str:
        """The location bar adds the delivery time; the cart says "Delivering to Home"."""
        lines = [line for line in map(str.strip, shown.splitlines()) if line and not NOT_ADDRESS.search(line)]
        text = re.sub(r"(?i)^delivering to\s+", "", "\n".join(lines))
        return "" if re.match(r"(?i)select|detect", text) else text

    def open_picker(self) -> bool:
        """After login the picker can already be open, over the bar that opens it."""
        return self._wait(self.page.locator(ADDRESS_ITEM).first, 3) or super().open_picker()

    def choose_address(self, label: str) -> None:
        saved = self.saved_addresses()
        if (n := next((n for n, a in enumerate(saved) if a.splitlines()[0] == label), None)) is None:
            raise ShopError(
                f"there is no saved address called '{label}'; add it in Blinkit's app first",
                "no saved address by that name",
            )
        self.pick_address(n)

    def _card(self, product: Product):
        return self.page.locator(f"{CARD}[id='{product.variant}']").first

    def add(self, product: Product, qty: int) -> str | None:
        """From the search page, where each result is the product's own card."""
        self.goto(self.SEARCH.format(query=quote(product.name)))
        card = self._card(product)
        if not self._wait(card, 15):
            log.info("listing changed", product=product.name)
            return "the shop changed this product"
        if not self._size_shown(product, text_of(card)):
            return "the size you picked isn't sold any more"
        add = card.get_by_text("ADD", exact=True)
        if not add.count():
            return "out of stock"
        for _ in range(2):  # a tap on a page still loading can be lost
            add.first.click()
            if self._wait(card.locator(PLUS).first, 5):
                break
        else:
            return "the shop didn't take it into the cart"
        for n in range(1, qty):
            before = text_of(card)
            card.locator(PLUS).first.click()
            if self.page.get_by_text(CAP).filter(visible=True).count():
                self._capped[product.variant] = n
                break
            if not self._changed(card, before, 5):
                break
        log.info("added", product=product.name, qty=qty, capped=self._capped.get(product.variant))
        return None

    def _open_cart(self) -> dict | None:
        """None when the header shows an empty cart: it has no amount on it then ("My Cart")."""
        button = self.page.locator(CART_BUTTON).first
        if "₹" not in text_of(button):
            return None
        button.click()
        if not self._wait(self.page.locator(CART).first, 30):
            raise ShopError("the cart did not open")
        self.page.wait_for_timeout(1500)
        read = self.page.evaluate(CART_JS) or {"text": "", "rows": [], "bill": [], "strips": []}
        log.info(
            "cart read",
            quantities=[r["qty"] for r in read["rows"]],
            bill=[lines[0] for lines in read["bill"] if lines],  # labels only: amounts can hold the wallet
            strips=len(read["strips"]),
            closed=bool(CLOSED.search(read["text"])),
        )
        return read

    def empty_cart(self) -> None:
        """Raises if anything is left."""
        if not (read := self._open_cart()):
            return
        cart = self.page.locator(CART).filter(visible=True).first
        taps = 0
        while (tap := cart.locator(MINUS).first).count() and taps < 100:
            before = text_of(cart)
            try:
                tap.click(timeout=10_000)
            except PlaywrightTimeout:
                break
            taps += 1
            if not self._changed(cart, before):
                break
        read = self.page.evaluate(CART_JS) or {"rows": []}
        log.info("cart emptied", taps=taps, left=len(read["rows"]))
        if read["rows"]:
            self.screenshot("cart-stuck")
            raise ShopError("the shop's cart has items the bot can't remove; empty it in the app")
        self.page.keyboard.press("Escape")

    def review_cart(self, expected: dict[Product, int], rules: Rules) -> Cart:
        result = Cart(cod=True)
        read = self._open_cart()
        if read and CLOSED.search(read["text"]):
            raise ShopError("Blinkit isn't taking orders at this address right now")
        if not read or not read["rows"]:
            if self._logged_out():
                raise ShopError(LOGIN_EXPIRED)
            result.problems.append("the cart is empty: the shop did not take the items")
            return result
        if not any(rules.address_ok(self._address(s)) for s in read["strips"]):
            result.problems.append("the cart is not delivering to your saved address (/settings)")
        match_by_name(expected, read["rows"], result, self._capped)
        item_total, result.total = _bill(read["bill"], r"items? total"), _bill(read["bill"], r"grand total|to pay")
        if item_total is None:
            result.problems.append("could not read the item total")
        else:
            result.item_total = item_total
        if result.total is None:
            result.problems.append("could not read the amount to pay")
        elif over := rules.over(result.total):
            result.problems.append(over)
        return result

    def ready(self, cart: Cart) -> str | None:
        """Proceeds to payment and selects cash; the order is placed only by place's Pay Now."""
        proceed = self.page.locator(CART).get_by_text(PROCEED).filter(visible=True)
        if not proceed.count():
            return "the cart has no Proceed button"
        proceed.last.click()
        log.info("tapped", button="Proceed")
        widget = self.page.frame_locator(PAYMENT)
        cash = widget.locator("div[title='Cash']").first
        if not self._wait(cash, 30):
            log.info("no cash option", widget=bool(self.page.locator(PAYMENT).count()))
            return "cash on delivery is not offered for this order"
        if cash.get_attribute("disabled") is not None or cash.locator("[aria-disabled=true]").count():
            return "cash on delivery is not offered for this order"
        cash.locator("[role=button]").or_(cash).first.click()
        log.info("tapped", button="Cash")
        self.page.wait_for_timeout(2000)
        if not cash.evaluate(SELECTED_JS):  # else Pay Now pays by the account's default, maybe UPI or a card
            log.info("cash not selected", states=cash.evaluate(STATES_JS))
            return "could not select cash on delivery"
        shown = {money(m) for m in AMOUNT.findall(text_of(widget.locator("body")))}
        if cart.total not in shown:
            log.info("payment amount differs", total=cart.total, shown=sorted(shown))
            return f"the payment page doesn't show ₹{cart.total:g}"
        return None

    def place(self, cart: Cart) -> str | None:
        pay = self.page.locator("button, [class*=Zpayments__Button]").filter(has_text=PAY_NOW).filter(visible=True)
        if not pay.count():
            pay = self.page.frame_locator(PAYMENT).get_by_text(PAY_NOW).filter(visible=True)
        if not pay.count():
            return "no Pay Now button"
        pay.first.click()
        log.info("tapped", button="Pay Now")
        self.page.wait_for_timeout(15_000)
        return None

    def confirm(self, total: float, since: datetime) -> bool:
        for _ in range(3):
            self.goto("/account/orders")
            latest = self.page.locator(ORDER_CARD).filter(has_text=AMOUNT).filter(has_text=PLACED).first
            if self._wait(latest, 15):
                text = text_of(latest)
                amount, placed = AMOUNT.search(text), _placed(text, datetime.now())
                log.info(
                    "latest order",
                    amount=amount and money(amount.group(1)),
                    placed=placed and placed.isoformat(),
                    total=total,
                )
                if amount and placed and money(amount.group(1)) == total and placed >= since - timedelta(minutes=1):
                    return True
            else:
                log.info("no order on the orders page")
            self.page.wait_for_timeout(5000)
        return False
