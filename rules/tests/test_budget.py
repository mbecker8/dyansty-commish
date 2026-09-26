from rules.budget import BASE_BUDGET, compute_budget
from rules.contracts import Contract

LEWIS = Contract("Royce Lewis", 9, 2024, 3)  # $19/yr 2025-2027
BAZ = Contract("Shane Baz", 12, 2025, 1)  # $12, 2026 only
WINN_OLD = Contract("Old", 1, 2022, 3)  # expired after 2025
GARRETT = Contract("Braxton Garrett", 1, 2023, 3)  # $11/yr 2024-2026


def test_base_budget_is_400():
    assert BASE_BUDGET == 400


def test_empty_team_keeps_base_budget():
    b = compute_budget(season=2026)
    assert b.remaining == 400


def test_full_budget():
    b = compute_budget(
        season=2026,
        contracts=[LEWIS, BAZ, WINN_OLD],
        buyouts=[(GARRETT, 2024)],  # 70% of $11 in 2026 -> $8
        farm_salaries={"Josue De Paula": 4, "Walker Jenkins": 3},
        missed_ip_penalties=[5, 10],
        cash_net=5,
    )
    assert b.contracts == 31
    assert b.buyouts == 8
    assert b.farm == 7
    assert b.missed_ip == 15
    assert b.cash_net == 5
    assert b.remaining == 400 + 5 - 31 - 8 - 7 - 15


def test_every_charge_is_itemized():
    b = compute_budget(season=2026, contracts=[LEWIS, WINN_OLD], buyouts=[(GARRETT, 2024)], farm_salaries={"X": 2})
    assert ("contract", "Royce Lewis", 19) in b.lines
    assert ("buyout", "Braxton Garrett", 8) in b.lines
    assert ("farm", "X", 2) in b.lines
    # Contracts not covering the season don't appear.
    assert all(name != "Old" for _, name, _ in b.lines)


def test_cash_out_reduces_budget():
    assert compute_budget(season=2026, cash_net=-75).remaining == 325


def test_zero_dollar_contract_is_still_itemized():
    free = Contract("Free", 0, 2025, 1)  # $0, 2026 only
    b = compute_budget(season=2026, contracts=[free])
    assert ("contract", "Free", 0) in b.lines


def test_zero_percent_buyout_year_is_still_itemized():
    c = Contract("Ten", 0, 2025, 10)  # $40/yr; dropped 2026 -> 2035 charged at 0%
    b = compute_budget(season=2035, buyouts=[(c, 2026)])
    assert ("buyout", "Ten", 0) in b.lines


def test_budget_totals_are_derived_from_lines():
    b = compute_budget(season=2026, contracts=[LEWIS], farm_salaries={"X": 2})
    assert b.contracts == sum(a for k, _, a in b.lines if k == "contract")
    hash(b)  # frozen and hashable
