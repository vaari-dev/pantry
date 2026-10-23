import contextlib
import dataclasses
import time

import pytest

from grocerybot import bot as botmod
from grocerybot.config import Config

ADMIN, NEW = 1, 42
ADDRESS = "Home\nT6/503, MG Road"


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str, dict | None]] = []
        self.answers: list[str | None] = []

    def send(self, chat_id, text, buttons=None, markup=None):
        self.sent.append((chat_id, text, markup or buttons))
        return len(self.sent)

    def delete(self, *_): ...
    def answer(self, _, text=None):
        self.answers.append(text)

    def remove_buttons(self, *_): ...


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
    return b


def message(text="", uid=NEW, **extra):
    return {"message_id": 7, "chat": {"id": uid, "type": "private"}, "from": {"id": uid, "first_name": "Asha"}} | {
        "text": text,
        **extra,
    }


def said(bot, start: str, timeout=5.0):
    """The markup of the first message to NEW starting with start, waiting for it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for chat_id, text, markup in bot.tg.sent:
            if chat_id == NEW and text.startswith(start):
                return markup
        time.sleep(0.01)
    raise AssertionError(f"never said {start!r}: {[t for c, t, _ in bot.tg.sent if c == NEW]}")


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


MIN = 100


@pytest.fixture
def shop(bot, monkeypatch):
    """A connected household; each item costs ₹100, so one reaches the smallest order. Records each order."""
    (session := bot.cfg.data_dir / "state.json").write_text("{}")
    hh = bot.accounts.connect(NEW, "Asha", "Home", "T6/503", session)
    bot.accounts.allow(NEW)
    calls = []

    def order(cfg, hh, items, hold=False):
        calls.append((items, hold))
        if hh.daily_cap < MIN:
            return botmod.orders.Outcome(problems=["today's limit is used up"])
        basket = hh.ledger().basket() + items
        total = MIN * sum(q for _, q in basket)
        out = botmod.orders.Outcome(total=total, item_total=total, lines=[f"{k} ×{q}" for k, q in basket])
        if hold:
            out.held = True
            hh.ledger().set_basket(basket)
        else:
            out.placed = True
            hh.ledger().set_basket([])
        return out

    monkeypatch.setattr(botmod.orders, "order", order)
    return hh, calls


def tap(bot, data, uid=NEW):
    bot._on_button({"id": "q", "from": {"id": uid}, "data": data, "message": {"chat": {"id": uid}, "message_id": 1}})


def idle(bot, timeout=5.0):
    deadline = time.monotonic() + timeout
    time.sleep(0.05)
    while (not bot.jobs.empty() or botmod.BUSY.exists()) and time.monotonic() < deadline:
        time.sleep(0.01)


def test_order_reaching_the_minimum_waits_then_places(bot, shop, monkeypatch):
    monkeypatch.setattr(botmod, "HOLD", 0.2)
    hh, calls = shop
    bot.jobs.put((bot._order, (NEW, hh, [("bread", 1)])))
    assert said(bot, "⏳ Cart ready") == botmod._order_now(hh)
    assert hh.ledger().hold()[0] == NEW
    said(bot, "✅ Ordered")
    assert calls == [([("bread", 1)], True), ([], False)]
    assert not bot.holds
    assert hh.ledger().hold() is None


def test_order_now_places_at_once_and_only_once(bot, shop):
    hh, calls = shop
    bot.jobs.put((bot._order, (NEW, hh, [("bread", 1)])))
    said(bot, "⏳ Cart ready")
    tap(bot, f"now:{hh.id}")
    tap(bot, f"now:{hh.id}")
    said(bot, "✅ Ordered")
    said(bot, "The basket is empty")
    assert calls == [([("bread", 1)], True), ([], False)]
    assert bot.tg.answers[-2:] == ["Ordering …", "Ordering …"]
    assert not bot.holds


def test_order_now_after_more_items_places_them_all(bot, shop):
    hh, calls = shop
    bot.jobs.put((bot._order, (NEW, hh, [("bread", 1)])))
    said(bot, "⏳ Cart ready")
    bot.jobs.put((bot._order, (NEW, hh, [("milk", 2)])))
    tap(bot, f"now:{hh.id}")
    said(bot, "✅ Ordered")
    idle(bot)
    assert calls[-1] == ([], False)
    assert not bot.holds


def test_order_now_is_only_for_its_own_account(bot, shop):
    hh, calls = shop
    bot.jobs.put((bot._order, (NEW, hh, [("bread", 1)])))
    said(bot, "⏳ Cart ready")
    tap(bot, "now:someone-else")
    idle(bot)
    assert bot.tg.answers[-1] == "That order is for another shop account."
    assert len(calls) == 1
    assert hh.id in bot.holds


def test_more_items_restart_the_wait(bot, shop):
    hh, calls = shop
    bot.jobs.put((bot._order, (NEW, hh, [("bread", 1)])))
    said(bot, "⏳ Cart ready")
    first = bot.holds[hh.id]
    bot.jobs.put((bot._order, (NEW, hh, [("milk", 2)])))
    idle(bot)
    assert bot.holds[hh.id] is not first
    assert not first.timer.is_alive()
    assert calls == [([("bread", 1)], True), ([("milk", 2)], True)]
    bot.jobs.put((bot._clear, (NEW, hh)))
    said(bot, "Basket emptied")
    assert not bot.holds
    assert hh.ledger().hold() is None


def test_a_restart_picks_the_wait_up_again(bot, shop):
    hh, calls = shop
    hh.ledger().set_basket([("bread", 1)])
    hh.ledger().set_hold(NEW, botmod.datetime.now() - botmod.timedelta(seconds=1))
    bot._resume()
    said(bot, "✅ Ordered")
    assert calls == [([], False)]


def test_a_failed_order_keeps_the_basket_and_offers_order_now(bot, shop):
    hh, _ = shop
    bot.jobs.put((bot._order, (NEW, hh, [("bread", 1)])))
    said(bot, "⏳ Cart ready")
    bot.accounts.update(hh.id, daily_cap=1.0)
    tap(bot, f"now:{hh.id}")
    assert said(bot, "❌ Not ordered") == botmod._order_now(hh)
    assert hh.ledger().basket() == [("bread", 1)]


def test_a_member_leaving_stops_the_wait_they_started(bot, shop):
    hh, calls = shop
    member = 7
    hh = bot.accounts.join(member, "Ravi", bot.accounts.invite(hh.id))
    bot.jobs.put((bot._order, (member, hh, [("bread", 1)])))
    deadline = time.monotonic() + 5
    while hh.id not in bot.holds and time.monotonic() < deadline:
        time.sleep(0.01)
    bot.jobs.put((bot._drop, (NEW, hh, member)))
    assert said(bot, "⏸ The order waiting") == botmod._order_now(hh)
    assert not bot.holds
    assert calls == [([("bread", 1)], True)]
    assert hh.ledger().basket() == [("bread", 1)]


def test_busy_clears_after_a_skipped_job(bot, shop):
    hh, _ = shop

    def slow(_):
        time.sleep(0.1)

    bot.jobs.put((slow, (NEW,)))  # still queued when it finishes: a household gone since
    bot.jobs.put((bot._clear, (NEW, dataclasses.replace(hh, id="gone"))))
    time.sleep(0.3)
    assert not botmod.BUSY.exists()
