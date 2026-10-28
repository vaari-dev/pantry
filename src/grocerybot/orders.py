from dataclasses import dataclass, field

import structlog

from .accounts import Household
from .catalog import combine, rupees
from .shop import Rules, ShopError, Zepto

log = structlog.get_logger(__name__)


@dataclass
class Outcome:
    placed: bool = False
    total: float | None = None
    lines: list[str] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)  # item -> why it couldn't be added
    problems: list[str] = field(default_factory=list)
    item_total: float = 0
    waiting: bool = False
    held: bool = False
    unconfirmed: bool = False  # the order call was made, but no new order seen
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


def rules(hh: Household) -> Rules:
    return Rules(hh.address_id, hh.daily_cap - hh.ledger().spent_today())


def check(hh: Household) -> None:
    """Also renews the login before it lapses."""
    with Zepto(hh.state_file) as z:
        z.deliver_to(hh.address_id)


def priced(hh: Household) -> tuple[list[tuple[str, int, float | None]], float, int]:
    """The basket at the prices last seen in the shop: (label, qty, amount or None) rows, total, unpriced."""
    catalog, rows = hh.catalog(), []
    for key, qty in hh.ledger().basket():
        product = (catalog.options(key) or [None])[0]
        price = rupees(product.price) if product else None
        rows.append((product.label() if product else key, qty, price * qty if price else None))
    return rows, sum(a or 0 for *_, a in rows), sum(a is None for *_, a in rows)


def order(hh: Household, items: list[tuple[str, int]], hold: bool = False) -> Outcome:
    """hold: price the basket from the catalog and leave it there; the shop is used only to place
    an order (or, with ordering off, to fill its cart). New items join the basket first, so a failure
    loses nothing.

    The attempt is written to the ledger before the order call, so a crash can never lead
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

    with Zepto(hh.state_file) as z:
        z.deliver_to(limits.address_id)
        expected, added, capped = {}, [], {}
        for key, qty in items:
            why = None
            for n, product in enumerate(catalog.options(key)):
                if isinstance(allowed := z.stock(product), int):
                    expected[product] = min(qty, allowed)
                    if allowed < qty:
                        log.info("quantity capped", asked=qty, allowed=allowed)
                        capped[product] = qty
                    added.append((key, product, n))
                    break
                log.info("option skipped", item=key, option=n, reason=allowed)
                why = why or allowed
            else:
                out.unavailable[key] = why or "no choice saved"
        kept = [(k, q) for k, q in items if k in out.unavailable]  # stay in the basket, whatever happens
        if not expected:
            out.problems.append("none of the items can be added right now")
            return out

        z.fill(expected)
        cart = z.review(expected, limits)
        catalog.reprice({p.url: price for p, price in cart.prices.items()})
        placed_items = []
        for key, product, n in added:
            placed_items.append((key, expected[product]))
            note = f"  (the most the shop allows, of {capped[product]})" if product in capped else ""
            out.lines.append(f"{product.label()} ×{expected[product]}" + ("  (backup choice)" if n else "") + note)
        out.total, out.problems, out.item_total, out.cod = cart.total, cart.problems, cart.item_total, cart.cod
        out.over_limit = cart.total is not None and cart.total > limits.budget
        if cart.problems:
            return out
        if cart.item_total < hh.min_order:
            out.waiting = True
            ledger.set_basket(items)
            return out
        if not hh.place_orders:
            return out

        before = z.latest_order()
        attempt = ledger.begin(placed_items, cart.total)
        ledger.set_basket(kept)  # never ordered twice, even if the outcome is unclear
        log.info("attempt recorded", total=cart.total, cod=cart.cod)
        stopped = placed = None
        try:
            placed = z.place(limits, cart.cod)
        except Exception as e:  # it may have gone through all the same: the history tells
            log.warning("placing interrupted", exc_info=True)
            stopped = str(e) if isinstance(e, ShopError) else "the shop stopped answering while placing it"
        try:
            out.placed = z.confirm(before, placed)
        except Exception:
            log.warning("order history not read", exc_info=True)
        out.unconfirmed = not out.placed
        if stopped and out.unconfirmed:
            out.problems.append(stopped)
        ledger.finish(attempt, "placed" if out.placed else "unknown")
        return out
