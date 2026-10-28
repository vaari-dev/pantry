import json
from types import SimpleNamespace

import pytest

from grocerybot.intent import Reader, Request, grounded

EMPTY = {
    "add": [],
    "change": [],
    "settings": [],
    "show_basket": False,
    "clear_basket": False,
    "now": False,
    "address": "",
}


def item(said, name, qty=1, amount=("", 0, "")):
    return {"said": said, "name": name, "qty": qty, "amount": dict(zip(("said", "value", "unit"), amount, strict=True))}


def test_only_what_the_message_says_counts():
    reply = EMPTY | {
        "add": [item("do anda", "Egg", 2), item("jam", "jam"), item("bread", "bread", 0), item("bread", "!")],
        "change": [item("Doodh", "milk", 0)],
        "address": "office",
        "settings": [
            {"said": "limit 2000", "name": "limit", "value": "2000"},
            {"said": "ordering off", "name": "ordering", "value": "off"},
        ],
    }
    assert grounded(reply, "Do anda, bread, doodh nahi, limit 2000.") == Request(
        add=[("egg", 2)], change=[("milk", 0)], settings=[("limit", "2000")]
    )


def test_a_setting_needs_its_value_said():
    reply = EMPTY | {
        "settings": [
            {"said": "order kar do", "name": "ordering", "value": "on"},
            {"said": "limit badha do", "name": "limit", "value": "5000"},
            {"said": "minimum 99", "name": "minimum", "value": "9"},
            {"said": "limit 2,000", "name": "limit", "value": "2000"},
            {"said": "hold it on", "name": "ordering", "value": "on"},
        ]
    }
    text = "Bread order kar do, limit badha do, minimum 99, limit 2,000, hold it on"
    assert grounded(reply, text).settings == [("limit", "2000")]


def test_now_needs_the_word():
    assert not grounded(EMPTY | {"now": True}, "bread please").now
    assert grounded(EMPTY | {"now": True}, "bread abhi").now


def test_an_address_must_be_quoted():
    assert grounded(EMPTY | {"address": "Work"}, "bread to work").address == "Work"
    assert not grounded(EMPTY | {"address": ""}, "bread").address


class FakeClient:
    def __init__(self, reply=None, error=None, stop="end_turn"):
        self.reply, self.error, self.stop, self.asked = reply, error, stop, None
        self.messages = self

    def create(self, **asked):
        self.asked = asked
        if self.error:
            raise self.error
        usage = SimpleNamespace(input_tokens=1, cache_read_input_tokens=0, output_tokens=1)
        text = SimpleNamespace(type="text", text=json.dumps(self.reply))
        return SimpleNamespace(stop_reason=self.stop, content=[text], usage=usage)


def reader(client):
    r = Reader("key")
    r._client = client
    return r


def test_claude_reads_with_the_households_words_and_basket():
    client = FakeClient(EMPTY | {"add": [item("anda", "eggs")], "now": True})
    assert reader(client)("anda abhi", ["eggs", "bread"], ["milk"]) == Request(add=[("eggs", 1)], now=True)
    content = client.asked["messages"][0]["content"]
    assert "eggs, bread" in content and "Basket: milk" in content and "<message>anda abhi</message>" in content


def test_a_refusal_is_not_a_reading():
    with pytest.raises(ValueError, match="refusal"):
        reader(FakeClient(EMPTY, stop="refusal"))("2 milk", [], [])


def test_an_amount_counts_when_the_message_says_it_with_its_unit():
    reply = EMPTY | {
        "add": [
            item("cow milk", "cow milk", 1, ("2 litre", 2, "l")),
            item("paneer", "paneer", 1, ("aadha kilo", 0.5, "kg")),
            item("doodh", "milk", 2, ("2 litre", 2, "l")),  # one "2" read twice
            item("atta", "atta", 1, ("5 kg", 10, "kg")),  # not the amount said
            item("rice", "rice", 1, ("bada", 5, "kg")),  # no unit said
            item("oil", "oil", 1, ("1 litre", 1, "kg")),  # a weight for a volume
        ]
    }
    text = "2 litre cow milk, aadha kilo paneer, 2 litre doodh, 5 kg atta, bada rice, 1 litre oil"
    assert grounded(reply, text).add == [
        ("cow milk", "2 l"),
        ("paneer", "500 g"),
        ("milk", "2 l"),
        ("atta", 1),
        ("rice", 1),
        ("oil", 1),
    ]
