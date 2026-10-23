import contextlib
import time

import pytest

from grocerybot import bot as botmod
from grocerybot.config import Config

ADMIN, NEW = 1, 42
ADDRESS = "Home\nT6/503, MG Road"


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str, dict | None]] = []

    def send(self, chat_id, text, buttons=None, markup=None, entities=None):
        self.sent.append((chat_id, text, markup or buttons))
        return len(self.sent)

    def delete(self, *_): ...
    def answer(self, *_): ...
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
