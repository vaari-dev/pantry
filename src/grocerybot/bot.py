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

from . import orders
from .accounts import Accounts, Household, address_parts
from .catalog import Product, plan
from .config import Config
from .shop import ShopError
from .speech import Transcriber
from .telegram import Telegram, TelegramError

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
SHORT = "Groceries from a voice note."
DESCRIPTION = (
    f"🛒 {SHORT}\n\n"
    'Say "bread aur do anda" and I\'ll order it on your shop account, paid from its wallet. '
    "No app to open, no browsing, no upsells."
)
HELP = (
    "🛒 Just tell me what you need, by voice or text:\n"
    '• "bread aur do anda"\n'
    '• "2 milk, bananas"\n\n'
    "Something new? I'll show you options with photos: tap ✅ Pick and I'll remember it next time.\n"
    "Small lists wait in the /basket until they reach the smallest order (/settings)."
)
TRY = 'Try it now 👉 send a voice note saying "bread aur do anda", or type "2 milk, bananas".'
CONNECT = "🛒 Connect my shop account"
CONNECT_BUTTON = {"keyboard": [[{"text": CONNECT}]], "resize_keyboard": True}
NOT_SET_UP = "Connect your shop account to start ordering: it takes a minute. Or open an invite link from family."
DEBUG_KEEP = timedelta(days=7)
ALERT_REPEAT = timedelta(hours=6)
ANSWER_WAIT = 600
OTP_WAIT = 300  # Chrome waits on the login page meanwhile, holding up every order
SHARE_PHONE = {"keyboard": [[{"text": "📱 Share my phone number", "request_contact": True}]], "resize_keyboard": True}
NO_KEYBOARD = {"remove_keyboard": True}
RETRY = "Nothing changed; send /setup to try again."
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


class SetupStopped(Exception):
    pass


@dataclass
class Picking:
    chat_id: int
    household: str
    ready: list[tuple[str, int]]
    asks: dict[int, tuple[str, int, list[Product], list[int]]]  # n -> spoken, qty, options, message ids
    picked: list[tuple[str, int]] = field(default_factory=list)


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tg = Telegram(cfg.telegram_token)
        self.accounts = Accounts(cfg.data_dir, cfg.admin_id)
        self.transcribe = Transcriber(cfg.whisper_model)
        self.jobs: queue.Queue = queue.Queue()
        self.picking: dict[str, Picking] = {}
        self.replies: dict[int, queue.Queue] = {}
        self.lock = threading.Lock()
        self.alerted: dict[tuple[int, str], datetime] = {}
        self.username = ""

    def run(self) -> None:
        self.username = self.tg.setup(COMMANDS, DESCRIPTION, SHORT)
        log.info("bot started", bot=self.username, households=len(self.accounts.all()))
        for hh in self.accounts.all():
            for o in hh.ledger().interrupted():
                self.alert(
                    hh.owner,
                    f"⚠️ An order (₹{o['total']:g}, {o['at']}) was interrupted while being placed. "
                    "Check the shop's app; it was NOT retried.",
                )
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._daily, daemon=True).start()
        offset = None
        while True:
            HEARTBEAT.touch()
            try:
                for update in self.tg.updates(offset):
                    offset = update["update_id"] + 1
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

    def _worker(self) -> None:
        """Runs jobs (function, (chat_id, ...)); failures are told to chat_id."""
        while True:
            job, args = self.jobs.get()
            # settings or membership may have changed while queued
            args = tuple(self.accounts.get(a.id) if isinstance(a, Household) else a for a in args)
            if None in args:
                continue
            name, started = job.__name__.lstrip("_"), time.monotonic()
            BUSY.touch()
            try:
                job(*args)
                log.info("job done", job=name, seconds=round(time.monotonic() - started, 1))
            except ShopError as e:
                log.warning("job stopped by the shop", job=name, reason=str(e))
                self._say(args[0], f"❌ Couldn't order: {e}.")
            except Exception:
                log.exception("job failed", job=name)
                self._say(args[0], "Something went wrong. Nothing was ordered unless you got a ✅.")
            finally:
                if self.jobs.empty():
                    BUSY.unlink(missing_ok=True)

    def _daily(self) -> None:
        while True:
            now = datetime.now()
            run_at = now.replace(hour=self.cfg.health_hour, minute=0, second=0, microsecond=0)
            time.sleep(((run_at if run_at > now else run_at + timedelta(days=1)) - now).total_seconds())
            self.jobs.put((self._health, (self.cfg.admin_id,)))
            cutoff = (datetime.now() - DEBUG_KEEP).timestamp()
            for f in self.cfg.data_dir.glob("**/debug/*-*.*"):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)

    def _health(self, admin: int) -> None:
        try:
            log.info("home exit ok", exit_ip=orders.exit_ip(self.cfg))
        except ShopError as e:
            log.error("health check failed", reason=str(e))
            return self.alert(admin, f"⚠️ Daily check failed: {e}. Orders will fail until this is fixed.")
        for hh in self.accounts.all():
            try:
                orders.check(self.cfg, hh)
                log.info("health check ok", household=hh.id)
            except ShopError as e:
                log.warning("health check failed", household=hh.id, reason=str(e))
                self.alert(hh.owner, f"⚠️ Daily check of your shop account failed: {e}.")

    def _say(self, chat_id: int, text: str, buttons=None, markup=None) -> int | None:
        try:
            return self.tg.send(chat_id, text, buttons, markup)
        except TelegramError as e:
            log.warning("telegram send failed", error=str(e))
            return None

    def alert(self, uid: int, text: str, buttons=None) -> None:
        last = self.alerted.get((uid, text))
        if last and datetime.now() - last < ALERT_REPEAT:
            return
        self.alerted[(uid, text)] = datetime.now()
        self._say(uid, text, buttons)

    # incoming

    def _on_message(self, msg: dict) -> None:
        chat_id, user = msg["chat"]["id"], msg.get("from", {})
        uid, text = user.get("id"), (msg.get("text") or "").strip()
        private = msg["chat"].get("type") == "private"
        if uid in self.replies and private:
            return self.replies[uid].put(msg)
        command, _, arg = ("/setup", "", "") if text == CONNECT else text.partition(" ")
        command = command.split("@")[0] if command.startswith("/") else ""
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
                self._request_access(user)
            return
        hh = self.accounts.household_of(uid)
        if command in ("/start", "/help"):
            if hh:
                self._say(chat_id, HELP)
            else:
                self._not_set_up(chat_id, f"{HELP}\n\n")
        elif command == "/setup":
            if private:
                self.replies[uid] = queue.Queue()
                args = (chat_id, uid, user.get("first_name", ""))
                threading.Thread(target=self._setup, args=args, daemon=True).start()
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
            self.jobs.put((self._clear, (chat_id, hh)))
        elif command == "/usual":
            due = hh.ledger().due()
            self._say(chat_id, f"Due now: {_items(due)}\nSend that back to order it." if due else "Nothing is due.")
        elif voice := msg.get("voice") or msg.get("audio"):
            self.jobs.put((self._voice, (chat_id, hh, voice["file_id"])))
        elif text and not command:
            if not self.jobs.empty():
                self._say(chat_id, "Another order is in progress; yours is next.")
            self.jobs.put((self._handle_list, (chat_id, hh, text)))

    def _on_button(self, cb: dict) -> None:
        self.tg.answer(cb["id"])
        uid, data = cb.get("from", {}).get("id"), cb["data"]
        kind, _, arg = data.partition(":")
        if kind in ("allow", "remove"):
            done = self._allow(uid, int(arg)) if kind == "allow" else self._remove(uid, int(arg))
            if done and (msg := cb.get("message")):
                self.tg.remove_buttons(msg["chat"]["id"], msg["message_id"])
        elif hh := self.accounts.household_of(uid):
            self._pick(hh, data)

    # lists and picks

    def _voice(self, chat_id: int, hh: Household, file_id: str) -> None:
        audio = self.tg.download(file_id)
        try:
            text = self.transcribe(audio, hh.catalog().vocabulary())
        finally:
            audio.unlink(missing_ok=True)
        log.info("voice transcribed", words=len(text.split()))
        self._say(chat_id, f'🎙️ Heard: "{text}"')
        if text:
            self._handle_list(chat_id, hh, text)

    def _handle_list(self, chat_id: int, hh: Household, text: str) -> None:
        p = plan(text, hh.catalog())
        for note in p.notes:
            self._say(chat_id, note)
        found = []
        if p.to_pick:
            with orders.browser(self.cfg, hh, images=True) as b:
                found = [(name, qty, b.search(name)) for name, qty in p.to_pick]
        for name, _, options in found:
            if not options:
                self._say(chat_id, f'Couldn\'t find "{name}" in the shop; leaving it out.')
        asks = {n: (name, qty, options, []) for n, (name, qty, options) in enumerate(found) if options}
        if not asks:
            return self._order(chat_id, hh, p.ready) if p.ready else self._say(chat_id, "I didn't catch any items.")

        pid = str(next(PICK_IDS))
        with self.lock:
            self.picking = {k: v for k, v in self.picking.items() if v.chat_id != chat_id}  # newest list wins
            self.picking[pid] = Picking(chat_id, hh.id, p.ready, asks)
        if p.ready:
            self._say(chat_id, f"Got: {_items(p.ready)}. Pick the rest:")
        for n, (name, qty, options, ids) in asks.items():
            ids.append(self._say(chat_id, f'Which "{name}" (×{qty})?', [("Skip this item", f"{pid}:{n}:x")]))
            ids += [self._offer(chat_id, product, f"{pid}:{n}:{i}") for i, product in enumerate(options)]

    def _offer(self, chat_id: int, product: Product, data: str) -> int | None:
        caption = f"{product.name}\nSize: {product.size}" + (f"  ·  {product.price}" if product.price else "")
        buttons = [("✅ Pick", data)]
        if product.image:
            try:
                return self.tg.send_photo(chat_id, product.image, caption, buttons)
            except TelegramError as e:
                log.info("photo not shown", error=str(e))
        return self._say(chat_id, caption, buttons)

    def _pick(self, hh: Household, data: str) -> None:
        try:
            pid, n, choice = data.rsplit(":", 2)
        except ValueError:
            return
        with self.lock:
            picking = self.picking.get(pid)
            if not picking or picking.household != hh.id or int(n) not in picking.asks:
                return
            name, qty, options, ids = picking.asks.pop(int(n))
            if choice == "x":
                result = "✖ skipped"
            else:
                product = options[int(choice)]
                picking.picked.append((hh.catalog().learn(name, product), qty))
                result = f"✔ {product.label()}"
            done = not picking.asks
            if done:
                self.picking.pop(pid)
        for mid in filter(None, ids):
            self.tg.remove_buttons(picking.chat_id, mid)
        if ids[0]:
            self.tg.edit_text(picking.chat_id, ids[0], f'"{name}": {result}')
        if done:
            if items := picking.ready + picking.picked:
                self.jobs.put((self._order, (picking.chat_id, hh, items)))
            else:
                self._say(picking.chat_id, "Nothing left to order.")

    # ordering

    def _clear(self, chat_id: int, hh: Household) -> None:
        hh.ledger().set_basket([])
        self._say(chat_id, "Basket emptied.")

    def _order(self, chat_id: int, hh: Household, items: list[tuple[str, int]]) -> None:
        self._say(chat_id, f"Adding {_items(items)} …")
        out = orders.order(self.cfg, hh, items)
        log.info(
            "order",
            household=hh.id,
            result=out.result,
            items=len(out.lines),
            unavailable=out.unavailable,
            item_total=out.item_total,
            wallet=out.total,
            problems=out.problems,
        )
        details = "\n".join([*out.lines, *(f"Couldn't add {k} (out of stock or changed)" for k in out.unavailable)])
        total = f"₹{out.total:g}" if out.total is not None else ""
        if out.placed:
            text = f"✅ Ordered, {total} from the wallet:"
        elif out.unconfirmed:
            text = "⚠️ I tapped Place Order but couldn't confirm it. Check the shop's app before resending."
        elif out.waiting:
            text = f"🧺 In the basket, ₹{out.item_total:g} so far. I'll order at ₹{hh.min_order:g}; send more to add."
        elif not out.problems:
            text = f"🛒 Cart ready ({total}); placing orders is off (/settings)."
        else:
            text = "❌ Not ordered:\n" + "\n".join(f"- {p}" for p in out.problems)
        self._say(chat_id, f"{text}\n{details}".strip())

    # accounts

    def _owner(self, chat_id: int, uid: int, hh: Household) -> bool:
        if hh.owner != uid:
            self._say(chat_id, f"Only {self.accounts.name(hh.owner)}, who connected this account, can do that.")
        return hh.owner == uid

    def _request_access(self, user: dict) -> None:
        uid, name = user.get("id"), user.get("first_name") or "Someone"
        handle = f" (@{user['username']})" if user.get("username") else ""
        log.info("access requested", user=uid)
        self.alert(self.cfg.admin_id, f"{name}{handle} wants to use the bot.", [(f"✅ Let {name} in", f"allow:{uid}")])

    def _allow(self, uid: int, new: int) -> bool:
        if uid != self.cfg.admin_id:
            return False
        if not self.accounts.allowed(new):
            self.accounts.allow(new)
            log.info("access granted", user=new)
            self._not_set_up(new, "🎉 You're in! ")
        return True

    def _not_set_up(self, chat_id: int, prefix: str = "") -> None:
        # Group chats have negative ids; a reply keyboard there would pop up for everyone.
        self._say(chat_id, prefix + NOT_SET_UP, markup=CONNECT_BUTTON if chat_id > 0 else None)

    def _join(self, chat_id: int, user: dict, code: str) -> None:
        uid, name = user.get("id"), user.get("first_name", "")
        mine = self.accounts.household_of(uid)
        if mine and mine.owner == uid and len(mine.members) == 1:
            return self._say(chat_id, "You have your own shop account here. /leave it first, then open the link.")
        if not (hh := self.accounts.join(uid, name, code)):
            return self._say(chat_id, "That invite link has expired or was used. Ask for a new one.")
        log.info("household joined", household=hh.id, user=uid)
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
        self.jobs.put((self._drop, (uid, hh, member)))
        return True

    def _leave(self, chat_id: int, uid: int, hh: Household, arg: str) -> None:
        if len(hh.members) == 1 and arg.lower() != "yes":
            return self._say(
                chat_id,
                "You're the only one here, so this deletes the account's login, picks and history. "
                "Send /leave yes to go ahead.",
            )
        self.jobs.put((self._drop, (chat_id, hh, uid)))

    def _drop(self, chat_id: int, hh: Household, member: int) -> None:
        """On the worker, so an account is never deleted mid-order."""
        if member not in hh.members:
            return
        self.accounts.leave(member, hh.id)
        log.info("household left", household=hh.id, user=member)
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
            return self._say(chat_id, f"For example: {command} {example}")
        self.accounts.update(hh.id, **{key: value})
        log.info("setting changed", household=hh.id, setting=key)
        self._settings(chat_id, self.accounts.get(hh.id))

    # setup

    def _ask(self, chat_id: int, uid: int, question: str, wait: int = ANSWER_WAIT, markup=None, secret=False) -> str:
        """Their next message; secret ones (phone number, OTP) are deleted from the chat."""
        while not self.replies[uid].empty():
            self.replies[uid].get_nowait()
        self._say(chat_id, question, markup=markup)
        try:
            msg = self.replies[uid].get(timeout=wait)
        except queue.Empty:
            raise SetupStopped("I didn't hear back, so I stopped") from None
        if secret:
            self.tg.delete(chat_id, msg["message_id"])
        answer = ((msg.get("contact") or {}).get("phone_number") or msg.get("text") or "").strip()
        if answer.startswith("/"):
            raise SetupStopped("Stopped")
        return answer

    def _setup(self, chat_id: int, uid: int, name: str) -> None:
        try:
            phone = self._ask(
                chat_id,
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
            return self._say(chat_id, f"{e}. {RETRY}", markup=NO_KEYBOARD)
        finally:
            self.replies.pop(uid, None)
        busy = "" if self.jobs.empty() else " right after the order in progress"
        self._say(
            chat_id, f"Thanks! Asking the shop for a login code for ••••••{phone[-4:]}{busy}…", markup=NO_KEYBOARD
        )
        self.jobs.put((self._login, (chat_id, uid, name, phone)))

    def _login(self, chat_id: int, uid: int, name: str, phone: str) -> None:
        session = self.cfg.data_dir / f".login-{uid}.json"
        self.replies[uid] = queue.Queue()
        try:
            with orders.login_browser(self.cfg, session) as b:
                b.request_otp(phone)
                otp = self._ask(
                    chat_id,
                    uid,
                    "📩 Step 2 of 3\n\nThe shop just texted you a login code. Send it here:",
                    OTP_WAIT,
                    secret=True,
                )
                b.enter_otp(re.sub(r"\D", "", otp))
                self._say(chat_id, "✅ Logged in!\n\n📍 Step 3 of 3: where should orders go?")
                label, match = self._choose_address(chat_id, uid, b)
            hh = self.accounts.connect(uid, name, label, match, session)
            log.info("household connected", household=hh.id, user=uid)
            paid = "paid from your shop wallet" if hh.place_orders else "carts filled for you to pay (/ordering on)"
            self._say(
                chat_id,
                f"🎉 You're all set{', ' + name if name else ''}!\n\n{TRY}\n\n"
                f"Orders go to {hh.address_label}, {paid}, up to ₹{hh.daily_cap:g} a day "
                "(/settings). Want family to order too? Send /invite.",
                markup=NO_KEYBOARD,
            )
        except (SetupStopped, ShopError) as e:
            log.info("setup stopped", user=uid, reason=str(e))
            reason = str(e)
            self._say(chat_id, f"❌ {reason[:1].upper()}{reason[1:]}. {RETRY}", markup=NO_KEYBOARD)
        except Exception:
            log.exception("setup failed", user=uid)
            self._say(chat_id, f"❌ The shop's login page wasn't what I expected. {RETRY}", markup=NO_KEYBOARD)
        finally:
            self.replies.pop(uid, None)
            session.unlink(missing_ok=True)

    def _choose_address(self, chat_id: int, uid: int, b) -> tuple[str, str]:
        if saved := b.saved_addresses():
            return self._pick_saved(chat_id, uid, b, saved)
        return self._name_address(chat_id, uid, b)

    def _pick_saved(self, chat_id: int, uid: int, b, saved: list[str]) -> tuple[str, str]:
        listed = "\n\n".join(f"{n}. {' · '.join(a.splitlines())}" for n, a in enumerate(saved, 1))
        buttons = [f"{n}. {' · '.join(a.splitlines()[:2])}"[:48] for n, a in enumerate(saved, 1)]
        keyboard = {"keyboard": [[{"text": t}] for t in buttons], "resize_keyboard": True}
        question = f"Your saved addresses:\n\n{listed}\n\nWhich one should orders go to?"
        for _ in range(3):
            answer = self._ask(chat_id, uid, question, markup=keyboard)
            if (m := re.match(r"\s*(\d+)", answer)) and 1 <= (n := int(m[1])) <= len(saved):
                return address_parts(b.pick_address(n - 1))
            question = "Tap one of the addresses below."
        raise SetupStopped("We couldn't settle on an address")

    def _name_address(self, chat_id: int, uid: int, b) -> tuple[str, str]:
        """When the shop's address list can't be read: confirm the current one or type a name."""
        for _ in range(3):
            if address := b.delivery_address():
                answer = self._ask(
                    chat_id, uid, f"Your shop account delivers to:\n\n{address}\n\nOrder there?", markup=CONFIRM
                )
                if answer == YES:
                    return address_parts(address)
            name = self._ask(
                chat_id,
                uid,
                "Which of your saved addresses? Send its name as the shop's app shows it, e.g. Home",
                markup=NO_KEYBOARD,
            )
            try:
                b.choose_address(name)
            except ShopError as e:
                self._say(chat_id, f"Hmm, {e}.")
        raise SetupStopped("We couldn't settle on an address")
