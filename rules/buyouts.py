"""Buyout penalties for contracts dropped before their final year.

Dropping a contract during (or right after) season D leaves seasons D+1 .. final
year unpaid. Each unpaid season is charged a falling share of the annual price
against that season's auction budget: 80%, 70%, 60%, ... never below 0%.
"""

from rules.contracts import Contract

FIRST_YEAR_PCT = 80
STEP_PCT = 10


def round_half_up_pct(amount: int, pct: int) -> int:
    """amount * pct / 100, rounded half up like the spreadsheet's ROUND()."""
    return (amount * pct * 2 + 100) // 200


def buyout_schedule(contract: Contract, dropped_in_season: int) -> dict[int, int]:
    """Penalty owed per season, keyed by the season whose budget it's charged to."""
    if not contract.year_signed <= dropped_in_season <= contract.final_year:
        raise ValueError(
            f"{contract.player_id}: drop season {dropped_in_season} is outside the contract "
            f"({contract.year_signed} signing, final year {contract.final_year})"
        )
    schedule = {}
    for k, season in enumerate(range(dropped_in_season + 1, contract.final_year + 1)):
        pct = max(0, FIRST_YEAR_PCT - STEP_PCT * k)
        schedule[season] = round_half_up_pct(contract.annual_price, pct)
    return schedule
