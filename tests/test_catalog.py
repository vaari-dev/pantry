import pytest

from grocerybot import store
from grocerybot.catalog import Catalog, Product, combine, plan

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


def test_lookup_by_name_or_alias(catalog):
    assert catalog.lookup("Anda") == "egg"
    assert catalog.lookup("egg") == "egg"
    assert catalog.lookup("pomegranate") is None


def test_plan_splits_ready_and_to_pick_and_caps(catalog):
    p = plan([("egg", 10), ("bread", 1), ("doodh", 1), ("anar", 1)], catalog)
    assert p.ready == [("egg", 3), ("bread", 1)]
    assert p.to_pick == [("milk", 1), ("anar", 1)]  # known word without a product is picked too
    assert p.notes == ["egg: asked for 10, capped at 3"]


def test_plan_merges_repeats(catalog):
    assert plan([("anda", 1), ("egg", 2)], catalog).ready == [("egg", 3)]


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


def test_a_pick_saved_with_its_brand_as_name_reads_as_its_full_name(tmp_path):
    """Until 24 Oct, cards with a brand line were saved as name=brand, size=name."""
    catalog = Catalog(tmp_path / "catalog.json")
    url = "/pn/elle-18-eyedrama-kajal-bold-black/pvid/0006f6cd-ed90-49ea-9e8d-bcddaebe00c4"
    catalog.learn("kajal", Product("Elle 18", "Elle 18 Eyedrama Kajal Bold Black", url, "₹119"))
    catalog.learn("lemon", Product("Lemon", "1 pc", "/pn/lemon/pvid/0cc2ba2e", "₹51"))
    [kajal] = catalog.options("kajal")
    assert (kajal.name, kajal.size, kajal.label()) == ("Elle 18 Eyedrama Kajal Bold Black", "", kajal.name)
    assert kajal.variant == "0006f6cd-ed90-49ea-9e8d-bcddaebe00c4"
    assert catalog.options("lemon")[0].label() == "Lemon (1 pc)"
