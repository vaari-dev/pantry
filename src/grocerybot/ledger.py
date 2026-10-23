"""ledger.json: {"orders": [{"id", "day", "at", "status", "total", "items"}], "basket": [[key, qty]],
"hold": {"chat", "until"} or null, "offer": {"chat", "message"} or null}

hold: the basket reached the smallest order and is placed at `until` unless more is added.
offer: the one message whose Order now button still works.

status: placing -> placed | failed | unknown. "placing" left over after a crash may or may not
have gone through: it is reported, never retried.
"""

import statistics
import uuid
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

from . import store


class Ledger:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        return {"orders": [], "basket": [], "hold": None, "offer": None, **store.read(self.path, {})}

    def _orders(self) -> list[dict]:
        return self._read()["orders"]

    def _save(self, orders: list[dict]) -> None:
        store.write(self.path, {**self._read(), "orders": orders[-1000:]})

    def basket(self) -> list[tuple[str, int]]:
        return [(key, qty) for key, qty in self._read()["basket"]]

    def set_basket(self, items: list[tuple[str, int]]) -> None:
        store.write(self.path, {**self._read(), "basket": [list(i) for i in items]})

    def hold(self) -> tuple[int, datetime] | None:
        """(chat id, when to place it) for a held basket."""
        if h := self._read()["hold"]:
            return h["chat"], datetime.fromisoformat(h["until"])
        return None

    def set_hold(self, chat_id: int | None, until: datetime | None = None) -> None:
        hold = {"chat": chat_id, "until": until.isoformat(timespec="seconds")} if until else None
        store.write(self.path, {**self._read(), "hold": hold})

    def offer(self) -> tuple[int, int] | None:
        """(chat id, message id) of the live Order now button."""
        if o := self._read()["offer"]:
            return o["chat"], o["message"]
        return None

    def set_offer(self, chat_id: int | None, message_id: int | None = None) -> None:
        offer = {"chat": chat_id, "message": message_id} if message_id else None
        store.write(self.path, {**self._read(), "offer": offer})

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
        orders = self._orders()
        orders.append(
            {
                "id": order_id,
                "day": now.date().isoformat(),
                "at": now.isoformat(timespec="seconds"),
                "status": "placing",
                "total": total,
                "items": [list(i) for i in items],
            }
        )
        self._save(orders)
        return order_id

    def finish(self, order_id: str, status: str) -> None:
        orders = self._orders()
        for o in orders:
            if o["id"] == order_id:
                o["status"] = status
        self._save(orders)

    def interrupted(self) -> list[dict]:
        """Attempts still 'placing' (the bot stopped mid-order). Marks them 'unknown'."""
        orders = self._orders()
        stuck = [o for o in orders if o["status"] == "placing"]
        for o in stuck:
            o["status"] = "unknown"
        if stuck:
            self._save(orders)
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
