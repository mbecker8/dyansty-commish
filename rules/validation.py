"""Checks on a team's signing decisions."""

from collections import Counter
from collections.abc import Mapping, Sequence

from rules.contracts import Contract

CONTRACT_LIMIT = 10
# The rulebook sets no maximum; 8-year contracts exist. League decision 2026-09-26 (RULES.md).
MAX_CONTRACT_LENGTH = 10


def validate_signing(
    existing: Sequence[Contract],
    new: Sequence[Contract],
    after_season: int,
    limit: int = CONTRACT_LIMIT,
    names: Mapping[str, str] | None = None,
) -> list[str]:
    """Problems with a team's contracts going into the next season's auction.

    existing: contracts the team already holds (bought-out ones excluded).
    new: contracts being signed at this signing period.
    names: player id -> display name for the messages.
    """

    def name(player_id):
        return (names or {}).get(player_id, player_id)

    errors = []
    next_season = after_season + 1
    for c in new:
        if c.year_signed != after_season:
            errors.append(f"{name(c.player_id)}: new contracts must be signed in {after_season}, not {c.year_signed}")
        if c.length > MAX_CONTRACT_LENGTH:
            errors.append(
                f"{name(c.player_id)}: {c.length} years is longer than the {MAX_CONTRACT_LENGTH}-year maximum"
            )
    for player, n in Counter(c.player_id for c in new).items():
        if n > 1:
            errors.append(f"{name(player)} is signed {n} times")
    under_contract = {c.player_id for c in existing if c.covers(next_season)}
    for c in new:
        if c.player_id in under_contract:
            errors.append(f"{name(c.player_id)} already has a contract covering {next_season}")
    active = [c for c in [*existing, *new] if c.covers(next_season)]
    if len(active) > limit:
        errors.append(f"{len(active)} contracts for {next_season}; the limit is {limit}")
    return errors
