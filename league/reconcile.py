"""Replay a season's Fantrax moves against the contracts to find what changed.

The app detects; the commissioner confirms. Nothing here writes contracts.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from league.fantrax_data import Move


class Outcome(Enum):
    CONTINUES = "continues"  # still on the same team, contract runs past this season
    EXPIRING = "expiring"  # still on the team, final year was this season
    TRADED = "traded"  # contract moved by trade; ends on another team
    DROPPED = "dropped"  # dropped before the final year: buyout owed by the dropping team
    DROPPED_FREE = "dropped_free"  # dropped in the final year: contract void, no penalty
    INCONSISTENT = "inconsistent"  # the moves don't fit the contract; commissioner must look


@dataclass
class Replay:
    outcome: Outcome
    holder: str | None  # team holding the contract at the end (None once dropped)
    buyout_team: str | None = None
    dropped_at: Move | None = None
    claimed_by: str | None = None  # last team to claim him after the drop
    tx_ids: list[str] = field(default_factory=list)
    detail: str = ""


def replay(start_team: str, final_year: int, moves: Sequence[Move], season: int, end_team=...) -> Replay:
    """Walk one contracted player's moves (oldest first) from `start_team`.

    end_team: Fantrax team he's rostered on at the end (None if unrostered); omit to skip the check.
    """
    holder, on_roster = start_team, start_team
    dropped_at = None
    claimed_by = None
    tx_ids = []
    notes = []

    def inconsistent(msg):
        return Replay(Outcome.INCONSISTENT, holder, dropped_at=dropped_at, tx_ids=tx_ids, detail=msg)

    for m in moves:
        if m.kind in ("TRADE", "CLAIM") and m.to_team == on_roster and m.from_team != on_roster:
            # A move into the team that already has him (a pre-auction trade, or a farm draft pick
            # entered as a claim): the sheet was updated ahead of Fantrax.
            notes.append(f"{m.kind.lower()} {m.tx_id} ({m.when:%Y-%m-%d}) already reflected")
            continue
        tx_ids.append(m.tx_id)
        if m.kind in ("TRADE", "DROP") and m.from_team != on_roster:
            return inconsistent(f"{m.kind} from {m.from_team} on {m.when:%Y-%m-%d}, but he was on {on_roster}")
        if m.kind == "CLAIM" and on_roster is not None:
            return inconsistent(f"CLAIM by {m.to_team} on {m.when:%Y-%m-%d} while on {on_roster}")
        if m.kind == "TRADE":
            on_roster = m.to_team
            if dropped_at is None:
                holder = m.to_team
        elif m.kind == "DROP":
            on_roster = None
            if dropped_at is None:
                dropped_at, holder = m, None
        elif m.kind == "CLAIM":
            on_roster = claimed_by = m.to_team

    if end_team is not ... and end_team != on_roster:
        return inconsistent(f"moves end with him on {on_roster}, but the final roster has him on {end_team}")

    detail = "; ".join(notes)
    if dropped_at is not None:
        if final_year > season:
            return Replay(Outcome.DROPPED, None, dropped_at.from_team, dropped_at, claimed_by, tx_ids, detail)
        return Replay(Outcome.DROPPED_FREE, None, None, dropped_at, claimed_by, tx_ids, detail)
    if holder != start_team:
        return Replay(Outcome.TRADED, holder, tx_ids=tx_ids, detail=detail)
    outcome = Outcome.EXPIRING if final_year == season else Outcome.CONTINUES
    return Replay(outcome, holder, tx_ids=tx_ids, detail=detail)


class FarmOutcome(Enum):
    CONTINUES = "continues"  # still in his team's minors
    TRADED = "traded"  # moved to another team's farm by trade
    PROMOTED = "promoted"  # on an active roster now; can never return to the farm
    RELEASED = "released"  # dropped from the farm
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True)
class EndState:
    team: str
    status: str  # Fantrax roster status: Active, Reserve, IR, Minors
    games_played: int


@dataclass
class FarmReplay:
    outcome: FarmOutcome
    team: str | None
    mlb_debut: bool = False
    detail: str = ""


def replay_farm(start_team: str, moves: Sequence[Move], end: EndState | None, had_mlb: bool) -> FarmReplay:
    """Classify one farm player's season from his moves and where he ended up."""
    r = replay(start_team, final_year=10**6, moves=moves, season=0, end_team=end.team if end else None)
    if r.outcome is Outcome.INCONSISTENT:
        return FarmReplay(FarmOutcome.INCONSISTENT, r.holder, detail=r.detail)
    if r.outcome is Outcome.DROPPED:
        return FarmReplay(FarmOutcome.RELEASED, None, detail=r.detail)
    debut = not had_mlb and end is not None and end.games_played > 0
    if end.status != "Minors":
        return FarmReplay(FarmOutcome.PROMOTED, r.holder, debut, r.detail)
    outcome = FarmOutcome.TRADED if r.outcome is Outcome.TRADED else FarmOutcome.CONTINUES
    return FarmReplay(outcome, r.holder, debut, r.detail)
