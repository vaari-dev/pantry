"""Households: one shop account each, shared by the Telegram users in it.

households.json:
  {"households": {"<id>": {"owner": uid, "address_label", "address_match", "daily_cap", "min_order", "place_orders"}},
   "users": {"<uid>": {"name": "Asha", "household": "<id>" | null}},
   "invites": {"<code>": {"household": "<id>", "expires": "<iso time>"}}}
"""

import contextlib
import os
import secrets
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import store
from .catalog import Catalog
from .ledger import Ledger

INVITE_VALID = timedelta(hours=24)
INVITE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEFAULTS = {"address_label": "", "address_match": "", "daily_cap": 1000.0, "min_order": 100.0, "place_orders": False}


@dataclass(frozen=True)
class Household:
    id: str
    dir: Path
    owner: int
    members: tuple[int, ...]
    address_label: str  # name of the saved address on the shop
    address_match: str  # text that must appear in that address
    daily_cap: float
    min_order: float  # the shop charges a delivery fee below this
    place_orders: bool

    @property
    def state_file(self) -> Path:
        return self.dir / "session.json"

    @property
    def debug_dir(self) -> Path:
        return self.dir / "debug"

    def catalog(self) -> Catalog:
        return Catalog(self.dir / "catalog.json")

    def ledger(self) -> Ledger:
        return Ledger(self.dir / "ledger.json")


class Accounts:
    def __init__(self, data_dir: Path, admin: int):
        self.root, self.admin = data_dir / "households", admin
        self.path = data_dir / "households.json"
        self._lock = threading.Lock()

    def _read(self) -> dict:
        return {"households": {}, "users": {}, "invites": {}, **store.read(self.path, {})}

    @contextlib.contextmanager
    def _edit(self):
        with self._lock:
            data = self._read()
            yield data
            store.write(self.path, data)

    def _household(self, data: dict, hid: str | None) -> Household | None:
        if (h := data["households"].get(hid)) is None:
            return None
        members = tuple(int(u) for u, user in data["users"].items() if user["household"] == hid)
        settings = {**DEFAULTS, **h}
        return Household(hid, self.root / hid, h["owner"], members, *(settings[k] for k in DEFAULTS))

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

    def connect(self, uid: int, name: str, address_label: str, address_match: str, session: Path) -> Household:
        """Install a shop login: into the household uid owns (keeping its picks and history), else a new one."""
        with self._edit() as data:
            user = data["users"].setdefault(str(uid), {"name": name, "household": None})
            user["name"] = name or user["name"]
            hid = user["household"]
            if data["households"].get(hid, {}).get("owner") != uid:
                _leave(self.root, data, uid)
                hid = user["household"] = secrets.token_hex(4)
                data["households"][hid] = {**DEFAULTS, "owner": uid}
            data["households"][hid].update(address_label=address_label, address_match=address_match)
            (self.root / hid).mkdir(parents=True, exist_ok=True)
            os.replace(session, self.root / hid / "session.json")
        return self._household(data, hid)

    def update(self, hid: str, **settings) -> None:
        if not settings.keys() <= DEFAULTS.keys():
            raise ValueError(f"not settings: {settings.keys() - DEFAULTS.keys()}")
        with self._edit() as data:
            data["households"][hid].update(settings)

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
        (root / hid / "session.json").unlink(missing_ok=True)
