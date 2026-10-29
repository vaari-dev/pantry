"""How much of an item -> packs to buy: the cheapest mix of the sizes on sale, within each one's limit."""

from dataclasses import dataclass

from .catalog import Product

MAX_PACKS = 12  # of one size: more is a misheard amount, not a household's order
UNPRICED = 10**6  # a pack whose price isn't known is taken only when nothing priced will do


@dataclass(frozen=True)
class Offer:
    product: Product
    size: int  # in the unit the item is wanted in
    most: int
    price: float | None


def cheapest(want: int, offers: list[Offer]) -> dict[Product, int]:
    """{product: packs}: the cheapest mix making want, over by less than its smallest pack and at most
    twice want (300 ml from a 500 ml pack, not a 5 l one); when the limits can't reach that, the most
    under want, cheapest. Ties go to fewer packs."""
    if not offers:
        return {}
    top = min(want + min(o.size for o in offers) - 1, 2 * want)
    best: dict[int, tuple[float, int, tuple[int, ...]]] = {0: (0, 0, ())}  # got -> (cost, packs, counts)
    for o in offers:
        unit = o.price if o.price is not None else UNPRICED
        grown: dict[int, tuple[float, int, tuple[int, ...]]] = {}
        for got, (cost, packs, counts) in best.items():
            for n in range(min(o.most, MAX_PACKS) + 1):
                if (total := got + n * o.size) > top:
                    break
                state = (cost + n * unit, packs + n, (*counts, n))
                if total not in grown or state[:2] < grown[total][:2]:
                    grown[total] = state
        best = grown
    enough = [s for got, s in best.items() if got >= want]
    _, _, counts = min(enough) if enough else max(best.items(), key=lambda i: (i[0], -i[1][0], -i[1][1]))[1]
    return {o.product: n for o, n in zip(offers, counts, strict=True) if n}
