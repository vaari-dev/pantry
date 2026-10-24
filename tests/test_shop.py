import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from grocerybot.shop import ADDRESS, LOGGED_OUT, Browser, Rules, ShopError, _prices


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


def test_an_empty_cart_is_one_problem():
    b = Browser.__new__(Browser)
    b._pay_with = b._open_cart = lambda *_, **__: {"text": "Your cart is empty", "items": [], "boxes": []}
    cart = b.review_cart({"Kinley Soda": 1}, Rules("Home", "T6/503", 900))
    assert cart.problems == ["the cart is empty: the shop did not take the items"]
    assert cart.total is None


class Found:
    def __init__(self, n, text=""):
        self.n, self.text, self.first = n, text, self

    def wait_for(self, timeout):
        if not self.n:
            raise PlaywrightTimeout("not shown")

    def count(self):
        return self.n

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
