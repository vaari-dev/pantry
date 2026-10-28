"""Words people say -> the products a household buys for them, best first, each in the sizes seen.

catalog.json: {"milk": {"aliases": ["doodh"], "choices": [{"name": "Amul Cow Milk", "sizes": [product, ...]}, ...]}}
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import store

MAX_CHOICES = 3

QUANTITY = re.compile(
    r"(?<![\w.])(?:(\d+)\s*x\s*)?(\d+(?:\.\d+)?)\s*"
    r"(kg|gms?|g|mg|ml|ltrs?|litres?|liters?|l|pcs?|pieces?|units?|packs?|dozen|n)(?![a-z])(?:\s*x\s*(\d+)\b)?"
    r"|pack of (\d+)",
    re.I,
)
FILLER_IN_SIZE = re.compile(r"\([^)]*\)|[^\w\s]|\d|\b(x|approx|each)\b", re.I)
UNITS = {"k": ("g", 1000), "g": ("g", 1), "mg": ("g", 0.001), "m": ("ml", 1), "l": ("ml", 1000)}  # by first letters
PACK = "pack"  # the unit of things sold by the piece

Qty = int | str  # packs, or an amount like "2 l"


def measure(text: str) -> tuple[int, str] | None:
    """ "2 l" -> (2000, "ml"), "1 pack (2 x 500 g)" -> (1000, "g"); None for counts like "6 pcs"."""
    for m in QUANTITY.finditer(text or ""):
        unit = (m.group(3) or "").lower()
        base, factor = UNITS.get(unit[:2]) or UNITS.get(unit[:1], (None, 0))
        times = int(m.group(1) or 1) * int(m.group(4) or 1)  # 2 x 500 ml, 500 ml x 2
        if base and (value := round(times * float(m.group(2)) * factor)) > 0:
            return value, base
    return None


def amount(value: float, unit: str) -> str:
    """(2000, "ml") -> "2 l"."""
    big = {"ml": "l", "g": "kg"}[unit]
    return f"{value / 1000:g} {big}" if value >= 1000 else f"{value:g} {unit}"


def shown(qty: Qty) -> str:
    return f"×{qty}" if isinstance(qty, int) else qty


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
    family: str = ""  # the shop's id for the product, the same in all its sizes

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


def size_of(p: Product) -> tuple[int, str] | None:
    """Before 24 Oct, some picks were saved with their size in the name only."""
    return measure(p.size) or (measure(p.name) if not p.size else None)


def same_product(a: Product, b: Product) -> bool:
    """Two sizes of one product: by the shop's id, else by the name without its size."""
    if a.family and b.family:
        return a.family == b.family
    return normalize(QUANTITY.sub(" ", a.name)) == normalize(QUANTITY.sub(" ", b.name))


@dataclass
class Choice:
    """A product the household buys for an item, in the sizes seen; the first is their usual pack."""

    name: str
    sizes: list[Product] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"name": self.name, "sizes": [vars(p) for p in self.sizes]}

    @classmethod
    def from_json(cls, d: dict) -> "Choice":
        """Also a single product, as picks open at the 28 Oct deploy were saved."""
        return cls(d["name"], [_repaired(Product(**p)) for p in d.get("sizes", [d])])

    def wants(self, qty: Qty) -> tuple[int, str] | None:
        """qty in the unit its sizes compare in: a count is of usual packs; None if they don't compare."""
        usual = size_of(self.sizes[0]) if self.sizes else None
        if isinstance(qty, int):
            return (qty * usual[0], usual[1]) if usual else (qty, PACK)
        want = measure(qty)
        return want if want and usual and want[1] == usual[1] else None


def grouped(found: list[Product], limit: int) -> list[Choice]:
    """Search results as products, each in all the sizes found."""
    choices: list[Choice] = []
    for p in found:
        if same := next((c for c in choices if same_product(c.sizes[0], p)), None):
            same.sizes.append(p)
        elif len(choices) < limit:
            choices.append(Choice(QUANTITY.sub(" ", p.name).strip() or p.name, [p]))
    for c in choices:
        c.sizes.sort(key=lambda p: (not (m := size_of(p)), m and m[0]))
    return choices


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

    def choices(self, key: str) -> list[Choice]:
        return _choices(self._load().get(key, {}))

    def reprice(self, prices: dict[str, str]) -> None:
        """url -> price, as the shop's cart showed it."""
        with store.edit(self.path, {}) as items:
            for entry in items.values():
                entry["choices"] = [c.to_json() for c in _choices(entry)]
                for c in entry["choices"]:
                    for p in c["sizes"]:
                        p["price"] = prices.get(p["url"], p["price"])
                entry.pop("options", None)

    def vocabulary(self) -> list[str]:
        return [n for key, entry in self._load().items() for n in (key, *entry.get("aliases", []))]

    def learn(self, spoken: str, choice: Choice, usual: Product | None = None) -> str:
        usual = usual or choice.sizes[0]
        picked = Choice(choice.name, [usual, *(p for p in choice.sizes if p.url != usual.url)])
        with store.edit(self.path, {}) as items:
            key = self._index(items).get(normalize(spoken)) or normalize(spoken)
            entry = items.setdefault(key, {"aliases": []})
            rest = [c for c in _choices(entry) if not same_product(c.sizes[0], usual)]
            entry["choices"] = [c.to_json() for c in [picked, *rest][:MAX_CHOICES]]
            entry.pop("options", None)
        return key


def _choices(entry: dict) -> list[Choice]:
    """Before 28 Oct, each pick was saved as one product in one size ("options")."""
    if "choices" in entry:
        return [Choice.from_json(c) for c in entry["choices"] if c["sizes"]]
    old = [_repaired(Product(o["name"], o["size"], o["url"], o.get("price", ""))) for o in entry.get("options", [])]
    old = [p for p in old if p.url]
    return [Choice(c.name, sorted(c.sizes, key=old.index)) for c in grouped(old, MAX_CHOICES)]  # latest pick first


def _repaired(p: Product) -> Product:
    """Before 24 Oct, cards with a brand line were saved as name=brand, size=full name."""
    if p.size and not is_size(p.size) and squash(p.size).startswith(squash(p.name)):
        return Product(p.size, "", p.url, p.price, p.image, p.mrp, p.family)
    return p


@dataclass
class Plan:
    ready: list[tuple[str, Qty]]
    to_pick: list[tuple[str, Qty]]


def add(a: Qty | None, b: Qty) -> Qty:
    """Counts add up, and amounts of one kind; otherwise the later one stands."""
    if isinstance(a, int) and isinstance(b, int):
        return a + b
    if isinstance(a, str) and isinstance(b, str) and (x := measure(a)) and (y := measure(b)) and x[1] == y[1]:
        return amount(x[0] + y[0], x[1])
    return b


def plan(items: list[tuple[str, Qty]], catalog: Catalog) -> Plan:
    merged: dict[str, Qty] = {}
    to_pick = []
    for name, qty in items:
        key = catalog.lookup(name)
        if key and catalog.choices(key):
            merged[key] = add(merged.get(key), qty)
        else:
            to_pick.append((key or name, qty))
    return Plan(list(merged.items()), to_pick)


def combine(basket: list[tuple[str, Qty]], items: list[tuple[str, Qty]]) -> list[tuple[str, Qty]]:
    merged: dict[str, Qty] = {}
    for key, qty in [*basket, *items]:
        merged[key] = add(merged.get(key), qty)
    return list(merged.items())
