"""How a post in a #trades-<year>-assets channel becomes cash trades. Pure: no database, no network.

Cash for the auctions from FIRST_SEASON on comes only from these posts. A channel's year is the auction
its cash applies to (CashTrade.budget_season); earlier auctions were settled by hand.
"""

import re
from dataclasses import dataclass, field

FIRST_SEASON = 2027
TRADE_CHANNEL = re.compile(r"^trades-(\d{4})-assets$")


def channel_season(name: str) -> int | None:
    """The auction a trade channel's cash is for, or None for any other channel or an earlier auction."""
    m = TRADE_CHANNEL.match(name)
    return int(m[1]) if m and int(m[1]) >= FIRST_SEASON else None


CASH, NOT_CASH, EXCEPTION = "CASH", "NOT_CASH", "EXCEPTION"  # DiscordMessage.Status values
MENTION = re.compile(r"<@!?(\d+)>")
AMOUNT = re.compile(r"\$\s?(\d+)")
YEAR = re.compile(r"\b(20\d\d)\b")
SENDS = re.compile(r"<@!?(\d+)>\s*sends\b.*?\bto\b\s*<@!?(\d+)>(.*)", re.IGNORECASE | re.DOTALL)
NOTE_LENGTH = 200  # CashTrade.note


@dataclass(frozen=True)
class Wanted:
    """One cash trade a post asks for."""

    from_team: object
    to_team: object
    amount: int
    note: str


@dataclass
class Reading:
    status: str
    detail: str
    trades: list[Wanted] = field(default_factory=list)
    needs_link: bool = False  # a mentioned manager isn't linked yet: linking them clears it, not a resolve


def readable_text(content: str, names: dict[str, str]) -> str:
    return MENTION.sub(lambda m: "@" + names.get(m[1], m[1]), content)


def read_post(content: str, has_attachments: bool, season: int, teams: dict, names: dict[str, str]) -> Reading:
    """What a post in the `season` trade channel says. `teams`: Discord user ID -> Team (linked managers)."""
    text = content.strip()
    amounts = AMOUNT.findall(text)
    if not amounts:
        if not text and has_attachments:
            return Reading(EXCEPTION, "A picture with no text: if it moves cash, enter the trade here")
        return Reading(NOT_CASH, "No cash amount")
    if len(amounts) > 1:
        return Reading(EXCEPTION, f"More than one amount: enter the {season} cash here")
    if other := sorted({int(y) for y in YEAR.findall(text)} - {season}):
        years = ", ".join(map(str, other))
        return Reading(EXCEPTION, f"Mentions {years} in the {season} channel: check which auction the cash is for")
    m = SENDS.search(text)
    if not m:
        return Reading(
            EXCEPTION, "Couldn't tell who sends and who receives: posts need @mentions (“@A sends $5 to @B”)"
        )
    for uid in (m[1], m[2]):
        if uid not in teams:
            name = names.get(uid, uid)
            return Reading(
                EXCEPTION,
                f"@{name} isn't linked to a manager: set their Discord ID in Admin → Managers, then Sync from Discord",
                needs_link=True,
            )
    sender, receiver, amount = teams[m[1]], teams[m[2]], int(amounts[0])
    if sender == receiver:
        return Reading(EXCEPTION, "Sender and receiver are the same team")
    note = readable_text(m[3], names).strip(" .,-–—")[:NOTE_LENGTH]
    return Reading(
        CASH, f"{sender.code} sends ${amount} to {receiver.code} for {season}", [Wanted(sender, receiver, amount, note)]
    )
