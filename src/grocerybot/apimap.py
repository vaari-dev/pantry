"""A map of the shop's API calls, as its page makes them.

It is printed in workflow logs, so it keeps only plain-word names, value types, and where each
header's value comes from, never a value.
"""

import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import structlog

from . import store

log = structlog.get_logger(__name__)

# api, productVariant, send-otp, _ga, v2. Not ids, slugs, or capitalised names and places;
# a lowercase word such as a one-word search still shows.
WORD = re.compile(r"_*[a-z][A-Za-z]*(?:[-_][A-Za-z]+){0,2}|v\d")
BROWSER_HEADERS = frozenset(
    "accept accept-encoding accept-language cache-control connection content-length content-type cookie host "
    "origin pragma priority referer user-agent".split()
)


def name(text: str) -> str:
    """Anything but a plain word (see WORD) becomes {x}."""
    parts = re.split(r"([/.:;,]+)", re.sub(r"[^/]*@[^/]*", "{x}", text))  # odd parts are the separators
    return "".join(p if n % 2 or _word(p) else "{x}" for n, p in enumerate(parts))


def _word(part: str) -> bool:
    return part in ("", "{x}") or (len(part) <= 40 and bool(WORD.fullmatch(part)))


def shape(value):
    if isinstance(value, dict):
        return {name(k): shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return value if value is None or isinstance(value, bool) else type(value).__name__


def body(data: bytes | None, content_type: str) -> object:
    if not data:
        return None
    try:
        return shape(json.loads(data))
    except (ValueError, RecursionError):
        if "form-urlencoded" in content_type:
            return {name(k): "str" for k, _ in parse_qsl(data.decode(errors="replace"))}
        return f"{content_type.split(';')[0] or 'unknown'}, {len(data)} bytes"


def source(value: str, stored: dict[str, str]) -> str | None:
    for key, item in stored.items():
        if len(item) >= 8 and item in value:
            return key if item == value else f"contains {key}"
        if len(value) >= 8 and value in item:
            return f"inside {key}"
    return None


class Recorder:
    def __init__(self, path: Path):
        self.path = path
        self.calls: list[dict] = []

    def attach(self, ctx) -> None:
        ctx.on("requestfinished", self._seen)

    def _seen(self, req) -> None:
        if req.resource_type not in ("xhr", "fetch"):
            return
        try:
            resp, url, headers = req.response(), urlsplit(req.url), req.all_headers()
            status = resp.status if resp else "no reply"
            content_type = headers.get("content-type", "")
            reply = None
            if resp:
                try:
                    reply = body(resp.body(), resp.headers.get("content-type", ""))
                except Exception:  # bodies of some responses are not kept
                    reply = "unreadable"
            try:
                page = name("/" + urlsplit(req.frame.url).path.split("/")[1])
            except Exception:  # service workers have no frame
                page = "service-worker"
            self.calls.append(
                {
                    "endpoint": f"{req.method} {name(url.hostname or '')}{name(url.path)} → {status}",
                    "query": {name(k) for k, _ in parse_qsl(url.query, keep_blank_values=True)},
                    "headers": {
                        k: v for k, v in headers.items() if k not in BROWSER_HEADERS and not k.startswith(("sec-", ":"))
                    },
                    "type": content_type.split(";")[0] or None,
                    "body": body(req.post_data_buffer, content_type),
                    "reply": reply,
                    "page": page,
                }
            )
        except Exception:
            log.warning("api call not recorded", exc_info=True)

    def write(self, page, ctx) -> None:
        """Never raises: the map must not get in the way of an order."""
        if not self.calls:
            return
        try:
            stored = {f"cookie:{name(c['name'])}": c["value"] for c in ctx.cookies()}
            stored |= page.evaluate(
                """() => Object.fromEntries([['local', localStorage], ['session', sessionStorage]].flatMap(
                    ([kind, s]) => Object.keys(s).map(k => [kind + ':' + k, s.getItem(k)])))"""
            )
            stored = {name(k): v for k, v in stored.items()}
            self._merge(stored)
        except Exception:
            log.warning("api map not written", exc_info=True)

    def _merge(self, stored: dict[str, str]) -> None:
        values = defaultdict(set)
        for call in self.calls:
            for header, value in call["headers"].items():
                values[call["endpoint"], header].add(value)
        known = store.read(self.path, {})
        endpoints = known.setdefault("endpoints", {})
        for call in self.calls:
            entry = endpoints.setdefault(call["endpoint"], {})
            entry["pages"] = sorted({*entry.get("pages", []), call["page"]})
            entry["query"] = sorted({*entry.get("query", []), *call["query"]})
            entry.update({k: call[k] for k in ("type", "body", "reply") if call[k] is not None})
            headers = entry.setdefault("headers", {})
            for header, value in call["headers"].items():
                same = len(values[call["endpoint"], header]) == 1
                headers[name(header)] = source(value, stored) or ("same each call" if same else "varies")
        known["stored"] = sorted({*known.get("stored", []), *stored})
        store.write(self.path, known)
        log.info("api map updated", calls=len(self.calls), endpoints=len(endpoints))
