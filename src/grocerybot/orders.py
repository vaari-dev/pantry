"""A list -> a checked, placed order."""

from dataclasses import dataclass, field
from datetime import datetime

import structlog

from .catalog import Catalog, Product, combine
from .config import Config
from .ledger import Ledger
from .shop import Browser, Rules

log = structlog.get_logger(__name__)


@dataclass
class Outcome:
    placed: bool = False
    total: float | None = None
    lines: list[str] = field(default_factory=list)  # added to the cart
    unavailable: list[str] = field(default_factory=list)  # keys with no addable option
    problems: list[str] = field(default_factory=list)  # why not placed
    item_total: float = 0
    waiting: bool = False  # under the minimum; in the basket

    @property
    def result(self) -> str:
        if self.placed:
            return "placed"
        if self.unconfirmed:
            return "unconfirmed"
        if self.waiting:
            return "waiting"
        return "blocked" if self.problems else "cart ready"

    unconfirmed: bool = False  # tapped, not found on Orders page


def browser(cfg: Config, images: bool = False) -> Browser:
    return Browser(cfg.shop_url, cfg.state_file, cfg.proxy, cfg.debug_dir, images=images)


def rules(cfg: Config, ledger: Ledger) -> Rules:
    return Rules(cfg.address_label, cfg.address_match, cfg.daily_cap - ledger.spent_today())


def search(cfg: Config, query: str) -> list[Product]:
    with browser(cfg, images=True) as b:
        return b.search(query)


def check(cfg: Config) -> str:
    """Login, address, home exit. Returns the exit IP; raises ShopError."""
    with browser(cfg) as b:
        ip = b.public_ip()
        b.check_session(rules(cfg, Ledger(cfg.ledger_file)))
        return ip


def order(cfg: Config, catalog: Catalog, ledger: Ledger, items: list[tuple[str, int]]) -> Outcome:
    """Basket + list -> cart (first available option each) -> checks -> place once at the minimum.

    The attempt is recorded before Place Order, so a crash never causes a second order.
    """
    out, limits = Outcome(), rules(cfg, ledger)
    if limits.budget <= 0:
        out.problems.append(f"today's limit of ₹{cfg.daily_cap:g} is used up")
        return out

    items = combine(ledger.basket(), items, catalog)
    with browser(cfg) as b:
        b.check_session(limits)
        b.empty_cart()
        expected, placed_items = {}, []
        for key, qty in items:
            options = catalog.options(key)
            for n, product in enumerate(options):
                if (skipped := b.add(product, qty)) is None:
                    expected[product.name] = qty
                    placed_items.append((key, qty))
                    out.lines.append(f"{product.label()} ×{qty}" + ("  (backup choice)" if n else ""))
                    break
                log.info("option skipped", item=key, option=n, reason=skipped)
            else:
                out.unavailable.append(key)
        if not expected:
            out.problems.append("none of the items can be added right now")
            return out

        cart = b.review_cart(expected, limits)
        out.total, out.problems, out.item_total = cart.total, cart.problems, cart.item_total
        if cart.problems:
            b.screenshot("cart")
            return out
        if cart.item_total < cfg.min_order:
            out.waiting = True
            ledger.set_basket(placed_items)
            return out
        if not cfg.place_orders:
            return out

        attempt, started = ledger.begin(placed_items, cart.total), datetime.now()
        ledger.set_basket([])  # never twice
        b.place()
        out.placed = b.confirm(cart.total, started)
        out.unconfirmed = not out.placed
        ledger.finish(attempt, "placed" if out.placed else "unknown")
        if out.unconfirmed:
            b.screenshot("unconfirmed")
        return out
