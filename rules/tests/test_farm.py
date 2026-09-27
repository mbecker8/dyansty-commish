import pytest

from rules.farm import DRAFT_PICK_SALARY, pick_number, retained_salary


def test_draft_pick_costs_one_dollar():
    assert DRAFT_PICK_SALARY == 1


@pytest.mark.parametrize(("salary", "mlb", "expected"), [(1, False, 2), (3, False, 4), (4, True, 6), (1, True, 3)])
def test_retention_bump(salary, mlb, expected):
    # +$1 if he hasn't appeared in an MLB game, +$2 if he has.
    assert retained_salary(salary, has_mlb_appearance=mlb) == expected


def test_salary_history_matches_sheet():
    # Marcelo Mayer: drafted 2022 at $1, kept 2023-2025 without MLB time, debuted in 2025 -> $6 for 2026.
    salary = DRAFT_PICK_SALARY
    for mlb in (False, False, False, True):
        salary = retained_salary(salary, has_mlb_appearance=mlb)
    assert salary == 6


def test_pick_numbers_follow_the_rulebook_order():
    # 8th picks first, 12th third, the champion last; round 2 repeats.
    assert pick_number(1, 8) == 1
    assert pick_number(1, 12) == 3
    assert pick_number(1, 7) == 10
    assert pick_number(1, 1) == 14
    assert pick_number(2, 8) == 15
    assert pick_number(2, 14) == 25
    assert pick_number(2, 1) == 28


def test_every_place_gets_one_pick_per_round():
    assert sorted(pick_number(1, place) for place in range(1, 15)) == list(range(1, 15))
