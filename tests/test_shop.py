import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from grocerybot.shop import ADDRESS, LOGGED_OUT, Browser, Rules, ShopError, _card, _loggable, _prices, _shows_size


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
    ("lines", "card"),
    [
        (["ADD", "₹89", "Eggs", "1 pack"], ("Eggs", "1 pack")),
        (["Lemon", "1 pc", "₹51", "ADD"], ("Lemon", "1 pc")),
        # 24 Oct: a brand line above the name; saved as name "Elle 18", size the full name
        (["₹119", "₹199", "ADD", "Elle 18", "Elle 18 Eyedrama Kajal Bold Black", "1 pc (0.35 g)", "4.3"],
         ("Elle 18 Eyedrama Kajal Bold Black", "1 pc (0.35 g)")),
        (["24 Mantra Organic", "24 Mantra Organic Toor Dal", "500 g"], ("24 Mantra Organic Toor Dal", "500 g")),
        (["Amul Taaza Milk", "2 x 500 ml"], ("Amul Taaza Milk", "2 x 500 ml")),
        (["Coke", "Pack of 6"], ("Coke", "Pack of 6")),
        (["Bestseller", "Fresh Coriander Bunch"], ("Fresh Coriander Bunch", "")),  # no size shown
        (["ADD", "₹20"], None),
    ],
)  # fmt: skip
def test_card(lines, card):
    assert _card(lines) == card


@pytest.mark.parametrize(
    ("size", "page", "shown"),
    [
        ("750 ml", "Kinley Soda 750 ml Add to Cart", True),
        ("0.35 g", "Net Qty: 0.35g", True),
        ("0.35 g", "Net Qty 0.35 gm", True),
        ("1 pc (0.35 g)", "Net Qty: 0.35 g", True),
        ("1 kg", "Pack size 1000 g", True),
        ("1 Litre", "1 l", True),
        ("4 pcs", "4 pieces", True),
        ("(24.1k)", "Kajal Deep Black", True),  # not a size: nothing to compare
        ("500 g", "Net Qty: 1 kg", False),
        ("1 pc (0.35 g)", "1 pc (1.2 g)", False),
        ("6 pcs", "12 pieces", False),
        ("1 l", "1 lemon", False),
    ],
)
def test_shows_size(size, page, shown):
    assert _shows_size(size, page) is shown


@pytest.mark.parametrize(
    ("text", "login_button", "problem"),
    [
        ("Your cart is empty", False, "the cart is empty: the shop did not take the items"),
        ("All items are out of stock", False, "the cart is empty: the shop did not take the items"),
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
    assert _loggable(cart) == ["Out of stock", "Remove", "Item Total", "To Pay"]


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
