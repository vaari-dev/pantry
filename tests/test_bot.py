import contextlib
import dataclasses
import threading
import time
from datetime import datetime, timedelta

import pytest

from pantry import bot as botmod
from pantry import store
from pantry.catalog import Choice, Product
from pantry.config import Config
from pantry.intent import Request
from pantry.ledger import Ledger
from pantry.shop import Address, Cart, ShopError

ADMIN, NEW = 1, 42
ADDRESS = Address("addr-home", "Home", "T6/503, MG Road")


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str, dict | None]] = []
        self.answers: list[str | None] = []
        self.unbuttoned: set[tuple[int, int]] = set()

    def send(self, chat_id, text, buttons=None, markup=None, entities=None):
        self.sent.append((chat_id, text, markup or buttons))
        return len(self.sent)

    def delete(self, *_): ...

    def edit_text(self, *_): ...

    def answer(self, _, text=None):
        self.answers.append(text)

    def remove_buttons(self, chat_id, message_id):
        self.unbuttoned.add((chat_id, message_id))


class FakeLogin:
    def request_otp(self, phone):
        assert phone == "9876543210"

    def enter_otp(self, code):
        assert code == "123456"
        return {"client_id": "c", "access_token": "a", "refresh_token": "r", "expires_at": time.time() + 604800}


class FakeAddresses:
    def __enter__(self):
        return self

    def __exit__(self, *_): ...

    def addresses(self):
        return [ADDRESS, Address("addr-work", "Work", "Tower 3")]

    def deliver_to(self, address_id):
        assert address_id == ADDRESS.id
        return ADDRESS


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "Login", FakeLogin)
    monkeypatch.setattr(botmod, "Zepto", lambda _: FakeAddresses())
    monkeypatch.setattr(botmod.signal, "signal", lambda *_: None)  # run() must not take pytest's SIGTERM
    cfg = Config("token", ADMIN, "small", tmp_path, 9, "key")
    b = botmod.Bot(cfg)
    b.tg = FakeTelegram()
    b.read = read_list
    botmod.threading.Thread(target=b._worker, daemon=True).start()
    yield b
    for timer in b.holds.values():
        timer.cancel()


def read_list(text, *_):
    """What Claude makes of a plain list: "2 bread, milk"."""
    items = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        qty, _, name = part.partition(" ")
        items.append((name, int(qty)) if qty.isdigit() else (part, 1))
    return Request(add=items)


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


def sent(bot, start: str, chat=NEW, having="", timeout=5.0) -> str:
    """The text of a message to chat starting with start and holding having, waiting for it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if found := [t for c, t, _ in bot.tg.sent if c == chat and t.startswith(start) and having in t]:
            return found[-1]
        time.sleep(0.01)
    raise AssertionError(f"never said {start!r} with {having!r}: {[t for c, t, _ in bot.tg.sent if c == chat]}")


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
    assert (hh.owner, hh.address_id, hh.address, hh.state_file.exists()) == (NEW, ADDRESS.id, str(ADDRESS), True)


def test_let_in_without_setup_offers_the_connect_button(bot):
    bot._on_message(message("hi"))
    bot._on_button({"id": "q", "from": {"id": ADMIN}, "data": f"allow:{NEW}"})
    assert said(bot, "🎉 You're in! Connect") == botmod.CONNECT_BUTTON
    bot._on_message(message(botmod.CONNECT))
    assert said(bot, "Let's connect") == botmod.SHARE_PHONE


def test_offer_crosses_out_the_original_price(bot):
    product = Choice("Eggs 🥚", [Product("Eggs 🥚", "1 pack", "/pn/eggs/pvid/1", "₹89", mrp="₹104")])
    bot.tg.send = lambda chat_id, text, buttons=None, markup=None, entities=None: bot.tg.sent.append(
        (chat_id, text, entities)
    )
    bot._offer(NEW, product, 1, "1:0:0")

    _, text, entities = bot.tg.sent[-1]
    assert text == "Eggs 🥚\n1 pack ₹89 ₹104"
    # the egg emoji is two UTF-16 units, so the offset is one more than the str index
    assert entities == [{"type": "strikethrough", "offset": text.index("₹104") + 1, "length": 4}]


class FakeShop:
    """The shop, as orders.order sees it: every item costs ₹60, so two reach the ₹100 minimum."""

    def __init__(self):
        self.fail: Exception | None = None
        self.opened = 0
        self.unit = 60  # what the shop charges for any item
        self.gone: set[str] = set()  # product names out of stock
        self.why = "out of stock"  # what stock says of the gone ones
        self.limits: dict[str, int] = {}  # product label -> the most one order may have
        self.cart: dict = {}
        self.orders = 0
        self.placed_to: list[str] = []

    def __enter__(self):
        self.opened += 1
        return self

    def __exit__(self, *_): ...

    def deliver_to(self, address_id):
        if self.fail:
            raise self.fail

    def stock(self, product):
        return self.why if product.name in self.gone else self.limits.get(product.label(), 9)

    def fill(self, items):
        self.cart = dict(items)

    def review(self, expected, rules):
        assert expected == self.cart
        total = self.unit * sum(expected.values())
        prices = dict.fromkeys(expected, f"₹{self.unit}")
        problems = ["over today's limit"] if total > rules.budget else []
        return Cart(total, total, problems=problems, prices=prices)

    def place(self, rules, cod):
        self.orders += 1
        self.placed_to.append(rules.address_id)
        return self.orders

    def addresses(self):
        return FakeAddresses().addresses()

    def confirm(self, placed):
        return self.orders == placed

    def search(self, query, limit=4):
        return [Product(f"{query.title()} {n}", "1 pc", f"https://shop.example/{query}/{n}", "₹60") for n in (1, 2)]

    def price(self, product):
        return ""


def one(product):
    return Choice(product.name, [product])


@pytest.fixture
def shop(bot, monkeypatch):
    (session := bot.cfg.data_dir / "state.json").write_text("{}")
    hh = bot.accounts.connect(NEW, "Asha", ADDRESS.id, str(ADDRESS), session)
    bot.accounts.allow(NEW)
    for item in ("bread", "milk"):
        hh.catalog().learn(item, one(Product(item.title(), "1 pc", f"https://shop.example/{item}", "₹60")))
    fake = FakeShop()
    monkeypatch.setattr(botmod, "Zepto", lambda _: fake)
    monkeypatch.setattr(botmod.orders, "Zepto", lambda _: fake)
    return hh, fake


def tap(bot, hh, offer=None, uid=NEW, household=None):
    deadline = time.monotonic() + 5
    while not (offer := offer or hh.ledger().offer()) and time.monotonic() < deadline:  # recorded just after it is sent
        time.sleep(0.01)
    chat_id, message_id = offer
    data = f"now:{household or hh.id}"
    bot._on_button(
        {"id": "q", "from": {"id": uid}, "data": data, "message": {"chat": {"id": chat_id}, "message_id": message_id}}
    )


def placed(hh):
    return [o["items"] for o in hh.ledger()._orders() if o["status"] == "placed"]


def idle(bot, timeout=5.0):
    deadline = time.monotonic() + timeout
    time.sleep(0.05)
    while bot.jobs.unfinished_tasks and time.monotonic() < deadline:
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
    assert sent(bot, "🧺 Your basket").endswith("Total ₹60 (orders start at ₹100)\nSend more to add.")
    assert hh.ledger().basket() == [("bread", 1)]
    assert not bot.holds


def test_reaching_the_minimum_waits_then_places(bot, shop, monkeypatch):
    monkeypatch.setattr(botmod, "HOLD", 0.2)
    hh, _ = shop
    bot._on_message(message("2 bread"))
    assert said(bot, "⏳ I'll order this") == botmod._order_now(hh)
    sent(bot, "⏳ I'll order this", having="Total ₹120")
    offer = hh.ledger().offer()
    said(bot, "✅ Ordered")
    assert placed(hh) == [[["bread", 2]]]
    assert offer in bot.tg.unbuttoned
    assert (hh.ledger().hold(), hh.ledger().offer(), bot.holds) == (None, None, {})


def test_order_now_places_at_once_and_only_once(bot, shop):
    hh, _ = shop
    bot._on_message(message("2 bread"))
    said(bot, "⏳ I'll order this")
    tap(bot, hh)
    tap(bot, hh)
    said(bot, "✅ Ordered")
    said(bot, "The basket is empty")
    assert placed(hh) == [[["bread", 2]]]
    assert bot.tg.answers[0].startswith("Ordering")


def test_order_now_takes_items_sent_just_before(bot, shop):
    hh, _ = shop
    bot._on_message(message("2 bread"))
    said(bot, "⏳ I'll order this")
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
    said(bot, "⏳ I'll order this")
    with worker_paused(bot):
        tap(bot, hh)
        time.sleep(0.3)
    idle(bot)
    assert placed(hh) == [[["bread", 2]]]


def test_order_now_refuses_other_accounts_and_old_buttons(bot, shop):
    hh, _ = shop
    bot._on_message(message("2 bread"))
    said(bot, "⏳ I'll order this")
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
    sent(bot, "⏳ I'll order this", having="Total ₹120")
    first, offer = bot.holds[hh.id], hh.ledger().offer()
    bot._on_message(message("milk"))
    sent(bot, "⏳ I'll order this", having="Total ₹180")
    assert first.finished.is_set()
    assert offer in bot.tg.unbuttoned
    bot._on_message(message("/clear"))
    said(bot, "Basket emptied")
    assert (hh.ledger().basket(), hh.ledger().hold(), hh.ledger().offer(), bot.holds) == ([], None, None, {})


@pytest.mark.parametrize("fail", [ShopError("the shop logged us out"), RuntimeError("timeout")])
def test_a_failure_keeps_new_items_and_offers_order_now(bot, shop, fail):
    hh, fake = shop
    bot._on_message(message("2 bread"))
    bot._on_message(message("milk"))
    said(bot, "⏳ I'll order this")
    fake.fail = fail
    tap(bot, hh)
    assert said(bot, "❌") == botmod._order_now(hh)
    assert hh.ledger().basket() == [("bread", 2), ("milk", 1)]
    assert not bot.holds
    fake.fail = None
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


def test_a_start_asks_accounts_from_the_chrome_bot_to_set_up_again_and_drops_its_files(bot, shop):
    hh, _ = shop
    (session := bot.cfg.data_dir / "old.json").write_text("{}")
    bot.accounts.allow(ADMIN)
    old = bot.accounts.connect(ADMIN, "Admin", "", "", session)  # households.json from before had no address_id
    for left in (
        hh.dir / "debug" / "x.png",
        bot.cfg.data_dir / "tailscale" / "state",
        bot.cfg.data_dir / "apimap.json",
    ):
        left.parent.mkdir(parents=True, exist_ok=True)
        left.write_text("x")
    bot._resume()
    assert [t for c, t, _ in bot.tg.sent if t == botmod.RECONNECT] == [botmod.RECONNECT]
    said(bot, "🔌", chat=old.owner)
    assert not [*bot.cfg.data_dir.glob("**/debug")] and not (bot.cfg.data_dir / "tailscale").exists()
    assert not (bot.cfg.data_dir / "apimap.json").exists()


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
    said(bot, "⏳ I'll order this", chat=member)
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
    said(bot, "⏳ I'll order this")
    time.sleep(0.4)
    assert placed(hh) == []
    bot.picking.clear()
    said(bot, "✅ Ordered")


def test_busy_clears_after_a_skipped_job(bot, shop):
    hh, _ = shop
    with worker_paused(bot):
        bot._queue(bot._clear, NEW, dataclasses.replace(hh, id="gone"))
    idle(bot)
    assert not bot.jobs.unfinished_tasks


def test_a_pick_while_the_options_are_still_coming_in(bot, shop):
    """Tapping the first item's option while the second's are still being sent."""
    hh, _ = shop
    send = bot.tg.send

    def tap_first_option(chat_id, text, buttons=None, markup=None, entities=None):
        if text.startswith('Which "jam"'):
            pid = buttons[0][1].split(":")[0]
            bot._pick(hh, f"{pid}:0:0")
        return send(chat_id, text, buttons, markup, entities)

    bot.tg.send = tap_first_option
    bot.tg.edit_text = lambda *_: None
    bot._on_message(message("eggs, jam"))
    said(bot, "Jam 2")
    idle(bot)
    assert not [t for _, t, _ in bot.tg.sent if t.startswith("Something went wrong")]
    pid = next(iter(bot.picking))
    bot._pick(hh, f"{pid}:1:1")
    sent(bot, "⏳ I'll order this", having="Total ₹120")


def restarted(bot):
    new = botmod.Bot(bot.cfg)
    new.tg = bot.tg
    botmod.threading.Thread(target=new._worker, daemon=True).start()
    return new


def test_a_pick_survives_a_restart(bot, shop):
    hh, _ = shop
    bot.tg.edit_text = lambda *_: None
    bot._on_message(message("jam"))
    said(bot, "Jam 2")
    pid = next(iter(bot.picking))

    new = restarted(bot)
    new._pick(hh, f"{pid}:0:1")
    said(new, "🧺 Your basket")
    assert hh.ledger().basket() == [("jam", 1)]


def test_a_stop_lets_the_running_job_finish_and_keeps_the_offset(bot):
    running, go, finished = threading.Event(), threading.Event(), []

    def job(_):
        running.set()
        go.wait()
        finished.append(True)

    def updates(offset):
        if offset is None:
            bot._queue(job, NEW)
            return [{"update_id": 5}]
        running.wait()
        bot.stopping.set()  # SIGTERM mid-job
        threading.Timer(0.2, go.set).start()
        return []

    bot.tg.updates, bot.tg.setup = updates, lambda *_: "joey"
    bot.run()
    assert finished == [True]  # run() returned only after the job
    assert restarted(bot).offset == 6  # update 5 is not handled again


def test_a_stop_ends_a_setup_waiting_for_an_answer(bot):
    bot.accounts.allow(NEW)
    bot._on_message(message("/setup"))
    said(bot, "Let's connect")
    bot.stopping.set()
    bot._drain()
    said(bot, "❌ I'm restarting for an update")


def test_a_stop_hands_back_jobs_not_yet_started(bot, shop):
    with worker_paused(bot):
        bot._queue(bot._handle, NEW, NEW, shop[0], "2 bread")
        bot.stopping.set()
    bot._drain()
    said(bot, "⏸ I restarted for an update")
    assert shop[0].ledger().basket() == []


def test_a_stale_or_unreadable_saved_state_is_dropped(bot):
    store = botmod.store
    old = {"chat_id": NEW, "household": "h", "ready": [], "asks": {}, "picked": [], "started": time.time() - 2 * 86400}
    store.write(bot.saved, {"offset": 9, "at": time.time() - 2 * 86400, "picking": {"old": old, "bad": {"x": 1}}})
    new = botmod.Bot(bot.cfg)
    assert (new.offset, new.picking) == (None, {})


def test_basket_lists_prices_and_total_with_order_now(bot, shop):
    hh, _ = shop
    hh.catalog().learn("bread", one(Product("Harvest Gold", "400 g", "https://shop.example/bread", "₹45")))
    hh.catalog().learn("milk", one(Product("Milk", "1 pc", "https://shop.example/milk/2")))  # no price seen
    hh.ledger().set_basket([("bread", 2), ("milk", 1)])
    bot._on_message(message("/basket"))
    assert said(bot, "🧺 Your basket") == botmod._order_now(hh)
    text = next(t for _, t, _ in bot.tg.sent if t.startswith("🧺 Your basket"))
    assert text.splitlines() == [
        "🧺 Your basket",
        "• Bread ×2: Harvest Gold 400 g ×2 · ₹90",
        "• Milk ×1: Milk 1 pc ×1",
        "",
        "Total ₹90 + 1 without a price",
    ]
    assert hh.ledger().offer()  # the button is the live Order now


def test_an_empty_basket_has_no_button(bot, shop):
    bot._on_message(message("/basket"))
    assert said(bot, "The basket is empty.") is None


def test_basket_shows_what_the_minimum_needs(bot, shop):
    hh, _ = shop
    hh.catalog().learn("bread", one(Product("Harvest Gold", "400 g", "https://shop.example/bread", "₹45")))
    hh.ledger().set_basket([("bread", 1)])
    bot._on_message(message("/basket"))
    said(bot, "🧺 Your basket\n• Bread ×1: Harvest Gold 400 g ×1 · ₹45\n\nTotal ₹45 (orders start at ₹100)")


def test_basket_waits_for_an_order_in_progress(bot, shop):
    hh, _ = shop
    hh.ledger().set_basket([("bread", 1)])
    with worker_paused(bot):
        bot._on_message(message("/basket"))
        said(bot, "An order is in progress")
    assert hh.ledger().offer() is None


def test_ledger_edits_from_two_threads_both_land(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    threads = [threading.Thread(target=ledger.set_offer, args=(1, n)) for n in range(20)]
    threads += [threading.Thread(target=ledger.begin, args=([("bread", 1)], 60)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ledger._orders()) == 20


@pytest.mark.parametrize("error", [ShopError("the shop is not answering"), RuntimeError("boom")])
def test_a_failure_after_place_order_is_unconfirmed_never_not_ordered(bot, shop, error):
    hh, fake = shop

    def place(rules, cod):
        raise error

    fake.place = place
    bot._on_message(message("2 bread"))
    said(bot, "⏳ I'll order this")
    tap(bot, hh)
    said(bot, "⚠️ I couldn't confirm the order")
    assert [o["status"] for o in hh.ledger()._orders()] == ["unknown"]
    assert not [t for _, t, _ in bot.tg.sent if "Couldn't order" in t or "othing was ordered" in t]


def test_the_shop_is_opened_once_per_order_not_per_message(bot, shop):
    hh, fake = shop
    bot._on_message(message("bread"))
    bot._on_message(message("milk"))
    sent(bot, "⏳ I'll order this", having="Total ₹120")
    assert fake.opened == 0
    tap(bot, hh)
    said(bot, "✅ Ordered")
    assert fake.opened == 1


def test_the_shops_lower_total_waits_and_corrects_the_prices(bot, shop, monkeypatch):
    monkeypatch.setattr(botmod, "HOLD", 0.1)
    hh, fake = shop
    fake.unit = 45
    bot._on_message(message("bread, milk"))
    sent(bot, "⏳ I'll order this", having="Total ₹120")
    said(bot, "🧺 The shop's total is ₹90, below the ₹100 minimum")
    assert hh.ledger().basket() == [("bread", 1), ("milk", 1)]
    bot._on_message(message("/basket"))
    sent(bot, "🧺 Your basket", having="Total ₹90 (orders start at ₹100)")


def test_an_item_unavailable_at_order_time_stays_in_the_basket(bot, shop):
    hh, fake = shop
    fake.gone = {"Milk"}
    bot._on_message(message("2 bread, milk"))
    said(bot, "⏳ I'll order this")
    tap(bot, hh)
    sent(bot, "✅ Ordered", having="Couldn't add milk (out of stock); it stays in the /basket")
    assert placed(hh) == [[["bread", 2]]]
    assert hh.ledger().basket() == [("milk", 1)]


def test_a_skipped_item_says_why(bot, shop):
    hh, fake = shop
    fake.gone, fake.why = {"Milk"}, "the shop no longer sells this"
    bot._on_message(message("2 bread, milk"))
    said(bot, "⏳ I'll order this")
    tap(bot, hh)
    sent(bot, "✅ Ordered", having="Couldn't add milk (the shop no longer sells this)")


def test_the_shops_limit_per_order_is_ordered_and_told(bot, shop):
    """24 Oct, 14:29 UTC: 3 of an item the shop sells 2 at a time blocked the whole order."""
    hh, fake = shop
    fake.limits = {"Bread (1 pc)": 2}
    bot._on_message(message("3 bread, milk"))
    said(bot, "⏳ I'll order this")
    tap(bot, hh)
    sent(bot, "✅ Ordered", having="Bread ×3: Bread 1 pc ×2 · ₹120  (2 packs of 3 packs: all the shop allows)")
    assert placed(hh) == [[["bread", 3], ["milk", 1]]]


def test_with_ordering_off_nothing_is_ordered_until_order_now(bot, shop):
    hh, fake = shop
    bot.accounts.update(hh.id, place_orders=False)
    reads(bot, add=[("bread", 2)])
    bot._on_message(message("2 bread"))
    assert said(bot, "🧺 Your basket") == botmod._order_now(hh)
    sent(bot, "🧺 Your basket", having="Ordering is off")
    idle(bot)
    assert (fake.opened, bot.holds, placed(hh)) == (0, {}, [])
    tap(bot, hh)
    said(bot, "✅ Ordered")
    assert placed(hh) == [[["bread", 2]]]


def test_one_accounts_failed_check_does_not_skip_the_others(bot, shop, monkeypatch):
    hh, _ = shop
    (session := bot.cfg.data_dir / "other.json").write_text("{}")
    bot.accounts.allow(ADMIN)
    other = bot.accounts.connect(ADMIN, "Admin", ADDRESS.id, str(ADDRESS), session)
    checked = []

    def check(household):
        checked.append(household.id)
        if household.id == hh.id:
            raise KeyError("id")
        raise ShopError("the shop login has expired")

    monkeypatch.setattr(botmod.orders, "check", check)
    bot._health(ADMIN)
    assert sorted(checked) == sorted([hh.id, other.id])
    said(bot, "⚠️ Daily check of your shop account failed", chat=ADMIN)


def reads(bot, **request):
    bot.read = lambda *_: Request(**request)


def test_order_now_places_below_the_minimum(bot, shop):
    hh, _ = shop
    hh.ledger().set_basket([("bread", 1)])
    bot._on_message(message("/basket"))
    tap(bot, hh)
    said(bot, "✅ Ordered, ₹60")
    assert placed(hh) == [[["bread", 1]]]


def test_said_now_orders_at_once(bot, shop):
    hh, _ = shop
    reads(bot, add=[("bread", 1)], now=True)
    bot._on_message(message("bread abhi"))
    said(bot, "✅ Ordered, ₹60")
    assert placed(hh) == [[["bread", 1]]]


def test_said_now_orders_once_the_new_item_is_picked(bot, shop):
    hh, _ = shop
    reads(bot, add=[("jam", 1)], now=True)
    bot._on_message(message("jam now"))
    said(bot, 'Which "jam"')
    bot._on_button({"id": "q", "from": {"id": NEW}, "data": f"{next(iter(bot.picking))}:0:0"})
    said(bot, "✅ Ordered")
    assert placed(hh) == [[["jam", 1]]]


def test_changes_set_or_remove_basket_items(bot, shop):
    hh, _ = shop
    hh.ledger().set_basket([("bread", 2), ("milk", 1)])
    reads(bot, change=[("bread", 0), ("milk", 3), ("eggs", 1)])
    bot._on_message(message("no bread, 3 milk, 1 eggs"))
    said(bot, '"eggs" isn\'t in the basket.')
    sent(bot, "🧺 Your basket", having="Milk ×3: Milk 1 pc ×3")
    assert hh.ledger().basket() == [("milk", 3)]


def test_removing_the_last_item_ends_the_wait(bot, shop):
    hh, _ = shop
    bot._on_message(message("2 bread"))
    said(bot, "⏳ I'll order this")
    reads(bot, change=[("bread", 0)])
    bot._on_message(message("no bread"))
    said(bot, "The basket is empty.")
    assert (hh.ledger().hold(), bot.holds) == (None, {})


def test_a_named_saved_address_takes_this_basket_only(bot, shop):
    hh, fake = shop
    reads(bot, add=[("bread", 2)], address="work")
    bot._on_message(message("2 bread to work"))
    sent(bot, "⏳ I'll order this", having="Delivering to Work")
    tap(bot, hh)
    said(bot, "✅ Ordered to Work")
    assert (fake.placed_to, hh.ledger().address()) == (["addr-work"], None)


@pytest.mark.parametrize("address", ["office", "mummy's home", "home or work"])
def test_an_unknown_or_unclear_address_changes_nothing(bot, shop, address):
    hh, _ = shop
    reads(bot, add=[("bread", 2)], address=address)
    bot._on_message(message(f"2 bread to {address}"))
    said(bot, "Which saved address: Home, Work?")
    idle(bot)
    assert (hh.ledger().basket(), hh.ledger().address()) == ([], None)


def test_an_address_with_nothing_to_send_is_not_kept(bot, shop):
    hh, _ = shop
    reads(bot, address="work")
    bot._on_message(message("work pe bhejo"))
    said(bot, "The basket is empty; send the items with the address.")
    assert hh.ledger().address() is None


def test_the_address_goes_when_the_basket_empties(bot, shop):
    hh, _ = shop
    hh.ledger().set_basket([("bread", 1)])
    hh.ledger().set_address("addr-work", "Work")
    reads(bot, change=[("bread", 0)])
    bot._on_message(message("no bread"))
    said(bot, "The basket is empty.")
    assert hh.ledger().address() is None


def test_with_ordering_off_the_address_waits_with_the_basket(bot, shop):
    hh, fake = shop
    bot.accounts.update(hh.id, place_orders=False)
    reads(bot, add=[("bread", 2)], address="work")
    bot._on_message(message("2 bread to work"))
    sent(bot, "🧺 Your basket", having="Delivering to Work")
    tap(bot, hh)
    said(bot, "✅ Ordered to Work")
    assert fake.placed_to == ["addr-work"]


def test_an_address_goes_if_none_of_its_items_were_added(bot, shop):
    hh, _ = shop
    reads(bot, add=[("jam", 1)], address="work")
    bot._on_message(message("jam to work"))
    said(bot, 'Which "jam"')
    bot._on_button({"id": "q", "from": {"id": NEW}, "data": f"{next(iter(bot.picking))}:0:x"})
    said(bot, "Nothing left to order.")
    assert hh.ledger().address() is None


def test_an_address_with_clear_is_not_kept(bot, shop):
    hh, _ = shop
    hh.ledger().set_basket([("bread", 1)])
    reads(bot, clear_basket=True, address="work")
    bot._on_message(message("sab cancel, work pe bhejna"))
    said(bot, "Basket emptied.")
    idle(bot)
    assert hh.ledger().address() is None


def test_a_picked_item_goes_to_its_address(bot, shop):
    _, fake = shop
    reads(bot, add=[("jam", 1)], address="work", now=True)
    bot._on_message(message("jam to work abhi"))
    said(bot, 'Which "jam"')
    bot._on_button({"id": "q", "from": {"id": NEW}, "data": f"{next(iter(bot.picking))}:0:0"})
    said(bot, "✅ Ordered to Work")
    assert fake.placed_to == ["addr-work"]


def test_an_address_waits_for_its_items(bot, shop):
    hh, _ = shop
    reads(bot, add=[("jam", 1)], address="work")
    bot._on_message(message("jam to work"))
    said(bot, 'Which "jam"')
    reads(bot, add=[("milk", 2)])
    bot._on_message(message("2 milk"))
    said(bot, "⏳ I'll order this")
    assert hh.ledger().address() is None


def test_clear_drops_the_address(bot, shop):
    hh, _ = shop
    hh.ledger().set_address("addr-work", "Work")
    reads(bot, clear_basket=True)
    bot._on_message(message("cancel everything"))
    said(bot, "Basket emptied.")
    assert hh.ledger().address() is None


def test_show_basket(bot, shop):
    hh, _ = shop
    hh.ledger().set_basket([("bread", 1)])
    reads(bot, show_basket=True)
    bot._on_message(message("basket mein kya hai"))
    said(bot, "🧺 Your basket")


def test_settings_by_message_are_the_owners(bot, shop):
    hh, _ = shop
    reads(bot, settings=[("limit", "2000")])
    bot._on_message(message("limit 2000"))
    sent(bot, "Delivery:", having="Daily limit: ₹2000")
    bot.accounts.allow(ADMIN)
    bot.accounts.join(ADMIN, "Admin", bot.accounts.invite(hh.id))
    reads(bot, settings=[("limit", "5000")])
    bot._on_message(message("limit 5000", uid=ADMIN))
    said(bot, "Only Asha", chat=ADMIN)
    assert bot.accounts.get(hh.id).daily_cap == 2000


def test_chatter_in_a_group_gets_no_reply(bot, shop):
    reads(bot)
    bot._on_message(message("good morning") | {"chat": {"id": -5, "type": "group"}})
    idle(bot)
    assert not [c for c, _, _ in bot.tg.sent if c == -5]


def test_settings_apply_to_the_rest_of_the_message(bot, shop):
    reads(bot, settings=[("ordering", "off")], add=[("bread", 2)])
    bot._on_message(message("ordering off, 2 bread"))
    sent(bot, "🧺 Your basket", having="Ordering is off")


AMUL = [
    Product("Amul Cow Milk", s, f"https://shop.example/amul/{s}", p, family="amul")
    for s, p in (("500 ml", "₹36"), ("1 l", "₹70"))
]
DAIRY = Product("Mother Dairy Cow Milk", "1 l", "https://shop.example/dairy", "₹68", family="dairy")


def press(bot, start, label):
    """Taps the button labelled label on the message starting with start."""
    data = dict(said(bot, start))[label]
    message_id = next(n for n, (_, text, _) in enumerate(bot.tg.sent, 1) if text.startswith(start))
    bot._on_button(
        {"id": "q", "from": {"id": NEW}, "data": data, "message": {"chat": {"id": NEW}, "message_id": message_id}}
    )


def test_an_amount_is_made_up_from_the_sizes_on_sale(bot, shop):
    """28 Oct, 13:53 UTC: "2 litre cow milk" became 2 packs, of which the shop allowed 1."""
    hh, fake = shop
    fake.search = lambda query, limit=4: [AMUL[1], Product("Amul Taaza", "1 l", "https://shop.example/taaza"), AMUL[0]]
    fake.limits = {AMUL[1].label(): 1}
    reads(bot, add=[("cow milk", "2 l")])
    bot.tg.edit_text = lambda *_: None
    bot._on_message(message("2 litre cow milk"))
    assert said(bot, "Amul Cow Milk\n500 ml ₹36 · 1 l ₹70") == [("✅ Pick", f"{next(iter(bot.picking))}:0:0")]
    bot._pick(hh, f"{next(iter(bot.picking))}:0:0")
    sent(bot, "⏳ I'll order this", having="• Cow milk 2 l: Amul Cow Milk 1 l ×2 · ₹140")
    tap(bot, hh)
    sent(bot, "✅ Ordered", having="Cow milk 2 l: Amul Cow Milk 1 l ×1 + 500 ml ×2")
    assert fake.cart == {AMUL[0]: 2, AMUL[1]: 1}
    assert hh.catalog().choices("cow milk")[0].sizes[0].variant == AMUL[1].variant  # what the amount is mostly
    assert placed(hh) == [[["cow milk", "2 l"]]]


def test_sizes_new_to_the_household_are_used_without_asking(bot, shop):
    hh, fake = shop
    hh.catalog().learn("cow milk", Choice("Amul Cow Milk", [AMUL[1]]))
    hh.catalog().learn("cow milk", one(DAIRY))
    hh.catalog().learn("cow milk", Choice("Amul Cow Milk", [AMUL[1]]))
    fake.search = lambda query, limit=4: [*AMUL, DAIRY]
    fake.limits = {AMUL[1].label(): 1}
    reads(bot, add=[("cow milk", "2 l")])
    bot._on_message(message("2 litre cow milk"))
    said(bot, "⏳ I'll order this")
    tap(bot, hh)
    sent(bot, "✅ Ordered", having="Cow milk 2 l: Amul Cow Milk 1 l ×1 + 500 ml ×2")


def test_a_count_picks_its_usual_pack_on_the_card(bot, shop):
    hh, fake = shop
    fake.search = lambda query, limit=4: AMUL
    reads(bot, add=[("cow milk", 2)])
    bot.tg.edit_text = lambda *_: None
    bot._on_message(message("2 cow milk"))
    buttons = said(bot, "Amul Cow Milk")
    pid = next(iter(bot.picking))
    assert buttons == [("✅ 500 ml", f"{pid}:0:0.0"), ("✅ 1 l", f"{pid}:0:0.1")]
    bot._pick(hh, f"{pid}:0:0.1")
    sent(bot, "⏳ I'll order this", having="• Cow milk ×2: Amul Cow Milk 1 l ×2 · ₹140")


@pytest.mark.parametrize("answer", ["Yes", "No"])
def test_another_product_is_added_only_when_they_say_so(bot, shop, answer):
    hh, fake = shop
    hh.catalog().learn("cow milk", one(DAIRY))
    hh.catalog().learn("cow milk", Choice("Amul Cow Milk", [AMUL[1]]))
    fake.search = lambda query, limit=4: []
    fake.limits = {AMUL[1].label(): 1}
    reads(bot, add=[("cow milk", "2 l")])
    bot._on_message(message("2 litre cow milk"))
    said(bot, "⏳ I'll order this")
    tap(bot, hh)
    press(bot, "❓ Amul Cow Milk has only 1 l today. Add Mother Dairy Cow Milk 1 l ×1?", answer)
    if answer == "Yes":
        sent(bot, "✅ Ordered", having="Cow milk 2 l: Amul Cow Milk 1 l ×1 + Mother Dairy Cow Milk 1 l ×1")
    else:
        sent(bot, "✅ Ordered", having="Cow milk 2 l: Amul Cow Milk 1 l ×1 · ₹60  (1 l of 2 l: all the shop allows)")
    assert fake.orders == 1


def test_far_more_than_usual_is_asked_first(bot, shop):
    hh, _ = shop
    hh.catalog().learn("cow milk", Choice("Amul Cow Milk", [AMUL[1]]))
    hh.ledger().finish(hh.ledger().begin([("cow milk", "2 l")], 140), "placed")
    reads(bot, add=[("cow milk", "20 l"), ("bread", 1)])
    bot._on_message(message("20 litre cow milk, bread"))
    press(bot, "❓ cow milk 20 l? You usually get 2 l.", "2 l")
    press(bot, "❓ cow milk 20 l? You usually get 2 l.", "Yes, 20 l")  # answered already
    sent(bot, "⏳ I'll order this", having="• Bread ×1: Bread 1 pc ×1 · ₹60\n• Cow milk 2 l: Amul Cow Milk 1 l ×2")
    assert hh.ledger().basket() == [("bread", 1), ("cow milk", "2 l")]


def test_the_question_keeps_the_address_and_now_for_one_order(bot, shop):
    hh, fake = shop
    hh.catalog().learn("cow milk", Choice("Amul Cow Milk", [AMUL[1]]))
    bot.accounts.update(hh.id, min_order=0)
    reads(bot, add=[("cow milk", "20 l"), ("bread", 1)], now=True, address="work")
    bot._on_message(message("20 litre cow milk, bread to work abhi"))
    press(bot, "❓ cow milk 20 l? That's a lot for a first order.", "1 l")
    said(bot, "✅ Ordered to Work")
    assert fake.placed_to == ["addr-work"]


def test_a_new_item_or_a_change_far_over_one_pack_is_asked(bot, shop):
    hh, _ = shop
    reads(bot, add=[("jam", 20)])
    bot.tg.edit_text = lambda *_: None
    bot._on_message(message("20 jam"))
    said(bot, "Jam 1")
    bot._pick(hh, f"{next(iter(bot.picking))}:0:0")
    press(bot, "❓ jam ×20? That's a lot for a first order.", "Skip")
    said(bot, "Nothing left to order.")
    hh.ledger().set_basket([("bread", 1)])
    reads(bot, change=[("bread", 30)])
    bot._on_message(message("make it 30 bread"))
    press(bot, "❓ bread ×30? That's a lot for a first order.", "Yes, ×30")
    sent(bot, "🧺 Your basket", having="Bread ×30")
    assert hh.ledger().basket() == [("bread", 30)]


def test_with_ordering_off_an_answer_still_orders(bot, shop):
    hh, fake = shop
    bot.accounts.update(hh.id, place_orders=False)
    hh.catalog().learn("cow milk", one(DAIRY))
    hh.catalog().learn("cow milk", Choice("Amul Cow Milk", [AMUL[1]]))
    fake.search = lambda query, limit=4: []
    fake.limits = {AMUL[1].label(): 1}
    reads(bot, add=[("cow milk", "2 l")])
    bot._on_message(message("2 litre cow milk"))
    said(bot, "🧺 Your basket")
    tap(bot, hh)
    press(bot, "❓ Amul Cow Milk has only 1 l today.", "Yes")
    said(bot, "✅ Ordered")
    assert {p.variant: n for p, n in fake.cart.items()} == {AMUL[1].variant: 1, DAIRY.variant: 1}
    assert hh.ledger().basket() == [] and hh.ledger().others() == {}


def test_a_pick_saved_before_28_oct_in_two_sizes_is_one_product(bot, shop):
    hh, fake = shop
    store.write(hh.dir / "catalog.json", {"cow milk": {"aliases": [], "options": [vars(p) for p in reversed(AMUL)]}})
    fake.search = lambda query, limit=4: []
    fake.limits = {AMUL[1].label(): 1, AMUL[0].label(): 1}
    reads(bot, add=[("cow milk", "2 l")])
    bot._on_message(message("2 litre cow milk"))
    said(bot, "⏳ I'll order this")
    tap(bot, hh)
    sent(bot, "✅ Ordered", having="(1.5 l of 2 l: all the shop allows)")


def test_two_items_on_one_product_add_up_in_the_cart(bot, shop):
    hh, fake = shop
    hh.catalog().learn("cow milk", Choice("Amul Cow Milk", AMUL))
    hh.catalog().learn("dahi milk", Choice("Amul Cow Milk", [dataclasses.replace(AMUL[0], price="₹35")]))
    fake.search = lambda query, limit=4: []
    bot.accounts.update(hh.id, min_order=50)
    reads(bot, add=[("cow milk", "500 ml"), ("dahi milk", 1)])
    bot._on_message(message("500 ml cow milk, dahi milk"))
    said(bot, "⏳ I'll order this")
    tap(bot, hh)
    said(bot, "✅ Ordered")
    assert {p.variant: n for p, n in fake.cart.items()} == {AMUL[0].variant: 2}


def test_a_change_and_new_items_in_one_message_are_answered_together(bot, shop):
    hh, _ = shop
    hh.ledger().set_basket([("bread", 1), ("milk", 1)])
    reads(bot, change=[("bread", 30)], add=[("jam", 1)])
    bot.tg.edit_text = lambda *_: None
    bot._on_message(message("make it 30 bread, and jam"))
    said(bot, "Jam 1")
    press(bot, "❓ bread ×30? That's a lot for a first order.", "×1")
    bot._pick(hh, f"{next(iter(bot.picking))}:0:0")
    sent(bot, "⏳ I'll order this", having="• Bread ×1")
    assert hh.ledger().basket() == [("bread", 1), ("milk", 1), ("jam", 1)]


def test_a_newer_list_joins_the_open_one_and_keeps_its_address(bot, shop):
    hh, fake = shop
    hh.catalog().learn("cow milk", Choice("Amul Cow Milk", [AMUL[1]]))
    bot.accounts.update(hh.id, min_order=0)
    reads(bot, add=[("cow milk", "20 l")], address="work", now=True)
    bot._on_message(message("20 litre cow milk to work abhi"))
    said(bot, "❓ cow milk 20 l?")
    reads(bot, add=[("jam", 1), ("bread", 1)])
    bot.tg.edit_text = lambda *_: None
    bot._on_message(message("jam"))
    said(bot, "Jam 1")
    [(pid, picking)] = bot.picking.items()
    bot._pick(hh, f"{pid}:{next(iter(picking.asks))}:0")
    press(bot, "❓ cow milk 20 l?", "1 l")
    said(bot, "✅ Ordered to Work")
    assert fake.orders == 1 and fake.placed_to == ["addr-work"]
    assert hh.ledger().address() is None and placed(hh) == [[["bread", 1], ["jam", 1], ["cow milk", "1 l"]]]


def test_a_far_over_change_with_now_waits_for_the_answer(bot, shop):
    hh, _ = shop
    hh.ledger().set_basket([("bread", 1), ("milk", 1)])
    reads(bot, change=[("bread", 30)], now=True)
    bot._on_message(message("make it 30 bread abhi"))
    press(bot, "❓ bread ×30?", "×1")
    said(bot, "✅ Ordered")
    assert placed(hh) == [[["bread", 1], ["milk", 1]]]


def test_ordering_on_again_keeps_the_basket(bot, shop):
    hh, _ = shop
    hh.ledger().set_basket([("bread", 1)])
    bot._on_message(message("/ordering on"))
    said(bot, "Delivery:")
    assert hh.ledger().basket() == [("bread", 1)]


def test_a_pick_open_at_the_28_oct_deploy_survives_it(bot, shop):
    hh, _ = shop
    jam = vars(Product("Jam", "1 pc", "https://shop.example/jam", "₹60"))
    old = {"chat_id": NEW, "household": hh.id, "ready": [["bread", 1]], "asks": {"0": ["jam", 1, [jam], [None]]},
           "picked": [], "started": time.time(), "when": "hold", "address": []}  # fmt: skip
    store.write(bot.saved, {"bot": "token", "offset": 9, "at": time.time(), "picking": {"p1": old}})
    new = restarted(bot)
    new._pick(hh, "p1:0:0")
    sent(new, "⏳ I'll order this", having="• Jam ×1")
    assert placed(hh) == [] and hh.ledger().basket() == [("bread", 1), ("jam", 1)]


def test_an_old_question_cannot_answer_a_new_one(bot, shop):
    hh, fake = shop
    hh.catalog().learn("cow milk", one(DAIRY))
    hh.catalog().learn("cow milk", Choice("Amul Cow Milk", [AMUL[1]]))
    fake.search = lambda query, limit=4: []
    fake.limits = {AMUL[1].label(): 1}
    reads(bot, add=[("cow milk", "2 l")], now=True)
    for _ in range(2):  # asked again: the first question's buttons go
        bot._on_message(message("2 litre cow milk abhi"))
        idle(bot)
    first, second = (n for n, (_, t, _) in enumerate(bot.tg.sent, 1) if t.startswith("❓"))
    assert (NEW, first) in bot.tg.unbuttoned
    tap_more = {"id": "q", "from": {"id": NEW}, "data": f"more:{hh.id}:y", "message": {"chat": {"id": NEW}}}
    bot._on_button(tap_more | {"message": {"chat": {"id": NEW}, "message_id": first}})
    idle(bot)
    assert fake.orders == 0
    bot._on_button(tap_more | {"message": {"chat": {"id": NEW}, "message_id": second}})
    said(bot, "✅ Ordered")


def test_a_pick_left_open_too_long_holds_nothing_back(bot, shop, monkeypatch):
    hh, _ = shop
    bot.tg.edit_text = lambda *_: None
    reads(bot, add=[("jam", 1)])
    bot._on_message(message("jam"))
    said(bot, "Jam 1")
    [(old, picking)] = bot.picking.items()
    picking.started -= botmod.PICK_WAIT
    reads(bot, add=[("tea", 1)])
    bot._on_message(message("tea"))
    said(bot, "Tea 1")
    [(pid, _)] = bot.picking.items()
    assert pid != old
    bot._pick(hh, f"{pid}:0:0")
    sent(bot, "🧺 Your basket", having="• Tea ×1")


def test_a_stale_tap_never_answers_a_later_item(bot, shop):
    hh, _ = shop
    bot.tg.edit_text = lambda *_: None
    reads(bot, add=[("jam", 1), ("egg", 1)])
    bot._on_message(message("jam, egg"))
    said(bot, "Egg 1")
    [(pid, _)] = bot.picking.items()
    bot._pick(hh, f"{pid}:1:0")
    reads(bot, add=[("tea", 1)])
    bot._on_message(message("tea"))
    said(bot, "Tea 1")
    bot._pick(hh, f"{pid}:1:1")  # the egg card again
    assert [name for name, *_ in bot.picking[pid].asks.values()] == ["jam", "tea"]


@pytest.mark.parametrize("owner", [True, False])
def test_an_invite_to_another_account_asks_before_moving_anyone(bot, shop, owner):
    hh, _ = shop
    (session := bot.cfg.data_dir / "other.json").write_text("{}")
    other = bot.accounts.connect(ADMIN, "Meera", ADDRESS.id, str(ADDRESS), session)
    bot.accounts.join(7, "Ravi", bot.accounts.invite(hh.id))
    uid = NEW if owner else 7
    code = bot.accounts.invite(other.id)
    bot._on_message(message(f"/start {code}", uid=uid))
    text = sent(bot, "This link moves you", chat=uid)
    assert ("logs it out" in text) is owner
    assert bot.accounts.household_of(uid).id == hh.id
    bot._on_button(
        {
            "id": "q",
            "from": {"id": uid, "first_name": "Asha"},
            "data": f"join:{code}",
            "message": {"chat": {"id": uid}, "message_id": 3},
        }
    )
    sent(bot, "🎉 Welcome! You now order with Meera's", chat=uid)
    assert bot.accounts.household_of(uid).id == other.id


def test_an_invite_to_your_own_account_or_a_dead_one_needs_no_confirming(bot, shop):
    hh, _ = shop
    bot.accounts.join(7, "Ravi", bot.accounts.invite(hh.id))
    bot._on_message(message(f"/start {bot.accounts.invite(hh.id)}", uid=7))
    sent(bot, "🎉 Welcome!", chat=7)
    bot._on_message(message("/start NOSUCH", uid=7))
    sent(bot, "That invite link has expired", chat=7)


def test_another_bots_offset_and_picks_are_not_used(bot):
    bot.offset = 5
    bot._save()
    assert restarted(bot).offset == 5
    store.write(bot.saved, store.read(bot.saved, {}) | {"offset": 9, "bot": "123"})
    assert (restarted(bot).offset, restarted(bot).picking) == (None, {})


def test_a_switch_button_in_a_group_moves_nobody(bot, shop):
    hh, _ = shop
    (session := bot.cfg.data_dir / "other.json").write_text("{}")
    other = bot.accounts.connect(ADMIN, "Meera", ADDRESS.id, str(ADDRESS), session)
    code = bot.accounts.invite(other.id)
    bot.accounts.join(7, "Ravi", bot.accounts.invite(hh.id))
    group = {"chat": {"id": -5}, "message_id": 3}
    bot._on_button({"id": "q", "from": {"id": 7, "first_name": "Ravi"}, "data": f"join:{code}", "message": group})
    assert bot.accounts.household_of(7).id == hh.id


def test_ordering_turned_off_during_a_wait_asks_instead_of_ordering(bot, shop, monkeypatch):
    monkeypatch.setattr(botmod, "HOLD", 0.2)
    hh, fake = shop
    reads(bot, add=[("bread", 2)])
    bot._on_message(message("2 bread"))
    said(bot, "⏳ I'll order this")
    bot.accounts.update(hh.id, place_orders=False)
    sent(bot, "🧺 Your basket", having="Ordering is off")
    assert (fake.opened, placed(hh)) == (0, [])
