from datetime import date

from pantry import store
from pantry.ledger import Ledger


def test_spend_counts_placed_and_unconfirmed(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.finish(ledger.begin([("egg", 1)], 87), "placed")
    ledger.finish(ledger.begin([("milk", 1)], 60), "unknown")
    ledger.finish(ledger.begin([("bread", 1)], 75), "failed")
    assert ledger.spent_today() == 147


def test_interrupted_attempts_are_reported_until_finished(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    attempt = ledger.begin([("egg", 1)], 87)  # crash before finish()
    assert [o["total"] for o in ledger.interrupted()] == [87]
    assert [o["id"] for o in ledger.interrupted()] == [attempt]
    ledger.finish(attempt, "unknown")
    assert ledger.interrupted() == []
    assert ledger.spent_today() == 87  # still counted against the cap


def test_due_uses_usual_gap_and_skips_lapsed(tmp_path):
    path = tmp_path / "ledger.json"
    store.write(
        path,
        {
            "orders": [
                *(
                    {"id": str(i), "day": d, "at": d, "status": "placed", "total": 1, "items": [["egg", 1]]}
                    for i, d in enumerate(["2026-10-01", "2026-10-05", "2026-10-09"])
                ),
                *(
                    {"id": f"s{i}", "day": d, "at": d, "status": "placed", "total": 1, "items": [["skyr", 2]]}
                    for i, d in enumerate(["2026-08-01", "2026-08-03", "2026-08-05"])
                ),
            ]
        },
    )
    ledger = Ledger(path)
    assert ledger.due(date(2026, 10, 12)) == []  # 3 days since eggs, usual gap 4
    assert ledger.due(date(2026, 10, 14)) == [("egg", 1)]  # skyr lapsed long ago


def test_basket_is_kept_alongside_orders(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.set_basket([("bread", 1)])
    ledger.finish(ledger.begin([("egg", 1)], 87), "placed")
    assert ledger.basket() == [("bread", 1)]
    assert ledger.spent_today() == 87
    ledger.set_basket([])
    assert ledger.basket() == []
