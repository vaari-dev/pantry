import pytest

from grocerybot.shop import _prices


@pytest.mark.parametrize(
    ("lines", "struck", "expected"),
    [
        (["Eggs", "1 pack", "₹ 89", "ADD"], [], ("₹89", "")),
        (["₹15", "OFF", "Eggs", "1 pack", "₹89", "₹104", "ADD"], ["₹104"], ("₹89", "₹104")),
        (["₹15", "OFF", "Eggs", "1 pack", "₹89", "₹104", "ADD"], [], ("₹89", "₹104")),
        (["Eggs", "1 pack", "₹1,104", "₹89"], ["₹1,104"], ("₹89", "₹1,104")),
        (["Eggs", "1 pack", "₹89", "₹60"], [], ("₹89", "")),
    ],
)
def test_prices(lines, struck, expected):
    assert _prices(lines, struck) == expected
