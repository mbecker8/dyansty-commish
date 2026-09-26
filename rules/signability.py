"""Whether a rostered player can be signed at the signing period after a season."""

from enum import Enum

from rules.contracts import Contract


class Signability(Enum):
    UNDER_CONTRACT = "under_contract"  # contract continues; nothing to sign
    EXPIRING = "expiring"  # final year just ended; returns to the auction pool
    SIGNABLE = "signable"  # on the roster without a live contract

    @property
    def can_sign(self) -> bool:
        return self is Signability.SIGNABLE


def signability(contract: Contract | None, after_season: int) -> Signability:
    """Classify a player on a team's roster at the signing after `after_season`.

    `contract` is the player's live contract with this team, or None. A dropped
    contract is void, so pass None for a player picked up after being dropped.
    """
    if contract is None or contract.final_year < after_season:
        return Signability.SIGNABLE
    if contract.final_year == after_season:
        return Signability.EXPIRING
    return Signability.UNDER_CONTRACT
