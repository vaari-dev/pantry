"""Telegram front end.

One worker thread does all browser work, one job at a time (one Chrome). /setup asks its questions
on its own thread; the polling thread hands it the person's answers through `replies`.
"""

import itertools
import math
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import structlog
from structlog.contextvars import bind_contextvars, bound_contextvars, clear_contextvars, get_contextvars

from . import orders
from .accounts import Accounts, Household, address_parts
from .catalog import Product, plan
from .config import Config
from .shop import Browser, ShopError
from .speech import Transcriber
from .telegram import Telegram, TelegramError, strikethrough

log = structlog.get_logger(__name__)  # user_id, chat_id, household_id and job ride along as contextvars

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
NOT_SET_UP = "Connect your shop account to start ordering: it takes a minute. Or open an invite link from family."
DEBUG_KEEP = timedelta(days=7)
ALERT_REPEAT = timedelta(hours=6)
ANSWER_WAIT = 600
OTP_WAIT = 300  # Chrome waits on the login page meanwhile, holding up every order
HOLD = 180
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
YES = "✅ Yes, deliver here"
CONFIRM = {"keyboard": [[{"text": YES}], [{"text": "📍 Another saved address"}]], "resize_keyboard": True}
PICK_IDS = itertools.count()
HEARTBEAT = Path("/tmp/heartbeat")  # noqa: S108  read by the container healthcheck
BUSY = Path("/tmp/busy")  # noqa: S108  exists while jobs run; deploys wait for it


def _rupees(text: str) -> float:
    if not 0 < (value := float(text)) < math.inf:
        raise ValueError(text)
    return value


SETTINGS = {  # command: (setting, parse, example)
    "/limit": ("daily_cap", _rupees, "1500"),
    "/minimum": ("min_order", _rupees, "99"),
    "/ordering": ("place_orders", {"on": True, "off": False}.__getitem__, "on"),
}


def _items(items: list[tuple[str, int]]) -> str:
    return ", ".join(f"{k} ×{q}" for k, q in items)


def _search(b: Browser, name: str) -> tuple[str, list[Product]]:
    """Speech garbles words ("hass avocado" → "harsh avocado"); the item itself usually comes last."""
    words = name.split()
    for n in range(min(len(words), 3)):
        if options := b.search(query := " ".join(words[n:])):
            return query, options
    return name, []


def _order_now(hh: Household) -> list[tuple[str, str]]:
    return [("🛒 Order now", f"now:{hh.id}")]


class SetupStopped(Exception):
    pass


@dataclass
class Picking:
    chat_id: int
    household: str
    ready: list[tuple[str, int]]
    asks: dict[int, tuple[str, int, list[Product], list[int]]]  # n -> spoken, qty, options, message ids
    picked: list[tuple[str, int]] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tg = Telegram(cfg.telegram_token)
        self.accounts = Accounts(cfg.data_dir, cfg.admin_id)
        self.transcribe = Transcriber(cfg.whisper_model)
        self.jobs: queue.Queue = queue.Queue()
        self.picking: dict[str, Picking] = {}
        self.holds: dict[str, threading.Timer] = {}  # household id -> the timer that places its basket
        self.replies: dict[int, queue.Queue] = {}
        self.wants_setup: dict[int, str] = {}  # uid -> first name: asked for /setup before being let in
        self.lock = threading.Lock()
        self.alerted: dict[tuple[int, str], datetime] = {}
        self.username = ""

    def run(self) -> None:
        self.username = self.tg.setup(COMMANDS, DESCRIPTION, SHORT)
        log.info("bot started", bot_username=self.username, households=len(self.accounts.all()))
        self._resume()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._daily, daemon=True).start()
        offset = None
        while True:
            HEARTBEAT.touch()
            try:
                for update in self.tg.updates(offset):
                    offset = update["update_id"] + 1
                    clear_contextvars()
                    bind_contextvars(update_id=update["update_id"])
                    try:
                        if "message" in update:
                            self._on_message(update["message"])
                        elif "callback_query" in update:
                            self._on_button(update["callback_query"])
                    except Exception:
                        log.exception("update failed")
            except TelegramError as e:
                log.warning("telegram polling failed", error=str(e))
                time.sleep(5)

    def _resume(self) -> None:
        """What a restart left behind: orders cut off mid-placing, and held baskets."""
        for hh in self.accounts.all():
            for o in hh.ledger().interrupted():
                log.warning("interrupted order found", household_id=hh.id, total=o["total"], at=o["at"])
                self.alert(
                    hh.owner,
                    f"⚠️ An order (₹{o['total']:g}, {o['at']}) was interrupted while being placed. "
                    "Check the shop's app; it was NOT retried.",
                )
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
        """job(chat_id, ...) on the worker, logging with the context of whoever asked for it."""
        self.jobs.put((job, args, get_contextvars()))

    def _worker(self) -> None:
        """Runs jobs (function, (chat_id, ...), log context); failures are told to chat_id."""
        while True:
            job, args, context = self.jobs.get()
            name = job.__name__.lstrip("_")
            clear_contextvars()
            bind_contextvars(**{**context, "job": name})
            # settings or membership may have changed while queued
            args = tuple(self.accounts.get(a.id) if isinstance(a, Household) else a for a in args)
            if hh := next((a for a in args if isinstance(a, Household)), None):
                bind_contextvars(household_id=hh.id)
            started = time.monotonic()
            BUSY.touch()
            try:
                if None in args:
                    log.info("job dropped", reason="household gone")
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
                if self.jobs.empty():
                    BUSY.unlink(missing_ok=True)

    def _daily(self) -> None:
        while True:
            now = datetime.now()
            run_at = now.replace(hour=self.cfg.health_hour, minute=0, second=0, microsecond=0)
            time.sleep(((run_at if run_at > now else run_at + timedelta(days=1)) - now).total_seconds())
            self._queue(self._health, self.cfg.admin_id)
            cutoff = (datetime.now() - DEBUG_KEEP).timestamp()
            for f in self.cfg.data_dir.glob("**/debug/*-*.*"):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)

    def _health(self, admin: int) -> None:
        try:
            log.info("home exit ok", exit_ip=orders.exit_ip(self.cfg))
        except ShopError as e:
            log.error("home exit failed", error=e.logged)
            return self.alert(admin, f"⚠️ Daily check failed: {e}. Orders will fail until this is fixed.")
        for hh in self.accounts.all():
            with bound_contextvars(household_id=hh.id):
                try:
                    orders.check(self.cfg, hh)
                    log.info("health check ok")
                except ShopError as e:
                    log.warning("health check failed", error=e.logged)
                    self.alert(hh.owner, f"⚠️ Daily check of your shop account failed: {e}.")

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
            # Any first message, not just /start: people often tap /setup in the menu straight away.
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
            basket = hh.ledger().basket()
            self._say(chat_id, f"Waiting: {_items(basket)}" if basket else "The basket is empty.")
        elif command == "/clear":
            self._queue(self._clear, chat_id, hh)
        elif command == "/usual":
            due = hh.ledger().due()
            self._say(chat_id, f"Due now: {_items(due)}\nSend that back to order it." if due else "Nothing is due.")
        elif voice := msg.get("voice") or msg.get("audio"):
            self._queue(self._voice, chat_id, hh, voice["file_id"])
        elif text and not command:
            if not self.jobs.empty():
                self._say(chat_id, "Another order is in progress; yours is next.")
            self._queue(self._handle_list, chat_id, hh, text)

    def _on_button(self, cb: dict) -> None:
        uid, data = cb.get("from", {}).get("id"), cb["data"]
        kind, _, arg = data.partition(":")
        bind_contextvars(user_id=uid, chat_id=(cb.get("message") or {}).get("chat", {}).get("id"))
        if hh := self.accounts.household_of(uid):
            bind_contextvars(household_id=hh.id)
        log.info("button pressed", kind=kind if kind in ("allow", "remove", "now") else "pick")
        if kind == "now":
            return self._now(cb, hh, arg)
        self.tg.answer(cb["id"])
        if kind in ("allow", "remove"):
            done = self._allow(uid, int(arg)) if kind == "allow" else self._remove(uid, int(arg))
            if done and (msg := cb.get("message")):
                self.tg.remove_buttons(msg["chat"]["id"], msg["message_id"])
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
        busy = not self.jobs.empty() or BUSY.exists()
        self.tg.answer(cb["id"], "Ordering right after what's in progress …" if busy else "Ordering …")
        self._queue(self._order, chat_id, hh, [], False)

    def _voice(self, chat_id: int, hh: Household, file_id: str) -> None:
        audio = self.tg.download(file_id)
        started = time.monotonic()
        try:
            text = self.transcribe(audio, hh.catalog().vocabulary())
        finally:
            audio.unlink(missing_ok=True)
        log.info("voice transcribed", seconds=round(time.monotonic() - started, 1), words=len(text.split()))
        self._say(chat_id, f'🎙️ Heard: "{text}"')
        if text:
            self._handle_list(chat_id, hh, text)

    def _handle_list(self, chat_id: int, hh: Household, text: str) -> None:
        p = plan(text, hh.catalog())
        log.info("list planned", ready=len(p.ready), to_pick=len(p.to_pick), notes=len(p.notes))
        for note in p.notes:
            self._say(chat_id, note)
        found = []
        if p.to_pick:
            with orders.browser(self.cfg, hh, images=True) as b:
                found = [(name, qty, *_search(b, name)) for name, qty in p.to_pick]
        for name, _, query, options in found:
            if not options:
                log.info("item not found")
                self._say(chat_id, f'Couldn\'t find "{name}" in the shop; leaving it out.')
            elif query != name:
                log.info("item found by part of its name", words=len(name.split()), used=len(query.split()))
                self._say(chat_id, f'Nothing for "{name}", so here is "{query}".')
        asks = {n: (name, qty, options, []) for n, (name, qty, _, options) in enumerate(found) if options}
        if not asks:
            return self._order(chat_id, hh, p.ready) if p.ready else self._say(chat_id, "I didn't catch any items.")

        pid = str(next(PICK_IDS))
        with self.lock:
            self.picking = {k: v for k, v in self.picking.items() if v.chat_id != chat_id}  # newest list wins
            self.picking[pid] = Picking(chat_id, hh.id, p.ready, asks)
        if p.ready:
            self._say(chat_id, f"Got: {_items(p.ready)}. Pick the rest:")
        for n, (name, qty, options, ids) in list(asks.items()):  # taps answer (pop) items while these go out
            ids.append(self._say(chat_id, f'Which "{name}" (×{qty})?', [("Skip this item", f"{pid}:{n}:x")]))
            ids += [self._offer(chat_id, product, f"{pid}:{n}:{i}") for i, product in enumerate(options)]

    def _offer(self, chat_id: int, product: Product, data: str) -> int | None:
        caption = f"{product.name}\nSize: {product.size}" + (f"  ·  {product.price}" if product.price else "")
        entities = None
        if product.price and product.mrp:
            caption += f" {product.mrp}"
            entities = [strikethrough(caption, product.mrp)]
        buttons = [("✅ Pick", data)]
        if product.image:
            try:
                return self.tg.send_photo(chat_id, product.image, caption, buttons, entities)
            except TelegramError as e:
                log.info("photo not shown", to_chat_id=chat_id, error=str(e))
        return self._say(chat_id, caption, buttons, entities=entities)

    def _pick(self, hh: Household, data: str) -> None:
        try:
            pid, n, choice = data.rsplit(":", 2)
        except ValueError:
            return
        with self.lock:
            picking = self.picking.get(pid)
            if not picking or picking.household != hh.id or int(n) not in picking.asks:
                log.info("pick ignored", reason="stale or already answered")
                return
            name, qty, options, ids = picking.asks.pop(int(n))
            if choice == "x":
                result = "✖ skipped"
            else:
                product = options[int(choice)]
                picking.picked.append((hh.catalog().learn(name, product), qty))
                result = f"✔ {product.label()}"
            done = not picking.asks
            log.info("item picked" if choice != "x" else "item skipped", remaining=len(picking.asks))
            if done:
                self.picking.pop(pid)
        for mid in filter(None, ids):
            self.tg.remove_buttons(picking.chat_id, mid)
        if ids[0]:
            self.tg.edit_text(picking.chat_id, ids[0], f'"{name}": {result}')
        if done:
            if items := picking.ready + picking.picked:
                self._queue(self._order, picking.chat_id, hh, items)
            else:
                self._say(picking.chat_id, "Nothing left to order.")

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
            return any(p.household == hh.id and time.monotonic() - p.started < PICK_WAIT for p in self.picking.values())

    def _release(self, chat_id: int, hh: Household, timer: threading.Timer) -> None:
        if not self._unhold(hh, timer):
            return
        if self._picking(hh):
            return self._hold(chat_id, hh, PICK_HOLD)
        if (to := self._chat_for(hh, chat_id)) != chat_id:
            text = f"⏸ The waiting order wasn't placed, since {self.accounts.name(chat_id)} left. {KEPT}"
            return self._offer_now(to, hh, text)
        self._order(chat_id, hh, [], hold=False)

    def _order(self, chat_id: int, hh: Household, items: list[tuple[str, int]], hold: bool = True) -> None:
        self._unhold(hh)
        self._withdraw(hh)
        if items:
            self._say(chat_id, f"Adding {_items(items)} …")
        elif not hh.ledger().basket():
            return self._say(chat_id, "The basket is empty, so nothing was ordered.")
        else:
            self._say(chat_id, "Placing the order …")
        try:
            out = orders.order(self.cfg, hh, items, hold=hold)
        except Exception as e:
            if not hh.ledger().basket():  # emptied just before Place Order, so it may have gone through
                raise
            if isinstance(e, ShopError):
                log.warning("order stopped by the shop", error=e.logged)
                reason = f"Couldn't order: {e}."
            else:
                log.exception("order failed")
                reason = "Something went wrong, so nothing was ordered."
            return self._offer_now(chat_id, hh, f"❌ {reason}\n{KEPT}")
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
        details = "\n".join([*out.lines, *(f"Couldn't add {k} (out of stock or changed)" for k in out.unavailable)])
        total = f"₹{out.total:g}" if out.total is not None else ""
        how = ", cash on delivery" if out.cod else ""
        problems = "".join(f"\n- {p}" for p in out.problems)
        if out.placed:
            text = f"✅ Ordered, {total} {'cash on delivery' if out.cod else 'from the wallet'}:"
        elif out.unconfirmed:
            text = f"⚠️ I couldn't confirm the order. Check the shop's app before resending.{problems}"
        elif out.waiting:
            text = f"🧺 In the basket, ₹{out.item_total:g} so far. I'll order at ₹{hh.min_order:g}; send more to add."
        elif out.held:
            text = f"⏳ Cart ready ({total}{how}). I'll order it in {HOLD // 60} minutes; send more to add, or:"
            self._offer_now(chat_id, hh, f"{text}\n{details}".strip())
            return self._hold(chat_id, hh, HOLD)
        elif not out.problems:
            text = f"🛒 Cart ready ({total}{how}); placing orders is off (/settings)."
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
        """Setup runs in the private chat, whose id is the user's id."""
        self.replies[uid] = queue.Queue()
        threading.Thread(target=self._setup, args=(uid, name), daemon=True).start()

    def _not_set_up(self, chat_id: int, prefix: str = "") -> None:
        # Group chats have negative ids; a reply keyboard there would pop up for everyone.
        self._say(chat_id, prefix + NOT_SET_UP, markup=CONNECT_BUTTON if chat_id > 0 else None)

    def _join(self, chat_id: int, user: dict, code: str) -> None:
        uid, name = user.get("id"), user.get("first_name", "")
        mine = self.accounts.household_of(uid)
        if mine and mine.owner == uid and len(mine.members) == 1:
            log.info("invite rejected", reason="owns an account")
            return self._say(chat_id, "You have your own shop account here. /leave it first, then open the link.")
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
            f"Delivery: {hh.address_label}, must include {hh.address_match!r} (/setup to change)\n"
            f"Daily limit: ₹{hh.daily_cap:g}, ₹{hh.ledger().spent_today():g} spent today (/limit 1500)\n"
            f"Smallest order: ₹{hh.min_order:g} (/minimum 99)\n"
            f"Placing orders: {'on' if hh.place_orders else 'off, cart only'} (/ordering on or off)",
        )

    def _change(self, chat_id: int, hh: Household, command: str, arg: str) -> None:
        key, parse, example = SETTINGS[command]
        try:
            value = parse(arg.lstrip("₹").replace(",", "").lower())
        except (ValueError, KeyError):
            log.info("setting rejected", setting=key)
            return self._say(chat_id, f"For example: {command} {example}")
        self.accounts.update(hh.id, **{key: value})
        log.info("setting changed", setting=key)
        self._settings(chat_id, self.accounts.get(hh.id))

    def _ask(self, uid: int, question: str, wait: int = ANSWER_WAIT, markup=None, secret=False) -> str:
        """Their next message; secret ones (phone number, OTP) are deleted from the chat."""
        while not self.replies[uid].empty():
            self.replies[uid].get_nowait()
        self._say(uid, question, markup=markup)
        try:
            msg = self.replies[uid].get(timeout=wait)
        except queue.Empty:
            log.info("setup answer timed out", seconds=wait)
            raise SetupStopped("I didn't hear back, so I stopped") from None
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
        bind_contextvars(user_id=uid, chat_id=uid)  # a fresh thread; maybe started by the admin's tap
        log.info("setup started")
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
        except SetupStopped as e:
            return self._stopped(uid, str(e))
        finally:
            self.replies.pop(uid, None)
        busy = "" if self.jobs.empty() else " right after the order in progress"
        self._say(uid, f"Thanks! Asking the shop for a login code for ••••••{phone[-4:]}{busy}…", markup=NO_KEYBOARD)
        self._queue(self._login, uid, name, phone)

    def _login(self, uid: int, name: str, phone: str) -> None:
        session = self.cfg.data_dir / f".login-{uid}.json"
        self.replies[uid] = queue.Queue()
        try:
            with orders.login_browser(self.cfg, session) as b:
                b.request_otp(phone)
                log.info("login code requested")
                otp = self._ask(
                    uid, "📩 Step 2 of 3\n\nThe shop just texted you a login code. Send it here:", OTP_WAIT, secret=True
                )
                b.enter_otp(re.sub(r"\D", "", otp))
                log.info("logged in")
                self._say(uid, "✅ Logged in!\n\n📍 Step 3 of 3: where should orders go?")
                label, match = self._choose_address(uid, b)
            hh = self.accounts.connect(uid, name, label, match, session)
            log.info("household connected", household_id=hh.id)
            paid = "paid from your wallet or on delivery" if hh.place_orders else "carts filled for you (/ordering on)"
            self._say(
                uid,
                f"🎉 You're all set{', ' + name if name else ''}!\n\n{TRY}\n\n"
                f"Orders go to {hh.address_label}, {paid}, up to ₹{hh.daily_cap:g} a day "
                "(/settings). Want family to order too? Send /invite.",
                markup=NO_KEYBOARD,
            )
        except (SetupStopped, ShopError) as e:
            log.info("setup stopped", error=getattr(e, "logged", str(e)))
            self._stopped(uid, str(e))
        except Exception:
            log.exception("setup failed")
            self._stopped(uid, "the shop's login page wasn't what I expected")
        finally:
            self.replies.pop(uid, None)
            session.unlink(missing_ok=True)

    def _choose_address(self, uid: int, b) -> tuple[str, str]:
        saved = b.saved_addresses()
        for _ in range(3):
            if address := (self._pick_saved(uid, b, saved) if saved else self._name_address(uid, b)):
                return address_parts(address)
        raise SetupStopped("We couldn't settle on an address")

    def _pick_saved(self, uid: int, b, saved: list[str]) -> str:
        listed = "\n\n".join(f"{n}. {' · '.join(a.splitlines())}" for n, a in enumerate(saved, 1))
        buttons = [f"{n}. {' · '.join(a.splitlines()[:2])}"[:48] for n, a in enumerate(saved, 1)]
        keyboard = {"keyboard": [[{"text": t}] for t in buttons], "resize_keyboard": True}
        answer = self._ask(uid, f"Your saved addresses:\n\n{listed}\n\nWhich one should orders go to?", markup=keyboard)
        if (m := re.match(r"\s*(\d+)", answer)) and 1 <= (n := int(m[1])) <= len(saved):
            log.info("address picked", option=n, saved=len(saved))
            return b.pick_address(n - 1)
        log.info("address answer not understood")
        return ""

    def _name_address(self, uid: int, b) -> str:
        """When the shop's address list can't be read: confirm the current one or type a name."""
        if address := b.delivery_address():
            answer = self._ask(uid, f"Your shop account delivers to:\n\n{address}\n\nOrder there?", markup=CONFIRM)
            if answer == YES:
                return address
        name = self._ask(
            uid,
            "Which of your saved addresses? Send its name as the shop's app shows it, e.g. Home",
            markup=NO_KEYBOARD,
        )
        try:
            b.choose_address(name)
        except ShopError as e:
            log.info("address not chosen", error=e.logged)
            self._say(uid, f"Hmm, {e}.")
        return ""
