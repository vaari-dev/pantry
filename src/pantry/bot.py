"""Telegram front end.

One worker thread runs the jobs, one at a time, so orders never overlap. /setup asks its questions
and signs in on its own thread; the polling thread hands it the person's answers through `replies`.
"""

import math
import queue
import re
import secrets
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import structlog
from structlog.contextvars import bind_contextvars, bound_contextvars, clear_contextvars, get_contextvars

from . import orders, store
from .accounts import Accounts, Household
from .catalog import Choice, Qty, combine, grouped, normalize, plan, shown
from .config import Config
from .intent import Reader, Request
from .shop import Address, Login, ShopError, Zepto
from .speech import Transcriber
from .telegram import Telegram, TelegramError, strikethrough

log = structlog.get_logger(__name__)

COMMANDS = {
    "basket": "What is waiting to be ordered",
    "clear": "Empty the waiting basket",
    "usual": "What we usually need around now",
    "settings": "Address, limits, ordering on/off",
    "members": "Who orders with this account",
    "invite": "Let someone order with your account",
    "setup": "Connect your shop account",
    "leave": "Stop ordering with this account",
    "help": "How to order",
}
NAME = "Pantry"
SHORT = "Groceries from a voice note."
DESCRIPTION = (
    f"🛒 {SHORT}\n\n"
    'Say "bread aur do anda" and I\'ll order it on your shop account, paid from its wallet or cash on delivery. '
    "No app to open, no browsing, no upsells."
)
HELP = (
    "🛒 Just tell me what you need, by voice or text:\n"
    '• "bread aur do anda"\n'
    '• "2 milk, bananas"\n\n'
    "Something new? I'll show you options with photos: tap ✅ Pick and I'll remember it next time.\n"
    "Small lists wait in the /basket until they reach the smallest order (/settings); "
    "then I order 3 minutes later, so you can still add something."
)
TRY = 'Try it now 👉 send a voice note saying "bread aur do anda", or type "2 milk, bananas".'
CONNECT = "🛒 Connect my shop account"
# One-time: left open, it can hide the next keyboard (Share my phone number).
CONNECT_BUTTON = {"keyboard": [[{"text": CONNECT}]], "resize_keyboard": True, "one_time_keyboard": True}
RECONNECT = "🔌 I now order through Zepto's own connection for assistants. Send /setup once to reconnect your account."
NOT_SET_UP = "Connect your shop account to start ordering: it takes a minute. Or open an invite link from family."
ALERT_REPEAT = timedelta(hours=6)
ANSWER_WAIT = 600
OTP_WAIT = 300
HOLD = 180
CARDS = 4
PICK_HOLD, PICK_WAIT = 60, 600  # an open pick delays a held order in PICK_HOLD steps, for up to PICK_WAIT
STALE = timedelta(minutes=5)  # a restart this late past a hold asks before ordering
KEPT = "Still in the /basket: tap Order now to try again, or /clear to empty it."
KEPT_LIMIT = "Still in the /basket: raise today's limit with /limit, then tap Order now; or /clear to empty it."
SHARE_PHONE = {
    "keyboard": [[{"text": "📱 Share my phone number", "request_contact": True}]],
    "resize_keyboard": True,
    "is_persistent": True,
}
NO_KEYBOARD = {"remove_keyboard": True}
HEARTBEAT = Path("/tmp/heartbeat")  # noqa: S108  read by the container healthcheck
RESTARTING = "I'm restarting for an update and will be back in a minute"
NOT_REACHED = "⏸ I restarted for an update before getting to your last request; please send it again."
KEEP = 86400  # Telegram keeps updates a day; older offsets may no longer match its update ids


def _rupees(text: str) -> float:
    if not 0 < (value := float(text)) < math.inf:
        raise ValueError(text)
    return value


SETTINGS = {
    "/limit": ("daily_cap", _rupees, "1500"),
    "/minimum": ("min_order", _rupees, "99"),
    "/ordering": ("place_orders", {"on": True, "off": False}.__getitem__, "on"),
}


def _items(items: list[tuple[str, Qty]]) -> str:
    return ", ".join(f"{k} {shown(q)}" for k, q in items)


def _basket(hh: Household) -> str:
    rows, total, unpriced = orders.priced(hh)
    if not rows:
        return ""
    lines = [f"• {text}" for text, _ in rows]
    total_line = f"Total ₹{total:g}" + (f" + {unpriced} without a price" if unpriced else "")
    if not unpriced and total < hh.min_order:
        total_line += f" (orders start at ₹{hh.min_order:g})"
    if to := hh.ledger().address():
        total_line += f"\nDelivering to {to[1]}"
    return "\n".join(["🧺 Your basket", *lines, "", total_line])


def _order_now(hh: Household) -> list[tuple[str, str]]:
    return [("🛒 Order now", f"now:{hh.id}")]


class SetupStopped(Exception):
    pass


class Check(NamedTuple):
    """Far more than usual of an item, until they say how much."""

    item: str
    asked: Qty
    usual: Qty
    change: bool  # sets the basket's quantity, rather than adds to it
    ids: list[int]


@dataclass
class Picking:
    """What messages left open in a chat: products to pick, amounts to confirm. What they settled
    waits in the basket; the rest is ordered once all is answered."""

    chat_id: int
    household: str
    asks: dict[int, tuple[str, Qty, list[Choice], list[int]]]  # n -> spoken, qty, choices, message ids
    checks: dict[int, Check] = field(default_factory=dict)
    picked: list[tuple[str, Qty]] = field(default_factory=list)
    changed: list[tuple[str, Qty]] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    when: str = "hold"
    address: tuple[str, ...] = ()  # (id, label) of a saved address said with the items
    numbered: int = 0  # asks and checks so far: a number is never used twice

    def to_json(self) -> dict:
        asks = {n: [name, qty, [c.to_json() for c in cs], ids] for n, (name, qty, cs, ids) in self.asks.items()}
        return {**vars(self), "asks": asks}

    @classmethod
    def from_json(cls, d: dict) -> "Picking":
        asks = {
            int(n): (name, qty, [Choice.from_json(c) for c in cs], ids) for n, (name, qty, cs, ids) in d["asks"].items()
        }
        checks = {int(n): Check(*c) for n, c in d["checks"].items()}
        picked, changed = [tuple(i) for i in d["picked"]], [tuple(i) for i in d["changed"]]
        return cls(**{**d, "asks": asks, "checks": checks, "picked": picked, "changed": changed})


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tg = Telegram(cfg.telegram_token)
        self.accounts = Accounts(cfg.data_dir, cfg.admin_id)
        self.transcribe = Transcriber(cfg.whisper_model)
        self.read = Reader(cfg.anthropic_api_key)
        self.jobs: queue.Queue = queue.Queue()
        self.stopping = threading.Event()
        # With the update offset on disk, a restart neither drops a pick nor redoes an update.
        self.saved = cfg.data_dir / "bot.json"
        saved = store.read(self.saved, {})
        self.bot_id = cfg.telegram_token.partition(":")[0]  # another bot's offset and messages mean nothing here
        fresh = time.time() - saved.get("at", 0) < KEEP and saved.get("bot") == self.bot_id
        self.offset: int | None = saved.get("offset") if fresh else None
        self.picking: dict[str, Picking] = {}
        for pid, p in saved.get("picking", {}).items() if fresh else ():
            try:
                if time.time() - p["started"] < KEEP:
                    self.picking[pid] = Picking.from_json(p)
            except (KeyError, TypeError, ValueError):
                log.warning("saved pick dropped", exc_info=True)
        self.holds: dict[str, threading.Timer] = {}
        self.replies: dict[int, queue.Queue] = {}
        self.wants_setup: dict[int, str] = {}  # uid -> first name: asked for /setup before being let in
        self.lock = threading.Lock()
        self.alerted: dict[tuple[int, str], datetime] = {}
        self.username = ""

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        self.username = self.tg.setup(NAME, COMMANDS, DESCRIPTION, SHORT)
        log.info("bot started", bot_username=self.username, households=len(self.accounts.all()))
        self._resume()
        threading.Thread(target=self._load_whisper, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._daily, daemon=True).start()
        while not self.stopping.is_set():
            HEARTBEAT.touch()
            try:
                for update in self.tg.updates(self.offset):
                    self.offset = update["update_id"] + 1
                    clear_contextvars()
                    self._save()
                    bind_contextvars(update_id=update["update_id"])
                    try:
                        if "message" in update:
                            self._on_message(update["message"])
                        elif "callback_query" in update:
                            self._on_button(update["callback_query"])
                    except Exception:
                        log.exception("update failed")
            except TelegramError as e:
                clear_contextvars()
                log.warning("telegram polling failed", error=str(e))
                time.sleep(5)
        self._drain()

    def _load_whisper(self) -> None:
        started = time.monotonic()
        try:
            self.transcribe.load()
        except Exception:  # the first voice note tries again
            log.exception("whisper not loaded")
            return
        log.info("whisper loaded", seconds=round(time.monotonic() - started, 1))

    def _drain(self) -> None:
        """On SIGTERM: Telegram keeps new updates for the next start; the running job finishes, the
        worker hands back the queued ones, then the bot exits."""
        clear_contextvars()
        log.info("stopping", jobs=self.jobs.unfinished_tasks)
        with self.lock:
            for timer in self.holds.values():  # the ledger keeps holds; _resume restarts them
                timer.cancel()
        for answers in list(self.replies.values()):
            answers.put(None)
        self.jobs.join()
        log.info("stopped")

    def _save(self) -> None:
        """Never raises: a full disk must not stop the bot from draining."""
        with self.lock:  # held through the write, so an older state never lands last
            picking = {pid: p.to_json() for pid, p in self.picking.items()}
            try:
                store.write(
                    self.saved, {"bot": self.bot_id, "offset": self.offset, "at": time.time(), "picking": picking}
                )
            except OSError:
                log.exception("bot state not saved")

    def _resume(self) -> None:
        for hh in self.accounts.all():
            if not hh.address_id:
                self.alert(hh.owner, RECONNECT)
            for o in hh.ledger().interrupted():
                log.warning("interrupted order found", household_id=hh.id, total=o["total"], at=o["at"])
                text = f"⚠️ An order (₹{o['total']:g}, {o['at']}) was interrupted while being placed. "
                if self._say(hh.owner, text + "Check the shop's app; it was NOT retried."):
                    hh.ledger().finish(o["id"], "unknown")
            if not (held := hh.ledger().hold()):
                continue
            chat_id, until = held
            if datetime.now() - until < STALE:
                self._hold(chat_id, hh, max(0.0, (until - datetime.now()).total_seconds()))
            else:
                hh.ledger().set_hold()
                text = f"⏸ I was offline, so the order due at {until:%H:%M} wasn't placed. {KEPT}"
                self._offer_now(self._chat_for(hh, chat_id), hh, text)

    def _queue(self, job, *args) -> None:
        self.jobs.put((job, args, get_contextvars()))

    def _worker(self) -> None:
        """Failures are told to the job's first argument, a chat."""
        while True:
            job, args, context = self.jobs.get()
            name = job.__name__.lstrip("_")
            clear_contextvars()
            bind_contextvars(**{**context, "job": name})
            args = tuple(self.accounts.get(a.id) if isinstance(a, Household) else a for a in args)
            if hh := next((a for a in args if isinstance(a, Household)), None):
                bind_contextvars(household_id=hh.id)
            started = time.monotonic()
            try:
                if None in args:
                    log.info("job dropped", reason="household gone")
                    continue
                if self.stopping.is_set() and job != self._settled:  # what was picked must not be lost
                    log.info("job dropped", reason="restarting")
                    if job != self._release:  # a hold stays in the ledger and resumes itself
                        self._say(args[0], NOT_REACHED)
                    continue
                job(*args)
                log.info("job done", seconds=round(time.monotonic() - started, 1))
            except ShopError as e:
                log.warning("job stopped by the shop", error=e.logged)
                self._say(args[0], f"❌ Couldn't order: {e}.")
            except Exception:
                log.exception("job failed")
                self._say(args[0], "Something went wrong. Nothing was ordered unless you got a ✅.")
            finally:
                self.jobs.task_done()

    def _daily(self) -> None:
        while True:
            now = datetime.now()
            run_at = now.replace(hour=self.cfg.health_hour, minute=0, second=0, microsecond=0)
            time.sleep(((run_at if run_at > now else run_at + timedelta(days=1)) - now).total_seconds())
            self._queue(self._health, self.cfg.admin_id)

    def _health(self, _admin: int) -> None:
        for hh in self.accounts.all():
            with bound_contextvars(household_id=hh.id):
                try:
                    orders.check(hh)
                    log.info("health check ok")
                except ShopError as e:
                    log.warning("health check failed", error=e.logged)
                    self.alert(hh.owner, f"⚠️ Daily check of your shop account failed: {e}.")
                except Exception:
                    log.exception("health check failed")

    def _say(self, chat_id: int, text: str, buttons=None, markup=None, entities=None) -> int | None:
        try:
            return self.tg.send(chat_id, text, buttons, markup, entities)
        except TelegramError as e:
            log.warning("telegram send failed", to_chat_id=chat_id, error=str(e))
            return None

    def alert(self, uid: int, text: str, buttons=None) -> None:
        last = self.alerted.get((uid, text))
        if last and datetime.now() - last < ALERT_REPEAT:
            return
        self.alerted[(uid, text)] = datetime.now()
        self._say(uid, text, buttons)

    def _on_message(self, msg: dict) -> None:
        chat_id, user = msg["chat"]["id"], msg.get("from", {})
        uid, text = user.get("id"), (msg.get("text") or "").strip()
        private = msg["chat"].get("type") == "private"
        bind_contextvars(user_id=uid, chat_id=chat_id, message_id=msg.get("message_id"))
        if uid in self.replies and private:
            log.info("setup answer received")
            return self.replies[uid].put(msg)
        command, _, arg = ("/setup", "", "") if text == CONNECT else text.partition(" ")
        command = command.split("@")[0] if command.startswith("/") else ""
        if msg.get("voice") or msg.get("audio"):
            kind = "voice"
        elif not command:
            kind = "text" if text else "other"
        elif command[1:] in COMMANDS or command in ("/start", "/id", *SETTINGS):
            kind = command
        else:
            kind = "unknown command"  # never log what they typed
        log.info("message received", kind=kind, private=private)
        if command == "/id":
            return self._say(chat_id, f"Your Telegram id is {uid}.")
        if command == "/start" and arg:
            return self._join(chat_id, user, arg)
        if not self.accounts.allowed(uid):
            if private:
                self._say(
                    chat_id,
                    f"👋 Hi {user.get('first_name') or 'there'}! I order groceries from a voice note or a quick "
                    "text.\n\nI'm invite-only, so I've asked the admin to let you in. "
                    "I'll message you right here when you're in.",
                )
                if command == "/setup":
                    self.wants_setup[uid] = user.get("first_name", "")
                self._request_access(user)
            return
        hh = self.accounts.household_of(uid)
        if hh:
            bind_contextvars(household_id=hh.id)
        if command in ("/start", "/help"):
            if hh:
                self._say(chat_id, HELP)
            else:
                self._not_set_up(chat_id, f"{HELP}\n\n")
        elif command == "/setup":
            if private:
                self._start_setup(uid, user.get("first_name", ""))
            else:
                self._say(chat_id, "Send /setup in a private chat with me: it needs your phone number and OTP.")
        elif not hh:
            self._not_set_up(chat_id)
        elif command == "/leave":
            self._leave(chat_id, uid, hh, arg)
        elif command == "/members":
            self._members(chat_id, uid, hh)
        elif command == "/settings":
            self._settings(chat_id, hh)
        elif command == "/invite":
            if self._owner(chat_id, uid, hh):
                self._invite(chat_id, hh)
        elif command in SETTINGS:
            if self._owner(chat_id, uid, hh):
                self._change(chat_id, hh, command, arg)
        elif command == "/basket":
            if self.jobs.unfinished_tasks:
                self._say(chat_id, "An order is in progress; send /basket again once it's done.")
            else:
                self._show_basket(chat_id, hh)
        elif command == "/clear":
            self._queue(self._clear, chat_id, hh)
        elif command == "/usual":
            due = hh.ledger().due()
            self._say(chat_id, f"Due now: {_items(due)}\nSend that back to order it." if due else "Nothing is due.")
        elif voice := msg.get("voice") or msg.get("audio"):
            self._queue(self._voice, chat_id, uid, hh, voice["file_id"])
        elif text and not command:
            if self.jobs.unfinished_tasks:
                self._say(chat_id, "Another order is in progress; yours is next.")
            self._queue(self._handle, chat_id, uid, hh, text)

    def _on_button(self, cb: dict) -> None:
        uid, data = cb.get("from", {}).get("id"), cb["data"]
        kind, _, arg = data.partition(":")
        bind_contextvars(user_id=uid, chat_id=(cb.get("message") or {}).get("chat", {}).get("id"))
        if hh := self.accounts.household_of(uid):
            bind_contextvars(household_id=hh.id)
        log.info("button pressed", kind=kind if kind in ("allow", "remove", "now", "more", "join") else "pick")
        if kind == "now":
            return self._now(cb, hh, arg)
        self.tg.answer(cb["id"])
        msg = cb.get("message") or {}
        if kind == "join" and msg.get("chat", {}).get("id") == uid:  # in a group, only for the one it asked
            self.tg.remove_buttons(msg["chat"]["id"], msg["message_id"])
            self._join(msg["chat"]["id"], cb["from"], arg, sure=True)
        elif kind in ("allow", "remove"):
            done = self._allow(uid, int(arg)) if kind == "allow" else self._remove(uid, int(arg))
            if done and msg:
                self.tg.remove_buttons(msg["chat"]["id"], msg["message_id"])
        elif hh and kind == "more" and msg and arg.startswith(f"{hh.id}:"):
            self._queue(self._answer_others, msg["chat"]["id"], msg["message_id"], hh, arg.endswith(":y"))
        elif hh:
            self._pick(hh, data)

    def _now(self, cb: dict, hh: Household | None, household: str) -> None:
        """Places the basket. Only the latest button works, so an old one can't cut a newer wait short."""
        if not hh:
            return self.tg.answer(cb["id"], "You no longer order with that shop account.")
        if hh.id != household:
            return self.tg.answer(cb["id"], "That order is for another shop account.")
        msg = cb.get("message") or {}
        chat_id, message_id = msg.get("chat", {}).get("id"), msg.get("message_id")
        if not message_id or hh.ledger().offer() != (chat_id, message_id):
            self.tg.answer(cb["id"], "That button is out of date; /basket shows what's waiting.")
            if message_id:
                self.tg.remove_buttons(chat_id, message_id)
            return
        busy = self.jobs.unfinished_tasks > 0
        self.tg.answer(cb["id"], "Ordering right after what's in progress …" if busy else "Ordering …")
        self._queue(self._order, chat_id, hh, [], "now")

    def _voice(self, chat_id: int, uid: int, hh: Household, file_id: str) -> None:
        audio = self.tg.download(file_id)
        started = time.monotonic()
        try:
            text = self.transcribe(audio, hh.catalog().vocabulary())
        finally:
            audio.unlink(missing_ok=True)
        log.info("voice transcribed", seconds=round(time.monotonic() - started, 1), words=len(text.split()))
        self._say(chat_id, f'🎙️ Heard: "{text}"')
        if text:
            self._handle(chat_id, uid, hh, text)

    def _handle(self, chat_id: int, uid: int, hh: Household, text: str) -> None:
        r = self.read(text, hh.catalog().vocabulary(), [key for key, _ in hh.ledger().basket()])
        address = ()
        if r.address:
            if not (to := self._saved_address(chat_id, hh, r)):
                return
            address = (to.id, to.label)
        for name, value in r.settings:
            if self._owner(chat_id, uid, hh):
                self._change(chat_id, hh, f"/{name}", value)
        hh = self.accounts.get(hh.id) or hh
        if r.clear_basket:
            self._clear(chat_id, hh)
        checks = self._change_basket(chat_id, hh, r.change) if r.change else []
        if r.add or checks:
            return self._add(chat_id, hh, r.add, "now" if r.now else "hold", address, checks)
        if address and hh.ledger().basket():
            hh.ledger().set_address(*address)
        if r.now:
            return self._order(chat_id, hh, [], "now")
        if r.show_basket or r.change or r.address:
            return self._show_basket(chat_id, hh)
        if not (r.settings or r.clear_basket) and chat_id > 0:  # a group's chatter isn't for the bot
            self._say(chat_id, "I didn't catch any items.")

    def _saved_address(self, chat_id: int, hh: Household, r: Request) -> Address | None:
        if not (r.add or hh.ledger().basket()):
            self._say(chat_id, "The basket is empty; send the items with the address.")
            return None
        with Zepto(hh.state_file) as z:
            saved = z.addresses()
        found = [a for a in saved if a.label and f" {normalize(r.address)} " in f" {normalize(a.label)} "]
        log.info("address matched", matches=len(found), saved=len(saved))
        if len(found) != 1:
            labels = ", ".join(a.label for a in saved if a.label)
            self._say(chat_id, f"Which saved address: {labels}? Nothing changed; send it again naming one.")
            return None
        return found[0]

    def _change_basket(self, chat_id: int, hh: Household, changes: list[tuple[str, Qty]], checked=False) -> list:
        """Returns checks for the changes far over usual, which wait for an answer."""
        catalog, basket, checks = hh.catalog(), dict(hh.ledger().basket()), []
        for name, qty in changes:
            if (key := catalog.lookup(name)) not in basket:
                self._say(chat_id, f'"{name}" isn\'t in the basket.')
            elif not qty:
                basket.pop(key)
            elif not checked and (usual := orders.unusual(hh, key, qty)) is not None:
                checks.append(Check(key, qty, usual, True, []))
            else:
                basket[key] = qty
        hh.ledger().set_basket(list(basket.items()))
        if not basket:
            self._unhold(hh)
            self._withdraw(hh)
        return checks

    def _show_basket(self, chat_id: int, hh: Household) -> None:
        if text := _basket(hh):
            self._offer_now(chat_id, hh, text)
        else:
            self._say(chat_id, "The basket is empty.")

    def _add(
        self, chat_id: int, hh: Household, items: list[tuple[str, Qty]], when: str, address: tuple, checks=()
    ) -> None:
        p = plan(items, hh.catalog())
        found = []
        if p.to_pick:
            with Zepto(hh.state_file) as z:
                z.deliver_to(hh.address_id)
                found = [(name, qty, grouped(z.search(name), CARDS)) for name, qty in p.to_pick]
        ready, checks = [], list(checks)
        for key, qty in p.ready:
            if (usual := orders.unusual(hh, key, qty)) is not None:
                checks.append(Check(key, qty, usual, False, []))
            else:
                ready.append((key, qty))
        log.info("list planned", ready=len(ready), to_pick=len(p.to_pick), unusual=len(checks))
        for name, _, choices in found:
            if not choices:
                log.info("item not found")
                self._say(chat_id, f'Couldn\'t find "{name}" in the shop; leaving it out.')
        asks = [(name, qty, choices, []) for name, qty, choices in found if choices]
        if not (asks or checks):
            if not ready:
                return self._say(chat_id, "I didn't catch any items.")
            return self._order(chat_id, hh, ready, when, address)
        if ready:
            hh.ledger().set_basket(combine(hh.ledger().basket(), ready))
            if address:
                hh.ledger().set_address(*address)
            self._say(chat_id, f"Got: {_items(ready)}. " + ("Pick the rest:" if asks else "One question:"))
        self._start_picking(chat_id, hh, asks, checks, when, address)

    def _start_picking(
        self, chat_id: int, hh: Household, asks: list, checks: list[Check], when: str, address: tuple
    ) -> None:
        """Joins the chat's open picking, if any."""
        with self.lock:
            mine = [(k, v) for k, v in self.picking.items() if (v.chat_id, v.household) == (chat_id, hh.id)]
            stale = [self.picking.pop(k) for k, v in mine if time.time() - v.started >= PICK_WAIT]
            pid, picking = next(((k, v) for k, v in mine if k in self.picking), None) or (
                secrets.token_hex(4),
                Picking(chat_id, hh.id, {}),
            )
            new_asks = dict(enumerate(asks, picking.numbered))
            new_checks = dict(enumerate(checks, picking.numbered + len(asks)))
            picking.numbered += len(asks) + len(checks)
            picking.asks |= new_asks
            picking.checks |= new_checks
            picking.when = "now" if "now" in (picking.when, when) else when
            picking.address = address or picking.address
            picking.started = time.time()
            self.picking[pid] = picking
        for old in stale:
            hh.ledger().set_basket(combine(hh.ledger().basket(), old.picked))
            for ids in [*(a[3] for a in old.asks.values()), *(c.ids for c in old.checks.values())]:
                for mid in filter(None, ids):
                    self.tg.remove_buttons(chat_id, mid)
        for n, (name, qty, choices, ids) in new_asks.items():
            ids.append(self._say(chat_id, f'Which "{name}" ({shown(qty)})?', [("Skip this item", f"{pid}:{n}:x")]))
            ids += [self._offer(chat_id, choice, qty, f"{pid}:{n}:{i}") for i, choice in enumerate(choices)]
        for n, check in new_checks.items():
            self._send_check(chat_id, hh, pid, n, check)
        self._save()

    def _send_check(self, chat_id: int, hh: Household, pid: str, n: int, check: Check) -> None:
        """Far more than usual is more often misheard than meant."""
        data, asked, usual = f"{pid}:c{n}:", shown(check.asked), shown(check.usual)
        why = f"You usually get {usual}." if hh.ledger().bought(check.item) else "That's a lot for a first order."
        buttons = [(f"Yes, {asked}", data + "a"), (usual, data + "u"), ("Skip", data + "x")]
        check.ids.append(self._say(chat_id, f"❓ {check.item} {asked}? {why}", buttons))

    def _offer(self, chat_id: int, choice: Choice, qty: Qty, data: str) -> int | None:
        """For a count, each size is a button: the tap also picks the usual pack."""
        caption, entities = f"{choice.name}\n", []
        for n, p in enumerate(choice.sizes):
            caption += (" · " if n else "") + " ".join(filter(None, (p.size, p.price)))
            if p.price and p.mrp:
                caption += f" {p.mrp}"
                entities.append(strikethrough(caption, p.mrp))
        if isinstance(qty, int) and len(choice.sizes) > 1:
            buttons = [(f"✅ {p.size}", f"{data}.{n}") for n, p in enumerate(choice.sizes)]
        else:
            buttons = [("✅ Pick", data)]
        if image := next((p.image for p in choice.sizes if p.image), ""):
            try:
                return self.tg.send_photo(chat_id, image, caption, buttons, entities or None)
            except TelegramError as e:
                log.info("photo not shown", to_chat_id=chat_id, error=str(e))
        return self._say(chat_id, caption, buttons, entities=entities or None)

    def _ask_others(self, chat_id: int, hh: Household, questions: dict[str, str], when: str) -> None:
        """Only the latest question for an item can be answered."""
        for key, question in questions.items():
            if old := hh.ledger().asked().get(key):
                self.tg.remove_buttons(*old[:2])
            data = f"more:{hh.id}:"
            message_id = self._say(chat_id, f"❓ {question}", [("Yes", data + "y"), ("No", data + "n")])
            hh.ledger().set_asked(key, chat_id, message_id, when)

    def _answer_others(self, chat_id: int, message_id: int, hh: Household, yes: bool) -> None:
        asked = hh.ledger().asked()
        if not (key := next((k for k, (c, m, _) in asked.items() if (c, m) == (chat_id, message_id)), None)):
            return
        hh.ledger().set_others(key, yes)
        self.tg.remove_buttons(chat_id, message_id)
        self.tg.edit_text(chat_id, message_id, f"{key}: {'✔ adding it' if yes else '✖ only what there is'}")
        log.info("others answered", allowed=yes)
        if not hh.ledger().asked():
            self._order(chat_id, hh, [], asked[key][2])

    def _pick(self, hh: Household, data: str) -> None:
        """A tap on a product card, or on a check (n starts with c)."""
        try:
            pid, n, choice = data.rsplit(":", 2)
            checking, n = n.startswith("c"), int(n.removeprefix("c"))
        except ValueError:
            return
        check = None
        with self.lock:
            picking = self.picking.get(pid)
            open_ = {} if not picking or picking.household != hh.id else picking.checks if checking else picking.asks
            if n not in open_:
                log.info("pick ignored", reason="stale or already answered")
                return
            if checking:
                c = picking.checks.pop(n)
                name, ids, qty = c.item, c.ids, {"a": c.asked, "u": c.usual}.get(choice)
                result = "✖ skipped" if qty is None else f"✔ {shown(qty)}"
                (picking.changed if c.change else picking.picked).extend([] if qty is None else [(name, qty)])
            else:
                name, qty, choices, ids = picking.asks.pop(n)
                result = "✖ skipped"
                if choice != "x":
                    i, _, size = choice.partition(".")
                    picked = choices[int(i)]
                    pack = picked.sizes[int(size)] if size else orders.usual_pack(picked, qty)
                    key = hh.catalog().learn(name, picked, pack)
                    result = f"✔ {picked.name}" + (f" {pack.size}" if size else "")
                    if (usual := orders.unusual(hh, key, qty)) is not None:
                        check = picking.checks[n] = Check(key, qty, usual, False, [])
                    else:
                        picking.picked.append((key, qty))
            done = not (picking.asks or picking.checks)
            log.info(
                "item picked" if choice != "x" else "item skipped", remaining=len(picking.asks) + len(picking.checks)
            )
            if done:
                self.picking.pop(pid)
        self._save()
        for mid in filter(None, ids):
            self.tg.remove_buttons(picking.chat_id, mid)
        if ids[0]:
            self.tg.edit_text(picking.chat_id, ids[0], f'"{name}": {result}')
        if check:
            self._send_check(picking.chat_id, hh, pid, n, check)
            self._save()
        if done:
            self._queue(self._settled, picking.chat_id, hh, picking)

    def _settled(self, chat_id: int, hh: Household, picking: Picking) -> None:
        if picking.changed:
            self._change_basket(chat_id, hh, picking.changed, checked=True)
        if picking.picked or picking.when == "now" or (hh.ledger().basket() and not picking.changed):
            return self._order(chat_id, hh, picking.picked, picking.when, picking.address)
        if picking.changed:
            return self._show_basket(chat_id, hh)
        self._say(chat_id, "Nothing left to order.")

    def _clear(self, chat_id: int, hh: Household) -> None:
        self._unhold(hh)
        self._withdraw(hh)
        hh.ledger().set_basket([])
        self._say(chat_id, "Basket emptied.")

    def _hold(self, chat_id: int, hh: Household, seconds: float) -> None:
        hh.ledger().set_hold(chat_id, datetime.now() + timedelta(seconds=seconds))
        timer = threading.Timer(seconds, lambda: self._queue(self._release, chat_id, hh, timer))
        timer.daemon = True
        with self.lock:
            self.holds[hh.id] = timer
        timer.start()

    def _unhold(self, hh: Household, timer: threading.Timer | None = None) -> bool:
        """False if there is no hold, or it isn't `timer`'s."""
        with self.lock:
            current = self.holds.get(hh.id)
            if not current or (timer and current is not timer):
                return False
            self.holds.pop(hh.id)
        current.cancel()
        hh.ledger().set_hold()
        return True

    def _offer_now(self, chat_id: int, hh: Household, text: str) -> None:
        self._withdraw(hh)
        if message_id := self._say(chat_id, text, _order_now(hh)):
            hh.ledger().set_offer(chat_id, message_id)

    def _withdraw(self, hh: Household) -> None:
        if offer := hh.ledger().offer():
            hh.ledger().set_offer()
            self.tg.remove_buttons(*offer)

    def _chat_for(self, hh: Household, chat_id: int) -> int:
        """The owner instead of the private chat (positive id) of someone who left."""
        return hh.owner if chat_id > 0 and chat_id not in hh.members else chat_id

    def _picking(self, hh: Household) -> bool:
        with self.lock:
            return any(p.household == hh.id and time.time() - p.started < PICK_WAIT for p in self.picking.values())

    def _release(self, chat_id: int, hh: Household, timer: threading.Timer) -> None:
        if not self._unhold(hh, timer):
            return
        if self._picking(hh):
            return self._hold(chat_id, hh, PICK_HOLD)
        if (to := self._chat_for(hh, chat_id)) != chat_id:
            text = f"⏸ The waiting order wasn't placed, since {self.accounts.name(chat_id)} left. {KEPT}"
            return self._offer_now(to, hh, text)
        self._order(chat_id, hh, [], "due")

    def _order(self, chat_id: int, hh: Household, items: list[tuple[str, Qty]], when="hold", address=()) -> None:
        if address:
            hh.ledger().set_address(*address)
        self._unhold(hh)
        self._withdraw(hh)
        if items:
            self._say(chat_id, f"Adding {_items(items)} …")
        elif not hh.ledger().basket():
            return self._say(chat_id, "The basket is empty, so nothing was ordered.")
        else:
            self._say(chat_id, "Placing the order …")
        try:
            to = hh.ledger().address()
            out = orders.order(hh, items, when)
        except Exception as e:
            if hh.ledger().placing():  # stopped after the order call, so it may have gone through
                raise
            if isinstance(e, ShopError):
                log.warning("order stopped by the shop", error=e.logged)
                reason = f"Couldn't order: {e}."
            else:
                log.exception("order failed")
                reason = "Something went wrong, so nothing was ordered."
            return self._offer_now(chat_id, hh, f"❌ {reason}\n{KEPT}")
        if out.questions:
            log.info("order asking", questions=len(out.questions))
            return self._ask_others(chat_id, hh, out.questions, when)
        log.info(
            "order finished",
            result=out.result,
            items=len(out.lines),
            unavailable=out.unavailable,
            item_total=out.item_total,
            total=out.total,
            cod=out.cod,
            problems=out.problems,
        )
        missing = (f"Couldn't add {k} ({why}); it stays in the /basket" for k, why in out.unavailable.items())
        details = "\n".join([*out.lines, *missing])
        total = f"₹{out.total:g}" if out.total is not None else ""
        problems = "".join(f"\n- {p}" for p in out.problems)
        if out.placed:
            where = f" to {to[1]}" if to else ""
            text = f"✅ Ordered{where}, {total} {'cash on delivery' if out.cod else 'from the wallet'}:"
        elif out.unconfirmed:
            text = f"⚠️ I couldn't confirm the order. Check the shop's app before resending.{problems}"
        elif out.waiting and out.estimated:
            text = f"{_basket(hh)}\nSend more to add."
        elif out.waiting:
            text = f"🧺 The shop's total is ₹{out.item_total:g}, below the ₹{hh.min_order:g} minimum; send more to add."
        elif out.held:
            text = f"⏳ I'll order this in {HOLD // 60} minutes; send more to add, or tap Order now.\n\n{_basket(hh)}"
            self._offer_now(chat_id, hh, f"{text}\n{details}".strip())
            return self._hold(chat_id, hh, HOLD)
        elif out.asking_first:
            return self._offer_now(chat_id, hh, f"{_basket(hh)}\n\nOrdering is off: tap Order now to order this.")
        else:
            text = f"❌ Not ordered:{problems}"
            if hh.ledger().basket():
                details = f"{details}\n{KEPT_LIMIT if out.over_limit else KEPT}"
                return self._offer_now(chat_id, hh, f"{text}\n{details}".strip())
        self._say(chat_id, f"{text}\n{details}".strip())

    def _owner(self, chat_id: int, uid: int, hh: Household) -> bool:
        if hh.owner != uid:
            self._say(chat_id, f"Only {self.accounts.name(hh.owner)}, who connected this account, can do that.")
        return hh.owner == uid

    def _request_access(self, user: dict) -> None:
        uid, name = user.get("id"), user.get("first_name") or "Someone"
        handle = f" (@{user['username']})" if user.get("username") else ""
        log.info("access requested")
        self.alert(self.cfg.admin_id, f"{name}{handle} wants to use the bot.", [(f"✅ Let {name} in", f"allow:{uid}")])

    def _allow(self, uid: int, new: int) -> bool:
        if uid != self.cfg.admin_id:
            return False
        if not self.accounts.allowed(new):
            self.accounts.allow(new)
            log.info("access granted", granted_user_id=new)
            if (name := self.wants_setup.pop(new, None)) is not None:
                self._say(new, "🎉 You're in!")
                self._start_setup(new, name)
            else:
                self._not_set_up(new, "🎉 You're in! ")
        return True

    def _start_setup(self, uid: int, name: str) -> None:
        self.replies[uid] = queue.Queue()
        threading.Thread(target=self._setup, args=(uid, name), daemon=True).start()

    def _not_set_up(self, chat_id: int, prefix: str = "") -> None:
        # Group chats have negative ids; a reply keyboard there would pop up for everyone.
        self._say(chat_id, prefix + NOT_SET_UP, markup=CONNECT_BUTTON if chat_id > 0 else None)

    def _join(self, chat_id: int, user: dict, code: str, sure: bool = False) -> None:
        uid, name = user.get("id"), user.get("first_name", "")
        mine = self.accounts.household_of(uid)
        if mine and mine.owner == uid and len(mine.members) == 1:
            log.info("invite rejected", reason="owns an account")
            return self._say(chat_id, "You have your own shop account here. /leave it first, then open the link.")
        other = self.accounts.get(self.accounts.invited(code) or "")
        if not sure and mine and other and other.id != mine.id:
            log.info("invite needs confirming")
            current = "your own" if mine.owner == uid else f"{self.accounts.name(mine.owner)}'s"
            theirs = f"{self.accounts.name(other.owner)}'s"
            text = f"This link moves you from {current} shop account to {theirs}."
            if mine.owner == uid:
                text += (
                    "\n\nYou connected the account you're in, so switching logs it out of the shop: "
                    "the others can't order until one of them sends /setup."
                )
            return self._say(chat_id, text, [(f"Switch to {theirs}", f"join:{code}")])
        if not (hh := self.accounts.join(uid, name, code)):
            log.info("invite rejected", reason="expired or used")
            return self._say(chat_id, "That invite link has expired or was used. Ask for a new one.")
        log.info("household joined", household_id=hh.id)
        self.wants_setup.pop(uid, None)
        owner = self.accounts.name(hh.owner)
        welcome = f"🎉 Welcome! You now order with {owner}'s shop account.\n\n{TRY}\n\n/help shows the rest."
        self._say(chat_id, welcome, markup=NO_KEYBOARD)
        self._say(hh.owner, f"👋 {name or uid} joined your account and can order now.")

    def _invite(self, chat_id: int, hh: Household) -> None:
        self._say(
            chat_id,
            "Forward the next message to one person. The link works once, for 24 hours, and lets them order "
            "from your account and wallet.",
        )
        link = f"https://t.me/{self.username}?start={self.accounts.invite(hh.id)}"
        log.info("invite created")
        self._say(chat_id, f"🛒 Order our groceries with a voice note! Tap to join: {link}")

    def _members(self, chat_id: int, uid: int, hh: Household) -> None:
        names = ", ".join(self.accounts.name(m) + (" (owner)" if m == hh.owner else "") for m in hh.members)
        removable = [m for m in hh.members if m != uid] if uid == hh.owner else []
        buttons = [(f"Remove {self.accounts.name(m)}", f"remove:{m}") for m in removable]
        self._say(chat_id, f"Ordering with this account: {names}\n/invite adds someone.", buttons)

    def _remove(self, uid: int, member: int) -> bool:
        hh = self.accounts.household_of(uid)
        if not (hh and hh.owner == uid and member != uid):
            return False
        self._queue(self._drop, uid, hh, member)
        return True

    def _leave(self, chat_id: int, uid: int, hh: Household, arg: str) -> None:
        if len(hh.members) == 1 and arg.lower() != "yes":
            return self._say(
                chat_id,
                "You're the only one here, so this deletes the account's login, picks and history. "
                "Send /leave yes to go ahead.",
            )
        self._queue(self._drop, chat_id, hh, uid)

    def _drop(self, chat_id: int, hh: Household, member: int) -> None:
        """On the worker, so an account is never deleted mid-order."""
        if member not in hh.members:
            return
        self.accounts.leave(member, hh.id)
        log.info("household left", member_id=member, removed=chat_id != member)
        self._not_set_up(member, "You no longer order with that account. ")
        if chat_id != member:
            self._say(chat_id, f"Removed {self.accounts.name(member)}.")
        if (now := self.accounts.get(hh.id)) and now.owner != hh.owner:
            self._say(now.owner, "You now own this shop account. /setup to log it in with yours.")

    def _settings(self, chat_id: int, hh: Household) -> None:
        self._say(
            chat_id,
            f"Delivery: {hh.address} (/setup to change)\n"
            f"Daily limit: ₹{hh.daily_cap:g}, ₹{hh.ledger().spent_today():g} spent today (/limit 1500)\n"
            f"Smallest order: ₹{hh.min_order:g} (/minimum 99)\n"
            f"Placing orders: {'on' if hh.place_orders else 'off, I ask first'} (/ordering on or off)",
        )

    def _change(self, chat_id: int, hh: Household, command: str, arg: str) -> None:
        key, parse, example = SETTINGS[command]
        try:
            value = parse(arg.lstrip("₹").replace(",", "").lower())
        except (ValueError, KeyError):
            log.info("setting rejected", setting=key)
            return self._say(chat_id, f"For example: {command} {example}")
        self.accounts.update(hh.id, **{key: value})
        if key == "place_orders" and not value and self._unhold(hh):
            self._queue(self._order, chat_id, hh, [], "due")
        log.info("setting changed", setting=key)
        self._settings(chat_id, self.accounts.get(hh.id))

    def _ask(self, uid: int, question: str, wait: int = ANSWER_WAIT, markup=None, secret=False) -> str:
        """Their next message; secret ones (phone number, OTP) are deleted from the chat."""
        while not self.replies[uid].empty():
            self.replies[uid].get_nowait()
        if self.stopping.is_set():
            raise SetupStopped(RESTARTING)
        self._say(uid, question, markup=markup)
        try:
            msg = self.replies[uid].get(timeout=wait)
        except queue.Empty:
            log.info("setup answer timed out", seconds=wait)
            raise SetupStopped("I didn't hear back, so I stopped") from None
        if msg is None:
            raise SetupStopped(RESTARTING)
        if secret:
            self.tg.delete(uid, msg["message_id"])
        answer = ((msg.get("contact") or {}).get("phone_number") or msg.get("text") or "").strip()
        if answer.startswith("/"):
            raise SetupStopped("Stopped")
        return answer

    def _stopped(self, uid: int, reason: str) -> None:
        self._say(
            uid, f"❌ {reason[:1].upper()}{reason[1:]}. Nothing changed; send /setup to try again.", markup=NO_KEYBOARD
        )

    def _setup(self, uid: int, name: str) -> None:
        bind_contextvars(user_id=uid, chat_id=uid)
        log.info("setup started")
        session = self.cfg.data_dir / f".login-{uid}.json"
        try:
            phone = self._ask(
                uid,
                "Let's connect your shop account 🛒  Step 1 of 3\n\n"
                "Tap the button to share the phone number you log in to the shop with, or type it. "
                "I only use it to log in, and I delete it from this chat. (/cancel stops)",
                markup=SHARE_PHONE,
                secret=True,
            )
            if len(phone := re.sub(r"\D", "", phone)[-10:]) != 10:
                raise SetupStopped("That doesn't look like a 10-digit mobile number")
            self._say(uid, f"Thanks! Asking the shop for a login code for ••••••{phone[-4:]}…", markup=NO_KEYBOARD)
            login = Login()
            login.request_otp(phone)
            log.info("login code requested")
            otp = self._ask(
                uid, "📩 Step 2 of 3\n\nThe shop just texted you a login code. Send it here:", OTP_WAIT, secret=True
            )
            store.write(session, login.enter_otp(re.sub(r"\D", "", otp)))
            log.info("logged in")
            self._say(uid, "✅ Logged in!\n\n📍 Step 3 of 3: where should orders go?")
            with Zepto(session) as z:
                address = self._choose_address(uid, z)
            hh = self.accounts.connect(uid, name, address.id, str(address), session)
            log.info("household connected", household_id=hh.id)
            paid = (
                "paid from your wallet or on delivery"
                if hh.place_orders
                else "each one once you tap Order now (/ordering on)"
            )
            self._say(
                uid,
                f"🎉 You're all set{', ' + name if name else ''}!\n\n{TRY}\n\n"
                f"Orders go to {address.label}, {paid}, up to ₹{hh.daily_cap:g} a day "
                "(/settings). Want family to order too? Send /invite.",
                markup=NO_KEYBOARD,
            )
        except (SetupStopped, ShopError) as e:
            log.info("setup stopped", error=getattr(e, "logged", str(e)))
            self._stopped(uid, str(e))
        except Exception:
            log.exception("setup failed")
            self._stopped(uid, "the shop's sign-in wasn't what I expected")
        finally:
            self.replies.pop(uid, None)
            session.unlink(missing_ok=True)

    def _choose_address(self, uid: int, z: Zepto) -> Address:
        saved = z.addresses()
        if not saved:
            raise SetupStopped("Your shop account has no saved address; add one in the shop's app first")
        listed = "\n\n".join(f"{n}. {a.label} · {a.line}" for n, a in enumerate(saved, 1))
        keyboard = {"keyboard": [[{"text": f"{n}. {a}"[:48]}] for n, a in enumerate(saved, 1)], "resize_keyboard": True}
        for _ in range(3):
            answer = self._ask(
                uid, f"Your saved addresses:\n\n{listed}\n\nWhich one should orders go to?", markup=keyboard
            )
            if (m := re.match(r"\s*(\d+)", answer)) and 1 <= (n := int(m[1])) <= len(saved):
                log.info("address picked", option=n, saved=len(saved))
                return z.deliver_to(saved[n - 1].id)
            log.info("address answer not understood")
        raise SetupStopped("We couldn't settle on an address")
