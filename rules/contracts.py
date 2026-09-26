"""Contract pricing and term.

A contract signed in year S (the league season that had just ended) with length
L covers seasons S+1 .. S+L inclusive, at a fixed annual price.
"""

from dataclasses import dataclass

# Contracts signed before this year use the legacy escalation.
CURRENT_FORMULA_SINCE = 2014


def annual_price(original_price: int, length: int, year_signed: int) -> int:
    """Per-year price for a contract of `length` years on a player acquired for `original_price`."""
    if length < 1:
        raise ValueError(f"contract length must be at least 1, got {length}")
    if original_price < 0:
        raise ValueError(f"original price cannot be negative, got {original_price}")
    if year_signed >= CURRENT_FORMULA_SINCE:
        surcharge = {1: 0, 2: 5, 3: 10, 4: 15}.get(length, 4 * length)
    else:
        surcharge = {1: 0, 2: 4, 3: 8}.get(length, 3 * length)
    return original_price + surcharge


@dataclass(frozen=True)
class Contract:
    player: str
    original_price: int
    year_signed: int
    length: int

    @property
    def annual_price(self) -> int:
        return annual_price(self.original_price, self.length, self.year_signed)

    @property
    def first_season(self) -> int:
        return self.year_signed + 1

    @property
    def final_year(self) -> int:
        return self.year_signed + self.length

    def covers(self, season: int) -> bool:
        return self.first_season <= season <= self.final_year

    def years_left(self, after_season: int) -> int:
        return max(0, self.final_year - after_season)

    def cost_for_season(self, season: int) -> int:
        return self.annual_price if self.covers(season) else 0
