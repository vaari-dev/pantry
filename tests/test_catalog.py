import pytest

from grocerybot import store
from grocerybot.catalog import PACK, Catalog, Choice, Product, combine, grouped, measure, plan

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


def test_plan_splits_ready_and_to_pick(catalog):
    p = plan([("egg", 10), ("Bread", 1), ("doodh", 1), ("anar", 1)], catalog)
    assert p.ready == [("egg", 10), ("bread", 1)]  # no cap: a far bigger order than usual is asked about
    assert p.to_pick == [("milk", 1), ("anar", 1)]  # known word without a product is picked too


def test_plan_merges_repeats(catalog):
    assert plan([("anda", 1), ("egg", 2)], catalog).ready == [("egg", 3)]
    assert plan([("bread", "500 g"), ("bread", "1 kg")], catalog).ready == [("bread", "1.5 kg")]
    assert plan([("bread", 2), ("bread", "1 kg")], catalog).ready == [("bread", "1 kg")]  # the later one stands


@pytest.mark.parametrize(
    "size, measured",
    [
        ("1 L", (1000, "ml")),
        ("1 pack (2 x 500 g)", (1000, "g")),
        ("1.5 kg", (1500, "g")),
        ("6 pcs", None),
        ("0 ml", None),
        ("", None),
    ],
)
def test_measure(size, measured):
    assert measure(size) == measured


def test_a_choice_wants_an_amount_or_counts_its_usual_pack():
    milk = Choice(
        "Amul Cow Milk", [Product("Amul Cow Milk", "500 ml", "/a/pvid/1"), Product("Amul Cow Milk", "1 l", "/a/pvid/2")]
    )
    assert milk.wants("2 l") == (2000, "ml")
    assert milk.wants(3) == (1500, "ml")  # three of the usual 500 ml
    assert milk.wants("1 kg") is None  # sold by volume
    assert Choice("Eggs", [EGGS]).wants(2) == (2, PACK)


def test_search_results_group_into_products_in_all_their_sizes():
    litre, half = (Product("Amul Cow Milk", s, f"/a/pvid/{s}", family="a") for s in ("1 l", "500 ml"))
    toned = Product("Amul Taaza Toned Milk", "1 l", "/t/pvid/1")
    named = Product("Mother Dairy Cow Milk 500 ml", "500 ml", "/m/pvid/1")  # no id: by name, size dropped
    named_litre = Product("Mother Dairy Cow Milk 1 L", "1 l", "/m/pvid/2")
    found = grouped([litre, toned, half, named, named_litre], 4)
    assert [(c.name, [p.size for p in c.sizes]) for c in found] == [
        ("Amul Cow Milk", ["500 ml", "1 l"]),
        ("Amul Taaza Toned Milk", ["1 l"]),
        ("Mother Dairy Cow Milk", ["500 ml", "1 l"]),
    ]
    assert len(grouped([litre, toned, named], 2)) == 2


def test_learn_puts_the_pick_first_with_its_usual_pack(catalog):
    half, litre = Product("Brown Eggs", "6 pcs", "/b/pvid/3"), Product("Brown Eggs", "12 pcs", "/b/pvid/4")
    assert catalog.learn("anda", Choice("Brown Eggs", [half, litre]), usual=litre) == "egg"
    first, before = catalog.choices("egg")
    assert [p.url for p in first.sizes] == [litre.url, half.url]
    assert before.sizes == [EGGS]  # saved before 28 Oct as a single "option"
    catalog.learn("egg", Choice("Brown Eggs", [half]))
    assert [c.name for c in catalog.choices("egg")] == ["Brown Eggs", "Urban White Eggs"]
    assert catalog.learn("anar", one(Product("Pomegranate", "4 pcs", "/pn/anar/pvid/4"))) == "anar"


def test_reprice_keeps_old_picks(catalog):
    catalog.reprice({EGGS.url: "₹89"})
    assert catalog.choices("egg")[0].sizes[0].price == "₹89"


def test_combine_adds_new_list_to_basket(catalog):
    assert combine([("egg", 2), ("milk", "1 l")], [("egg", 2), ("milk", "500 ml"), ("bread", 1)]) == [
        ("egg", 4),
        ("milk", "1.5 l"),
        ("bread", 1),
    ]


def one(product):
    return Choice(product.name, [product])


def test_a_pick_saved_with_its_brand_as_name_reads_as_its_full_name(tmp_path):
    """Until 24 Oct, cards with a brand line were saved as name=brand, size=name."""
    url = "/pn/elle-18-eyedrama-kajal-bold-black/pvid/0006f6cd-ed90-49ea-9e8d-bcddaebe00c4"
    kajal = {"name": "Elle 18", "size": "Elle 18 Eyedrama Kajal Bold Black", "url": url, "price": "₹119"}
    store.write(tmp_path / "catalog.json", {"kajal": {"options": [kajal]}})
    catalog = Catalog(tmp_path / "catalog.json")
    catalog.learn("lemon", one(Product("Lemon", "1 pc", "/pn/lemon/pvid/0cc2ba2e", "₹51")))
    [[kajal]] = [c.sizes for c in catalog.choices("kajal")]
    assert (kajal.name, kajal.size, kajal.label()) == ("Elle 18 Eyedrama Kajal Bold Black", "", kajal.name)
    assert kajal.variant == "0006f6cd-ed90-49ea-9e8d-bcddaebe00c4"
    assert catalog.choices("lemon")[0].sizes[0].label() == "Lemon (1 pc)"
