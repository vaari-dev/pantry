"""Words people say -> shop products, best first.

catalog.json: {"egg": {"aliases": ["anda"], "max_qty": 3, "options": [{"name", "size", "url"}, ...]}}
"""

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from . import store

DEFAULT_MAX_QTY = 3
MAX_OPTIONS = 3

FILLER = frozenset(
    """order please pls get buy bring some a an the me us i want need and also mujhe hume
    humko chahiye lao le aao bhej dena dedo kar karo mangwa mangao ka ki ke packet packets pack packs""".split()
)
NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "ek": 1,
    "teen": 3,
    "char": 4,
    "chaar": 4,
    "paanch": 5,
    "panch": 5,
    "chhe": 6,
    "saat": 7,
    "aath": 8,
    "nau": 9,
    "das": 10,
}
# How speech-to-text tends to spell Hindi words.
SPELLINGS = {
    "dudh": "doodh",
    "dood": "doodh",
    "anday": "anda",
    "unda": "anda",
    "aaloo": "aloo",
    "dhaniya": "dhania",
    "pyaj": "pyaz",
    "tamater": "tamatar",
    "bred": "bread",
}
SEPARATORS = re.compile(r"[,;\n&.!?]|\band\b|\baur\b|\bor\b|\bplus\b")


@dataclass(frozen=True)
class Product:
    name: str
    size: str
    url: str
    price: str = ""
    image: str = ""
    mrp: str = ""  # the original price, when the shop shows it struck through

    def label(self) -> str:
        return f"{self.name} ({self.size})"


def singular(word: str) -> str:
    """eggs -> egg, potatoes -> potato, berries -> berry; leaves chips/oats alone."""
    if word.endswith(("ss", "chips", "oats")):
        return word
    for suffix, repl in (("ies", "y"), ("oes", "o"), ("ches", "ch"), ("shes", "sh"), ("s", "")):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[: -len(suffix)] + repl
    return word


def normalize(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()
    return " ".join(singular(SPELLINGS.get(w, w)) for w in words)


def split_spoken(text: str) -> list[tuple[str, int]]:
    """'Order bread aur do anda, 3 milk please' -> [('bread', 1), ('anda', 2), ('milk', 3)]."""
    out = []
    for part in SEPARATORS.split(text.lower()):
        words = part.split()
        qty, name = 1, []
        for i, w in enumerate(words):
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if w.isdigit() or w in NUMBERS:
                qty = int(w) if w.isdigit() else NUMBERS[w]
            elif w == "do" and nxt and nxt not in FILLER:  # Hindi "two"
                qty = 2
            elif m := re.fullmatch(r"x(\d+)", w):
                qty = int(m.group(1))
            elif m := re.fullmatch(r"(?:do|du|doo)(anda|ande|anday|unda)", w):  # "duanda": glued "do anda"
                qty, name = 2, [*name, m.group(1)]
            elif w not in FILLER:
                name.append(w)
        if name:
            out.append((" ".join(name), max(qty, 1)))
    return out


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
        """Catalog key for a spoken name: exact (normalized), else a close spelling."""
        index = self._index(self._load())
        norm = normalize(name)
        if norm in index:
            return index[norm]
        close = difflib.get_close_matches(norm, index, n=1, cutoff=0.82)
        return index[close[0]] if close else None

    def options(self, key: str) -> list[Product]:
        entry = self._load().get(key, {})
        return [Product(o["name"], o["size"], o["url"]) for o in entry.get("options", []) if o.get("url")]

    def vocabulary(self) -> list[str]:
        return [n for key, entry in self._load().items() for n in (key, *entry.get("aliases", []))]

    def max_qty(self, key: str) -> int:
        return self._load().get(key, {}).get("max_qty", DEFAULT_MAX_QTY)

    def learn(self, spoken: str, product: Product) -> str:
        """Remember a picked product as the first choice for what was said. Returns the catalog key."""
        items = self._load()
        key = self._index(items).get(normalize(spoken)) or normalize(spoken)
        entry = items.setdefault(key, {"aliases": [], "max_qty": DEFAULT_MAX_QTY, "options": []})
        rest = [o for o in entry["options"] if o.get("url") != product.url]
        entry["options"] = [{"name": product.name, "size": product.size, "url": product.url}, *rest][:MAX_OPTIONS]
        store.write(self.path, items)
        return key


@dataclass
class Plan:
    ready: list[tuple[str, int]]
    to_pick: list[tuple[str, int]]
    notes: list[str]


def plan(text: str, catalog: Catalog) -> Plan:
    merged: dict[str, int] = {}
    to_pick, notes = [], []
    for name, qty in split_spoken(text):
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
