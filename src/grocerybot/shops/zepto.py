"""Zepto: its markup and its app's own cart reply, as learned from production."""

import re
import time
from datetime import datetime
from urllib.parse import urlsplit

import structlog
from playwright.sync_api import Locator, Request

from ..catalog import Product, squash
from . import base
from .base import LOGIN_EXPIRED, STRUCK, Browser, Cart, NotPlaced, Rules, ShopError, core, match_by_name, money, text_of

WALLET = re.compile(r"\bCash\b(?! on [Dd]elivery)")  # the prepaid wallet
COINS = re.compile(r"\bCoins\b")  # reward coins: never used
# Wallet short: the cart's pay button opens Payment Options; "Pay by Cash / UPI on delivery" shows "Pay ₹N on delivery".
PAY = re.compile(r"^\s*(?:click to )?pay ₹\s?([\d,.]+)(?: on delivery)?\s*$", re.I)
COD = re.compile(r"^\s*pay by cash\b.*on delivery\s*$", re.I)
PAY_COD = re.compile(r"^\s*pay ₹\s?([\d,.]+) on delivery\s*$", re.I)
LOGGED_OUT = "[data-testid=login-btn]"
CART_COUNT = "[data-testid=cart-items-number]"
CART = "[data-testid=cart-screen]"
CART_API = "/cfs/api/v1/cart"
STOCK = re.compile(r"out of stock|sold out|unavailable|back in stock", re.I)
REMOVE = re.compile(r"^\s*(remove|delete)(\s+item)?\s*$", re.I)
# The cart shows the address and wallet balance: only known words are logged.
LOGGABLE = re.compile(rf"^(item total|to pay|remove|delete)$|{STOCK.pattern}", re.I)
ADDRESS = "[data-testid=user-address]"
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
log = structlog.get_logger(__name__)


def _amount(text: str, label: str) -> float | None:
    """The bill line's amount, after any struck-out one."""
    m = re.search(label + r"\s*\n(?:-?₹[\d,.]+\s*\n)?-?₹([\d,.]+)", text)
    return money(m.group(1)) if m else None


def _owed(cart: dict) -> bool:
    return (_amount(cart["text"], "To Pay") or 0) > 0


def _loggable(text: str) -> list[str]:
    return [line for line in map(str.strip, text.split("\n")) if LOGGABLE.search(line)][:40]


def _rupees(paise: int) -> str:
    return f"₹{paise // 100}" if paise % 100 == 0 else f"₹{paise / 100:.2f}"


class Zepto(Browser):
    NAME = "Zepto"
    PAYS = "from its wallet, or cash on delivery when that falls short"
    ADDRESS = ADDRESS
    PICKER_BUTTON = f"button:has({ADDRESS}), {ADDRESS}"  # the header button around the address opens it
    PICKER = f":is({MODAL}):visible"
    # The picker's markup has no stable ids: a saved address is the innermost clickable block of two or
    # more lines with a digit or comma after its name.
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
    SEARCH = "/search?query={query}"
    CARD = "a[data-testid=product-card]"
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

    def _image(self, src: str) -> str:
        return src.replace("f-auto", "f-jpg")

    def _opened(self) -> None:
        self._carts: list[tuple[float, Request]] = []  # the shop app's own cart calls, newest last
        self._cart: dict | None = None
        self._cart_at = 0.0
        self.page.on("requestfinished", self._seen)  # not "response": a navigation can cut its body off forever

    def _leaving(self) -> None:
        self._cart_api()

    def _seen(self, req: Request) -> None:
        """Keeps it, unread: reading the body here, inside Playwright's event, is not safe."""
        if req.method == "PUT" and urlsplit(req.url).path == CART_API:
            self._carts.append((time.monotonic(), req))

    def _cart_api(self) -> dict | None:
        """The latest cart reply, as far as the bot reads it: never its address or wallet parts."""
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

    def _logged_out(self) -> bool:
        return bool(self.page.locator(LOGGED_OUT).filter(visible=True).count())

    def _open_login(self) -> None:
        self.page.locator(LOGGED_OUT).first.click()

    def _logged_in(self) -> bool:
        return self._wait(self.page.locator(LOGGED_OUT).first, 30, "detached") and self._wait(
            self.page.locator(ADDRESS).first, 20
        )

    def _address(self, shown: str) -> str:
        return "" if shown.strip().startswith("Select") else shown.strip()

    def choose_address(self, label: str) -> None:
        if self.page.locator(ADDRESS).first.inner_text().startswith(label):
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

    def add(self, product: Product, qty: int) -> str | None:
        """None on success, else why not, for people."""
        self.goto(product.url)
        title, headings = self.page.title(), self.page.locator("h1").all_inner_texts()
        if not any(squash(t).startswith(core(product.name)) for t in [title, *headings]):
            log.info("listing changed", product=product.name, title=title, headings=headings)
            return "the shop changed this product"
        if not self._size_shown(product, self.page.locator("body").inner_text()):
            return "the size you picked isn't sold any more"
        add = self.page.locator(PLUS, has_text="Add to Cart")
        if not add.count():
            return "out of stock"
        for _ in range(2):  # a tap on a page still loading can be lost
            add.first.evaluate("b => b.click()")  # the sticky search bar overlaps it for mouse clicks
            if self._wait(add.first, 5, "hidden"):
                break
        else:
            return "the shop didn't take it into the cart"
        for _ in range(qty - 1):
            self.page.locator(PLUS).first.evaluate("b => b.click()")
            self.page.wait_for_timeout(1000)
        badge = self.page.locator(CART_COUNT)
        log.info("added", product=product.name, qty=qty, cart_count=badge.first.inner_text() if badge.count() else None)
        return None

    def _open_cart(self) -> dict:
        self.page.locator("[data-testid=cart-btn]").first.click()
        cart = self.page.locator(CART).first
        if not self._wait(cart, base.TIMEOUT_MS // 1000):
            raise ShopError("the cart did not open")
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
        taps = self._tap_away(cart, tap)
        left = text_of(cart)
        # The count, not the cart's words: an emptied cart can say "All items are out of stock".
        stuck = not self._wait(badge, 10, "hidden")
        log.info("cart emptied", taps=taps, stuck=stuck, lines=_loggable(left))
        if stuck:
            self.screenshot("cart-stuck")
            raise ShopError("the shop's cart has items the bot can't remove, maybe out of stock; empty it in the app")
        self.page.keyboard.press("Escape")

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
            match_by_name(expected, cart["items"], result, {})
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

    def ready(self, cart: Cart) -> str | None:
        """Taps nothing: the wallet's Place Order is the cart's own button."""
        if cart.cod and not self._pay_button(PAY, cart.total, 5):
            labels = self.page.locator(f"{CART} button").all_inner_texts()
            log.info("no pay button", labels=[t for t in labels if re.search(r"₹|pay|order", t, re.I)])
            return f"could not find the cart's Pay ₹{cart.total:g} button"
        return None

    def place(self, cart: Cart) -> str | None:
        return self._place_cod(cart.total) if cart.cod else self._place_order()

    def _place_order(self) -> None:
        self.page.locator(f"{CART} button", has_text="Place Order").click()
        log.info("tapped", button="Place Order")
        self.page.wait_for_timeout(3000)
        self._skip_upsell("Place Order")
        self.page.wait_for_timeout(15_000)  # "Confirming your order..."

    def _place_cod(self, total: float) -> str | None:
        """None once "Pay ₹total on delivery" is tapped, else where it stopped."""
        if not (button := self._pay_button(PAY, total, 5)):
            raise NotPlaced(f"the cart's Pay ₹{total:g} button went away")
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
        if not (m and money(m.group(1)) == total):
            log.info("pay button amount differs", button=label, total=total)
            return None
        return button

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

    def _latest_order(self) -> tuple[float, datetime] | None:
        body = self.page.locator("body").inner_text().split("Log Out", 1)[-1]
        m = re.search(r"Order [^\n]+?\s+₹([\d,.]+)\s+Placed at (\d+)\w* (\w+ \d{4}), (\d+:\d+ [ap]m)", body)
        if not m:
            return None
        return money(m.group(1)), datetime.strptime(f"{m.group(2)} {m.group(3)} {m.group(4)}", "%d %b %Y %I:%M %p")
