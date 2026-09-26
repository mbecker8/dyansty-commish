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
