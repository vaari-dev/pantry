"""Telegram front end. One worker thread runs all browser jobs in order; polling stays responsive."""

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from . import orders
from .catalog import Catalog, Product, plan
from .config import Config
from .ledger import Ledger
from .shop import ShopError
from .speech import Transcriber
from .telegram import Telegram, TelegramError

log = structlog.get_logger(__name__)

DESCRIPTION = 'Family grocery helper. Send a voice note or text like "bread aur do anda" and I\'ll order it.'
COMMANDS = {
    "basket": "What is waiting to be ordered",
    "clear": "Empty the waiting basket",
    "usual": "What we usually need around now",
    "help": "How to order",
    "id": "Show your Telegram id",
}
HELP = (
    'Send what we need, by voice or text, e.g. "bread aur do anda" or "2 milk, bananas".\n'
    "New things: I'll show a few options to pick from, and remember your pick.\n"
    "Small lists wait in the basket and are ordered once they add up to the shop's minimum."
)
DEBUG_KEEP = timedelta(days=7)
ALERT_REPEAT = timedelta(hours=6)
# Polling loop liveness, read by the container healthcheck.
HEARTBEAT = Path("/tmp/heartbeat")  # noqa: S108


@dataclass
class Picking:
    """A list waiting for product picks."""

    chat_id: int
    ready: list[tuple[str, int]]
    asks: dict[int, tuple[str, int, list[Product], list[int]]]  # n -> name, qty, options, message ids
    picked: list[tuple[str, int]] = field(default_factory=list)


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tg = Telegram(cfg.telegram_token)
        self.catalog = Catalog(cfg.catalog_file)
        self.ledger = Ledger(cfg.ledger_file)
        self.transcribe = Transcriber(cfg.whisper_model, self.catalog)
        self.jobs: queue.Queue = queue.Queue()
        self.picking: dict[str, Picking] = {}
        self.lock = threading.Lock()
        self.alerted: dict[str, datetime] = {}

    def run(self) -> None:
        name = self.tg.setup(COMMANDS, DESCRIPTION, "Order groceries by voice or text.")
        log.info("bot started", bot=name, daily_cap=self.cfg.daily_cap, place_orders=self.cfg.place_orders)
        for o in self.ledger.interrupted():
            self.alert(
                f"⚠️ An order (₹{o['total']:g}, {o['at']}) was interrupted while being placed. "
                "Check the shop's app; it was NOT retried."
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
                self._say(chat_id or self.cfg.user_ids[0], f"❌ Couldn't order: {e}.")
            except Exception:
                log.exception("job failed", job=name)
                if args:
                    self._say(args[0], "Something went wrong. Nothing was ordered unless you got a ✅.")
            finally:
                self.jobs.task_done()

    def _daily(self) -> None:
        """Daily self-check and screenshot pruning."""
        while True:
            now = datetime.now()
            run_at = now.replace(hour=self.cfg.health_hour, minute=0, second=0, microsecond=0)
            time.sleep(((run_at if run_at > now else run_at + timedelta(days=1)) - now).total_seconds())
            self.jobs.put((self._health, ()))
            cutoff = datetime.now() - DEBUG_KEEP
            for f in self.cfg.debug_dir.glob("*.png"):
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink(missing_ok=True)

    def _health(self) -> None:
        try:
            log.info("health check ok", exit_ip=orders.check(self.cfg))
        except ShopError as e:
            log.error("health check failed", reason=str(e))
            self.alert(f"⚠️ Daily check failed: {e}. Orders will fail until this is fixed.")

    def _say(self, chat_id: int, text: str, buttons=None) -> int | None:
        try:
            return self.tg.send(chat_id, text, buttons)
        except TelegramError as e:
            log.warning("telegram send failed", error=str(e))
            return None

    def alert(self, text: str) -> None:
        """Message the first user, at most once per ALERT_REPEAT."""
        last = self.alerted.get(text)
        if last and datetime.now() - last < ALERT_REPEAT:
            return
        self.alerted[text] = datetime.now()
        self._say(self.cfg.user_ids[0], text)

    def _on_message(self, msg: dict) -> None:
        chat_id, user = msg["chat"]["id"], msg.get("from", {})
        uid, text = user.get("id"), (msg.get("text") or "").strip()
        if text.startswith("/id") or (text.startswith("/start") and uid not in self.cfg.user_ids):
            self._say(chat_id, f"Your Telegram id is {uid}.")
            return
        if uid not in self.cfg.user_ids:
            log.info("message ignored: user not allowed")
            return
        if text.startswith(("/start", "/help")):
            self._say(chat_id, HELP)
        elif text.startswith("/basket"):
            basket = self.ledger.basket()
            self._say(
                chat_id,
                "Waiting: " + ", ".join(f"{k} ×{q}" for k, q in basket) if basket else "The basket is empty.",
            )
        elif text.startswith("/clear"):
            self.jobs.put((self._clear, (chat_id,)))  # on the worker: never races an order
        elif text.startswith("/usual"):
            due = self.ledger.due()
            self._say(
                chat_id,
                "Due now: "
                + (", ".join(f"{k} ×{q}" for k, q in due) or "nothing")
                + ("\nSend that back to order it." if due else ""),
            )
        elif voice := msg.get("voice") or msg.get("audio"):
            self.jobs.put((self._voice, (chat_id, voice["file_id"])))
        elif text and not text.startswith("/"):
            self._queue_list(chat_id, text)

    def _voice(self, chat_id: int, file_id: str) -> None:
        audio = self.tg.download(file_id)
        try:
            text = self.transcribe(audio)
        finally:
            audio.unlink(missing_ok=True)
        log.info("voice transcribed", words=len(text.split()))
        self._say(chat_id, f'🎙️ Heard: "{text}"')
        if text:
            self._handle_list(chat_id, text)  # already on the worker

    def _queue_list(self, chat_id: int, text: str) -> None:
        if not self.jobs.empty():
            self._say(chat_id, "Another order is in progress; yours is next.")
        self.jobs.put((self._handle_list, (chat_id, text)))

    def _handle_list(self, chat_id: int, text: str) -> None:
        p = plan(text, self.catalog)
        for note in p.notes:
            self._say(chat_id, note)
        if not p.to_pick:
            return self._order(chat_id, p.ready) if p.ready else self._say(chat_id, "I didn't catch any items.")

        with orders.browser(self.cfg, images=True) as b:
            found = [(name, qty, b.search(name)) for name, qty in p.to_pick]
        for name, _, options in found:
            if not options:
                self._say(chat_id, f'Couldn\'t find "{name}" in the shop; leaving it out.')
        asks = {n: (name, qty, options, []) for n, (name, qty, options) in enumerate(found) if options}
        if not asks:
            return self._order(chat_id, p.ready) if p.ready else None

        pid = f"{chat_id % 100000}-{int(time.time()) % 100000}"  # callback data <= 64 bytes
        with self.lock:
            self.picking = {k: v for k, v in self.picking.items() if v.chat_id != chat_id}  # newest list wins
            self.picking[pid] = Picking(chat_id, p.ready, asks)
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
        if cb.get("from", {}).get("id") not in self.cfg.user_ids:
            return
        try:
            pid, n, choice = cb["data"].rsplit(":", 2)
        except ValueError:
            return
        with self.lock:
            picking = self.picking.get(pid)
            if not picking or int(n) not in picking.asks:
                return  # answered or superseded
            name, qty, options, ids = picking.asks.pop(int(n))
            if choice == "x":
                result = "✖ skipped"
            else:
                product = options[int(choice)]
                picking.picked.append((self.catalog.learn(name, product), qty))
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
                self.jobs.put((self._order, (picking.chat_id, items)))
            else:
                self._say(picking.chat_id, "Nothing left to order.")

    def _clear(self, chat_id: int) -> None:
        self.ledger.set_basket([])
        self._say(chat_id, "Basket emptied.")

    def _order(self, chat_id: int, items: list[tuple[str, int]]) -> None:
        self._say(chat_id, "Adding " + ", ".join(f"{k} ×{q}" for k, q in items) + " …")
        out = orders.order(self.cfg, self.catalog, self.ledger, items)
        log.info(
            "order",
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
