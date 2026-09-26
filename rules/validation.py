"""Checks on a team's signing decisions."""

from collections import Counter
from collections.abc import Sequence

from rules.contracts import Contract

CONTRACT_LIMIT = 10


def validate_signing(
    existing: Sequence[Contract],
    new: Sequence[Contract],
    after_season: int,
    limit: int = CONTRACT_LIMIT,
    extra_allowed: int = 0,
) -> list[str]:
    """Problems with a team's contracts going into the next season's auction.

    existing: contracts the team already holds (bought-out ones excluded).
    new: contracts being signed at this signing period.
    extra_allowed: sign-and-trade exception, granted by the commissioner.
    """
    errors = []
    next_season = after_season + 1
    for c in new:
        if c.year_signed != after_season:
            errors.append(f"{c.player_id}: new contracts must be signed in {after_season}, not {c.year_signed}")
    for player, n in Counter(c.player_id for c in new).items():
        if n > 1:
            errors.append(f"{player} is signed {n} times")
    under_contract = {c.player_id for c in existing if c.covers(next_season)}
    for c in new:
        if c.player_id in under_contract:
            errors.append(f"{c.player_id} already has a contract covering {next_season}")
    active = [c for c in [*existing, *new] if c.covers(next_season)]
    if len(active) > limit + extra_allowed:
        errors.append(f"{len(active)} contracts for {next_season}; the limit is {limit + extra_allowed}")
    return errors
