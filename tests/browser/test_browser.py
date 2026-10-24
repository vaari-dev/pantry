"""shop.Browser in real Chrome against shop.html, under xvfb-run (as CI runs them)."""

import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from grocerybot import shop as shop_module
from grocerybot.catalog import Product
from grocerybot.shop import Browser, Rules, ShopError

RULES = Rules("Home", "T6/503", 900)
PRODUCTS = {k: Product(name, size, f"/pn/{k}") for k, name, size in [
    ("kinley", "Kinley Soda", "750 ml"), ("chips", "BRB Chips", "48 g"), ("orange", "Orange Nagpur", "1 kg"),
]}  # fmt: skip
CANT_REMOVE = "the shop's cart has items the bot can't remove"


class Shop(SimpleHTTPRequestHandler):
    stalls = 0  # requests to /stall to leave unanswered for 3 s, as at 06:31 and 06:36 UTC on 24 Oct

    def do_GET(self):
        if self.path == "/stall" and Shop.stalls:
            Shop.stalls -= 1
            return time.sleep(3)
        super().do_GET()

    def translate_path(self, path):
        return str(Path(__file__).with_name("shop.html"))

    def log_message(self, *_): ...


@pytest.fixture(scope="module")
def server():
    if not os.environ.get("DISPLAY"):
        pytest.skip("needs a display: run under xvfb-run")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture
def shop(server, tmp_path):
    """(browser, given): given(items, oos, **cfg) sets the shop's state."""
    with Browser(server, None, None, tmp_path) as b:

        def given(items=None, oos=(), **cfg):
            b.goto("/")
            state = {"items": items or {}, "oos": dict.fromkeys(oos, True), "cfg": cfg}
            b.page.evaluate("s => localStorage.shop = JSON.stringify(s)", state)
            b.goto("/")
            b.check_session(RULES)

        yield b, given


def cart(b) -> dict:
    return b.page.evaluate("JSON.parse(localStorage.shop).items")


def order(b, *keys) -> tuple[list, object]:
    """Fills the cart as orders.order does; (skipped, reviewed cart)."""
    b.empty_cart()
    expected, skipped = {}, []
    for k in keys:
        if why := b.add(PRODUCTS[k], 1):
            skipped.append((k, why))
        else:
            expected[PRODUCTS[k].name] = 1
    return skipped, b.review_cart(expected, RULES)


@pytest.mark.parametrize(
    ("items", "oos", "cfg"),
    [
        ({}, (), {}),
        ({"kinley": 2, "chips": 1}, (), {}),
        ({"kinley": 1, "orange": 1}, ["orange"], {}),  # 23 Oct: an out of stock leftover
        ({"kinley": 2}, (), {"lag": 2500}),  # slow exit node
    ],
)
def test_empty_cart(shop, items, oos, cfg):
    b, given = shop
    given(items, oos, **cfg)
    b.empty_cart()
    assert cart(b) == {}


@pytest.mark.parametrize(
    ("items", "cfg"),
    [
        ({"orange": 1}, {"noRemove": True, "countsOos": True}),
        ({"kinley": 1}, {"lag": 10**9}),  # a tap that changes nothing
    ],
)
def test_empty_cart_stops_on_what_it_cannot_remove(shop, items, cfg):
    b, given = shop
    given(items, ["orange"] if "orange" in items else (), **cfg)
    with pytest.raises(ShopError, match=CANT_REMOVE):
        b.empty_cart()


@pytest.mark.xfail(reason="known gap: a row the count leaves out and that has no Remove button", strict=True)
def test_an_uncounted_row_without_remove_is_left(shop):
    b, given = shop
    given({"kinley": 1, "orange": 1}, ["orange"], noRemove=True)
    with pytest.raises(ShopError, match=CANT_REMOVE):
        b.empty_cart()


def test_order_over_leftovers(shop):
    b, given = shop
    given({"orange": 1, "chips": 3}, ["orange"])
    skipped, reviewed = order(b, "kinley", "chips")
    assert (skipped, reviewed.problems, reviewed.total) == ([], [], 50)
    assert reviewed.prices == {"Kinley Soda": "₹20", "BRB Chips": "₹30"}


def test_slow_cart(shop):
    b, given = shop
    given(cartLoad=4000)
    _, reviewed = order(b, "kinley")
    assert (reviewed.problems, reviewed.total) == ([], 20)


def test_a_lost_add_tap_is_tapped_again(shop):
    b, given = shop
    given(dropAdds=1)  # 24 Oct 06:14 UTC: Avocado
    _, reviewed = order(b, "kinley")
    assert (reviewed.problems, reviewed.total) == ([], 20)


def test_an_add_the_shop_never_takes_is_skipped(shop):
    b, given = shop
    given(dropAdds=9)
    skipped, _ = order(b, "kinley")
    assert skipped == [("kinley", "the shop did not take it")]


def test_an_out_of_stock_product_is_never_tapped(shop):
    b, given = shop
    given(oos=["kinley"])
    assert b.add(PRODUCTS["kinley"], 1) == "out of stock"
    assert cart(b) == {}


def test_logged_out_mid_run(shop):
    b, given = shop
    given()
    b.add(PRODUCTS["kinley"], 1)
    b.page.wait_for_load_state()  # the fake reloads after an add
    b.page.evaluate("() => { const s = JSON.parse(localStorage.shop); s.cfg.loggedOut = true; save(s); }")
    with pytest.raises(ShopError, match="login has expired"):
        b.review_cart({"Kinley Soda": 1}, RULES)


def test_unreachable_shop(shop):
    b, _ = shop
    with pytest.raises(ShopError, match="did not load"):
        b.goto("http://127.0.0.1:9/")


def test_a_stalled_page_loads_on_the_second_try(shop, monkeypatch):
    b, _ = shop
    monkeypatch.setattr(shop_module, "TIMEOUT_MS", 4000)  # 2 s a try
    Shop.stalls = 1
    with capture_logs() as logs:
        b.goto("/stall")
    assert [(e["event"], e["attempt"]) for e in logs if e["event"].startswith("page")] == [
        ("page failed", 1),
        ("page loaded", 2),
    ]


def test_any_page_error_reaches_the_user_as_a_reason(server, tmp_path):
    with pytest.raises(ShopError, match="stopped responding"), Browser(server, None, None, tmp_path) as b:
        b.page.locator("#missing").click(timeout=100)


def test_a_failed_state_save_does_not_change_the_outcome(server, tmp_path):
    (state := tmp_path / "state.json").write_text('{"cookies": [], "origins": []}')
    with Browser(server, state, None, tmp_path) as b:
        b._ctx.close()  # as when Chrome dies during Place Order
