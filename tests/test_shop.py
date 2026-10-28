import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from grocerybot import shop, store
from grocerybot.catalog import Product
from grocerybot.shop import Login, Rules, ShopError, Zepto

HOME, OTHER = "addr-home", "addr-other"
MILK = Product("Amul Taaza Toned Fresh Milk | Pouch", "500 ml", "https://shop.example/pn/milk/pvid/pv-milk", "₹30")
EGGS = Product("Eggs", "6 pcs", "https://shop.example/pn/eggs/pvid/pv-eggs", "₹60")


class FakeZepto:
    """Zepto's sign-in and MCP servers, in the shapes they answered with on 28 Oct 2026: bills at MRP less
    a discount, without item prices; the history showing a new order a read or two late."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []  # (tool, arguments)
        self.address = None
        self.cart: dict[str, int] = {}
        self.wallet = 100_000  # paise
        self.orders = ["old-order"]
        self.access, self.refresh = "access-1", "refresh-1"
        self.expired = False
        self.products = {
            "pv-milk": {"price": 3000, "mrp": 3200, "available": 30, "max": 30, "active": True},
            "pv-eggs": {"price": 6000, "mrp": 6000, "available": 2, "max": 5, "active": True},
        }
        self.refusals: dict[str, str] = {}  # tool -> what it says when refusing
        self.bill_address = None  # the address the bill says, if not the selected one
        self.wallet_extra: dict[str, int] = {}  # put only in the wallet order's bill
        self.history_down = 0  # order history reads that fail
        self.history_lag = 1  # reads after an order that still miss it
        self.lagging = 0

    def __call__(self, url, body, headers):
        parts = urlsplit(url)
        path = parts.path
        if parts.netloc == "auth.zepto.co.in":
            return self.auth(path, parts.query, body)
        if path == "/.well-known/oauth-protected-resource":
            return self.reply({"resource": shop.RESOURCE})
        if headers.get("authorization") != f"Bearer {self.access}" or self.expired:
            return 401, {}, b'{"error":"unauthorized"}'
        msg = json.loads(body)
        if msg["method"] != "tools/call":
            return (
                200,
                {"mcp-session-id": "s1"},
                json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}).encode(),
            )
        name, args = msg["params"]["name"], msg["params"]["arguments"]
        self.calls.append((name, args))
        if name in self.refusals:
            result = {"content": [{"type": "text", "text": f"Error: {self.refusals[name]}"}], "isError": True}
        else:
            result = {"content": [{"type": "text", "text": "…"}], "structuredContent": getattr(self, name)(**args)}
        # replies come as a server-sent event stream
        event = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})}\n\n"
        return 200, {"Content-Type": "text/event-stream"}, event.encode()

    def reply(self, data, status=200):
        return status, {}, json.dumps(data).encode()

    def auth(self, path, query, body):
        if path == "/register":
            return self.reply({"client_id": "client-1"}, 201)
        if path == "/authorize":
            self.challenge = parse_qs(query)
            page = '<script id="__NEXT_DATA__">{"props":{"pageProps":{"sessionId":"sess-1"}}}</script>'
            return 200, {}, page.encode()
        if path == "/api/send-otp":
            return self.reply({"data": {"msg": "OTP Sent."}})
        if path == "/api/login":
            sent = json.loads(body)
            if sent["otpToken"] != "123456" or sent["sessionId"] != "sess-1":
                return self.reply({"message": "Invalid OTP"}, 400)
            state = self.state if hasattr(self, "state") else sent["state"]
            return self.reply({"redirect": f"{shop.REDIRECT}?code=code-1&state={state}"})
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if form["grant_type"] == "refresh_token" and form["refresh_token"] != self.refresh:
                return self.reply({"error": "invalid_grant"}, 400)
            n = int(self.refresh.rsplit("-", 1)[1]) + 1
            self.access, self.refresh, self.expired = f"access-{n}", f"refresh-{n}", False
            return self.reply(
                {
                    "access_token": self.access,
                    "refresh_token": self.refresh,
                    "expires_in": 604800,
                    "token_type": "Bearer",
                }
            )
        return 404, {}, b""

    def list_saved_addresses(self):
        return {"addresses": [{"id": HOME, "label": "Home", "addressLine": "T6/503, MG Road"},
                              {"id": OTHER, "label": "Work", "addressLine": "Tower 3"}]}  # fmt: skip

    def select_saved_address(self, addressId):
        self.address = addressId
        return {"success": True, "address": {"id": addressId, "label": "Home", "addressLine": "T6/503, MG Road"}}

    def search_products(self, query, **_):
        return {
            "products": [
                {"productVariantId": "pv-ad", "storeProductId": "sp-ad", "name": "Ad", "price": 100, "isAd": True,
                 "availableQuantity": 9},
                {"productVariantId": "pv-milk", "storeProductId": "sp-milk", "name": "Amul Taaza | Pouch",
                 "price": 3000, "mrp": 3000, "packSize": "1 pack (500 ml)", "availableQuantity": 30, "isAd": False,
                 "imageUrl": "https://cdn.example/tr:f-auto/milk.jpeg"},
                {"productVariantId": "pv-gone", "storeProductId": "sp-gone", "name": "Gone", "price": 100,
                 "availableQuantity": 0, "isAd": False},
                {"productVariantId": "pv-kajal", "storeProductId": "sp-kajal", "name": "Kajal", "price": 13250,
                 "mrp": 22000, "packSize": "1 pc (0.35 g)", "availableQuantity": 3, "isAd": False},
            ]
        }  # fmt: skip

    def get_product_details(self, product_variant_id):
        if not (p := self.products.get(product_variant_id)):
            raise KeyError(product_variant_id)
        return {
            "productVariantId": product_variant_id,
            "storeProductId": "sp-" + product_variant_id[3:],
            "sellingPrice": p["price"],
            "availableQuantity": p["available"],
            "maxAllowedQuantity": p["max"],
            "isActive": p["active"],
            "isInStock": p.get("in_stock", p["available"] > 0),
        }

    def update_cart(self, deviceId, cartItems, replaceCart=False):
        assert deviceId
        if replaceCart:
            self.cart = {}
        for i in cartItems:
            assert i["storeProductId"] == "sp-" + i["productVariantId"][3:]
            self.cart[i["productVariantId"]] = i["quantity"]
        self.cart = {k: q for k, q in self.cart.items() if q}
        return {"items": [], "isEmpty": not self.cart}

    def _bill(self, cod, confirmOrder=False, userAddressId=None, **_):
        unknown = {"price": 500, "mrp": 500}
        mrp = sum(self.products.get(v, unknown)["mrp"] * q for v, q in self.cart.items())
        sub = sum(self.products.get(v, unknown)["price"] * q for v, q in self.cart.items())
        total = sub + (0 if sub >= 19900 else 3000)
        if not cod and not self.wallet:
            raise KeyError("wallet")
        if confirmOrder:
            self.orders.insert(0, f"order-{len(self.orders)}")
            self.cart, self.lagging = {}, self.history_lag
            summary = {
                "orderId": self.orders[0],
                "status": "INITIATED",
                "paymentMethod": "COD" if cod else "ZEPTO_CASH",
            }
            return {"isConfirmation": True, "success": True, "summary": summary}
        return {
            "isPreview": True,
            "items": [{"productVariantId": v, "quantity": q, "price": None} for v, q in self.cart.items()],
            "toPayAmount": total if cod else max(0, total - self.wallet),  # the wallet may cover part
            "subTotal": mrp,
            "discount": mrp - sub,
            "deliveryFee": total - sub,
            "deliverable": True,
            "deliveryAddress": {"id": self.bill_address or userAddressId or self.address},
        }

    def create_order(self, **args):
        return self._bill(True, **args)

    def create_wallet_order(self, **args):
        bill = self._bill(False, **args)
        bill.get("items", []).extend({"productVariantId": v, "quantity": q} for v, q in self.wallet_extra.items())
        return bill

    def list_order_history(self, **_):
        if self.history_down:
            self.history_down -= 1
            raise KeyError("history")
        if self.lagging:
            self.lagging -= 1
            return {"orders": [{"id": o, "formattedStatus": "PLACED"} for o in self.orders[1:]]}
        return {"orders": [{"id": o, "formattedStatus": "PLACED", "grandTotalAmount": 1} for o in self.orders]}


@pytest.fixture
def zepto(monkeypatch):
    fake = FakeZepto()

    def request(url, body, headers):
        try:
            return fake(url, body, headers)
        except KeyError:  # the tool refuses
            msg = json.loads(body)
            result = {"content": [{"type": "text", "text": "Error: not possible"}], "isError": True}
            return 200, {}, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode()

    monkeypatch.setattr(shop, "_request", request)
    monkeypatch.setattr(shop.time, "sleep", lambda _: None)
    return fake


@pytest.fixture
def login_file(tmp_path, zepto):
    login = Login()
    login.request_otp("9876543210")
    store.write(path := tmp_path / "session.json", login.enter_otp("123456"))
    return path


def test_sign_in_with_phone_and_code_gives_tokens(zepto):
    login = Login()
    login.request_otp("9876543210")
    assert zepto.challenge["code_challenge_method"] == ["S256"]
    assert zepto.challenge["resource"] == [shop.RESOURCE]
    tokens = login.enter_otp("123456")
    assert (tokens["client_id"], tokens["refresh_token"]) == ("client-1", "refresh-2")
    assert tokens["expires_at"] > time.time() + 6 * 86400


def test_a_wrong_code_is_refused(zepto):
    login = Login()
    login.request_otp("9876543210")
    with pytest.raises(ShopError, match="did not accept that code"):
        login.enter_otp("000000")


def test_a_code_for_another_sign_in_is_refused(zepto):
    login = Login()
    login.request_otp("9876543210")
    zepto.state = "someone-elses"
    with pytest.raises(ShopError, match="did not accept that code"):
        login.enter_otp("123456")


def test_a_changed_sign_in_page_says_so(zepto, monkeypatch):
    auth = zepto.auth
    monkeypatch.setattr(
        zepto, "auth", lambda p, *a: (200, {}, b"<html>new</html>") if p == "/authorize" else auth(p, *a)
    )
    with pytest.raises(ShopError, match="needs an update"):
        Login().request_otp("9876543210")


@pytest.mark.parametrize("saved", [None, {"cookies": [], "origins": []}])  # none, or a Chrome session
def test_without_a_login_the_account_is_not_connected(tmp_path, zepto, saved):
    if saved is not None:
        store.write(tmp_path / "session.json", saved)
    with pytest.raises(ShopError, match="not connected"), Zepto(tmp_path / "session.json"):
        pass


def test_a_login_near_its_end_is_renewed_and_kept(login_file, zepto):
    store.write(login_file, {**store.read(login_file, {}), "expires_at": time.time() + 3600})
    with Zepto(login_file) as z:
        z.deliver_to(HOME)
    assert store.read(login_file, {})["refresh_token"] == zepto.refresh == "refresh-3"


def test_a_login_renewed_by_the_other_process_is_used_not_renewed_again(login_file, zepto):
    """`check` runs beside the bot: the refresh token both read works only once."""
    with Zepto(login_file) as z:
        with Zepto(login_file) as other:
            zepto.expired = True
            other.deliver_to(HOME)  # renews
        z.deliver_to(HOME)  # its token is the old one now
    assert store.read(login_file, {})["refresh_token"] == zepto.refresh == "refresh-3"


def test_a_server_error_on_renewal_is_not_an_expired_login(login_file, zepto, monkeypatch):
    store.write(login_file, {**store.read(login_file, {}), "expires_at": time.time()})
    auth = zepto.auth
    monkeypatch.setattr(zepto, "auth", lambda p, *a: (503, {}, b"") if p == "/token" else auth(p, *a))
    with pytest.raises(ShopError, match="not answering"), Zepto(login_file):
        pass
    assert store.read(login_file, {})["refresh_token"] == "refresh-2"


def test_a_refused_token_is_renewed_once_then_the_login_has_expired(login_file, zepto):
    with Zepto(login_file) as z:
        zepto.expired = True
        z.deliver_to(HOME)  # renewed, and retried
        zepto.expired, zepto.refresh = True, "revoked"
        with pytest.raises(ShopError, match="login has expired"):
            z.deliver_to(HOME)


def test_search_skips_ads_and_sold_out_items_and_reads_sizes_and_prices(login_file, zepto):
    with Zepto(login_file) as z:
        found = z.search("milk")
    assert found == [
        Product("Amul Taaza | Pouch", "500 ml", "https://www.zepto.com/pn/amul-taaza-pouch/pvid/pv-milk", "₹30",
                "https://cdn.example/tr:f-jpg/milk.jpeg"),
        Product("Kajal", "0.35 g", "https://www.zepto.com/pn/kajal/pvid/pv-kajal", "₹132.50", "", "₹220"),
    ]  # fmt: skip
    assert found[0].variant == "pv-milk"


@pytest.mark.parametrize(
    ("change", "stock"),
    [
        ({}, 2),  # fewer in the store than the order limit
        ({"available": 30, "max": 4}, 4),
        ({"available": 0}, "out of stock"),
        ({"active": False}, "out of stock"),
        ({"in_stock": False}, "out of stock"),
    ],
)
def test_stock_is_what_one_order_may_have(login_file, zepto, change, stock):
    zepto.products["pv-eggs"].update(change)
    with Zepto(login_file) as z:
        assert z.stock(EGGS) == stock


def test_a_product_the_shop_dropped(login_file, zepto):
    with Zepto(login_file) as z:
        assert z.stock(Product("Old", "", "https://shop.example/pn/old/pvid/pv-old")) == "the shop no longer sells this"


def order(z, items, budget=1000.0, address=HOME):
    for p in items:
        z.stock(p)
    z.fill(items)
    return z.review(items, Rules(address, budget))


def test_the_cart_becomes_exactly_the_basket(login_file, zepto):
    zepto.cart = {"pv-stranger": 3}  # put there in the app
    with Zepto(login_file) as z:
        z.deliver_to(HOME)
        cart = order(z, {MILK: 2, EGGS: 1})
    assert zepto.cart == {"pv-milk": 2, "pv-eggs": 1}
    assert (cart.problems, cart.item_total, cart.total, cart.cod) == ([], 120, 150, False)


@pytest.mark.parametrize("wallet", [0, 5_000])  # refused, or covering only part
def test_a_wallet_too_small_pays_on_delivery(login_file, zepto, wallet):
    zepto.wallet = wallet
    with Zepto(login_file) as z:
        cart = order(z, {MILK: 1})
    assert (cart.problems, cart.total, cart.cod) == ([], 60, True)


@pytest.mark.parametrize(
    ("setup", "problem"),
    [
        (lambda f: setattr(f, "bill_address", OTHER), "the bill is not for your saved address (/settings)"),
        (lambda f: f.products["pv-milk"].update(price=199_000), "₹1990 is more than what is left of today's limit"),
    ],
)
def test_the_bill_is_checked_before_ordering(login_file, zepto, setup, problem):
    setup(zepto)
    with Zepto(login_file) as z:
        assert order(z, {MILK: 1}).problems[0].startswith(problem)


def test_the_wallet_bill_is_checked_too(login_file, zepto):
    zepto.wallet_extra = {"pv-stranger": 1}
    with Zepto(login_file) as z:
        assert order(z, {MILK: 1}).problems == ["an item the bot did not add is in the cart"]


def test_a_cart_unlike_the_basket_is_a_problem(login_file, zepto, monkeypatch):
    with Zepto(login_file) as z:
        for p in (MILK, EGGS):
            z.stock(p)
        z.fill({MILK: 2, EGGS: 1})
        zepto.cart = {"pv-milk": 1, "pv-stranger": 1}
        problems = z.review({MILK: 2, EGGS: 1}, Rules(HOME, 1000)).problems
    assert problems == [
        "Amul Taaza Toned Fresh Milk | Pouch: 1 in the cart, expected 2",
        "Eggs did not make it into the cart",
        "an item the bot did not add is in the cart",
    ]


def test_a_refusal_says_what_the_shop_said(login_file, zepto):
    zepto.refusals["update_cart"] = "Store not selected. Please use select_store first."
    with Zepto(login_file) as z, pytest.raises(ShopError) as e:
        z.stock(MILK)
        z.fill({MILK: 1})
    assert str(e.value) == "the shop said: Store not selected. Please use select_store first."
    assert e.value.logged == "update_cart refused"


@pytest.mark.parametrize("cod", [False, True])
def test_placing_is_confirmed_by_its_id_in_the_history(login_file, zepto, cod):
    with Zepto(login_file) as z:
        cart = order(z, {MILK: 1})
        placed = z.place(Rules(HOME, 1000), cod)
        assert placed == zepto.orders[0] != "old-order"
        assert z.confirm(placed)
    assert [n for n, a in zepto.calls if a.get("confirmOrder")] == ["create_order" if cod else "create_wallet_order"]
    assert cart.cod is False


def test_a_failed_history_read_is_tried_again(login_file, zepto):
    with Zepto(login_file) as z:
        order(z, {MILK: 1})
        placed = z.place(Rules(HOME, 1000), cod=False)
        zepto.history_down, zepto.history_lag = 1, 0
        assert z.confirm(placed)


def test_only_the_placed_order_is_confirmed(login_file, zepto):
    zepto.orders.insert(0, "placed-in-the-app")
    with Zepto(login_file) as z:
        assert not z.confirm(None)
        assert not z.confirm("ours")


def test_reachable_needs_no_login(zepto):
    assert shop.reachable()
