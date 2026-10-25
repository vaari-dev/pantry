"""The Blinkit driver in real Chrome against blinkit.html, under xvfb-run (as CI runs them)."""

from datetime import datetime, timedelta

import pytest
from fake_shop import Page, serving

from grocerybot import orders
from grocerybot.accounts import Accounts
from grocerybot.catalog import Product
from grocerybot.config import Config
from grocerybot.shops import Rules, ShopError, blinkit
from grocerybot.shops.blinkit import Blinkit, _placed

RULES = Rules("Home", "T6/503", 900)
KINLEY = Product("Kinley Soda", "750 ml", "/prn/kinley-soda/prid/101")
CHIPS = Product("BRB Chips", "48 g", "/prn/brb-chips/prid/102")


class Shop(Page):
    html = "blinkit.html"


@pytest.fixture(scope="module")
def server():
    with serving(Shop) as url:
        yield url


@pytest.fixture
def shop(server, tmp_path):
    """(browser, given): given(items, logged_in, **cfg) sets the shop's state."""
    with Blinkit(server, None, None, tmp_path) as b:

        def given(items=None, logged_in=True, **cfg):
            b.goto("/")
            state = {"items": items or {}, "orders": [], "cfg": cfg}
            b.page.evaluate("s => localStorage.shop = JSON.stringify(s)", state)
            if logged_in:
                b.page.evaluate("() => localStorage.auth = JSON.stringify({accessToken: 't'})")
            b.goto("/")

        yield b, given


def order(b, *products, qty=1):
    """Fills and checks the cart as orders.order does: (skipped, reviewed cart)."""
    b.check_session(RULES)
    b.empty_cart()
    expected, skipped = {}, []
    for p in products:
        if why := b.add(p, qty):
            skipped.append((p.name, why))
        else:
            expected[p] = qty
    return skipped, b.review_cart(expected, RULES)


def test_search_reads_cards_by_product_id_and_skips_ads(shop):
    b, given = shop
    given()
    [kinley] = b.search("kinley")
    assert (kinley.label(), kinley.url, kinley.price) == (KINLEY.label(), KINLEY.url, "₹20")
    [chips] = b.search("brb")
    assert (chips.url, chips.price, chips.mrp, chips.variant) == (CHIPS.url, "₹30", "₹40", "102")
    assert b.search("bisleri") == []  # an ad


def test_a_full_order_over_leftovers_pays_cash_and_is_confirmed(shop):
    b, given = shop
    given({"103": 1})
    started = datetime.now()
    skipped, cart = order(b, KINLEY, CHIPS, qty=2)
    assert (skipped, cart.problems, cart.item_total, cart.total, cart.cod) == ([], [], 100, 102, True)
    assert b.ready(cart) is None
    assert b.place(cart) is None
    assert b.confirm(cart.total, started)


def test_the_shops_limit_per_order_is_ordered_not_failed(shop):
    b, given = shop
    given(limits={"Kinley Soda": 2})
    _, cart = order(b, KINLEY, qty=3)
    assert (cart.problems, cart.capped) == ([], {KINLEY: 2})


def test_a_different_size_is_skipped(shop):
    b, given = shop
    given(sizes={"Kinley Soda": "1.25 l"})
    skipped, _ = order(b, KINLEY)
    assert skipped == [("Kinley Soda", "the size you picked isn't sold any more")]


def test_a_product_no_longer_listed_is_skipped(shop):
    b, given = shop
    given()
    assert b.add(Product("Kinley Soda", "750 ml", "/prn/kinley-soda/prid/999"), 1) == "the shop changed this product"


def test_a_cart_for_another_address_is_a_problem(shop):
    b, given = shop
    given(elsewhere=True)
    _, cart = order(b, KINLEY)
    assert cart.problems == ["the cart is not delivering to your saved address (/settings)"]


def test_over_the_limit_is_a_problem(shop):
    b, given = shop
    given()
    b.check_session(RULES)
    b.add(KINLEY, 2)
    assert b.review_cart({KINLEY: 2}, Rules("Home", "T6/503", 40)).problems == [
        "₹42 is more than what is left of today's limit (₹40)"
    ]


@pytest.mark.parametrize(
    ("cfg", "problem"),
    [
        ({"noCash": True}, "cash on delivery is not offered for this order"),
        ({"payShows": 99}, "the payment page doesn't show ₹22"),
    ],
)
def test_payment_stops_before_paying(shop, cfg, problem):
    b, given = shop
    given(**cfg)
    _, cart = order(b, KINLEY)
    assert b.ready(cart) == problem
    assert b.page.evaluate("JSON.parse(localStorage.shop).orders") == []


def test_logged_out(shop):
    b, given = shop
    given(logged_in=False)
    with pytest.raises(ShopError, match="login has expired"):
        b.check_session(RULES)


def test_login_and_address_setup(server, tmp_path):
    (state := tmp_path / "state.json").write_text('{"cookies": [], "origins": []}')
    with Blinkit(server, state, None, tmp_path, fresh=True) as b:
        b.request_otp("9876543210")
        b.enter_otp("1234")
        saved = b.saved_addresses()
        assert [a.splitlines()[0] for a in saved] == ["Home", "Work"]
        assert b.pick_address(1) == "Work - 4th floor, Tower B, Whitefield"


def test_a_wrong_code_is_refused(server, tmp_path, monkeypatch):
    monkeypatch.setattr(blinkit, "LOGIN_WAIT_MS", 2000)
    with Blinkit(server, None, None, tmp_path) as b:
        b.request_otp("9876543210")
        with pytest.raises(ShopError, match="did not accept"):
            b.enter_otp("9999")


@pytest.mark.parametrize(
    ("text", "placed"),
    [
        ("Arriving in 8 mins ₹102 25 Oct, 3:44 pm", datetime(2026, 10, 25, 15, 44)),
        ("Delivered ₹60 31 Dec, 11:05 am", datetime(2025, 12, 31, 11, 5)),  # last year's
        ("Delivered ₹60 3 Mar 2024, 9:00 AM", datetime(2024, 3, 3, 9, 0)),
        ("Arriving soon", None),
    ],
)
def test_placed(text, placed):
    assert _placed(text, datetime(2026, 10, 25, 16, 0)) == placed


def test_end_to_end_picked_then_ordered_cash_on_delivery(server, tmp_path, monkeypatch):
    accounts = Accounts(tmp_path, 1)
    (session := tmp_path / "state.json").write_text('{"cookies": [], "origins": []}')
    hh = accounts.connect(1, "Asha", "blinkit", "Home", "T6/503", session, ordering=True)
    with Blinkit(server, hh.state_file, None, tmp_path) as b:
        b.goto("/")
        b.page.evaluate("() => localStorage.auth = JSON.stringify({accessToken: 't'})")
        [found] = b.search("kinley")
    key = hh.catalog().learn("soda", found)

    monkeypatch.setattr(orders, "browser", lambda cfg, hh: Blinkit(server, hh.state_file, None, tmp_path))
    cfg = Config({"blinkit": server}, "", 1, "", tmp_path, None, 9)
    accounts.update(hh.id, min_order=10.0)
    out = orders.order(cfg, accounts.get(hh.id), [(key, 2)])
    assert (out.result, out.total, out.cod, out.problems) == ("placed", 42, True, [])
    assert [(o["status"], o["shop"]) for o in hh.ledger()._orders()] == [("placed", "blinkit")]
    assert hh.catalog().options(key)[0].price == "₹20"
    assert datetime.now() - datetime.fromisoformat(hh.ledger()._orders()[0]["at"]) < timedelta(minutes=5)
