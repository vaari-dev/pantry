"""Words people say -> shop products, best first.

catalog.json: {"egg": {"aliases": ["anda"], "max_qty": 3, "options": [{"name", "size", "url", "price"}, ...]}}
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
QUANTITY = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|gms?|g|mg|ml|ltrs?|litres?|liters?|l|pcs?|pieces?|units?|packs?|n)(?![a-z])", re.I
)
UNITS = {"gm": "g", "gms": "g", "ltr": "l", "ltrs": "l", "litre": "l", "litres": "l", "liter": "l", "liters": "l",
         "pcs": "pc", "piece": "pc", "pieces": "pc", "unit": "pc", "units": "pc", "n": "pc",
         "packs": "pack"}  # fmt: skip
SCALE = {"kg": (1000, "g"), "l": (1000, "ml")}
# A pack size line: "500 g", "1 pc (0.35 g)", "2 x 200 ml", "Pack of 6". Names may hold a size too,
# but never start with one.
SIZE = re.compile(rf"^(?:\d+\s*x\s*)?(?:{QUANTITY.pattern}|pack of \d+)", re.I)


def quantities(text: str) -> set[tuple[float, str]]:
    """{(350.0, "g")}-like: "0.35 kg", "0.35kg" and "350 gm" read the same, as do "1 kg" and "1000 g"."""
    found = set()
    for number, unit in QUANTITY.findall(text):
        unit = UNITS.get(unit.lower(), unit.lower())
        factor, unit = SCALE.get(unit, (1, unit))
        found.add((round(float(number) * factor, 3), unit))
    return found


def is_size(line: str) -> bool:
    return len(line) <= 40 and bool(SIZE.match(line.strip()))


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
        """The shop's id for this pack: the URL's last part ("/pn/<slug>/pvid/<id>")."""
        return self.url.rstrip("/").rsplit("/", 1)[-1]


def rupees(text: str) -> float | None:
    """ "₹1,104" -> 1104.0; None for anything else."""
    try:
        return float(text.lstrip("₹").replace(",", "")) if text else None
    except ValueError:
        return None


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
    """Until 24 Oct, a card with a brand line above the name was saved as name=brand, size=name
    ("Elle 18", "Elle 18 Eyedrama Kajal Bold Black"): such a pick is its full name, size unknown."""
    squash = lambda t: " ".join(t.lower().split())  # noqa: E731
    if p.size and not is_size(p.size) and squash(p.size).startswith(squash(p.name)):
        return Product(p.size, "", p.url, p.price, p.image, p.mrp)
    return p


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
