import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from grocerybot.shop import ADDRESS, LOGGED_OUT, Browser, Rules, ShopError, _loggable, _prices


@pytest.mark.parametrize(
    ("lines", "struck", "expected"),
    [
        (["Eggs", "1 pack", "₹ 89", "ADD"], [], ("₹89", "")),
        (["₹15", "OFF", "Eggs", "1 pack", "₹89", "₹104", "ADD"], ["₹104"], ("₹89", "₹104")),
        (["₹15", "OFF", "Eggs", "1 pack", "₹89", "₹104", "ADD"], [], ("₹89", "₹104")),
        (["Eggs", "1 pack", "₹1,104", "₹89"], ["₹1,104"], ("₹89", "₹1,104")),
        (["Eggs", "1 pack", "₹89", "₹60"], [], ("₹89", "")),
        (["Eggs", "1 pack", "₹89", "₹."], [], ("₹89", "")),  # malformed amounts must not raise
    ],
)
def test_prices(lines, struck, expected):
    assert _prices(lines, struck) == expected


@pytest.mark.parametrize(
    ("text", "login_button", "problem"),
    [
        ("Your cart is empty", False, "the cart is empty: the shop did not take the items"),
        ("Orange Nagpur\nOut of stock\nRemove", False, "the cart shows only out of stock items"),
        ("", True, "login has expired"),
    ],
)
def test_a_cart_without_items(text, login_button, problem):
    b = Browser.__new__(Browser)
    b._pay_with = b._open_cart = lambda *_, **__: {"text": text, "items": [], "boxes": []}
    b.page = type("Page", (), {"locator": lambda _, s: Found(int(login_button))})()
    if login_button:
        with pytest.raises(ShopError, match=problem):
            b.review_cart({"Kinley Soda": 1}, Rules("Home", "T6/503", 900))
    else:
        assert b.review_cart({"Kinley Soda": 1}, Rules("Home", "T6/503", 900)).problems == [problem]


def test_loggable_keeps_the_address_and_balance_out():
    cart = "Home\nFlat 12, Lake View Apartments\nIndiranagar, Bengaluru 560038\nRiya Sharma\nOrange Nagpur\n"
    cart += "Out of stock\nRemove\nZepto Cash ₹412 available\nItem Total\n₹123\nTo Pay\n₹0"
    assert _loggable(cart) == ["Out of stock", "Remove", "Item Total", "₹123", "To Pay", "₹0"]


class Found:
    def __init__(self, n, text=""):
        self.n, self.text, self.first = n, text, self

    def wait_for(self, state, timeout):
        if not self.n:
            raise PlaywrightTimeout("not shown")

    def count(self):
        return self.n

    def filter(self, visible):
        return self

    def inner_text(self):
        return self.text


@pytest.mark.parametrize(
    ("address", "login_button", "error"),
    [
        ("Home T6/503", False, None),
        ("Home T6/503", True, "login has expired"),  # logged out, the address still showing
        ("", True, "login has expired"),
        ("", False, "could not read the delivery address"),
    ],
)
def test_check_session(address, login_button, error):
    b = Browser.__new__(Browser)
    b.base = "https://shop.example"
    found = {ADDRESS: Found(bool(address), address), LOGGED_OUT: Found(int(login_button))}
    b.page = type("Page", (), {"url": b.base + "/", "locator": lambda _, s: found[s]})()
    if error:
        with pytest.raises(ShopError, match=error):
            b.check_session(Rules("Home", "T6/503", 900))
    else:
        assert b.check_session(Rules("Home", "T6/503", 900)) == address
