import json
from types import SimpleNamespace

from grocerybot.apimap import Recorder, shape, source, template

TOKEN = "eyJhbGciOiJIUzI1NiJ9.c2VjcmV0.c2ln"
CART = {
    "phone": "9876543210",
    "pincode": 560001,
    "address": {"line": "T6/503, MG Road", "lat": 12.97, "lng": 77.59},
    "items": [{"productVariantId": "0f8fad5b-d9cb-469f-a165-70867728950e", "quantity": 2}, {"quantity": 1}],
    "paymentMode": "COD",
    "useWallet": False,
    "f3a1c9e07b2d4c11": {"qty": 1},
}


def test_shape_keeps_structure_not_values():
    assert shape(CART) == {
        "phone": "str",
        "pincode": "int",
        "address": {"line": "str", "lat": "float", "lng": "float"},
        "items": [{"productVariantId": "str", "quantity": 2}],
        "paymentMode": "COD",
        "useWallet": False,
        "{id}": {"qty": 1},
    }


def test_template_blanks_ids():
    assert template("/api/v2/product/0f8fad5b-d9cb-469f-a165-70867728950e/pvid/123456") == (
        "/api/v2/product/{id}/pvid/{id}"
    )
    assert template("/api/v1/cart/") == "/api/v1/cart/"


def test_source_names_where_a_header_comes_from():
    cookies, storage = {"session": "abc123xyz"}, {"local.user": json.dumps({"token": TOKEN})}
    assert source("abc123xyz", cookies, storage) == "cookie:session"
    assert source(f"Bearer {TOKEN}", cookies, {"local.token": TOKEN}) == "storage:local.token, with Bearer around it"
    assert source(TOKEN, cookies, storage) == "inside storage:local.user"
    assert source("unrelated", cookies, storage) is None


class Request(SimpleNamespace):
    def response(self):
        return self.resp

    def all_headers(self):
        return self.headers


def test_map_holds_no_secrets(tmp_path):
    reply = SimpleNamespace(status=200, headers={"content-type": "application/json"}, text=lambda: json.dumps(CART))
    req = Request(
        resource_type="fetch",
        url="https://api.shop.test/api/v1/cart/0f8fad5b-d9cb-469f-a165-70867728950e?storeId=s1&q=bread",
        method="POST",
        post_data=json.dumps(CART),
        frame=SimpleNamespace(url="https://shop.test/search?query=bread"),
        resp=reply,
        headers={
            "authorization": f"Bearer {TOKEN}",
            "x-device-id": "d9cb469fa16570867728950e",
            "platform": "WEB",
            "x-request-id": "Zm9vYmFyYmF6cXV4cXV1eA",
            "content-type": "application/json",
            "user-agent": "Chrome",
        },
    )
    ctx = SimpleNamespace(cookies=lambda: [{"name": "device", "value": "d9cb469fa16570867728950e"}])
    page = SimpleNamespace(evaluate=lambda _: {"local.token": TOKEN})
    path = tmp_path / "apimap.json"
    rec = Recorder(path, skip=lambda url: False)
    rec._seen(req)
    rec._seen(SimpleNamespace(resource_type="image", url="https://shop.test/a.png"))
    rec.write(page, ctx)

    text = path.read_text()
    for secret in (TOKEN, "d9cb469fa165", "9876543210", "560001", "MG Road", "12.97", "bread", "s1"):
        assert secret not in text
    entry = json.loads(text)["endpoints"]["POST api.shop.test/api/v1/cart/{id}"]
    assert entry["query"] == ["q", "storeId"]
    assert entry["pages"] == ["/search"]
    assert entry["headers"] == {
        "authorization": "storage:local.token, with Bearer around it",
        "x-device-id": "cookie:device",
        "platform": "WEB",
        "x-request-id": "other (22 chars)",
        "content-type": "application/json",
    }
    assert entry["body"]["paymentMode"] == "COD"
