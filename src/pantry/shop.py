"""Zepto through its official MCP server. Sign-in is its OAuth page's own two calls: phone, then the texted code."""

import base64
import fcntl
import hashlib
import http.client
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

SITE = "https://www.zepto.com"
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
DEVICE = "pantry"  # update_cart keys its cart by it: not the Zepto app's cart (checked live, 29 Oct)
NOT_CONNECTED = "this account is not connected to the shop; its owner can /setup again"
LOGIN_EXPIRED = "the shop login has expired; the account's owner can /setup again"
NOT_ANSWERING = "the shop is not answering; try again in a minute"
TOKENS = frozenset({"access_token", "refresh_token"})
ADDRESSED = frozenset({"select_saved_address", "create_order", "create_wallet_order"})  # refusals may quote it
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


def _url(name: str, variant: str) -> str:
    """Like the shop's own product links, which end /pvid/<variant id>."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "item"
    return f"{SITE}/pn/{slug}/pvid/{variant}"


def _request(url: str, body: bytes | None, headers: dict) -> tuple[int, dict, bytes]:
    """(status, headers, body); a refused request is a status, an unreachable shop raises ShopError."""
    req = urllib.request.Request(url, body, headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        log.info("shop unreachable", host=urlsplit(url).netloc, error=type(e).__name__)
        raise ShopError(NOT_ANSWERING) from e


def _json(url: str, data: dict | None = None, form: bool = False) -> tuple[int, dict]:
    if data is None:
        body, kind = None, None
    elif form:
        body, kind = urlencode(data).encode(), "application/x-www-form-urlencoded"
    else:
        body, kind = json.dumps(data).encode(), "application/json"
    status, _, raw = _request(url, body, {"content-type": kind} if kind else {})
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
        "expires_at": time.time() + (reply.get("expires_in") or 7 * 86400),
    }


def _problems(bill: dict, expected: dict[Product, int], rules: Rules) -> list[str]:
    """What is wrong with a bill preview: anything but exactly the expected items, to the saved address."""
    if not bill.get("isPreview"):
        return ["the shop did not show the bill"]
    problems = []
    if not bill.get("deliverable"):
        problems.append("the shop can't deliver there right now")
    if (bill.get("deliveryAddress") or {}).get("id") != rules.address_id:
        problems.append("the bill is not for your saved address (/settings)")
    rows = {i.get("productVariantId"): i.get("quantity") for i in bill.get("items") or []}
    for product, want in expected.items():
        if (got := rows.pop(product.variant, None)) is None:
            problems.append(f"{product.name} did not make it into the cart")
        elif got != want:
            problems.append(f"{product.name}: {got} in the cart, expected {want}")
    return problems + ["an item the bot did not add is in the cart"] * len(rows)


class Login:
    """One sign-in: request_otp, then enter_otp gives the tokens to keep."""

    def __init__(self):
        self.verifier, self.state = secrets.token_urlsafe(48), secrets.token_urlsafe(8)
        self.client_id = self.session = self.phone = ""

    def request_otp(self, phone: str) -> None:
        status, client = _json(
            f"{AUTH}/register",
            {
                "client_name": "pantry",
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
        if status >= 300 or not reply.keys() >= TOKENS:
            log.info("sign-in refused", step="token", status=status)
            raise ShopError("the shop did not finish the sign-in; try again")
        return _tokens(reply, self.client_id)


class Zepto:
    """One MCP session on one account. login_file holds its tokens; renewals are written back."""

    def __init__(self, login_file: Path):
        self.login_file = login_file
        self._sid: str | None = None
        self._spid: dict[str, str] = {}  # variant -> its id in the selected store
        self._price: dict[str, str] = {}  # variant -> price there; bills don't say
        self._n = 0

    def __enter__(self) -> "Zepto":
        self._login = store.read(self.login_file, {})
        if "refresh_token" not in self._login:  # also a Chrome session saved before the MCP
            raise ShopError(NOT_CONNECTED)
        if self._login["expires_at"] - time.time() < RENEW:
            self._renew()
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "pantry", "version": "1"}})  # fmt: skip
        self._rpc("notifications/initialized")
        return self

    def __exit__(self, *_) -> None: ...

    def _renew(self) -> None:
        """Locked across processes (`check` runs beside the bot): a refresh token works once."""
        with open(self.login_file.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            saved = store.read(self.login_file, {})
            if saved.keys() >= TOKENS and saved["refresh_token"] != self._login["refresh_token"]:
                self._login = saved  # renewed meanwhile by the other process
                return
            self._renew_now()

    def _renew_now(self) -> None:
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
        if status >= 300 or not reply.keys() >= TOKENS:
            log.info("login not renewed", status=status)
            raise ShopError(LOGIN_EXPIRED if status in (400, 401) else NOT_ANSWERING)
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
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError:
            log.info("shop reply unreadable", method=method)
            raise ShopError(NOT_ANSWERING) from None

    def tool(self, name: str, quiet: bool = False, **args) -> dict | None:
        """Its structured reply. A refusal raises ShopError with the shop's words, or with quiet, is None."""
        reply = self._rpc("tools/call", {"name": name, "arguments": args})
        result = reply.get("result") or {}
        if "error" in reply or result.get("isError"):
            said = " ".join(c.get("text", "") for c in result.get("content", [])) or str(reply.get("error"))
            said = said.removeprefix("Error:").strip().split("\n")[0][:200]
            log.info("tool refused", tool=name, said=None if name in ADDRESSED else said)
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
        return Address(address_id, got.get("label") or "", got.get("addressLine") or "")

    def search(self, query: str, limit: int = 12) -> list[Product]:
        found = []
        for p in self.tool("search_products", query=query).get("products") or []:
            if p.get("isAd") or not (p.get("availableQuantity") and p.get("storeProductId")):
                continue
            price, mrp = p.get("price"), p.get("mrp")
            found.append(
                Product(
                    p["name"],
                    _size(p.get("packSize")),
                    _url(p["name"], p["productVariantId"]),
                    _rupees(price) if price else "",
                    (p.get("imageUrl") or "").replace("f-auto", "f-jpg"),  # Telegram can't show the shop's avif
                    _rupees(mrp) if price and mrp and mrp > price else "",
                    str(p.get("productId") or ""),
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
        if p.get("sellingPrice"):
            self._price[product.variant] = _rupees(p["sellingPrice"])
        return max(0, min(p.get("availableQuantity") or 0, p.get("maxAllowedQuantity") or 0)) or "out of stock"

    def price(self, product: Product) -> str:
        return self._price.get(product.variant, "")

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
        result.problems = _problems(bill, expected, rules)
        result.prices = {p: self._price[p.variant] for p in expected if p.variant in self._price}
        result.item_total = ((bill.get("subTotal") or 0) - (bill.get("discount") or 0)) / 100  # subTotal is at MRP
        result.total = bill["toPayAmount"] / 100 if isinstance(bill.get("toPayAmount"), int) else None
        wallet = self.tool("create_wallet_order", quiet=True, confirmOrder=False, userAddressId=rules.address_id)
        result.cod = not (wallet and wallet.get("isPreview") and wallet.get("toPayAmount") == 0)
        if not result.cod:  # the bill that would be placed
            result.problems += [p for p in _problems(wallet, expected, rules) if p not in result.problems]
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

    def place(self, rules: Rules, cod: bool) -> str | None:
        """The one irreversible call; the new order's id, if the reply has it."""
        reply = self.tool(
            "create_order" if cod else "create_wallet_order", confirmOrder=True, userAddressId=rules.address_id
        )
        summary = reply.get("summary") or {}
        log.info(
            "order reply",
            success=reply.get("success"),
            status=summary.get("status"),
            payment=summary.get("paymentMethod"),
            wallet_used=summary.get("walletBalanceUsed"),
            to_pay=summary.get("toPayAmount"),
        )
        return summary.get("orderId")

    def confirm(self, placed: str | None) -> bool:
        """By the placed order's id in the history, which lists it a few seconds late."""
        for attempt in range(3 if placed else 0):
            if attempt:
                time.sleep(5)
            try:
                o = next(iter(self.tool("list_order_history", limit=1, pageNumber=1).get("orders") or []), {})
            except ShopError:
                continue
            if o.get("id") == placed:
                log.info("order found", status=o.get("formattedStatus"), total=o.get("grandTotalAmount"))
                return True
        log.info("no new order in the history")
        return False
