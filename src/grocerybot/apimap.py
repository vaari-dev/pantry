"""A map of the shop's own API calls, seen from the browser, so search and ordering can call it directly.

Records the page's fetch/XHR calls as shapes only: paths with ids blanked, field names with value types,
and where each custom header's value comes from (a cookie, localStorage, ...), never the value itself.
So the map holds no tokens, phone numbers, addresses or locations, and can be printed in a workflow log.
"""

import json
import re
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import structlog

from . import store

log = structlog.get_logger(__name__)

TYPES = frozenset({"xhr", "fetch"})
# Sent by every browser request; the rest are added by the shop's own code.
BROWSER_HEADERS = frozenset(
    """accept accept-encoding accept-language connection content-length cookie host origin pragma
    priority referer user-agent cache-control""".split()
)
ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,}|\d{4,}|[A-Za-z0-9_-]{24,}")
CONSTANT = re.compile(r"[A-Z][A-Z0-9_]{0,30}")  # enum-like strings, e.g. "COD": kept
SHORT = re.compile(r"[A-Za-z0-9._-]{1,12}")  # header values like "WEB" or "2.3.1": kept
MAX_DEPTH = 8
_lock = threading.Lock()  # searches and orders can finish together


def template(path: str) -> str:
    return "/".join("{id}" if ID.fullmatch(seg) else seg for seg in path.split("/"))


def shape(value, depth: int = 0):
    """The value's structure: field names and types, enum-like strings, small whole numbers."""
    if depth > MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        return {("{id}" if ID.fullmatch(k) else k): shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if abs(value) < 100_000 else "int"  # quantities, not phones or pincodes
    if isinstance(value, float):
        return "float"  # prices, but also latitude and longitude
    if isinstance(value, str):
        return value if CONSTANT.fullmatch(value) else "str"
    return type(value).__name__


def _body(text: str | None):
    if not text:
        return None
    try:
        return shape(json.loads(text))
    except ValueError:
        if "=" in text and " " not in text:
            return {k: "str" for k, _ in parse_qsl(text)}
        return f"text ({len(text)} chars)"


def source(value: str, cookies: dict[str, str], storage: dict[str, str]) -> str | None:
    """Where a header's value comes from, without the value."""
    for kind, items in (("cookie", cookies), ("storage", storage)):
        for name, stored in items.items():
            if not stored or len(stored) < 4:
                continue
            if value == stored:
                return f"{kind}:{name}"
            if stored in value:
                return f"{kind}:{name}, with {value.replace(stored, '').strip() or 'nothing'} around it"[:80]
            if value in stored and len(value) >= 8:
                return f"inside {kind}:{name}"
    return None


class Recorder:
    """Attach to a browser context; write() merges what it saw into the map at path."""

    def __init__(self, path: Path, skip):
        self.path, self.skip = path, skip
        self.calls: list[dict] = []

    def attach(self, ctx) -> None:
        ctx.on("requestfinished", self._seen)

    def _seen(self, req) -> None:
        if req.resource_type not in TYPES or self.skip(req.url):
            return
        try:
            resp = req.response()
            url = urlsplit(req.url)
            try:
                reply = resp.text() if resp and "json" in (resp.headers.get("content-type") or "") else None
            except Exception:  # bodies of some responses are not kept
                reply = None
            self.calls.append(
                {
                    "endpoint": f"{req.method} {url.netloc}{template(url.path)}",
                    "query": sorted({k for k, _ in parse_qsl(url.query, keep_blank_values=True)}),
                    "headers": req.all_headers(),
                    "body": _body(req.post_data),
                    "status": resp.status if resp else None,
                    "reply": _body(reply),
                    "page": template(urlsplit(req.frame.url).path) if req.frame else None,
                }
            )
        except Exception:  # never break the shop run over the map
            log.warning("api call not recorded", exc_info=True)

    def write(self, page, ctx) -> None:
        if not self.calls:
            return
        try:
            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            storage = page.evaluate(
                """() => Object.fromEntries([localStorage, sessionStorage].flatMap(s =>
                    Object.keys(s).map(k => [(s === localStorage ? 'local.' : 'session.') + k, s.getItem(k)])))"""
            )
        except Exception:
            cookies, storage = {}, {}
            log.warning("cookies and storage not read", exc_info=True)
        with _lock:
            self._merge(cookies, storage)

    def _merge(self, cookies: dict[str, str], storage: dict[str, str]) -> None:
        known = store.read(self.path, {})
        endpoints = known.setdefault("endpoints", {})
        for call in self.calls:
            entry = endpoints.setdefault(call["endpoint"], {"seen": 0, "pages": [], "headers": {}})
            entry["seen"] += 1
            if call["page"] and call["page"] not in entry["pages"]:
                entry["pages"].append(call["page"])
            entry["query"] = sorted(set(entry.get("query", [])) | set(call["query"]))
            for key in ("body", "status", "reply"):
                if call[key] is not None:
                    entry[key] = call[key]
            for name, value in call["headers"].items():
                if name in BROWSER_HEADERS or name.startswith(("sec-", ":")):
                    continue
                if name == "content-type":
                    entry["headers"][name] = value
                    continue
                found = source(value, cookies, storage)
                short = SHORT.fullmatch(value) and not ID.fullmatch(value)
                entry["headers"][name] = found or (value if short else f"other ({len(value)} chars)")
        known["cookies"] = sorted(set(known.get("cookies", [])) | set(cookies))
        known["storage"] = sorted(set(known.get("storage", [])) | set(storage))
        store.write(self.path, known)
        log.info("api map updated", calls=len(self.calls), endpoints=len(endpoints))
