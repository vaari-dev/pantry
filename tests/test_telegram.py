import pytest

from pantry.telegram import Telegram, TelegramError


class Recording(Telegram):
    def __init__(self, name: str, refuse: str = ""):
        super().__init__("token")
        self.name, self.refuse, self.calls = name, refuse, []

    def call(self, method: str, http_timeout: float = 40, **params):
        self.calls.append(method)
        if method == self.refuse:
            raise TelegramError("Too Many Requests: retry after 600")
        return {"getMyName": {"name": self.name}, "getMe": {"username": "pantry_bot"}}.get(method, True)


@pytest.mark.parametrize(("current", "renamed"), [("Joey Butler", True), ("Pantry", False)])
def test_setup_names_the_bot_only_when_its_name_differs(current, renamed):
    tg = Recording(current)
    assert tg.setup("Pantry", {"help": "How to order"}, "description", "short") == "pantry_bot"
    assert ("setMyName" in tg.calls) is renamed


def test_a_refused_rename_does_not_stop_the_bot():
    tg = Recording("Joey Butler", refuse="setMyName")
    assert tg.setup("Pantry", {"help": "How to order"}, "description", "short") == "pantry_bot"
    assert "setMyCommands" in tg.calls


def test_a_tap_too_old_to_answer_is_still_handled():
    Recording("Pantry", refuse="answerCallbackQuery").answer("q")
