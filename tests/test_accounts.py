from datetime import datetime, timedelta

import pytest

from grocerybot import store
from grocerybot.accounts import Accounts

ADMIN, ASHA, RAVI, MEERA = 1, 2, 3, 4


@pytest.fixture
def accounts(tmp_path):
    return Accounts(tmp_path, (ADMIN,))


def login(tmp_path, name="s.json"):
    (tmp_path / name).write_text("{}")
    return tmp_path / name


def test_bootstrap_moves_single_account_files_into_home(tmp_path):
    for name in ("session.json", "catalog.json", "ledger.json"):
        (tmp_path / name).write_text("{}")
    accounts = Accounts(tmp_path, (ADMIN, ASHA))
    accounts.bootstrap(tmp_path, "Home", "12B", 1000)
    home = accounts.household_of(ASHA)
    assert (home.id, home.owner, home.members, home.address_label) == ("home", ADMIN, (ADMIN, ASHA), "Home")
    assert home.logged_in and not (tmp_path / "session.json").exists()
    accounts.bootstrap(tmp_path, "Elsewhere", "9", 1)  # only ever once
    assert accounts.get("home").address_label == "Home"


def test_bootstrap_without_address_starts_empty(tmp_path, accounts):
    accounts.bootstrap(tmp_path, "", "", 1000)
    assert accounts.all() == []
    assert accounts.allowed(ADMIN) and not accounts.allowed(ASHA)


def test_setup_creates_own_household_then_logs_it_in_again(tmp_path, accounts):
    accounts.allow(ASHA)
    assert accounts.allowed(ASHA) and accounts.household_of(ASHA) is None
    hh = accounts.connect(ASHA, "Asha", "Home", "12B", login(tmp_path), 1000)
    assert (hh.owner, hh.members, hh.logged_in) == (ASHA, (ASHA,), True)
    hh.ledger().set_basket([("bread", 1)])
    again = accounts.connect(ASHA, "Asha", "Work", "Tower 3", login(tmp_path), 1000)
    assert again.id == hh.id and again.address_label == "Work"
    assert again.ledger().basket() == [("bread", 1)]  # picks and history are kept


def test_invite_is_single_use_and_expires(tmp_path, accounts):
    hh = accounts.connect(ASHA, "Asha", "Home", "12B", login(tmp_path), 1000)
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
    hh = accounts.connect(ASHA, "Asha", "Home", "12B", login(tmp_path), 1000)
    accounts.join(RAVI, "Ravi", accounts.invite(hh.id))
    accounts.leave(ASHA)
    assert accounts.get(hh.id).owner == RAVI
    assert accounts.household_of(ASHA) is None and accounts.allowed(ASHA)
    accounts.leave(RAVI)
    assert accounts.get(hh.id) is None and not hh.dir.exists()


def test_member_running_setup_gets_their_own_account(tmp_path, accounts):
    family = accounts.connect(ASHA, "Asha", "Home", "12B", login(tmp_path), 1000)
    accounts.join(RAVI, "Ravi", accounts.invite(family.id))
    own = accounts.connect(RAVI, "Ravi", "Flat", "4A", login(tmp_path), 500)
    assert own.id != family.id and own.owner == RAVI and own.daily_cap == 500
    assert accounts.get(family.id).members == (ASHA,)


def test_daily_cap_per_household(tmp_path, accounts):
    hh = accounts.connect(ASHA, "Asha", "Home", "12B", login(tmp_path), 1000)
    accounts.set_cap(hh.id, 1500)
    assert accounts.get(hh.id).daily_cap == 1500
