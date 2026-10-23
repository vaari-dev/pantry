import contextlib
import dataclasses
import threading
import time
from datetime import datetime, timedelta

import pytest

from grocerybot import bot as botmod
from grocerybot.catalog import Product
from grocerybot.config import Config
from grocerybot.shop import Cart, ShopError

ADMIN, NEW = 1, 42
ADDRESS = "Home\nT6/503, MG Road"


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str, dict | None]] = []
        self.answers: list[str | None] = []
        self.unbuttoned: set[tuple[int, int]] = set()

    def send(self, chat_id, text, buttons=None, markup=None, entities=None):
        self.sent.append((chat_id, text, markup or buttons))
        return len(self.sent)

    def delete(self, *_): ...

    def answer(self, _, text=None):
        self.answers.append(text)

    def remove_buttons(self, chat_id, message_id):
        self.unbuttoned.add((chat_id, message_id))


class FakeShop:
    def __init__(self, session):
        self.session = session

    def request_otp(self, phone):
        assert phone == "9876543210"

    def enter_otp(self, code):
        assert code == "123456"

    def saved_addresses(self):
        return [ADDRESS]

    def pick_address(self, n):
        return ADDRESS


@pytest.fixture
def bot(tmp_path, monkeypatch):
    @contextlib.contextmanager
    def login_browser(cfg, session):
        yield FakeShop(session)
        session.write_text("{}")

    monkeypatch.setattr(botmod.orders, "login_browser", login_browser)
    monkeypatch.setattr(botmod, "BUSY", tmp_path / "busy")
    cfg = Config("https://shop.example", "token", ADMIN, "small", tmp_path, None, 9)
    b = botmod.Bot(cfg)
    b.tg = FakeTelegram()
    botmod.threading.Thread(target=b._worker, daemon=True).start()
    yield b
    for hold in b.holds.values():
        hold.timer.cancel()


def message(text="", uid=NEW, **extra):
    return {"message_id": 7, "chat": {"id": uid, "type": "private"}, "from": {"id": uid, "first_name": "Asha"}} | {
        "text": text,
        **extra,
    }


def said(bot, start: str, chat=NEW, timeout=5.0):
    """The markup of the first message to chat starting with start, waiting for it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for chat_id, text, markup in bot.tg.sent:
            if chat_id == chat and text.startswith(start):
                return markup
        time.sleep(0.01)
    raise AssertionError(f"never said {start!r}: {[t for c, t, _ in bot.tg.sent if c == chat]}")


def test_setup_before_access_starts_once_let_in_and_connects(bot):
    bot._on_message(message("/setup"))
    assert bot.tg.sent[-1][0] == ADMIN
    bot._on_button({"id": "q", "from": {"id": ADMIN}, "data": f"allow:{NEW}"})

    assert said(bot, "Let's connect") == botmod.SHARE_PHONE
    assert not any(t.startswith("🎉 You're in! Connect") for _, t, _ in bot.tg.sent)
    bot._on_message(message(contact={"phone_number": "(+91) 98765 43210"}))
    said(bot, "📩 Step 2")
    bot._on_message(message("123456"))
    keyboard = said(bot, "Your saved addresses")
    bot._on_message(message(keyboard["keyboard"][0][0]["text"]))

    assert said(bot, "🎉 You're all set") == botmod.NO_KEYBOARD
    hh = bot.accounts.household_of(NEW)
    assert (hh.owner, hh.address_label, hh.address_match, hh.state_file.exists()) == (NEW, "Home", "T6/503", True)


def test_let_in_without_setup_offers_the_connect_button(bot):
    bot._on_message(message("hi"))
    bot._on_button({"id": "q", "from": {"id": ADMIN}, "data": f"allow:{NEW}"})
    assert said(bot, "🎉 You're in! Connect") == botmod.CONNECT_BUTTON
    bot._on_message(message(botmod.CONNECT))
    assert said(bot, "Let's connect") == botmod.SHARE_PHONE


def test_offer_crosses_out_the_original_price(bot):
    product = botmod.Product("Eggs 🥚", "1 pack", "/pn/eggs/pvid/1", "₹89", mrp="₹104")
    bot.tg.send = lambda chat_id, text, buttons=None, markup=None, entities=None: bot.tg.sent.append(
        (chat_id, text, entities)
    )
    bot._offer(NEW, product, "1:0:0")

    _, text, entities = bot.tg.sent[-1]
    assert text == "Eggs 🥚\nSize: 1 pack  ·  ₹89 ₹104"
    # the egg emoji is two UTF-16 units, so the offset is one more than the str index
    assert entities == [{"type": "strikethrough", "offset": text.index("₹104") + 1, "length": 4}]


class FakeBrowser:
    """The shop, as orders.order sees it: every item costs ₹60, so two reach the ₹100 minimum."""

    def __init__(self):
        self.fail: Exception | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_): ...

    def check_session(self, rules):
        if self.fail:
            raise self.fail

    def empty_cart(self): ...

    def add(self, product, qty): ...

    def review_cart(self, expected, rules):
        total = 60 * sum(expected.values())
        return Cart(total, total, problems=["over today's limit"] if total > rules.budget else [])

    def place(self): ...

    def confirm(self, total, since):
        return True

    def screenshot(self, tag): ...


@pytest.fixture
def shop(bot, monkeypatch):
    (session := bot.cfg.data_dir / "state.json").write_text("{}")
    hh = bot.accounts.connect(NEW, "Asha", "Home", "T6/503", session)
    bot.accounts.allow(NEW)
    for item in ("bread", "milk"):
        hh.catalog().learn(item, Product(item.title(), "1 pc", f"https://shop.example/{item}"))
    browser = FakeBrowser()
    monkeypatch.setattr(botmod.orders, "browser", lambda *_, **__: browser)
    return hh, browser


def tap(bot, hh, offer=None, uid=NEW, household=None):
    chat_id, message_id = offer or hh.ledger().offer()
    data = f"now:{household or hh.id}"
    bot._on_button(
        {"id": "q", "from": {"id": uid}, "data": data, "message": {"chat": {"id": chat_id}, "message_id": message_id}}
    )


def placed(hh):
    return [o["items"] for o in hh.ledger()._orders() if o["status"] == "placed"]


def idle(bot, timeout=5.0):
    deadline = time.monotonic() + timeout
    time.sleep(0.05)
    while (not bot.jobs.empty() or botmod.BUSY.exists()) and time.monotonic() < deadline:
        time.sleep(0.01)


@contextlib.contextmanager
def worker_paused(bot):
    go = threading.Event()
    bot._queue(lambda _: go.wait(), NEW)
    yield
    go.set()


def test_below_the_minimum_waits_in_the_basket(bot, shop):
    hh, _ = shop
    bot._on_message(message("bread"))
    said(bot, "🧺 In the basket, ₹60")
    assert hh.ledger().basket() == [("bread", 1)]
    assert not bot.holds


def test_reaching_the_minimum_waits_then_places(bot, shop, monkeypatch):
    monkeypatch.setattr(botmod, "HOLD", 0.2)
    hh, _ = shop
    bot._on_message(message("2 bread"))
    assert said(bot, "⏳ Cart ready (₹120)") == botmod._order_now(hh)
    offer = hh.ledger().offer()
    said(bot, "✅ Ordered")
    assert placed(hh) == [[["bread", 2]]]
    assert offer in bot.tg.unbuttoned
    assert (hh.ledger().hold(), hh.ledger().offer(), bot.holds) == (None, None, {})


def test_order_now_places_at_once_and_only_once(bot, shop):
    hh, _ = shop
    bot._on_message(message("2 bread"))
    said(bot, "⏳ Cart ready")
    tap(bot, hh)
    tap(bot, hh)
    said(bot, "✅ Ordered")
    said(bot, "The basket is empty")
    assert placed(hh) == [[["bread", 2]]]
    assert bot.tg.answers[0].startswith("Ordering")


def test_order_now_takes_items_sent_just_before(bot, shop):
    hh, _ = shop
    bot._on_message(message("2 bread"))
    said(bot, "⏳ Cart ready")
    with worker_paused(bot):
        bot._on_message(message("milk"))
        tap(bot, hh)
    said(bot, "✅ Ordered")
    idle(bot)
    assert placed(hh) == [[["bread", 2], ["milk", 1]]]


def test_timer_and_order_now_together_order_once(bot, shop, monkeypatch):
    monkeypatch.setattr(botmod, "HOLD", 0.1)
    hh, _ = shop
    bot._on_message(message("2 bread"))
    said(bot, "⏳ Cart ready")
    with worker_paused(bot):
        tap(bot, hh)
        time.sleep(0.3)
    idle(bot)
    assert placed(hh) == [[["bread", 2]]]


def test_order_now_refuses_other_accounts_and_old_buttons(bot, shop):
    hh, _ = shop
    bot._on_message(message("2 bread"))
    said(bot, "⏳ Cart ready")
    tap(bot, hh, household="other")
    tap(bot, hh, uid=99)
    tap(bot, hh, offer=(NEW, 1))
    idle(bot)
    assert bot.tg.answers == [
        "That order is for another shop account.",
        "You no longer order with that shop account.",
        "That button is out of date; /basket shows what's waiting.",
    ]
    assert (NEW, 1) in bot.tg.unbuttoned
    assert placed(hh) == []
    assert hh.id in bot.holds


def test_more_items_restart_the_wait_and_clear_ends_it(bot, shop):
    hh, _ = shop
    bot._on_message(message("2 bread"))
    said(bot, "⏳ Cart ready (₹120)")
    first, offer = bot.holds[hh.id], hh.ledger().offer()
    bot._on_message(message("milk"))
    said(bot, "⏳ Cart ready (₹180)")
    assert first.timer.finished.is_set()
    assert offer in bot.tg.unbuttoned
    bot._on_message(message("/clear"))
    said(bot, "Basket emptied")
    assert (hh.ledger().basket(), hh.ledger().hold(), hh.ledger().offer(), bot.holds) == ([], None, None, {})


@pytest.mark.parametrize("fail", [ShopError("the shop logged us out"), RuntimeError("timeout")])
def test_a_failure_keeps_new_items_and_offers_order_now(bot, shop, fail):
    hh, browser = shop
    bot._on_message(message("2 bread"))
    said(bot, "⏳ Cart ready")
    browser.fail = fail
    bot._on_message(message("milk"))
    assert said(bot, "❌") == botmod._order_now(hh)
    assert hh.ledger().basket() == [("bread", 2), ("milk", 1)]
    assert not bot.holds
    browser.fail = None
    tap(bot, hh)
    said(bot, "✅ Ordered")
    assert placed(hh) == [[["bread", 2], ["milk", 1]]]


def test_over_the_limit_says_how_to_go_on(bot, shop):
    hh, _ = shop
    bot.accounts.update(hh.id, daily_cap=100.0)
    bot._on_message(message("2 bread"))
    said(bot, "❌ Not ordered")
    assert next(t for _, t, _ in bot.tg.sent if t.startswith("❌")).endswith(botmod.KEPT_LIMIT)
    assert hh.ledger().basket() == [("bread", 2)]


@pytest.mark.parametrize(("due", "ordered"), [(timedelta(minutes=1), False), (timedelta(0), True)])
def test_a_restart_picks_the_wait_up_again(bot, shop, due, ordered):
    hh, _ = shop
    hh.ledger().set_basket([("bread", 2)])
    hh.ledger().set_hold(NEW, datetime.now() + due)
    bot._resume()
    idle(bot)
    assert bool(placed(hh)) == ordered
    assert (hh.id in bot.holds) != ordered


def test_a_restart_long_after_offers_instead_of_ordering(bot, shop):
    hh, _ = shop
    hh.ledger().set_basket([("bread", 2)])
    hh.ledger().set_hold(NEW, datetime.now() - timedelta(hours=2))
    bot._resume()
    assert said(bot, "⏸ I was offline") == botmod._order_now(hh)
    idle(bot)
    assert (placed(hh), hh.ledger().hold(), bot.holds) == ([], None, {})


def test_a_wait_started_by_someone_who_left_goes_to_the_owner(bot, shop, monkeypatch):
    monkeypatch.setattr(botmod, "HOLD", 0.2)
    hh, _ = shop
    member = 7
    bot.accounts.join(member, "Ravi", bot.accounts.invite(hh.id))
    bot._on_message(message("2 bread", uid=member))
    said(bot, "⏳ Cart ready", chat=member)
    bot.accounts.leave(member, hh.id)  # what /leave, removal, another invite and /setup all do
    assert said(bot, "⏸ The waiting order wasn't placed, since Ravi left") == botmod._order_now(hh)
    assert placed(hh) == []
    tap(bot, hh)
    said(bot, "✅ Ordered")


def test_an_open_pick_holds_the_basket_back(bot, shop, monkeypatch):
    monkeypatch.setattr(botmod, "HOLD", 0.1)
    monkeypatch.setattr(botmod, "PICK_HOLD", 0.1)
    hh, _ = shop
    bot.picking["p"] = botmod.Picking(NEW, hh.id, [], {})
    bot._on_message(message("2 bread"))
    said(bot, "⏳ Cart ready")
    time.sleep(0.4)
    assert placed(hh) == []
    bot.picking.clear()
    said(bot, "✅ Ordered")


def test_busy_clears_after_a_skipped_job(bot, shop):
    hh, _ = shop
    with worker_paused(bot):
        bot._queue(bot._clear, NEW, dataclasses.replace(hh, id="gone"))
    idle(bot)
    assert not botmod.BUSY.exists()
