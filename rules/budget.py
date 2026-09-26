"""Auction budget: base budget minus everything already committed, plus cash traded in."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from rules.buyouts import buyout_schedule
from rules.contracts import Contract

BASE_BUDGET = 400


@dataclass(frozen=True)
class Budget:
    season: int
    base: int
    contracts: int
    buyouts: int
    farm: int
    missed_ip: int
    cash_net: int
    lines: list[tuple[str, str, int]] = field(default_factory=list)

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
    farm_salaries: retained farm players and their salary for `season`.
    cash_net: offseason cash received minus cash sent.
    """
    lines = []
    for c in contracts:
        if cost := c.cost_for_season(season):
            lines.append(("contract", c.player, cost))
    for c, dropped_in in buyouts:
        if penalty := buyout_schedule(c, dropped_in).get(season, 0):
            lines.append(("buyout", c.player, penalty))
    for name, salary in (farm_salaries or {}).items():
        lines.append(("farm", name, salary))
    missed_ip = list(missed_ip_penalties)

    def total(kind):
        return sum(amount for k, _, amount in lines if k == kind)

    return Budget(
        season=season,
        base=base,
        contracts=total("contract"),
        buyouts=total("buyout"),
        farm=total("farm"),
        missed_ip=sum(missed_ip),
        cash_net=cash_net,
        lines=lines,
    )
