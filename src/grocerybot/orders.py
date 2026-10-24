from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import structlog

from .accounts import Household
from .catalog import combine, rupees
from .config import Config
from .shop import Browser, Rules, ShopError

log = structlog.get_logger(__name__)


@dataclass
class Outcome:
    placed: bool = False
    total: float | None = None
    lines: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    why: dict[str, str] = field(default_factory=dict)  # unavailable item -> why it couldn't be added
    problems: list[str] = field(default_factory=list)
    item_total: float = 0
    waiting: bool = False
    held: bool = False
    unconfirmed: bool = False  # tapped Place Order, but no such order on the Orders page
    over_limit: bool = False
    cod: bool = False
    estimated: bool = False  # item_total from catalog prices: the shop was not opened

    @property
    def result(self) -> str:
        if self.placed:
            return "placed"
        if self.unconfirmed:
            return "unconfirmed"
        if self.waiting:
            return "waiting"
        if self.held:
            return "held"
        return "blocked" if self.problems else "cart ready"


def browser(cfg: Config, hh: Household, images: bool = False) -> Browser:
    return Browser(cfg.shop_url, hh.state_file, cfg.proxy, hh.debug_dir, images=images, record=cfg.api_map)


def login_browser(cfg: Config, state_file: Path) -> Browser:
    return Browser(cfg.shop_url, state_file, cfg.proxy, cfg.debug_dir, fresh=True)


def rules(hh: Household) -> Rules:
    return Rules(hh.address_label, hh.address_match, hh.daily_cap - hh.ledger().spent_today())


def exit_ip(cfg: Config) -> str:
    with Browser(cfg.shop_url, None, cfg.proxy, cfg.debug_dir) as b:
        return b.public_ip()


def check(cfg: Config, hh: Household) -> None:
    with browser(cfg, hh) as b:
        b.check_session(rules(hh))


def priced(hh: Household) -> tuple[list[tuple[str, int, float | None]], float, int]:
    """The basket at the prices last seen in the shop: (label, qty, amount or None) rows, total, unpriced."""
    catalog, rows = hh.catalog(), []
    for key, qty in hh.ledger().basket():
        product = (catalog.options(key) or [None])[0]
        price = rupees(product.price) if product else None
        rows.append((product.label() if product else key, qty, price * qty if price else None))
    return rows, sum(a or 0 for *_, a in rows), sum(a is None for *_, a in rows)


def order(cfg: Config, hh: Household, items: list[tuple[str, int]], hold: bool = False) -> Outcome:
    """hold: price the basket from the catalog and leave it there; the shop is opened only to place
    an order (or, with ordering off, to fill its cart). New items join the basket first, so a failure
    loses nothing.

    The attempt is written to the ledger before Place Order is tapped, so a crash can never lead
    to a silent second order: an unfinished attempt is reported, not retried."""
    catalog, ledger = hh.catalog(), hh.ledger()
    out, limits = Outcome(), rules(hh)
    items = combine(ledger.basket(), items, catalog)
    if hh.place_orders:
        ledger.set_basket(items)
    if limits.budget <= 0:
        out.over_limit = True
        out.problems.append(f"today's limit of ₹{hh.daily_cap:g} is used up")
        return out
    if hold and hh.place_orders:
        out.estimated = True
        _, out.item_total, unpriced = priced(hh)
        if over := limits.over(out.item_total):
            out.over_limit = True
            out.problems.append(over)
        else:
            out.waiting = not unpriced and out.item_total < hh.min_order
            out.held = not out.waiting
        return out

    with browser(cfg, hh) as b:
        b.check_session(limits)
        b.empty_cart()
        expected, urls, ids, added = {}, {}, {}, []
        for key, qty in items:
            options = catalog.options(key)
            for n, product in enumerate(options):
                if (skipped := b.add(product, qty)) is None:
                    expected[product.name], urls[product.name], ids[product.name] = qty, product.url, product.variant
                    added.append((key, product, n))
                    break
                log.info("option skipped", item=key, option=n, reason=skipped)
                out.why[key] = skipped
            else:
                out.unavailable.append(key)
        kept = [(k, q) for k, q in items if k in out.unavailable]  # stay in the basket, whatever happens
        if not expected:
            out.problems.append("none of the items can be added right now")
            return out

        cart = b.review_cart(expected, limits, ids)
        catalog.reprice({urls[name]: price for name, price in cart.prices.items()})
        placed_items = []
        for key, product, n in added:
            qty = cart.capped.get(product.name, expected[product.name])
            placed_items.append((key, qty))
            note = f"  (the most the shop allows, of {expected[product.name]})" if product.name in cart.capped else ""
            out.lines.append(f"{product.label()} ×{qty}" + ("  (backup choice)" if n else "") + note)
        out.total, out.problems, out.item_total, out.cod = cart.total, cart.problems, cart.item_total, cart.cod
        out.over_limit = cart.total is not None and cart.total > limits.budget
        if cart.problems:
            b.screenshot("cart")
            return out
        if cart.item_total < hh.min_order:
            out.waiting = True
            ledger.set_basket(placed_items + kept)
            return out
        if cart.cod and not b.has_pay_button(cart.total):
            out.problems.append(f"could not find the cart's Pay ₹{cart.total:g} button")
            b.screenshot("cart")
            return out
        if not hh.place_orders:
            return out

        attempt, started = ledger.begin(placed_items, cart.total), datetime.now()
        ledger.set_basket(kept)  # never ordered twice, even if the outcome is unclear
        log.info("attempt recorded", total=cart.total, cod=cart.cod)
        try:
            if stopped := (b.place_cod(cart.total) if cart.cod else b.place()):
                log.info("payment stopped", reason=stopped)
                b.screenshot("payment")
            out.placed = b.confirm(cart.total, started)
        except Exception as e:  # past the tap the order may have gone through: unconfirmed, never "not ordered"
            log.warning("placing interrupted", exc_info=True)
            stopped = str(e) if isinstance(e, ShopError) else "the shop stopped responding while placing it"
        out.unconfirmed = not out.placed
        if stopped and out.unconfirmed:
            out.problems.append(stopped)
        ledger.finish(attempt, "placed" if out.placed else "unknown")
        if out.unconfirmed:
            b.screenshot("unconfirmed")
        return out
