import contextlib
import dataclasses
import threading
import time
from datetime import datetime, timedelta

import pytest

from grocerybot import bot as botmod
from grocerybot.catalog import Product
from grocerybot.config import Config
from grocerybot.ledger import Ledger
from grocerybot.shop import Address, Cart, ShopError

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
    monkeypatch.setattr(botmod.orders, "login", FakeLogin)
    monkeypatch.setattr(botmod.orders, "shop", lambda *_: FakeAddresses())
    monkeypatch.setattr(botmod.signal, "signal", lambda *_: None)  # run() must not take pytest's SIGTERM
    cfg = Config("https://shop.example", "token", ADMIN, "small", tmp_path, 9)
    b = botmod.Bot(cfg)
    b.tg = FakeTelegram()
    botmod.threading.Thread(target=b._worker, daemon=True).start()
    yield b
    for timer in b.holds.values():
        timer.cancel()


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
    product = botmod.Product("Eggs 🥚", "1 pack", "/pn/eggs/pvid/1", "₹89", mrp="₹104")
    bot.tg.send = lambda chat_id, text, buttons=None, markup=None, entities=None: bot.tg.sent.append(
        (chat_id, text, entities)
    )
    bot._offer(NEW, product, "1:0:0")

    _, text, entities = bot.tg.sent[-1]
    assert text == "Eggs 🥚\nSize: 1 pack  ·  ₹89 ₹104"
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
        self.limits: dict[str, int] = {}  # product name -> the most one order may have
        self.cart: dict = {}
        self.orders = 0

    def __enter__(self):
        self.opened += 1
        return self

    def __exit__(self, *_): ...

    def deliver_to(self, address_id):
        if self.fail:
            raise self.fail

    def stock(self, product):
        return self.why if product.name in self.gone else self.limits.get(product.name, 9)

    def fill(self, items):
        self.cart = dict(items)

    def review(self, expected, rules):
        assert expected == self.cart
        total = self.unit * sum(expected.values())
        prices = dict.fromkeys(expected, f"₹{self.unit}")
        problems = ["over today's limit"] if total > rules.budget else []
        return Cart(total, total, problems=problems, prices=prices)

    def latest_order(self):
        return self.orders

    def place(self, rules, cod):
        self.orders += 1
        return self.orders

    def confirm(self, before, placed):
        return self.orders == placed

    def search(self, query):
        return [Product(f"{query.title()} {n}", "1 pc", f"https://shop.example/{query}/{n}", "₹60") for n in (1, 2)]


@pytest.fixture
def shop(bot, monkeypatch):
    (session := bot.cfg.data_dir / "state.json").write_text("{}")
    hh = bot.accounts.connect(NEW, "Asha", ADDRESS.id, str(ADDRESS), session)
    bot.accounts.allow(NEW)
    for item in ("bread", "milk"):
        hh.catalog().learn(item, Product(item.title(), "1 pc", f"https://shop.example/{item}", "₹60"))
    fake = FakeShop()
    monkeypatch.setattr(botmod.orders, "shop", lambda *_: fake)
    return hh, fake


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


def test_search_drops_leading_words_when_nothing_matches():
    class Shop:
        def search(self, query):
            return [query] if query == "avocado" else []

    assert botmod._search(Shop(), "harsh avocado") == ("avocado", ["avocado"])
    assert botmod._search(Shop(), "eggs") == ("eggs", [])


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
        bot._queue(bot._handle_list, NEW, shop[0], "2 bread")
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
    hh.catalog().learn("bread", Product("Bread", "400 g", "https://shop.example/bread", "₹45"))
    hh.catalog().learn("milk", Product("Milk", "1 pc", "https://shop.example/milk/2"))  # no price seen
    hh.ledger().set_basket([("bread", 2), ("milk", 1)])
    bot._on_message(message("/basket"))
    assert said(bot, "🧺 Your basket") == botmod._order_now(hh)
    text = next(t for _, t, _ in bot.tg.sent if t.startswith("🧺 Your basket"))
    assert text.splitlines() == [
        "🧺 Your basket",
        "• Bread (400 g) ×2  ₹90",
        "• Milk (1 pc) ×1",
        "",
        "Total ₹90 + 1 without a price",
    ]
    assert hh.ledger().offer()  # the button is the live Order now


def test_an_empty_basket_has_no_button(bot, shop):
    bot._on_message(message("/basket"))
    assert said(bot, "The basket is empty.") is None


def test_basket_shows_what_the_minimum_needs(bot, shop):
    hh, _ = shop
    hh.catalog().learn("bread", Product("Bread", "400 g", "https://shop.example/bread", "₹45"))
    hh.ledger().set_basket([("bread", 1)])
    bot._on_message(message("/basket"))
    said(bot, "🧺 Your basket\n• Bread (400 g) ×1  ₹45\n\nTotal ₹45 (orders start at ₹100)")


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


def test_the_shops_lower_total_waits_and_corrects_the_prices(bot, shop):
    hh, fake = shop
    fake.unit = 45
    bot._on_message(message("bread, milk"))
    sent(bot, "⏳ I'll order this", having="Total ₹120")
    tap(bot, hh)
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
    fake.limits = {"Bread": 2}
    bot._on_message(message("3 bread, milk"))
    said(bot, "⏳ I'll order this")
    tap(bot, hh)
    sent(bot, "✅ Ordered", having="Bread (1 pc) ×2  (the most the shop allows, of 3)")
    assert placed(hh) == [[["bread", 2], ["milk", 1]]]


def test_with_ordering_off_every_message_fills_the_shop_cart(bot, shop):
    hh, fake = shop
    bot.accounts.update(hh.id, place_orders=False)
    bot._on_message(message("2 bread"))
    said(bot, "🛒 Cart ready (₹120)")
    assert fake.opened == 1


def test_one_accounts_failed_check_does_not_skip_the_others(bot, shop, monkeypatch):
    hh, _ = shop
    (session := bot.cfg.data_dir / "other.json").write_text("{}")
    bot.accounts.allow(ADMIN)
    other = bot.accounts.connect(ADMIN, "Admin", ADDRESS.id, str(ADDRESS), session)
    checked = []

    def check(cfg, household):
        checked.append(household.id)
        if household.id == hh.id:
            raise KeyError("id")
        raise ShopError("the shop login has expired")

    monkeypatch.setattr(botmod.orders, "check", check)
    bot._health(ADMIN)
    assert sorted(checked) == sorted([hh.id, other.id])
    said(bot, "⚠️ Daily check of your shop account failed", chat=ADMIN)
