"""Words people say -> shop products, best first.

catalog.json: {"egg": {"aliases": ["anda"], "max_qty": 3, "options": [{"name", "size", "url", "price"}, ...]}}
"""

import re
from dataclasses import dataclass
from pathlib import Path

from . import store

DEFAULT_MAX_QTY = 3
MAX_OPTIONS = 3

QUANTITY = re.compile(
    r"(?<![\w.])(?:(\d+)\s*x\s*)?(\d+(?:\.\d+)?)\s*"
    r"(kg|gms?|g|mg|ml|ltrs?|litres?|liters?|l|pcs?|pieces?|units?|packs?|dozen|n)(?![a-z])"
    r"|pack of (\d+)",
    re.I,
)
FILLER_IN_SIZE = re.compile(r"\([^)]*\)|[^\w\s]|\d|\b(x|approx|each)\b", re.I)


def squash(text: str) -> str:
    return " ".join(text.split()).lower()


def is_size(line: str) -> bool:
    """ "1 kg (Medium)" and "500 ml Bottle" are; "3 Pcs Sponge Wipe" and "Onion 1 kg" are names."""
    return bool(QUANTITY.match(line.strip())) and len(FILLER_IN_SIZE.sub(" ", QUANTITY.sub(" ", line)).split()) <= 1


@dataclass(frozen=True)
class Product:
    name: str
    size: str
    url: str
    price: str = ""
    image: str = ""
    mrp: str = ""  # the original price, when the shop shows it struck through

    def label(self) -> str:
        return f"{self.name} ({self.size})" if self.size else self.name

    @property
    def variant(self) -> str:
        """The shop's productVariant.id: the URL ends /pvid/<id>."""
        return self.url.rstrip("/").rsplit("/", 1)[-1]


def rupees(text: str) -> float | None:
    """ "₹1,104" -> 1104.0; None for anything else."""
    try:
        return float(text.lstrip("₹").replace(",", "")) if text else None
    except ValueError:
        return None


def normalize(text: str) -> str:
    """ "Mummy's Home!" -> "mummy s home" """
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


class Catalog:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        return store.read(self.path, {})

    def _index(self, items: dict) -> dict[str, str]:
        index = {}
        for key, entry in items.items():
            for name in (key, *entry.get("aliases", [])):
                index.setdefault(normalize(name), key)
        return index

    def lookup(self, name: str) -> str | None:
        return self._index(self._load()).get(normalize(name))

    def options(self, key: str) -> list[Product]:
        entry = self._load().get(key, {})
        return [
            _repaired(Product(o["name"], o["size"], o["url"], o.get("price", "")))
            for o in entry.get("options", [])
            if o.get("url")
        ]

    def reprice(self, prices: dict[str, str]) -> None:
        """url -> price, as the shop's cart showed it."""
        with store.edit(self.path, {}) as items:
            for entry in items.values():
                for o in entry["options"]:
                    o["price"] = prices.get(o.get("url"), o.get("price", ""))

    def vocabulary(self) -> list[str]:
        return [n for key, entry in self._load().items() for n in (key, *entry.get("aliases", []))]

    def max_qty(self, key: str) -> int:
        return self._load().get(key, {}).get("max_qty", DEFAULT_MAX_QTY)

    def learn(self, spoken: str, product: Product) -> str:
        """Remember a picked product as the first choice for what was said. Returns the catalog key."""
        with store.edit(self.path, {}) as items:
            key = self._index(items).get(normalize(spoken)) or normalize(spoken)
            entry = items.setdefault(key, {"aliases": [], "max_qty": DEFAULT_MAX_QTY, "options": []})
            rest = [o for o in entry["options"] if o.get("url") != product.url]
            picked = {"name": product.name, "size": product.size, "url": product.url, "price": product.price}
            entry["options"] = [picked, *rest][:MAX_OPTIONS]
        return key


def _repaired(p: Product) -> Product:
    """Before 24 Oct, cards with a brand line were saved as name=brand, size=full name."""
    if p.size and not is_size(p.size) and squash(p.size).startswith(squash(p.name)):
        return Product(p.size, "", p.url, p.price, p.image, p.mrp)
    return p


@dataclass
class Plan:
    ready: list[tuple[str, int]]
    to_pick: list[tuple[str, int]]
    notes: list[str]


def plan(items: list[tuple[str, int]], catalog: Catalog) -> Plan:
    merged: dict[str, int] = {}
    to_pick, notes = [], []
    for name, qty in items:
        key = catalog.lookup(name)
        if key and catalog.options(key):
            merged[key] = merged.get(key, 0) + qty
        else:
            to_pick.append((key or name, qty))
    ready = []
    for key, qty in merged.items():
        limit = catalog.max_qty(key)
        if qty > limit:
            notes.append(f"{key}: asked for {qty}, capped at {limit}")
        ready.append((key, min(qty, limit)))
    return Plan(ready, [(n, min(q, DEFAULT_MAX_QTY)) for n, q in to_pick], notes)


def combine(basket: list[tuple[str, int]], items: list[tuple[str, int]], catalog: Catalog) -> list[tuple[str, int]]:
    """The waiting basket plus a new list; repeats add up, within each item's limit."""
    merged: dict[str, int] = {}
    for key, qty in [*basket, *items]:
        merged[key] = min(merged.get(key, 0) + qty, catalog.max_qty(key))
    return list(merged.items())
