"""The shop, through Zepto's official MCP server: JSON tools over HTTP, no browser.

Login is the shop's own OAuth sign-in (phone, then the code it texts), done with two of its page's
calls. The account's tokens live in a file and are renewed before they lapse."""

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import structlog

from . import store
from .catalog import Product

MCP = "https://mcp.zepto.co.in/mcp"
RESOURCE = "https://mcp.zepto.co.in"
AUTH = "https://auth.zepto.co.in"
REDIRECT = "http://localhost/callback"  # allowed by the sign-in server; never loaded
SCOPES = (
    "tools:read tools:write dev.ucp.shopping.cart:manage dev.ucp.shopping.catalog.search:read "
    "dev.ucp.shopping.catalog.lookup:read dev.ucp.common.location.search:read "
    "dev.ucp.common.location.lookup:read dev.ucp.shopping.order:manage dev.ucp.shopping.checkout:manage"
)
TIMEOUT = 60
RENEW = 3 * 86400  # tokens last 7 days; the daily health check renews them well before
DEVICE = "grocery-bot"  # update_cart wants one; the cart is the account's either way
NOT_CONNECTED = "this account is not connected to the shop; its owner can /setup again"
LOGIN_EXPIRED = "the shop login has expired; the account's owner can /setup again"
NOT_ANSWERING = "the shop is not answering; try again in a minute"
PACK = re.compile(r"^1 (?:pack|pc|unit) \((.+)\)$")  # "1 pack (500 ml)" says "500 ml"
log = structlog.get_logger(__name__)


class ShopError(Exception):
    """Its text is told to the user; `logged` is the same without their address in it."""

    def __init__(self, text: str, logged: str | None = None):
        super().__init__(text)
        self.logged = logged or text


@dataclass(frozen=True)
class Rules:
    address_id: str
    budget: float

    def over(self, total: float) -> str | None:
        return (
            f"₹{total:g} is more than what is left of today's limit (₹{self.budget:g})" if total > self.budget else None
        )


@dataclass(frozen=True)
class Address:
    id: str
    label: str
    line: str

    def __str__(self) -> str:
        return f"{self.label}: {self.line}"


@dataclass
class Cart:
    total: float | None = None
    item_total: float = 0
    problems: list[str] = field(default_factory=list)
    cod: bool = False
    prices: dict[Product, str] = field(default_factory=dict)  # unit price charged


def _rupees(paise: int) -> str:
    return f"₹{paise // 100}" if paise % 100 == 0 else f"₹{paise / 100:.2f}"


def _size(pack: str) -> str:
    return m.group(1) if (m := PACK.match(pack := (pack or "").strip())) else pack


def _url(base: str, name: str, variant: str) -> str:
    """Like the shop's own product links, which end /pvid/<variant id>."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "item"
    return f"{base.rstrip('/')}/pn/{slug}/pvid/{variant}"


def _request(url: str, body: bytes | None, headers: dict) -> tuple[int, dict, bytes]:
    """(status, headers, body); a refused request is a status, an unreachable shop raises ShopError."""
    req = urllib.request.Request(url, body, headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.info("shop unreachable", host=urlsplit(url).netloc, error=type(e).__name__)
        raise ShopError(NOT_ANSWERING) from e


def _json(url: str, data: dict | None = None, form: bool = False, headers: dict | None = None) -> tuple[int, dict]:
    if data is None:
        body, kind = None, None
    elif form:
        body, kind = urlencode(data).encode(), "application/x-www-form-urlencoded"
    else:
        body, kind = json.dumps(data).encode(), "application/json"
    status, _, raw = _request(url, body, {**({"content-type": kind} if kind else {}), **(headers or {})})
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {}


def reachable() -> bool:
    """Without a login: the MCP server's public metadata."""
    try:
        return _json(f"{RESOURCE}/.well-known/oauth-protected-resource")[0] == 200
    except ShopError:
        return False


def _tokens(reply: dict, client_id: str) -> dict:
    return {
        "client_id": client_id,
        "access_token": reply["access_token"],
        "refresh_token": reply["refresh_token"],
        "expires_at": time.time() + reply.get("expires_in", 0),
    }


class Login:
    """One sign-in: request_otp, then enter_otp gives the tokens to keep."""

    def __init__(self):
        self.verifier, self.state = secrets.token_urlsafe(48), secrets.token_urlsafe(8)
        self.client_id = self.session = self.phone = ""

    def request_otp(self, phone: str) -> None:
        status, client = _json(
            f"{AUTH}/register",
            {
                "client_name": "grocery-bot",
                "redirect_uris": [REDIRECT],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        if status >= 300 or not client.get("client_id"):
            log.info("sign-in refused", step="register", status=status)
            raise ShopError("the shop's sign-in is not working right now; try again later")
        self.client_id = client["client_id"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": self.state,
                "resource": RESOURCE,
                "scope": SCOPES,
            }
        )
        status, _, page = _request(f"{AUTH}/authorize?{query}", None, {})
        if not (m := re.search(rb'"sessionId":"([^"]+)"', page)):
            log.info("sign-in refused", step="authorize", status=status)
            raise ShopError("the shop's sign-in page changed; the bot needs an update")
        self.session, self.phone = m.group(1).decode(), phone
        status, _ = _json(f"{AUTH}/api/send-otp", {"mobileNumber": phone, "countryCode": "+91"})
        if status >= 300:
            log.info("sign-in refused", step="send-otp", status=status)
            raise ShopError("the shop did not send a code; is the number right?")

    def enter_otp(self, code: str) -> dict:
        status, reply = _json(
            f"{AUTH}/api/login",
            {
                "mobileNumber": self.phone,
                "countryCode": "+91",
                "otpToken": code,
                "sessionId": self.session,
                "clientId": self.client_id,
                "resource": RESOURCE,
                "state": self.state,
            },
        )
        query = parse_qs(urlsplit(reply.get("redirect", "")).query)
        if status >= 300 or query.get("state") != [self.state] or not query.get("code"):
            log.info("sign-in refused", step="login", status=status)
            raise ShopError("the shop did not accept that code")
        status, reply = _json(
            f"{AUTH}/token",
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "redirect_uri": REDIRECT,
                "code_verifier": self.verifier,
                "resource": RESOURCE,
                "code": query["code"][0],
            },
            form=True,
        )
        if status >= 300 or "refresh_token" not in reply:
            log.info("sign-in refused", step="token", status=status)
            raise ShopError("the shop did not finish the sign-in; try again")
        return _tokens(reply, self.client_id)


class Zepto:
    """One MCP session on one account. login_file holds its tokens; renewals are written back."""

    def __init__(self, base_url: str, login_file: Path):
        self.base, self.login_file = base_url, login_file
        self._sid: str | None = None
        self._spid: dict[str, str] = {}  # variant -> its id in the selected store
        self._n = 0

    def __enter__(self) -> "Zepto":
        self._login = store.read(self.login_file, {})
        if "refresh_token" not in self._login:  # also a Chrome session saved before the MCP
            raise ShopError(NOT_CONNECTED)
        if self._login["expires_at"] - time.time() < RENEW:
            self._renew()
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "grocery-bot", "version": "1"}})  # fmt: skip
        self._rpc("notifications/initialized")
        return self

    def __exit__(self, *_) -> None: ...

    def _renew(self) -> None:
        status, reply = _json(
            f"{AUTH}/token",
            {
                "grant_type": "refresh_token",
                "client_id": self._login["client_id"],
                "refresh_token": self._login["refresh_token"],
                "resource": RESOURCE,
            },
            form=True,
        )
        if status >= 300 or "refresh_token" not in reply:
            log.info("login not renewed", status=status)
            raise ShopError(LOGIN_EXPIRED)
        self._login = _tokens(reply, self._login["client_id"])
        store.write(self.login_file, self._login)  # the old refresh token no longer works
        log.info("login renewed")

    def _rpc(self, method: str, params: dict | None = None, renewed: bool = False) -> dict:
        self._n += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not method.startswith("notifications/"):
            body["id"] = self._n
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {self._login['access_token']}",
            **({"mcp-session-id": self._sid} if self._sid else {}),
        }
        status, got, raw = _request(MCP, json.dumps(body).encode(), headers)
        if status == 401 and not renewed:
            self._renew()
            return self._rpc(method, params, renewed=True)
        if status >= 300:
            log.info("shop refused", method=method, status=status)
            raise ShopError(LOGIN_EXPIRED if status == 401 else NOT_ANSWERING)
        self._sid = {k.lower(): v for k, v in got.items()}.get("mcp-session-id") or self._sid
        text = raw.decode()
        if "data:" in text and not text.lstrip().startswith("{"):  # a server-sent event stream
            text = [line[5:] for line in text.splitlines() if line.startswith("data:")][-1]
        return json.loads(text) if text.strip() else {}

    def tool(self, name: str, quiet: bool = False, **args) -> dict | None:
        """Its structured reply. A refusal raises ShopError with the shop's words, or with quiet, is None."""
        reply = self._rpc("tools/call", {"name": name, "arguments": args})
        result = reply.get("result") or {}
        if "error" in reply or result.get("isError"):
            said = " ".join(c.get("text", "") for c in result.get("content", [])) or str(reply.get("error"))
            said = said.removeprefix("Error:").strip().split("\n")[0][:200]
            log.info("tool refused", tool=name, said=said)
            if quiet:
                return None
            raise ShopError(f"the shop said: {said}", f"{name} refused")
        return result.get("structuredContent") or {}

    def addresses(self) -> list[Address]:
        found = self.tool("list_saved_addresses").get("addresses") or []
        return [Address(a["id"], a.get("label") or "", a.get("addressLine") or "") for a in found]

    def deliver_to(self, address_id: str) -> Address:
        """Picks the address, and with it the store that serves it."""
        if not address_id:
            raise ShopError(NOT_CONNECTED)
        a = self.tool("select_saved_address", addressId=address_id) or {}
        got = a.get("address") or {}
        if not a.get("success") or got.get("id") != address_id:
            raise ShopError("the shop would not deliver to the saved address; its owner can /setup again")
        return Address(got["id"], got.get("label") or "", got.get("addressLine") or "")

    def search(self, query: str, limit: int = 4) -> list[Product]:
        found = []
        for p in self.tool("search_products", query=query).get("products") or []:
            if p.get("isAd") or not p.get("availableQuantity"):
                continue
            price, mrp = p.get("price"), p.get("mrp")
            found.append(
                Product(
                    p["name"],
                    _size(p.get("packSize")),
                    _url(self.base, p["name"], p["productVariantId"]),
                    _rupees(price) if price else "",
                    (p.get("imageUrl") or "").replace("f-auto", "f-jpg"),  # Telegram can't show the shop's avif
                    _rupees(mrp) if price and mrp and mrp > price else "",
                )
            )
            self._spid[p["productVariantId"]] = p["storeProductId"]
            if len(found) == limit:
                break
        return found

    def stock(self, product: Product) -> int | str:
        """How many one order may have, or why none."""
        p = self.tool("get_product_details", quiet=True, product_variant_id=product.variant)
        if not p or not p.get("storeProductId"):
            return "the shop no longer sells this"
        if not (p.get("isActive") and p.get("isInStock")):
            return "out of stock"
        self._spid[product.variant] = p["storeProductId"]
        return max(0, min(p.get("availableQuantity") or 0, p.get("maxAllowedQuantity") or 0)) or "out of stock"

    def fill(self, items: dict[Product, int]) -> None:
        """Makes the cart exactly items: anything else in it goes."""
        cart = [
            {"productVariantId": p.variant, "storeProductId": self._spid[p.variant], "quantity": q}
            for p, q in items.items()
        ]
        self.tool("update_cart", deviceId=DEVICE, replaceCart=True, cartItems=cart)
        log.info("cart filled", items=len(cart))

    def review(self, expected: dict[Product, int], rules: Rules) -> Cart:
        """The shop's bill, without ordering. Pays from the wallet if it covers it all, else cash on delivery."""
        bill, result = self.tool("create_order", confirmOrder=False, userAddressId=rules.address_id), Cart()
        if not bill.get("isPreview"):
            result.problems.append("the shop did not show the bill")
            return result
        if not bill.get("deliverable"):
            result.problems.append("the shop can't deliver there right now")
        if (bill.get("deliveryAddress") or {}).get("id") != rules.address_id:
            result.problems.append("the bill is not for your saved address (/settings)")
        rows = {i["productVariantId"]: i for i in bill.get("items") or []}
        for product, want in expected.items():
            if (row := rows.pop(product.variant, None)) is None:
                result.problems.append(f"{product.name} did not make it into the cart")
            elif row.get("quantity") != want:
                result.problems.append(f"{product.name}: {row.get('quantity')} in the cart, expected {want}")
            elif row.get("price"):
                result.prices[product] = _rupees(row["price"])
        result.problems += ["an item the bot did not add is in the cart"] * len(rows)
        result.item_total = (bill.get("subTotal") or 0) / 100
        result.total = bill["toPayAmount"] / 100 if isinstance(bill.get("toPayAmount"), int) else None
        wallet = self.tool("create_wallet_order", quiet=True, confirmOrder=False, userAddressId=rules.address_id)
        result.cod = not (wallet and wallet.get("isPreview") and wallet.get("toPayAmount") == 0)
        log.info(
            "bill read",
            item_total=result.item_total,
            total=result.total,
            delivery_fee=bill.get("deliveryFee"),
            cod=result.cod,
            items=len(bill.get("items") or []),
        )
        if result.total is None:
            result.problems.append("could not read the amount to pay")
        elif over := rules.over(result.total):
            result.problems.append(over)
        return result

    def latest_order(self) -> str | None:
        orders = self.tool("list_order_history", limit=1, pageNumber=1).get("orders") or []
        return orders[0]["id"] if orders else None

    def place(self, rules: Rules, cod: bool) -> None:
        """The one irreversible call."""
        reply = self.tool(
            "create_order" if cod else "create_wallet_order", confirmOrder=True, userAddressId=rules.address_id
        )
        log.info("order reply", keys=sorted(reply)[:30], status=reply.get("status"), preview=reply.get("isPreview"))

    def confirm(self, before: str | None) -> bool:
        """By the order history: a newest order other than the one before placing."""
        for attempt in range(3):
            if attempt:
                time.sleep(5)
            orders = self.tool("list_order_history", limit=1, pageNumber=1).get("orders") or []
            if orders and orders[0]["id"] != before:
                o = orders[0]
                log.info("order found", status=o.get("formattedStatus"), total=o.get("grandTotalAmount"))
                return True
        log.info("no new order in the history")
        return False
