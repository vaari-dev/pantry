import pytest

from pantry.moved import announce
from pantry.telegram import Telegram, TelegramError


class Fake(Telegram):
    def __init__(self, username: str = "", fails: tuple = ()):
        super().__init__("token")
        self.username, self.fails, self.calls = username, fails, []

    def call(self, method: str, http_timeout: float = 40, **params):
        self.calls.append((method, params.get("chat_id")))
        if method in self.fails or params.get("chat_id") in self.fails:
            raise TelegramError(f"{method}: Forbidden")
        return {"getMe": {"username": self.username}, "sendMessage": {"message_id": 1}}.get(method, True)


def test_everyone_is_told_once_and_the_old_bot_points_to_the_new_one():
    old, new = Fake("joey_butler_bot", fails=(3,)), Fake("pantry_home_bot")
    assert announce(old, new, [1, 2, 3, 1]) == "moved to @pantry_home_bot; told 2 of 3"
    assert [c for m, c in old.calls if m == "sendMessage"] == [1, 2, 3]
    assert {"setMyDescription", "setMyShortDescription", "deleteMyCommands"} <= {m for m, _ in old.calls}


def test_a_bad_new_token_stops_the_move_before_the_old_bot_changes():
    old = Fake("joey_butler_bot")
    with pytest.raises(TelegramError):
        announce(old, Fake(fails=("getMe",)), [1])
    assert old.calls == []


def test_an_unreachable_old_bot_does_not_stop_the_move():
    old = Fake(fails=("getMe",))
    assert "nobody was told" in announce(old, Fake("pantry_home_bot"), [1])
    assert old.calls == [("getMe", None)]


def test_a_refused_profile_change_still_tells_everyone():
    old = Fake("joey_butler_bot", fails=("setMyShortDescription",))
    assert announce(old, Fake("pantry_home_bot"), [1]) == "moved to @pantry_home_bot; told 1 of 1"
