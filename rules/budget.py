"""Auction budget: base budget minus everything already committed, plus cash traded in."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from rules.buyouts import buyout_schedule
from rules.contracts import Contract

BASE_BUDGET = 400


@dataclass(frozen=True)
class Budget:
    season: int
    base: int
    missed_ip: int
    cash_net: int
    lines: tuple[tuple[str, str, int], ...] = ()  # (kind, player_id, amount)

    def _total(self, kind: str) -> int:
        return sum(amount for k, _, amount in self.lines if k == kind)

    @property
    def contracts(self) -> int:
        return self._total("contract")

    @property
    def buyouts(self) -> int:
        return self._total("buyout")

    @property
    def farm(self) -> int:
        return self._total("farm")

    @property
    def remaining(self) -> int:
        return self.base + self.cash_net - self.contracts - self.buyouts - self.farm - self.missed_ip


def compute_budget(
    season: int,
    contracts: Iterable[Contract] = (),
    buyouts: Iterable[tuple[Contract, int]] = (),
    farm_salaries: Mapping[str, int] | None = None,
    missed_ip_penalties: Iterable[int] = (),
    cash_net: int = 0,
    base: int = BASE_BUDGET,
) -> Budget:
    """Budget for the auction before `season`.

    buyouts: (contract, season it was dropped in) pairs.
    farm_salaries: retained farm players (by player ID) and their salary for `season`.
    cash_net: offseason cash received minus cash sent.
    """
    lines = []
    for c in contracts:
        if c.covers(season):
            lines.append(("contract", c.player_id, c.annual_price))
    for c, dropped_in in buyouts:
        schedule = buyout_schedule(c, dropped_in)
        if season in schedule:
            lines.append(("buyout", c.player_id, schedule[season]))
    for player_id, salary in (farm_salaries or {}).items():
        lines.append(("farm", player_id, salary))
    return Budget(
        season=season,
        base=base,
        missed_ip=sum(missed_ip_penalties),
        cash_net=cash_net,
        lines=tuple(lines),
    )
