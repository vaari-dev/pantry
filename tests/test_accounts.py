from datetime import datetime, timedelta

import pytest

from grocerybot import store
from grocerybot.accounts import Accounts, address_parts

ADMIN, ASHA, RAVI, MEERA = 1, 2, 3, 4


@pytest.fixture
def accounts(tmp_path):
    return Accounts(tmp_path, ADMIN)


def login(tmp_path, name="s.json"):
    (tmp_path / name).write_text("{}")
    return tmp_path / name


def test_only_admin_is_allowed_at_first(accounts):
    assert accounts.all() == []
    assert accounts.allowed(ADMIN) and not accounts.allowed(ASHA)


def test_setup_creates_own_household_then_logs_it_in_again(tmp_path, accounts):
    accounts.allow(ASHA)
    assert accounts.allowed(ASHA) and accounts.household_of(ASHA) is None
    hh = accounts.connect(ASHA, "Asha", "zepto", "Home", "12B", login(tmp_path), ordering=True)
    assert (hh.owner, hh.members, hh.state_file.exists()) == (ASHA, (ASHA,), True)
    hh.ledger().set_basket([("bread", 1)])
    again = accounts.connect(ASHA, "Asha", "zepto", "Work", "Tower 3", login(tmp_path), ordering=True)
    assert again.id == hh.id and again.address_label == "Work"
    assert again.ledger().basket() == [("bread", 1)]  # picks and history are kept


def test_invite_is_single_use_and_expires(tmp_path, accounts):
    hh = accounts.connect(ASHA, "Asha", "zepto", "Home", "12B", login(tmp_path), ordering=True)
    code = accounts.invite(hh.id)
    assert not accounts.allowed(RAVI)
    assert accounts.join(RAVI, "Ravi", code.lower()).members == (ASHA, RAVI)
    assert accounts.allowed(RAVI)
    assert accounts.join(MEERA, "Meera", code) is None
    old = accounts.invite(hh.id)
    data = store.read(accounts.path, {})
    data["invites"][old]["expires"] = (datetime.now() - timedelta(minutes=1)).isoformat()
    store.write(accounts.path, data)
    assert accounts.join(MEERA, "Meera", old) is None


def test_owner_leaving_hands_over_and_last_one_out_deletes(tmp_path, accounts):
    hh = accounts.connect(ASHA, "Asha", "zepto", "Home", "12B", login(tmp_path), ordering=True)
    accounts.join(RAVI, "Ravi", accounts.invite(hh.id))
    accounts.leave(ASHA, hh.id)
    assert accounts.get(hh.id).owner == RAVI
    assert not hh.state_file.exists()  # Asha's shop login leaves with her
    assert accounts.household_of(ASHA) is None and accounts.allowed(ASHA)
    accounts.leave(RAVI, hh.id)
    assert accounts.get(hh.id) is None and not hh.dir.exists()


def test_member_running_setup_gets_their_own_account(tmp_path, accounts):
    family = accounts.connect(ASHA, "Asha", "zepto", "Home", "12B", login(tmp_path), ordering=True)
    accounts.join(RAVI, "Ravi", accounts.invite(family.id))
    own = accounts.connect(RAVI, "Ravi", "zepto", "Flat", "4A", login(tmp_path), ordering=True)
    assert own.id != family.id and own.owner == RAVI
    assert accounts.get(family.id).members == (ASHA,)


def test_settings_are_per_household_with_safe_defaults(tmp_path, accounts):
    asha = accounts.connect(ASHA, "Asha", "zepto", "Home", "12B", login(tmp_path), ordering=True)
    ravi = accounts.connect(RAVI, "Ravi", "zepto", "Flat", "4A", login(tmp_path), ordering=True)
    assert (asha.daily_cap, asha.min_order, asha.place_orders) == (1000, 100, True)
    accounts.update(asha.id, daily_cap=1500, min_order=99, place_orders=False)
    asha, ravi = accounts.get(asha.id), accounts.get(ravi.id)
    assert (asha.daily_cap, asha.min_order, asha.place_orders) == (1500, 99, False)
    assert (ravi.daily_cap, ravi.min_order, ravi.place_orders) == (1000, 100, True)
    with pytest.raises(ValueError):
        accounts.update(asha.id, owner=RAVI)  # only settings


def test_leave_is_scoped_and_own_invite_is_harmless(tmp_path, accounts):
    hh = accounts.connect(ASHA, "Asha", "zepto", "Home", "12B", login(tmp_path), ordering=True)
    accounts.join(RAVI, "Ravi", accounts.invite(hh.id))
    assert accounts.join(ASHA, "Asha", accounts.invite(hh.id)).owner == ASHA
    accounts.leave(RAVI, "elsewhere")
    assert accounts.get(hh.id).members == (ASHA, RAVI)


@pytest.mark.parametrize(
    "text, parts",
    [
        ("Primanti\nT6/503, Prestige Road, Bengaluru", ("Primanti", "T6/503")),
        ("Home - 12B, MG Road", ("Home", "12B")),
        ("Work, Tower 3, Outer Ring Road", ("Work", "Tower 3")),
        ("Home", ("Home", "Home")),
    ],
)
def test_address_parts(text, parts):
    assert address_parts(text) == parts


def test_zepto_stays_where_a_household_from_before_blinkit_had_it(tmp_path):
    """So the version before Blinkit, rolled back to, reads its login, picks and settings as they now are."""
    old = {"owner": ASHA, "address_label": "Home", "address_match": "12B", "daily_cap": 800.0, "min_order": 99.0,
           "place_orders": True}  # fmt: skip
    users = {str(ASHA): {"name": "Asha", "household": "h1"}, str(RAVI): {"name": "Ravi", "household": "h1"}}
    store.write(tmp_path / "households.json", {"households": {"h1": old}, "users": users})
    (folder := tmp_path / "households" / "h1").mkdir(parents=True)
    (folder / "session.json").write_text("{}")
    (folder / "catalog.json").write_text('{"egg": {"options": []}}')

    accounts = Accounts(tmp_path, ADMIN)
    hh = accounts.get("h1")
    assert (hh.shop, hh.shops, hh.address_label, hh.address_match, hh.min_order, hh.daily_cap) == (
        "zepto", ("zepto",), "Home", "12B", 99.0, 800.0
    )  # fmt: skip
    assert hh.state_file == folder / "session.json" and hh.catalog().path == folder / "catalog.json"
    accounts.update("h1", place_orders=False)
    accounts.connect(ASHA, "Asha", "blinkit", "Flat", "4A", login(tmp_path), ordering=False)
    assert {k: store.read(accounts.path, {})["households"]["h1"][k] for k in old} == {**old, "place_orders": False}
    accounts.leave(ASHA, "h1")
    assert not (folder / "session.json").exists() and not (folder / "blinkit" / "session.json").exists()


def test_each_shop_keeps_its_own_settings(tmp_path, accounts):
    hh = accounts.connect(ASHA, "Asha", "zepto", "Home", "12B", login(tmp_path), ordering=True)
    accounts.update(hh.id, min_order=99.0, daily_cap=700.0)
    hh = accounts.connect(ASHA, "Asha", "blinkit", "Flat", "12B", login(tmp_path), ordering=False)
    assert (hh.shop, hh.shops, hh.address_label, hh.min_order, hh.place_orders, hh.daily_cap) == (
        "blinkit",
        ("zepto", "blinkit"),
        "Flat",
        100.0,
        False,
        700.0,
    )
    hh = accounts.switch(hh.id, "zepto")
    assert (hh.address_label, hh.min_order, hh.place_orders) == ("Home", 99.0, True)
    with pytest.raises(ValueError, match="shop"):
        accounts.update(hh.id, shop="blinkit")
