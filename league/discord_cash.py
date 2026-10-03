"""How a post in a #trades-<year>-assets channel becomes cash trades. Pure: no database, no network.

Cash for the auctions from FIRST_SEASON on comes only from these posts. A channel's year is the auction
its cash applies to (CashTrade.budget_season); earlier auctions were settled by hand.
"""

import re

FIRST_SEASON = 2027
TRADE_CHANNEL = re.compile(r"^trades-(\d{4})-assets$")


def channel_season(name: str) -> int | None:
    """The auction a trade channel's cash is for, or None for any other channel or an earlier auction."""
    m = TRADE_CHANNEL.match(name)
    return int(m[1]) if m and int(m[1]) >= FIRST_SEASON else None
