"""ledger.json: {"orders": [{"id", "day", "at", "status", "total", "items"}], "basket": [[key, qty]],
"hold": [chat, until] | null, "offer": [chat, message] | null, "address": [id, label] | null}

hold: when the basket gets placed. offer: the one message whose Order now button works.
address: where the basket goes instead of the household's address.

status: placing -> placed | failed | unknown. "placing" left over after a crash may or may not
have gone through: it is reported, never retried.
"""

import statistics
import uuid
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

from . import store

EMPTY = {"orders": [], "basket": [], "hold": None, "offer": None, "address": None}


class Ledger:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        return {**EMPTY, **store.read(self.path, {})}

    def _orders(self) -> list[dict]:
        return self._read()["orders"]

    def _edit(self):
        return store.edit(self.path, EMPTY)

    def basket(self) -> list[tuple[str, int]]:
        return [(key, qty) for key, qty in self._read()["basket"]]

    def _set(self, key: str, value) -> None:
        with self._edit() as data:
            data[key] = value

    def set_basket(self, items: list[tuple[str, int]]) -> None:
        self._set("basket", [list(i) for i in items])

    def hold(self) -> tuple[int, datetime] | None:
        return (h[0], datetime.fromisoformat(h[1])) if (h := self._read()["hold"]) else None

    def set_hold(self, chat_id: int | None = None, until: datetime | None = None) -> None:
        self._set("hold", until and [chat_id, until.isoformat(timespec="seconds")])

    def offer(self) -> tuple[int, int] | None:
        return tuple(o) if (o := self._read()["offer"]) else None

    def set_offer(self, chat_id: int | None = None, message_id: int | None = None) -> None:
        self._set("offer", message_id and [chat_id, message_id])

    def address(self) -> tuple[str, str] | None:
        return tuple(a) if (a := self._read()["address"]) else None

    def set_address(self, address_id: str | None = None, label: str = "") -> None:
        self._set("address", address_id and [address_id, label])

    def spent_today(self) -> float:
        today = date.today().isoformat()
        # unconfirmed attempts count: better to under-spend
        return sum(
            o["total"] or 0
            for o in self._orders()
            if o["day"] == today and o["status"] in ("placed", "placing", "unknown")
        )

    def begin(self, items: list[tuple[str, int]], total: float) -> str:
        order_id = uuid.uuid4().hex[:8]
        now = datetime.now()
        order = {
            "id": order_id,
            "day": now.date().isoformat(),
            "at": now.isoformat(timespec="seconds"),
            "status": "placing",
            "total": total,
            "items": [list(i) for i in items],
        }
        with self._edit() as data:
            data["orders"] = [*data["orders"], order][-1000:]
        return order_id

    def finish(self, order_id: str, status: str) -> None:
        with self._edit() as data:
            for o in data["orders"]:
                if o["id"] == order_id:
                    o["status"] = status

    def placing(self) -> bool:
        return any(o["status"] == "placing" for o in self._orders())

    def interrupted(self) -> list[dict]:
        """Attempts still 'placing' (the bot stopped mid-order). Marks them 'unknown'."""
        with self._edit() as data:
            stuck = [o for o in data["orders"] if o["status"] == "placing"]
            for o in stuck:
                o["status"] = "unknown"
        return stuck

    def due(self, today: date | None = None) -> list[tuple[str, int]]:
        """Items bought 3+ times whose usual gap has passed (but not lapsed: > 3 gaps)."""
        today = today or date.today()
        history: dict[str, list[tuple[date, int]]] = {}
        for o in self._orders():
            if o["status"] == "placed":
                for key, qty in o["items"]:
                    history.setdefault(key, []).append((date.fromisoformat(o["day"]), qty))
        due = []
        for key, buys in history.items():
            days = sorted({d for d, _ in buys})
            gaps = [(b - a).days for a, b in pairwise(days)]
            if len(gaps) < 2:
                continue
            every = statistics.median(gaps)
            since = (today - days[-1]).days
            if every <= since <= 3 * every:
                due.append((since / every, key, round(statistics.median(q for _, q in buys))))
        return [(key, qty) for _, key, qty in sorted(due, reverse=True)]
