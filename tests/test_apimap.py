import json
from types import SimpleNamespace

import pytest

from grocerybot.apimap import Recorder, name, shape

TOKEN = "eyJhbGciOiJIUzI1NiJ9.c2VjcmV0.c2ln"
SECRETS = [
    "Rahul",
    "rahul:s3cret",
    TOKEN,
    "9876543210",
    "560001",
    "MG Road",
    "12.9716",
    "77.5946",
    "amul",
    "RAHUL",
    "KA01AB1234",
    "4821",
]
CART = {
    "phone": "+15550109876",
    "address": {"line": "T6/503, MG Road", "lat": 12.9716, "name": "RAHUL", "vehicle": "KA01AB1234"},
    "items": [{"productVariantId": "0f8fad5b-d9cb-469f-a165-70867728950e", "quantity": 2}],
    "otp": 4821,
    "useWallet": False,
    "suggestions": {"amul milk": 1, "+15550109876": 1, "f3a1c9e07b2d4c11": 1, "Rahul": 1},
}


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("/api/v2/cart/send-otp", "/api/v2/cart/send-otp"),
        ("/api/v2/product/0f8fad5b-d9cb-469f-a165-70867728950e", "/api/v2/product/{x}"),
        ("/pn/amul-taaza-toned-milk/pvid/123", "/pn/{x}/pvid/{x}"),
        ("/api/search/amul%20milk", "/api/search/{x}"),
        ("/api/loc/12.9716,77.5946", "/api/loc/{x}.{x},{x}.{x}"),
        ("/city/bangalore-560001/vytxeTZskVKR7C7WgdSP3d", "/city/{x}/{x}"),
        ("local:mp_0123456789abcdef_mixpanel", "local:{x}"),
        ("/user/RahulSharma/loc/Bengaluru,Koramangala", "/user/{x}/loc/{x},{x}"),
        ("/u/rahul.sharma@gmail.com", "/u/{x}"),
        ("/api/product/amul_taaza_toned_fresh_milk", "/api/product/{x}"),
        ("_ga", "_ga"),
    ],
)
def test_name_keeps_only_words(text, kept):
    assert name(text) == kept


def test_shape_keeps_types_not_values():
    assert shape(CART) == {
        "phone": "str",
        "address": {"line": "str", "lat": "float", "name": "str", "vehicle": "str"},
        "items": [{"productVariantId": "str", "quantity": "int"}],
        "otp": "int",
        "useWallet": False,
        "suggestions": {"{x}": "int"},
    }


def request(url, headers, data=None, reply=None, frame="https://shop.test/search?query=amul"):
    resp = SimpleNamespace(status=200, headers={"content-type": "text/plain"}, body=lambda: reply)
    return SimpleNamespace(
        resource_type="fetch",
        url=url,
        method="POST" if data else "GET",
        post_data_buffer=data,
        frame=SimpleNamespace(url=frame),
        response=lambda: resp,
        all_headers=lambda: headers,
    )


def test_map_holds_no_secrets(tmp_path):
    headers = {
        "authorization": f"Bearer {TOKEN}",
        "x-device-id": "d9cb469fa16570867728950e",
        "x-loc": "12.971599,77.594566",
        "x-city": "RAHUL",
        "x-request-id": "1",
        "content-type": "application/json; charset=utf-8",
        "user-agent": "Chrome",
    }
    rec = Recorder(tmp_path / "apimap.json")
    for n in range(2):
        sent = json.dumps(CART).encode()
        rec._seen(
            request(
                "https://rahul:s3cret@api.shop.test/api/v1/cart/0f8fad5b?q=amul",
                headers | {"x-request-id": str(n)},
                sent,
                reply=sent,
            )
        )
    rec._seen(
        request("https://api.shop.test/api/v1/otp", {"content-type": "text/plain"}, b"eyJwaG9uZSI6Ijk4NzY1NDMyMTAifQ==")
    )
    rec._seen(request("https://api.shop.test/api/v1/deep", {}, b"[" * 100_000 + b"]" * 100_000))
    rec._seen(SimpleNamespace(resource_type="image"))
    ctx = SimpleNamespace(cookies=lambda: [{"name": "device", "value": "d9cb469fa16570867728950e"}])
    page = SimpleNamespace(evaluate=lambda _: {"local:token": TOKEN, "local:lat": "12.971599"})
    rec.write(page, ctx)

    text = (tmp_path / "apimap.json").read_text()
    for secret in [*SECRETS, "d9cb469fa165", "eyJwaG9uZ"]:
        assert secret not in text
    endpoints = json.loads(text)["endpoints"]
    cart = endpoints["POST api.shop.test/api/v1/cart/{x} → 200"]
    assert cart["query"] == ["q"]
    assert cart["pages"] == ["/search"]
    assert cart["reply"] == cart["body"] == shape(CART)
    assert cart["headers"] == {
        "authorization": "contains local:token",
        "x-device-id": "cookie:device",
        "x-loc": "contains local:lat",
        "x-city": "same each call",
        "x-request-id": "varies",
    }
    assert cart["type"] == "application/json"
    assert endpoints["POST api.shop.test/api/v1/otp → 200"]["body"] == "text/plain, 32 bytes"
    assert "POST api.shop.test/api/v1/deep → 200" in endpoints


def test_write_never_raises(tmp_path):
    rec = Recorder(tmp_path / "apimap.json")
    rec.calls.append({})
    rec.write(SimpleNamespace(evaluate=lambda _: 1 / 0), None)
