"""Households: shop accounts shared by the Telegram users in it, ordering from one shop at a time.

households.json:
  {"households": {"<id>": {"owner": uid, "daily_cap", "shop": "zepto",
                           "address_label", "address_match", "min_order", "place_orders",
                           "shops": {"blinkit": {"address_label", ...}}}},
   "users": {"<uid>": {"name": "Asha", "household": "<id>" | null}},
   "invites": {"<code>": {"household": "<id>", "expires": "<iso time>"}}}

households/<id>/: ledger.json (basket, orders, the daily limit: all shops), and per shop
session.json, catalog.json (the products picked there): beside the ledger for Zepto, in <shop>/ for others.

Zepto's settings and files stay where they were before Blinkit (Oct 2026), so an older version still
reads them as they are.
"""

import os
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import store
from .catalog import Catalog
from .ledger import Ledger

INVITE_VALID = timedelta(hours=24)
INVITE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
EMPTY = {"households": {}, "users": {}, "invites": {}}
DEFAULTS = {"daily_cap": 1000.0, "shop": "zepto"}
SHOP_DEFAULTS = {"address_label": "", "address_match": "", "min_order": 100.0, "place_orders": True}
FIRST = "zepto"


def _settings(h: dict, shop: str) -> dict:
    return h if shop == FIRST else h.setdefault("shops", {}).setdefault(shop, {})


def _connected(h: dict) -> tuple[str, ...]:
    return (FIRST,) * ("address_label" in h) + tuple(h.get("shops", {}))


@dataclass(frozen=True)
class Household:
    id: str
    dir: Path
    owner: int
    members: tuple[int, ...]
    shops: tuple[str, ...]  # connected at some point; logged_in tells if still usable
    daily_cap: float
    shop: str  # where orders go; the fields below are its settings
    address_label: str  # name of the saved address on the shop
    address_match: str  # text that must appear in that address
    min_order: float  # the shop charges a delivery fee below this
    place_orders: bool

    def _dir(self, shop: str) -> Path:
        return self.dir if shop == FIRST else self.dir / shop

    def session(self, shop: str) -> Path:
        return self._dir(shop) / "session.json"

    def logged_in(self, shop: str) -> bool:
        return self.session(shop).exists()

    @property
    def state_file(self) -> Path:
        return self.session(self.shop)

    @property
    def debug_dir(self) -> Path:
        return self.dir / "debug"

    def catalog(self, shop: str | None = None) -> Catalog:
        return Catalog(self._dir(shop or self.shop) / "catalog.json")

    def ledger(self) -> Ledger:
        return Ledger(self.dir / "ledger.json")


def address_parts(text: str) -> tuple[str, str]:
    """'Home\\nT6/503, MG Road' -> ('Home', 'T6/503'): what the address starts with, and must contain."""
    lines = [line.strip() for line in text.splitlines() if line.strip()] or [""]
    if len(lines) == 1:
        lines = re.split(r"\s+-\s+|,\s*", lines[0], maxsplit=1)
    label, rest = lines[0], " ".join(lines[1:])
    return label, rest.split(",")[0].strip() or label


class Accounts:
    def __init__(self, data_dir: Path, admin: int):
        self.root, self.admin = data_dir / "households", admin
        self.path = data_dir / "households.json"

    def _read(self) -> dict:
        return {**EMPTY, **store.read(self.path, {})}

    def _edit(self):
        return store.edit(self.path, EMPTY)

    def _household(self, data: dict, hid: str | None) -> Household | None:
        if (h := data["households"].get(hid)) is None:
            return None
        members = tuple(int(u) for u, user in data["users"].items() if user["household"] == hid)
        settings = {**DEFAULTS, **h}
        shop = {**SHOP_DEFAULTS, **(h if settings["shop"] == FIRST else h.get("shops", {}).get(settings["shop"], {}))}
        return Household(
            hid, self.root / hid, h["owner"], members, _connected(h), settings["daily_cap"], settings["shop"],
            *(shop[k] for k in SHOP_DEFAULTS),
        )  # fmt: skip

    def allowed(self, uid: int) -> bool:
        return uid == self.admin or str(uid) in self._read()["users"]

    def get(self, hid: str) -> Household | None:
        return self._household(self._read(), hid)

    def household_of(self, uid: int) -> Household | None:
        data = self._read()
        return self._household(data, data["users"].get(str(uid), {}).get("household"))

    def all(self) -> list[Household]:
        data = self._read()
        return [self._household(data, hid) for hid in data["households"]]

    def name(self, uid: int) -> str:
        return self._read()["users"].get(str(uid), {}).get("name") or str(uid)

    def allow(self, uid: int) -> None:
        with self._edit() as data:
            data["users"].setdefault(str(uid), {"name": "", "household": None})

    def connect(
        self, uid: int, name: str, shop: str, address_label: str, address_match: str, session: Path, ordering: bool
    ) -> Household:
        """Install a shop login and order there: into the household uid owns (keeping its picks and history),
        else a new one. ordering: whether a shop new to it places orders."""
        with self._edit() as data:
            user = data["users"].setdefault(str(uid), {"name": name, "household": None})
            user["name"] = name or user["name"]
            hid = user["household"]
            if data["households"].get(hid, {}).get("owner") != uid:
                _leave(self.root, data, uid)
                hid = user["household"] = secrets.token_hex(4)
                data["households"][hid] = {**DEFAULTS, "owner": uid}
            h = data["households"][hid]
            if shop not in _connected(h):
                _settings(h, shop).update({**SHOP_DEFAULTS, "place_orders": ordering})
            h["shop"] = shop
            _settings(h, shop).update(address_label=address_label, address_match=address_match)
            hh = self._household(data, hid)
            hh.state_file.parent.mkdir(parents=True, exist_ok=True)
            os.replace(session, hh.state_file)
        return hh

    def update(self, hid: str, **settings) -> None:
        """The household's settings, and those of the shop it orders from."""
        if unknown := settings.keys() - (DEFAULTS.keys() - {"shop"}) - SHOP_DEFAULTS.keys():
            raise ValueError(f"not settings: {unknown}")
        with self._edit() as data:
            h = data["households"][hid]
            for key, value in settings.items():
                (h if key in DEFAULTS else _settings(h, h.get("shop", FIRST)))[key] = value

    def switch(self, hid: str, shop: str) -> Household:
        with self._edit() as data:
            h = data["households"][hid]
            if shop not in _connected(h):
                raise ValueError(f"{shop} is not connected")
            h["shop"] = shop
        return self._household(data, hid)

    def invite(self, hid: str) -> str:
        now = datetime.now()
        code = "".join(secrets.choice(INVITE_LETTERS) for _ in range(6))
        with self._edit() as data:
            data["invites"] = {c: i for c, i in data["invites"].items() if datetime.fromisoformat(i["expires"]) > now}
            data["invites"][code] = {"household": hid, "expires": (now + INVITE_VALID).isoformat(timespec="seconds")}
        return code

    def join(self, uid: int, name: str, code: str) -> Household | None:
        """None if the code is unknown, used or expired."""
        with self._edit() as data:
            invite = data["invites"].pop(code.strip().upper(), {"household": None, "expires": "2000-01-01"})
            hid = invite["household"]
            if datetime.fromisoformat(invite["expires"]) < datetime.now() or hid not in data["households"]:
                return None
            if data["users"].get(str(uid), {}).get("household") == hid:
                return self._household(data, hid)
            _leave(self.root, data, uid)
            data["users"][str(uid)] = {"name": name, "household": hid}
        return self._household(data, hid)

    def leave(self, uid: int, hid: str) -> None:
        with self._edit() as data:
            if data["users"].get(str(uid), {}).get("household") == hid:
                _leave(self.root, data, uid)


def _leave(root: Path, data: dict, uid: int) -> None:
    """An owner hands over to another member, without their shop login; the last one out deletes it all."""
    user = data["users"].get(str(uid), {})
    if not (hid := user.get("household")):
        return
    user["household"] = None
    others = [int(u) for u, o in data["users"].items() if o["household"] == hid]
    if not others:
        del data["households"][hid]
        data["invites"] = {c: i for c, i in data["invites"].items() if i["household"] != hid}
        shutil.rmtree(root / hid, ignore_errors=True)
    elif data["households"][hid]["owner"] == uid:
        data["households"][hid]["owner"] = others[0]
        for session in [*(root / hid).glob("session.json"), *(root / hid).glob("*/session.json")]:
            session.unlink()
