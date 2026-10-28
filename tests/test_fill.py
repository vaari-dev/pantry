import pytest

from pantry.catalog import Product
from pantry.fill import Offer, cheapest

LITRE, HALF, FIVE, THREE_QUARTERS = (Product("Milk", s, f"/m/pvid/{s}") for s in ("1 l", "500 ml", "5 l", "750 ml"))


def offers(*rows):
    return [Offer(p, size, most, price) for p, size, most, price in rows]


@pytest.mark.parametrize(
    "want, rows, packs",
    [
        (2000, [(LITRE, 1000, 3, 70), (HALF, 500, 9, 36)], {LITRE: 2}),
        (2000, [(LITRE, 1000, 3, 74), (HALF, 500, 9, 36)], {HALF: 4}),  # cheaper in pouches
        (2000, [(LITRE, 1000, 1, 70), (HALF, 500, 9, 36)], {LITRE: 1, HALF: 2}),  # the shop's limit
        (300, [(LITRE, 1000, 3, 70), (HALF, 500, 9, 36)], {HALF: 1}),  # the smallest pack that covers it
        (1500, [(LITRE, 1000, 5, 70), (THREE_QUARTERS, 750, 5, 50)], {THREE_QUARTERS: 2}),  # exactly, cheaper
        (2000, [(LITRE, 1000, 1, 70), (FIVE, 5000, 2, 300)], {LITRE: 1}),  # never far over: short instead
        (3000, [(LITRE, 1000, 1, 70), (HALF, 500, 1, 36)], {LITRE: 1, HALF: 1}),  # as near as the limits allow
        (300, [(FIVE, 5000, 1, 300)], {}),  # far too big
        (500, [(LITRE, 1000, 3, 70)], {LITRE: 1}),  # twice what was asked, the only size there is
        (2, [(LITRE, 1, 9, None), (HALF, 1, 9, 36)], {HALF: 2}),  # a known price first
    ],
)
def test_cheapest(want, rows, packs):
    assert cheapest(want, offers(*rows)) == packs
