"""Telegram front end: voice/text lists in, grocery orders out, for several households.

One worker thread does all browser work in order (one Chrome, one cart at a time); the polling
thread only parses messages and button presses, so the bot stays responsive during an order.
/setup asks its questions on a thread of its own, then logs in on the worker, keeping Chrome open
on the shop's login (at most OTP_WAIT) while the person sends their OTP. The polling thread hands
their answers over through `replies`.
"""

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from . import orders
from .accounts import Accounts, Household
from .catalog import Product, plan
from .config import Config
from .shop import Rules, ShopError
from .speech import Transcriber
from .telegram import Telegram, TelegramError

log = structlog.get_logger(__name__)

DESCRIPTION = 'Family grocery helper. Send a voice note or text like "bread aur do anda" and I\'ll order it.'
COMMANDS = {
    "basket": "What is waiting to be ordered",
    "clear": "Empty the waiting basket",
    "usual": "What we usually need around now",
    "setup": "Connect your shop account (or log it in again)",
    "invite": "Let a family member order with your account",
    "members": "Who orders with this account",
    "limit": "Show or change the daily spending limit",
    "leave": "Stop ordering with this account",
    "help": "How to order",
    "id": "Show your Telegram id",
}
HELP = (
    'Send what we need, by voice or text, e.g. "bread aur do anda" or "2 milk, bananas".\n'
    "New things: I'll show a few options to pick from, and remember your pick.\n"
    "Small lists wait in the basket and are ordered once they add up to the shop's minimum."
)
NOT_SET_UP = (
    "You're not connected to a shop account yet. Send /setup to connect yours, "
    "or ask someone who already orders here for an invite code and send /join CODE."
)
DEBUG_KEEP = timedelta(days=7)
ALERT_REPEAT = timedelta(hours=6)
ANSWER_WAIT = 600  # seconds to answer a /setup question
OTP_WAIT = 300  # the shop's codes expire after a few minutes
SHARE_PHONE = {
    "keyboard": [[{"text": "📱 Share my phone number", "request_contact": True}]],
    "one_time_keyboard": True,
    "resize_keyboard": True,
}
NO_KEYBOARD = {"remove_keyboard": True}


class SetupStopped(Exception):
    """Ends /setup; the message is for the person setting up."""


# Touched by the polling loop; the container healthcheck reads its age (/tmp is a private tmpfs).
HEARTBEAT = Path("/tmp/heartbeat")  # noqa: S108


@dataclass
class Picking:
    """A list waiting for the family to pick products for some of its items."""

    chat_id: int
    household: str
    ready: list[tuple[str, int]]
    asks: dict[int, tuple[str, int, list[Product], list[int]]]  # n -> spoken, qty, options, message ids
    picked: list[tuple[str, int]] = field(default_factory=list)


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tg = Telegram(cfg.telegram_token)
        self.accounts = Accounts(cfg.data_dir, cfg.admin_ids)
        self.transcribe = Transcriber(cfg.whisper_model)
        self.jobs: queue.Queue = queue.Queue()
        self.picking: dict[str, Picking] = {}
        self.replies: dict[int, queue.Queue] = {}  # user id -> their messages while their /setup runs
        self.lock = threading.Lock()
        self.alerted: dict[tuple[int, str], datetime] = {}
        self.username = ""

    # ------------------------------------------------------------- lifecycle

    def run(self) -> None:
        self.username = self.tg.setup(COMMANDS, DESCRIPTION, "Order groceries by voice or text.")
        self.accounts.bootstrap(self.cfg.data_dir, self.cfg.address_label, self.cfg.address_match, self.cfg.daily_cap)
        households = self.accounts.all()
        log.info("bot started", bot=self.username, households=len(households), place_orders=self.cfg.place_orders)
        for hh in households:
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
                    self._dispatch(update)
            except TelegramError as e:
                log.warning("telegram polling failed", error=str(e))
                time.sleep(5)

    def _dispatch(self, update: dict) -> None:
        try:
            if "message" in update:
                self._on_message(update["message"])
            elif "callback_query" in update:
                self._on_pick(update["callback_query"])
        except Exception:
            log.exception("update failed")

    def _worker(self) -> None:
        while True:
            job, args = self.jobs.get()
            name, started = job.__name__.lstrip("_"), time.monotonic()
            try:
                job(*args)
                log.info("job done", job=name, seconds=round(time.monotonic() - started, 1))
            except ShopError as e:
                log.warning("job stopped by the shop", job=name, reason=str(e))
                chat_id = args[0] if args else None
                self._say(chat_id or self.cfg.admin_ids[0], f"❌ Couldn't order: {e}.")
            except Exception:
                log.exception("job failed", job=name)
                if args:
                    self._say(args[0], "Something went wrong. Nothing was ordered unless you got a ✅.")
            finally:
                self.jobs.task_done()

    def _daily(self) -> None:
        """Once a day: self-check (login, address, home exit) and prune debug screenshots."""
        while True:
            now = datetime.now()
            run_at = now.replace(hour=self.cfg.health_hour, minute=0, second=0, microsecond=0)
            time.sleep(((run_at if run_at > now else run_at + timedelta(days=1)) - now).total_seconds())
            self.jobs.put((self._health, ()))
            cutoff = datetime.now() - DEBUG_KEEP
            for f in [*self.cfg.debug_dir.glob("*.png"), *self.cfg.data_dir.glob("households/*/debug/*.png")]:
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink(missing_ok=True)

    def _health(self) -> None:
        try:
            log.info("home exit ok", exit_ip=orders.exit_ip(self.cfg))
        except ShopError as e:
            log.error("health check failed", reason=str(e))
            self.alert(self.cfg.admin_ids[0], f"⚠️ Daily check failed: {e}. Orders will fail until this is fixed.")
            return
        for hh in self.accounts.all():
            try:
                orders.check(self.cfg, hh)
                log.info("health check ok", household=hh.id)
            except ShopError as e:
                log.warning("health check failed", household=hh.id, reason=str(e))
                self.alert(hh.owner, f"⚠️ Daily check of your shop account failed: {e}. Orders will fail until then.")

    # ------------------------------------------------------------- messaging

    def _say(self, chat_id: int, text: str, buttons=None, markup=None) -> int | None:
        try:
            return self.tg.send(chat_id, text, buttons, markup)
        except TelegramError as e:
            log.warning("telegram send failed", error=str(e))
            return None

    def alert(self, uid: int, text: str, buttons=None) -> None:
        """Tell someone about a problem that needs them, at most once per ALERT_REPEAT."""
        last = self.alerted.get((uid, text))
        if last and datetime.now() - last < ALERT_REPEAT:
            return
        self.alerted[(uid, text)] = datetime.now()
        self._say(uid, text, buttons)

    # ------------------------------------------------------------- incoming

    def _on_message(self, msg: dict) -> None:
        chat_id, user = msg["chat"]["id"], msg.get("from", {})
        uid, text = user.get("id"), (msg.get("text") or "").strip()
        private = msg["chat"].get("type") == "private"
        if uid in self.replies and private:
            self.replies[uid].put(msg)  # an answer to their running /setup
            return
        command, _, arg = text.partition(" ")
        command = command.split("@")[0] if command.startswith("/") else ""
        if command == "/id":
            self._say(chat_id, f"Your Telegram id is {uid}.")
            return
        if command == "/join":
            self._join(chat_id, user, arg)
            return
        if not self.accounts.allowed(uid):
            if command == "/start" and private:
                self._say(chat_id, "Hi! I've asked for you to be let in; I'll tell you when you can start.")
                self._request_access(user)
            else:
                log.info("message ignored: user not allowed")
            return

        if command in ("/start", "/help"):
            self._say(chat_id, HELP if self.accounts.household_of(uid) else f"{HELP}\n\n{NOT_SET_UP}")
        elif command == "/setup":
            if not private:
                self._say(chat_id, "Send /setup in a private chat with me: it needs your phone number and OTP.")
            else:
                self.replies[uid] = queue.Queue()
                name = user.get("first_name", "")
                threading.Thread(target=self._setup, args=(chat_id, uid, name), daemon=True).start()
        elif command == "/leave":
            self._leave(chat_id, uid, arg)
        elif not (hh := self.accounts.household_of(uid)):
            self._say(chat_id, NOT_SET_UP)
        elif command == "/invite":
            self._invite(chat_id, uid, hh)
        elif command == "/members":
            self._members(chat_id, hh)
        elif command == "/limit":
            self._limit(chat_id, uid, hh, arg)
        elif command == "/basket":
            basket = hh.ledger().basket()
            self._say(
                chat_id,
                "Waiting: " + ", ".join(f"{k} ×{q}" for k, q in basket) if basket else "The basket is empty.",
            )
        elif command == "/clear":
            self.jobs.put((self._clear, (chat_id, hh)))  # on the worker, so it never races an order
        elif command == "/usual":
            due = hh.ledger().due()
            self._say(
                chat_id,
                "Due now: "
                + (", ".join(f"{k} ×{q}" for k, q in due) or "nothing")
                + ("\nSend that back to order it." if due else ""),
            )
        elif voice := msg.get("voice") or msg.get("audio"):
            self.jobs.put((self._voice, (chat_id, hh, voice["file_id"])))
        elif text and not text.startswith("/"):
            self._queue_list(chat_id, hh, text)

    def _voice(self, chat_id: int, hh: Household, file_id: str) -> None:
        audio = self.tg.download(file_id)
        try:
            text = self.transcribe(audio, hh.catalog().vocabulary())
        finally:
            audio.unlink(missing_ok=True)
        log.info("voice transcribed", words=len(text.split()))
        self._say(chat_id, f'🎙️ Heard: "{text}"')
        if text:
            self._handle_list(chat_id, hh, text)  # already on the worker

    def _queue_list(self, chat_id: int, hh: Household, text: str) -> None:
        if not self.jobs.empty():
            self._say(chat_id, "Another order is in progress; yours is next.")
        self.jobs.put((self._handle_list, (chat_id, hh, text)))

    # ------------------------------------------------------------- lists and picks

    def _handle_list(self, chat_id: int, hh: Household, text: str) -> None:
        p = plan(text, hh.catalog())
        for note in p.notes:
            self._say(chat_id, note)
        if not p.to_pick:
            return self._order(chat_id, hh, p.ready) if p.ready else self._say(chat_id, "I didn't catch any items.")

        with orders.browser(self.cfg, hh, images=True) as b:
            found = [(name, qty, b.search(name)) for name, qty in p.to_pick]
        for name, _, options in found:
            if not options:
                self._say(chat_id, f'Couldn\'t find "{name}" in the shop; leaving it out.')
        asks = {n: (name, qty, options, []) for n, (name, qty, options) in enumerate(found) if options}
        if not asks:
            return self._order(chat_id, hh, p.ready) if p.ready else None

        pid = f"{chat_id % 100000}-{int(time.time()) % 100000}"  # short: callback data is max 64 bytes
        with self.lock:
            self.picking = {k: v for k, v in self.picking.items() if v.chat_id != chat_id}  # newest list wins
            self.picking[pid] = Picking(chat_id, hh.id, p.ready, asks)
        if p.ready:
            self._say(chat_id, "Got: " + ", ".join(f"{k} ×{q}" for k, q in p.ready) + ". Pick the rest:")
        for n, (name, qty, options, ids) in asks.items():
            ids.append(
                self._say(
                    chat_id,
                    f'Which "{name}" (×{qty})? Tap Pick under the one you want.',
                    [("Skip this item", f"{pid}:{n}:x")],
                )
            )
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

    def _on_pick(self, cb: dict) -> None:
        self.tg.answer(cb["id"])
        uid = cb.get("from", {}).get("id")
        if cb["data"].startswith("allow:"):
            return self._on_allow(cb, uid)
        if not (hh := self.accounts.household_of(uid)):
            return
        try:
            pid, n, choice = cb["data"].rsplit(":", 2)
        except ValueError:
            return
        with self.lock:
            picking = self.picking.get(pid)
            if not picking or picking.household != hh.id or int(n) not in picking.asks:
                return  # already answered, replaced by a newer list, or someone else's
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
            items = picking.ready + picking.picked
            if items:
                self.jobs.put((self._order, (picking.chat_id, hh, items)))
            else:
                self._say(picking.chat_id, "Nothing left to order.")

    # ------------------------------------------------------------- ordering

    def _clear(self, chat_id: int, hh: Household) -> None:
        hh.ledger().set_basket([])
        self._say(chat_id, "Basket emptied.")

    def _order(self, chat_id: int, hh: Household, items: list[tuple[str, int]]) -> None:
        self._say(chat_id, "Adding " + ", ".join(f"{k} ×{q}" for k, q in items) + " …")
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
            self._say(chat_id, f"✅ Ordered, {total} from the wallet:\n{details}")
        elif out.unconfirmed:
            self._say(
                chat_id,
                f"⚠️ I tapped Place Order but couldn't confirm it. Check the shop's app before resending.\n{details}",
            )
        elif out.waiting:
            self._say(
                chat_id,
                f"🧺 In the basket, ₹{out.item_total:g} so far. I'll order once it reaches "
                f"₹{self.cfg.min_order:g}; send more to add.\n{details}",
            )
        elif not out.problems:
            self._say(chat_id, f"🛒 Cart ready ({total}); ordering is switched off.\n{details}")
        else:
            self._say(
                chat_id,
                "❌ Not ordered:\n" + "\n".join(f"- {p}" for p in out.problems) + (f"\n{details}" if details else ""),
            )

    # ------------------------------------------------------------- accounts

    def _request_access(self, user: dict) -> None:
        """Ask the first admin to let a new person in (once per ALERT_REPEAT)."""
        uid, name = user.get("id"), " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
        handle = f" (@{user['username']})" if user.get("username") else ""
        log.info("access requested", user=uid)
        self.alert(
            self.cfg.admin_ids[0],
            f"{name or 'Someone'}{handle}, Telegram id {uid}, wants to use the bot. "
            "Once let in, they can connect their own shop account.",
            [(f"✅ Let {name or uid} in", f"allow:{uid}")],
        )

    def _on_allow(self, cb: dict, uid: int) -> None:
        if uid not in self.cfg.admin_ids:
            return
        new = int(cb["data"].removeprefix("allow:"))
        if msg := cb.get("message"):
            self.tg.remove_buttons(msg["chat"]["id"], msg["message_id"])
        if self.accounts.allowed(new):
            return
        self.accounts.allow(new)
        log.info("access granted", user=new, by=uid)
        self._say(uid, f"✔ {new} is in.")
        self._say(new, f"You're in! {NOT_SET_UP}")

    def _join(self, chat_id: int, user: dict, code: str) -> None:
        uid, name = user.get("id"), user.get("first_name", "")
        mine = self.accounts.household_of(uid)
        if mine and mine.owner == uid and len(mine.members) == 1:
            self._say(chat_id, "You have your own shop account here. Send /leave first to drop it, then join.")
            return
        if not code.strip() or not (hh := self.accounts.join(uid, name, code)):
            self._say(chat_id, "That invite code doesn't work. Codes work once, for 24 hours: ask for a new /invite.")
            return
        log.info("household joined", household=hh.id, user=uid)
        self._say(chat_id, f"You're in: you now order with {self.accounts.name(hh.owner)}'s account.\n\n{HELP}")
        self._say(hh.owner, f"{name or uid} joined your account with an invite.")

    def _invite(self, chat_id: int, uid: int, hh: Household) -> None:
        if hh.owner != uid:
            self._say(chat_id, f"Only {self.accounts.name(hh.owner)}, who connected this account, can invite.")
            return
        code = self.accounts.invite(hh.id)
        self._say(
            chat_id,
            f"Send this to the person you want to add. It works once, for 24 hours, and lets them order "
            f"from your account and wallet:\n\nOpen t.me/{self.username} and send /join {code}",
        )

    def _members(self, chat_id: int, hh: Household) -> None:
        names = [self.accounts.name(m) + (" (owner)" if m == hh.owner else "") for m in hh.members]
        self._say(
            chat_id,
            f"Delivering to {hh.address_label}, daily limit ₹{hh.daily_cap:g}.\nOrdering: " + ", ".join(names),
        )

    def _limit(self, chat_id: int, uid: int, hh: Household, arg: str) -> None:
        if not arg.strip():
            spent = hh.ledger().spent_today()
            self._say(chat_id, f"Daily limit ₹{hh.daily_cap:g}; ₹{spent:g} spent today. Change it: /limit 1500")
            return
        if hh.owner != uid:
            self._say(chat_id, f"Only {self.accounts.name(hh.owner)}, who connected this account, can change it.")
            return
        try:
            cap = float(arg.strip().lstrip("₹").replace(",", ""))
        except ValueError:
            cap = 0
        if cap <= 0:
            self._say(chat_id, "Send an amount in rupees, e.g. /limit 1500")
            return
        self.accounts.set_cap(hh.id, cap)
        self._say(chat_id, f"Daily limit is now ₹{cap:g}.")

    def _leave(self, chat_id: int, uid: int, arg: str) -> None:
        hh = self.accounts.household_of(uid)
        if not hh:
            self._say(chat_id, "You're not ordering with any account.")
            return
        if len(hh.members) == 1 and arg.strip().lower() != "yes":
            self._say(
                chat_id,
                "You're the only one on this account, so leaving deletes its login, your picks and order "
                "history here. Send /leave yes to go ahead. (To end the login on the shop too, log out "
                "of other devices in the shop's app.)",
            )
            return
        self.jobs.put((self._do_leave, (chat_id, uid, hh)))  # on the worker, so it never races an order

    def _do_leave(self, chat_id: int, uid: int, hh: Household) -> None:
        self.accounts.leave(uid)
        log.info("household left", household=hh.id, user=uid)
        self._say(chat_id, "Done: you no longer order with that account. /setup connects your own.")
        if (now := self.accounts.get(hh.id)) and now.owner != hh.owner:
            self._say(now.owner, "You now manage this shop account: /setup, /invite and /limit are yours.")

    # ------------------------------------------------------------- setup

    def _ask(self, chat_id: int, uid: int, question: str, wait: int = ANSWER_WAIT, markup=None) -> dict:
        """Ask during /setup and return their next message. /cancel or silence ends the setup."""
        while not self.replies[uid].empty():  # anything sent while waiting for a turn
            self.replies[uid].get_nowait()
        self._say(chat_id, question, markup=markup)
        try:
            msg = self.replies[uid].get(timeout=wait)
        except queue.Empty:
            raise SetupStopped("Setup timed out; nothing changed. Send /setup to start again.") from None
        if (msg.get("text") or "").startswith("/"):
            raise SetupStopped("Setup stopped; nothing changed.")
        return msg

    def _ask_phone(self, chat_id: int, uid: int) -> str:
        question = (
            "Let's connect your shop account. I'll log in to it like the shop's website does, so I need "
            "the phone number you use there. Tap the button below, or type the number. (/cancel to stop)"
        )
        for _ in range(3):
            msg = self._ask(chat_id, uid, question, markup=SHARE_PHONE)
            phone = re.sub(r"\D", "", (msg.get("contact") or {}).get("phone_number") or msg.get("text") or "")
            self.tg.delete(chat_id, msg["message_id"])
            phone = phone[2:] if len(phone) == 12 and phone.startswith("91") else phone.lstrip("0")
            if len(phone) == 10:
                return phone
            question = "That doesn't look like a 10-digit mobile number. Try again:"
        raise SetupStopped("Setup stopped; nothing changed. Send /setup to try again.")

    def _setup(self, chat_id: int, uid: int, name: str) -> None:
        """The questions of /setup (no browser needed), then the login on the worker."""
        try:
            phone = self._ask_phone(chat_id, uid)
            label = self._ask(
                chat_id,
                uid,
                "Which saved address should orders go to? Send its name as the shop's app shows it, e.g. Home",
                markup=NO_KEYBOARD,
            ).get("text", "")
            match = self._ask(
                chat_id,
                uid,
                f"And something that is always in the {label} address, like the flat or house number. "
                "I check it before every order, so nothing is ever delivered elsewhere.",
            ).get("text", "")
            if not label.strip() or not match.strip():
                raise SetupStopped("I need both the address name and part of it. Send /setup to try again.")
        except SetupStopped as e:
            self.replies.pop(uid, None)
            self._say(chat_id, str(e), markup=NO_KEYBOARD)
            return
        if not self.jobs.empty():
            self._say(chat_id, "I'm busy with another order; I'll log in right after.")
        self.jobs.put((self._login, (chat_id, uid, name, phone, label.strip(), match.strip())))

    def _login(self, chat_id: int, uid: int, name: str, phone: str, label: str, match: str) -> None:
        """Log in to someone's shop account by phone + OTP, pick their delivery address, and save it
        as their household (a new one, or theirs again if they own one)."""
        session = self.cfg.data_dir / f".login-{uid}.json"
        try:
            self._say(chat_id, "Opening the shop…")
            with orders.login_browser(self.cfg, session) as b:
                b.request_otp(phone)
                msg = self._ask(chat_id, uid, "The shop has sent you an OTP by SMS. Send it here:", wait=OTP_WAIT)
                self.tg.delete(chat_id, msg["message_id"])
                b.enter_otp(re.sub(r"\D", "", msg.get("text") or ""))
                b.choose_address(label)
                b.check_session(Rules(label, match, budget=0))
            hh = self.accounts.connect(uid, name, label, match, session, self.cfg.daily_cap)
            log.info("household connected", household=hh.id, user=uid)
            self._say(
                chat_id,
                f"✅ Connected. Orders go to {hh.address_label}, paid from the shop's wallet, at most "
                f"₹{hh.daily_cap:g} a day (/limit changes it). /invite adds family.\n\n{HELP}",
            )
        except SetupStopped as e:
            self._say(chat_id, str(e))
        except ShopError as e:
            log.warning("setup stopped by the shop", user=uid, reason=str(e))
            self._say(chat_id, f"❌ Couldn't connect: {e}. Nothing changed; send /setup to try again.")
        finally:
            self.replies.pop(uid, None)
            session.unlink(missing_ok=True)
