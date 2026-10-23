import pytest

from grocerybot import store
from grocerybot.catalog import Catalog, Product, combine, normalize, plan, split_spoken

EGGS = Product("Urban White Eggs", "1 pack (10 pcs)", "/pn/eggs/pvid/1")
BREAD = Product("Multigrain Bread", "1 pack (400 g)", "/pn/bread/pvid/2")


@pytest.fixture
def catalog(tmp_path):
    store.write(
        tmp_path / "catalog.json",
        {
            "egg": {"aliases": ["anda"], "max_qty": 3, "options": [vars(EGGS)]},
            "bread": {"aliases": [], "options": [vars(BREAD)]},
            "milk": {"aliases": ["doodh"], "options": []},  # known word, no product yet
        },
    )
    return Catalog(tmp_path / "catalog.json")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Order bread aur do anda please", [("bread", 1), ("anda", 2)]),
        ("bread or duanda, and one dudh.", [("bread", 1), ("anda", 2), ("dudh", 1)]),
        ("mujhe teen kela aur doodh chahiye", [("kela", 3), ("doodh", 1)]),
        ("2 milk, eggs x3", [("milk", 2), ("eggs", 3)]),
        ("", []),
    ],
)
def test_split_spoken(text, expected):
    assert split_spoken(text) == expected


@pytest.mark.parametrize(
    "word, norm",
    [
        ("Eggs", "egg"),
        ("potatoes", "potato"),
        ("berries", "berry"),
        ("chips", "chips"),
        ("Dhaniya", "dhania"),
        ("glass", "glass"),
    ],
)
def test_normalize(word, norm):
    assert normalize(word) == norm


def test_lookup_aliases_plurals_and_misspellings(catalog):
    assert catalog.lookup("anda") == "egg"
    assert catalog.lookup("eggs") == "egg"
    assert catalog.lookup("bred") == "bread"
    assert catalog.lookup("dudh") == "milk"  # speech-to-text spelling of doodh
    assert catalog.lookup("pomegranate") is None


def test_plan_splits_ready_and_to_pick_and_caps(catalog):
    p = plan("10 eggs, bread, doodh, anar", catalog)
    assert p.ready == [("egg", 3), ("bread", 1)]
    assert p.to_pick == [("milk", 1), ("anar", 1)]  # known word without a product is picked too
    assert p.notes == ["egg: asked for 10, capped at 3"]


def test_plan_merges_repeats(catalog):
    assert plan("anda, 2 eggs", catalog).ready == [("egg", 3)]


def test_learn_puts_pick_first_and_keeps_backups(catalog):
    other = Product("Brown Eggs", "1 pack (6 pcs)", "/pn/brown/pvid/3")
    assert catalog.learn("anda", other) == "egg"
    assert [p.url for p in catalog.options("egg")] == [other.url, EGGS.url]
    assert catalog.learn("anar", Product("Pomegranate", "4 pcs", "/pn/anar/pvid/4")) == "anar"
    assert catalog.lookup("anar") == "anar"


def test_combine_adds_new_list_to_basket_within_limits(catalog):
    assert combine([("egg", 2), ("bread", 1)], [("egg", 2), ("milk", 1)], catalog) == [
        ("egg", 3),
        ("bread", 1),
        ("milk", 1),
    ]
