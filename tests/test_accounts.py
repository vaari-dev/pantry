from datetime import datetime, timedelta

import pytest

from pantry import store
from pantry.accounts import Accounts

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
    hh = accounts.connect(ASHA, "Asha", "addr-home", "Home: 12B", login(tmp_path))
    assert (hh.owner, hh.members, hh.state_file.exists()) == (ASHA, (ASHA,), True)
    hh.ledger().set_basket([("bread", 1)])
    again = accounts.connect(ASHA, "Asha", "addr-work", "Work: Tower 3", login(tmp_path))
    assert again.id == hh.id and (again.address_id, again.address) == ("addr-work", "Work: Tower 3")
    assert again.ledger().basket() == [("bread", 1)]  # picks and history are kept


def test_invite_is_single_use_and_expires(tmp_path, accounts):
    hh = accounts.connect(ASHA, "Asha", "addr-home", "Home: 12B", login(tmp_path))
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
    hh = accounts.connect(ASHA, "Asha", "addr-home", "Home: 12B", login(tmp_path))
    accounts.join(RAVI, "Ravi", accounts.invite(hh.id))
    accounts.leave(ASHA, hh.id)
    assert accounts.get(hh.id).owner == RAVI
    assert not hh.state_file.exists()  # Asha's shop login leaves with her
    assert accounts.household_of(ASHA) is None and accounts.allowed(ASHA)
    accounts.leave(RAVI, hh.id)
    assert accounts.get(hh.id) is None and not hh.dir.exists()


def test_member_running_setup_gets_their_own_account(tmp_path, accounts):
    family = accounts.connect(ASHA, "Asha", "addr-home", "Home: 12B", login(tmp_path))
    accounts.join(RAVI, "Ravi", accounts.invite(family.id))
    own = accounts.connect(RAVI, "Ravi", "addr-flat", "Flat: 4A", login(tmp_path))
    assert own.id != family.id and own.owner == RAVI
    assert accounts.get(family.id).members == (ASHA,)


def test_settings_are_per_household_with_safe_defaults(tmp_path, accounts):
    asha = accounts.connect(ASHA, "Asha", "addr-home", "Home: 12B", login(tmp_path))
    ravi = accounts.connect(RAVI, "Ravi", "addr-flat", "Flat: 4A", login(tmp_path))
    assert (asha.daily_cap, asha.min_order, asha.place_orders) == (1000, 100, True)
    accounts.update(asha.id, daily_cap=1500, min_order=99, place_orders=False)
    asha, ravi = accounts.get(asha.id), accounts.get(ravi.id)
    assert (asha.daily_cap, asha.min_order, asha.place_orders) == (1500, 99, False)
    assert (ravi.daily_cap, ravi.min_order, ravi.place_orders) == (1000, 100, True)
    with pytest.raises(ValueError):
        accounts.update(asha.id, owner=RAVI)  # only settings


def test_leave_is_scoped_and_own_invite_is_harmless(tmp_path, accounts):
    hh = accounts.connect(ASHA, "Asha", "addr-home", "Home: 12B", login(tmp_path))
    accounts.join(RAVI, "Ravi", accounts.invite(hh.id))
    assert accounts.join(ASHA, "Asha", accounts.invite(hh.id)).owner == ASHA
    accounts.leave(RAVI, "elsewhere")
    assert accounts.get(hh.id).members == (ASHA, RAVI)
