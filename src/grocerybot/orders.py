"""Turning a planned list into an order, safely."""

from dataclasses import dataclass, field
from datetime import datetime

from .catalog import Catalog, Product
from .config import Config
from .ledger import Ledger
from .shop import Browser, Rules


@dataclass
class Outcome:
    placed: bool = False
    total: float | None = None
    lines: list[str] = field(default_factory=list)  # what went into the cart
    unavailable: list[str] = field(default_factory=list)  # catalog keys none of whose options could be added
    problems: list[str] = field(default_factory=list)  # why it was not placed
    unconfirmed: bool = False  # Place Order was tapped but the order could not be found afterwards


def browser(cfg: Config, images: bool = False) -> Browser:
    return Browser(cfg.shop_url, cfg.state_file, cfg.proxy, cfg.debug_dir, images=images)


def rules(cfg: Config, ledger: Ledger) -> Rules:
    return Rules(cfg.address_label, cfg.address_match, cfg.min_order, cfg.daily_cap - ledger.spent_today())


def search(cfg: Config, query: str) -> list[Product]:
    with browser(cfg, images=True) as b:
        return b.search(query)


def check(cfg: Config) -> str:
    """Self-check: login valid, right address, traffic leaving from home. Raises ShopError."""
    with browser(cfg) as b:
        ip = b.public_ip()
        b.check_session(rules(cfg, Ledger(cfg.ledger_file)))
        return ip


def order(cfg: Config, catalog: Catalog, ledger: Ledger, items: list[tuple[str, int]]) -> Outcome:
    """Fill the cart with each item's first available option, verify every rule, then place it.

    The attempt is written to the ledger before Place Order is tapped, so a crash can never lead
    to a silent second order: an unfinished attempt is reported, not retried.
    """
    out, limits = Outcome(), rules(cfg, ledger)
    if limits.budget <= 0:
        out.problems.append(f"today's limit of ₹{cfg.daily_cap:g} is used up")
        return out

    with browser(cfg) as b:
        b.check_session(limits)
        b.empty_cart()
        expected, placed_items = {}, []
        for key, qty in items:
            options = catalog.options(key)
            for n, product in enumerate(options):
                if b.add(product, qty) is None:
                    expected[product.name] = qty
                    placed_items.append((key, qty))
                    out.lines.append(f"{product.label()} ×{qty}" + ("  (backup choice)" if n else ""))
                    break
            else:
                out.unavailable.append(key)
        if not expected:
            out.problems.append("none of the items can be added right now")
            return out

        cart = b.review_cart(expected, limits)
        out.total, out.problems = cart.total, cart.problems
        if cart.problems or not cfg.place_orders:
            if cart.problems:
                b.screenshot("cart")
            return out

        attempt, started = ledger.begin(placed_items, cart.total), datetime.now()
        b.place()
        out.placed = b.confirm(cart.total, started)
        out.unconfirmed = not out.placed
        ledger.finish(attempt, "placed" if out.placed else "unknown")
        if out.unconfirmed:
            b.screenshot("unconfirmed")
        return out
