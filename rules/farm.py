"""Farm system salaries.

Each farm pick starts at $1. Keeping a farm player for another season adds $1,
or $2 if he has appeared in an MLB game. Farm salaries come out of the auction
budget; retained farm players don't count toward the contract limit.
"""

DRAFT_PICK_SALARY = 1
RETAIN_BUMP_NO_MLB = 1
RETAIN_BUMP_MLB = 2


def retained_salary(current_salary: int, has_mlb_appearance: bool) -> int:
    """Salary for next season if the player is kept on the farm."""
    return current_salary + (RETAIN_BUMP_MLB if has_mlb_appearance else RETAIN_BUMP_NO_MLB)


# Farm draft order by the previous season's final place, repeated each round. Set by the commissioner
# 2026-09-27 after the playoff field changed; the Year 19 rulebook's order (7, 9, 11, …) is out of date.
DRAFT_ORDER_BY_PLACE = (8, 10, 12, 9, 11, 13, 5, 4, 6, 7, 14, 3, 2, 1)


def pick_number(round: int, place: int) -> int:
    """Overall pick in the farm draft (1.01 is 1, 2.01 is 15), from the original team's final place."""
    return (round - 1) * len(DRAFT_ORDER_BY_PLACE) + DRAFT_ORDER_BY_PLACE.index(place) + 1
