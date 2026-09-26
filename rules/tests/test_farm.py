import pytest

from rules.farm import DRAFT_PICK_SALARY, retained_salary


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
