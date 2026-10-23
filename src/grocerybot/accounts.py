"""Who orders with which shop account, and each account's settings.

A household is one shop account (its login, settings, picks, basket and order history) shared by
one or more Telegram users. Everyone in it can order; its owner can log it in again, change its
settings and invite others. The admin (TELEGRAM_ADMIN_ID) lets new people in.

households.json (on the data volume):
  {"households": {"<id>": {"owner": uid, "address_label": "Home", "address_match": "12B",
                           "daily_cap": 1000, "min_order": 100, "place_orders": false}},
   "users": {"<uid>": {"name": "Asha", "household": "<id>" | null}},
   "invites": {"<code>": {"household": "<id>", "expires": "<iso time>"}}}
Each household's files live in households/<id>/: session.json, catalog.json, ledger.json, debug/.
"""

import os
import secrets
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from . import store
from .catalog import Catalog
from .ledger import Ledger

INVITE_VALID = timedelta(hours=24)
INVITE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
LEGACY_FILES = ("session.json", "catalog.json", "ledger.json", "debug")  # single-account layout
# What a new account starts with; its owner changes them in Telegram (see Bot._settings).
DEFAULTS = {"address_label": "", "address_match": "", "daily_cap": 1000.0, "min_order": 100.0, "place_orders": False}
log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Household:
    id: str
    dir: Path
    owner: int
    members: tuple[int, ...]
    address_label: str  # name of the saved address on the shop, e.g. "Home"
    address_match: str  # text that must appear in that address, e.g. a flat number
    daily_cap: float  # ₹ a day, counting unconfirmed attempts
    min_order: float  # the shop adds a delivery fee below this, so smaller lists wait in the basket
    place_orders: bool  # False: fill and check the cart only

    @property
    def state_file(self) -> Path:  # shop login (cookies); secret
        return self.dir / "session.json"

    @property
    def logged_in(self) -> bool:
        return self.state_file.exists()

    @property
    def debug_dir(self) -> Path:  # screenshots of failed runs only, pruned
        return self.dir / "debug"

    def catalog(self) -> Catalog:
        return Catalog(self.dir / "catalog.json")

    def ledger(self) -> Ledger:
        return Ledger(self.dir / "ledger.json")


class Accounts:
    def __init__(self, data_dir: Path, admin: int):
        self.root, self.admin = data_dir / "households", admin
        self.path = data_dir / "households.json"
        self._lock = threading.RLock()  # read-modify-write from the polling and worker threads

    def _read(self) -> dict:
        return {"households": {}, "users": {}, "invites": {}, **store.read(self.path, {})}

    def _household(self, data: dict, hid: str) -> Household | None:
        h = data["households"].get(hid)
        if h is None:
            return None
        members = tuple(int(u) for u, user in data["users"].items() if user.get("household") == hid)
        settings = {**DEFAULTS, **h}
        return Household(hid, self.root / hid, h["owner"], members, *(settings[k] for k in DEFAULTS))

    # ------------------------------------------------------------- reading

    def allowed(self, uid: int) -> bool:
        return uid == self.admin or str(uid) in self._read()["users"]

    def get(self, hid: str) -> Household | None:
        return self._household(self._read(), hid)

    def household_of(self, uid: int) -> Household | None:
        data = self._read()
        hid = data["users"].get(str(uid), {}).get("household")
        return self._household(data, hid) if hid else None

    def all(self) -> list[Household]:
        data = self._read()
        return [h for hid in data["households"] if (h := self._household(data, hid))]

    def name(self, uid: int) -> str:
        return self._read()["users"].get(str(uid), {}).get("name") or str(uid)

    # ------------------------------------------------------------- changes

    def bootstrap(self, data_dir: Path) -> None:
        """First start: turn a single-account box (its files directly in /data) into the household
        "home", owned by the admin. Its address is unset and ordering off until the admin sets them."""
        with self._lock:
            if self.path.exists():
                return
            data = self._read()
            if any((data_dir / name).exists() for name in LEGACY_FILES):
                home = self.root / "home"
                home.mkdir(parents=True, exist_ok=True)
                for name in LEGACY_FILES:
                    if (data_dir / name).exists():
                        os.replace(data_dir / name, home / name)
                data["households"]["home"] = {**DEFAULTS, "owner": self.admin}
                data["users"][str(self.admin)] = {"name": "", "household": "home"}
                log.info("household created from the single-account setup", household="home")
            store.write(self.path, data)

    def allow(self, uid: int, name: str = "") -> None:
        """An admin let this person in; they can now /setup their own account."""
        with self._lock:
            data = self._read()
            data["users"].setdefault(str(uid), {"name": name, "household": None})
            store.write(self.path, data)

    def connect(self, uid: int, name: str, address_label: str, address_match: str, session: Path) -> Household:
        """Install a fresh shop login. The owner of a household logs that one in again (picks and
        history are kept); anyone else gets a new household of their own."""
        with self._lock:
            data = self._read()
            user = data["users"].setdefault(str(uid), {"name": name, "household": None})
            user["name"] = name or user.get("name", "")
            hid = user.get("household")
            if not hid or data["households"].get(hid, {}).get("owner") != uid:
                if hid:
                    self._leave(data, uid)
                hid = secrets.token_hex(4)
                data["households"][hid] = {**DEFAULTS, "owner": uid}
                user["household"] = hid
            data["households"][hid].update(address_label=address_label, address_match=address_match)
            (self.root / hid).mkdir(parents=True, exist_ok=True)
            os.replace(session, self.root / hid / "session.json")
            store.write(self.path, data)
            return self._household(data, hid)

    def update(self, hid: str, **settings) -> None:
        """Change some of a household's settings (keys of DEFAULTS)."""
        if not settings.keys() <= DEFAULTS.keys():
            raise ValueError(f"not settings: {settings.keys() - DEFAULTS.keys()}")
        with self._lock:
            data = self._read()
            data["households"][hid].update(settings)
            store.write(self.path, data)

    def invite(self, hid: str) -> str:
        """A one-time code that lets one person join the household, valid for INVITE_VALID."""
        with self._lock:
            data = self._read()
            now = datetime.now()
            data["invites"] = {c: i for c, i in data["invites"].items() if datetime.fromisoformat(i["expires"]) > now}
            code = "".join(secrets.choice(INVITE_LETTERS) for _ in range(6))
            data["invites"][code] = {"household": hid, "expires": (now + INVITE_VALID).isoformat(timespec="seconds")}
            store.write(self.path, data)
            return code

    def join(self, uid: int, name: str, code: str) -> Household | None:
        """Use an invite code. None if it is unknown, used or expired. Owners must /leave first."""
        with self._lock:
            data = self._read()
            invite = data["invites"].pop(code.strip().upper(), None)
            if not invite or datetime.fromisoformat(invite["expires"]) < datetime.now():
                return None
            hid = invite["household"]
            if hid not in data["households"]:
                return None
            if data["users"].get(str(uid), {}).get("household"):
                self._leave(data, uid)
            data["users"][str(uid)] = {"name": name, "household": hid}
            store.write(self.path, data)
            return self._household(data, hid)

    def leave(self, uid: int) -> None:
        """Leave the household. An owner hands it to the longest-standing other member; the last
        one out deletes it, shop login and all."""
        with self._lock:
            data = self._read()
            self._leave(data, uid)
            store.write(self.path, data)

    def _leave(self, data: dict, uid: int) -> None:
        user = data["users"].get(str(uid), {})
        hid = user.get("household")
        if not hid:
            return
        user["household"] = None
        others = [int(u) for u, o in data["users"].items() if o.get("household") == hid]
        if others and data["households"][hid]["owner"] == uid:
            data["households"][hid]["owner"] = others[0]
        elif not others:
            del data["households"][hid]
            data["invites"] = {c: i for c, i in data["invites"].items() if i["household"] != hid}
            shutil.rmtree(self.root / hid, ignore_errors=True)
            log.info("household deleted", household=hid)
