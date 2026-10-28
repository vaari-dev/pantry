import dataclasses
import statistics
from dataclasses import dataclass, field

import structlog

from .accounts import Household
from .catalog import PACK, QUANTITY, Choice, Product, Qty, amount, combine, measure, rupees, same_product, shown
from .fill import MAX_PACKS, Offer, cheapest
from .shop import Rules, ShopError, Zepto

log = structlog.get_logger(__name__)


@dataclass
class Outcome:
    placed: bool = False
    total: float | None = None
    lines: list[str] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)  # item -> why it couldn't be added
    questions: dict[str, str] = field(default_factory=dict)  # item -> whether to add another product for it
    problems: list[str] = field(default_factory=list)
    item_total: float = 0
    waiting: bool = False
    held: bool = False
    unconfirmed: bool = False  # the order call was made, but no new order seen
    over_limit: bool = False
    cod: bool = False
    estimated: bool = False  # item_total from catalog prices

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
        if self.questions:
            return "asking"
        return "blocked" if self.problems else "cart ready"


def rules(hh: Household) -> Rules:
    address_id = (hh.ledger().address() or [hh.address_id])[0]
    return Rules(address_id, hh.daily_cap - hh.ledger().spent_today())


def check(hh: Household) -> None:
    """Also renews the login before it lapses."""
    with Zepto(hh.state_file) as z:
        z.deliver_to(hh.address_id)


def _wants(qty: Qty, choice: Choice) -> tuple[int, str, str]:
    """(how much, its unit, a note when the sizes can't tell)."""
    if want := choice.wants(qty):
        return *want, ""
    return 1, PACK, f"its size doesn't say how much makes {qty}"


def _got(take: dict[Product, int], offers: list[Offer]) -> int:
    size = {o.product: o.size for o in offers}
    return sum(size[p] * n for p, n in take.items())


def _as(value: int, unit: str) -> str:
    return f"{value} pack{'s' * (value != 1)}" if unit == PACK else amount(value, unit)


def _parts(take: dict[Product, int]) -> str:
    """ "Amul Cow Milk 1 l x1 + 500 ml x2" """
    parts, last = [], None
    for p, n in take.items():
        name = QUANTITY.sub(" ", p.name).strip() or p.name
        parts.append(f"{'' if name == last else name + ' '}{p.size} ×{n}".strip())
        last = name
    return " + ".join(parts)


def _cost(take: dict[Product, int]) -> float | None:
    prices = [rupees(p.price) for p in take]
    return sum(price * n for price, n in zip(prices, take.values(), strict=True)) if take and all(prices) else None


def _sizes(choice: Choice, unit: str) -> list[Product]:
    """By the piece, only the usual pack counts."""
    return choice.sizes[:1] if unit == PACK else list(choice.sizes)


def _size(p: Product, unit: str) -> int:
    """How much one pack of p is, in unit; 0 if it isn't sold that way."""
    if unit == PACK:
        return 1
    return m[0] if (m := measure(p.size)) and m[1] == unit else 0


def line(key: str, qty: Qty, take: dict[Product, int], note: str = "") -> str:
    """ "Cow milk 2 l: Amul Cow Milk 1 l x1 + 500 ml x2 · ₹142" """
    cost = _cost(take)
    text = f"{key[:1].upper()}{key[1:]} {shown(qty)}: {_parts(take)}"
    return text + (f" · ₹{cost:g}" if cost else "") + (f"  ({note})" if note else "")


def priced(hh: Household) -> tuple[list[tuple[str, float | None]], float, int]:
    """The basket at the prices last seen in the shop, its usual products without the shop's limits:
    (line, amount or None) rows, total, unpriced."""
    catalog, rows = hh.catalog(), []
    for key, qty in hh.ledger().basket():
        if not (choices := catalog.choices(key)):
            rows.append((f"{key} {shown(qty)}", None))
            continue
        want, unit, note = _wants(qty, choices[0])
        sizes = [(p, size) for p in _sizes(choices[0], unit) if (size := _size(p, unit))]
        take = cheapest(want, [Offer(p, size, MAX_PACKS, rupees(p.price)) for p, size in sizes])
        rows.append((line(key, qty, take, note), _cost(take)))
    return rows, sum(a or 0 for _, a in rows), sum(a is None for _, a in rows)


def usual_pack(choice: Choice, qty: Qty) -> Product | None:
    """For an amount, the size its cheapest mix uses most; None for a count."""
    if isinstance(qty, int) or not (want := choice.wants(qty)):
        return None
    sizes = [Offer(p, size, MAX_PACKS, rupees(p.price)) for p in choice.sizes if (size := _size(p, want[1]))]
    take = cheapest(want[0], sizes)
    return max(take, key=take.get, default=None)


def unusual(hh: Household, key: str, qty: Qty) -> Qty | None:
    """What the household usually gets of key, when qty is more than three times that; before any
    order, more than three times three usual packs. Else None."""
    if not (choices := hh.catalog().choices(key)) or not (want := choices[0].wants(qty)):
        return None
    before = [w[0] for q in hh.ledger().bought(key) if (w := choices[0].wants(q)) and w[1] == want[1]]
    usual = statistics.median(before) if before else choices[0].wants(1)[0]
    if want[0] <= 3 * usual * (1 if before else 3):
        return None
    if want[1] == PACK or (isinstance(qty, int) and not before):
        return max(1, round(usual / (choices[0].wants(1)[0])))
    return amount(round(usual), want[1])


def _offers(z: Zepto, choice: Choice, unit: str) -> tuple[list[Offer], str | None]:
    """The choice's sizes on sale now, also ones not seen before; and why none, if so."""
    sizes = _sizes(choice, unit)
    if unit != PACK:
        try:
            found = z.search(choice.name, limit=12)
        except ShopError:
            found = []
        sizes += [p for p in found if same_product(p, choice.sizes[0]) and all(p.variant != s.variant for s in sizes)]
    offers, why = [], None
    for p in sizes:
        if not (size := _size(p, unit)):
            continue
        if isinstance(most := z.stock(p), int):
            offers.append(Offer(p, size, most, rupees(z.price(p) or p.price)))
        else:
            why = why or most
    return offers, why


@dataclass
class Fill:
    take: dict[Product, int] = field(default_factory=dict)
    note: str = ""  # on anything short
    why: str = ""  # nothing could be added
    ask: str = ""  # whether to make it up with the household's other products


def _fill(z: Zepto, qty: Qty, choices: list[Choice], others: bool | None) -> Fill:
    """Packs for qty from the household's first product; its other products only when the first can't
    make it up and others is True (None: ask)."""
    want, unit, note = _wants(qty, choices[0])
    first, why = _offers(z, choices[0], unit)
    take = cheapest(want, first)
    got = _got(take, first)
    if got < want and len(choices) > 1 and others is not False:
        extra, offers = {}, list(first)
        for choice in choices[1:]:  # in the household's order, each only for what is still missing
            if (wants := choice.wants(1)) and wants[1] == unit and got + _got(extra, offers) < want:
                found = _offers(z, choice, unit)[0]
                offers += found
                extra |= cheapest(want - got - _got(extra, offers), found)
        if extra:
            if not others:
                has = f"has only {_as(got, unit)}" if take else "is out of stock"
                return Fill(ask=f"{choices[0].name} {has} today. Add {_parts(extra)}?")
            take, first, got = {**take, **extra}, offers, got + _got(extra, offers)
    log.info("item filled", sizes=len(first), packs=sum(take.values()), short=got < want)
    if take and got < want:
        note = f"{_as(got, unit)} of {_as(want, unit)}: all the shop allows"
    return Fill(take, note, "" if take else why or "no size on sale comes near it")


def order(hh: Household, items: list[tuple[str, Qty]], when: str = "due") -> Outcome:
    """when: "hold" prices the basket from the catalog and leaves it there; the shop is used only to
    place an order (or, with ordering off, to fill its cart). "due" places it if it reaches the
    minimum, "now" places it anyway. New items join the basket first, so a failure loses nothing.

    The attempt is written to the ledger before the order call, so a crash can never lead
    to a silent second order: an unfinished attempt is reported, not retried."""
    catalog, ledger = hh.catalog(), hh.ledger()
    out, limits = Outcome(), rules(hh)
    before = ledger.basket() if hh.place_orders or not items else []  # with ordering off, only an answer's
    items, others = combine(before, items), ledger.others()
    if not hh.place_orders:  # nothing waits in the basket, so neither does its address
        ledger.set_basket([])
        ledger.set_address()
    if hh.place_orders:
        ledger.set_basket(items)
    if limits.budget <= 0:
        out.over_limit = True
        out.problems.append(f"today's limit of ₹{hh.daily_cap:g} is used up")
        return out
    if when == "hold" and hh.place_orders:
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
        expected, filled = {}, []
        for key, qty in items:
            if not (choices := catalog.choices(key)):
                out.unavailable[key] = "no choice saved"
                continue
            f = _fill(z, qty, choices, others.get(key))
            if f.ask:
                out.questions[key] = f.ask
            elif f.why:
                out.unavailable[key] = f.why
            else:
                filled.append((key, qty, f.take, f.note))
            for p, n in f.take.items():  # two items can come to one product
                p = next((e for e in expected if e.variant == p.variant), p)
                expected[p] = expected.get(p, 0) + n
        if out.questions:  # nothing is ordered until they are answered
            ledger.set_basket(items)  # also with ordering off, for the answer to find
            for key, allowed in {**others, **dict.fromkeys(out.questions)}.items():
                ledger.set_others(key, allowed)
            return out
        kept = [(k, q) for k, q in items if k in out.unavailable]  # stay in the basket, whatever happens
        if not expected:
            out.problems.append("none of the items can be added right now")
            return out

        z.fill(expected)
        cart = z.review(expected, limits)
        catalog.reprice({p.url: price for p, price in cart.prices.items()})
        priced_now = {p.variant: price for p, price in cart.prices.items()}
        for key, qty, take, note in filled:
            take = {dataclasses.replace(p, price=priced_now.get(p.variant, p.price)): n for p, n in take.items()}
            out.lines.append(line(key, qty, take, note))
        out.total, out.problems, out.item_total, out.cod = cart.total, cart.problems, cart.item_total, cart.cod
        out.over_limit = cart.total is not None and cart.total > limits.budget
        if cart.problems:
            return out
        if cart.item_total < hh.min_order and when != "now":
            out.waiting = True
            ledger.set_basket(items)
            return out
        if not hh.place_orders:
            return out

        attempt = ledger.begin([(key, qty) for key, qty, *_ in filled], cart.total)
        ledger.set_basket(kept)  # never ordered twice, even if the outcome is unclear
        log.info("attempt recorded", total=cart.total, cod=cart.cod)
        stopped = placed = None
        try:
            placed = z.place(limits, cart.cod)
        except ShopError as e:  # it may have gone through all the same
            log.warning("placing interrupted", error=e.logged)
            stopped = str(e)
        except Exception:
            log.warning("placing interrupted", exc_info=True)
            stopped = "the shop stopped answering while placing it"
        try:
            out.placed = z.confirm(placed)
        except Exception:
            log.warning("order history not read", exc_info=True)
        out.unconfirmed = not out.placed
        if stopped and out.unconfirmed:
            out.problems.append(stopped)
        ledger.finish(attempt, "placed" if out.placed else "unknown")
        return out
