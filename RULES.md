# Dynasty Commish — Rules

> Split out of VISION.md §6 on 2026-09-26. Living document: update it whenever
> a rule is clarified or decided.

The rules engine (`rules/`) is a pure-Python package with no web or database
dependencies, covered exhaustively by tests. It implements the rulebook
(`docs/reference/rulebook-year19.md`). This file records how the app reads
the rulebook, including decisions the rulebook doesn't spell out.

- **Contract term:** "year signed" (S) is the league season that had just ended
  when the contract was signed. A contract of length L covers seasons
  S+1 … S+L; **final year = S+L, inclusive** (the player finishes that season
  on the team at the contract price). Example: Royce Lewis, S=2024, L=3 →
  plays 2025–2027 at $19/yr; after the 2025 season he has 2 years left.
- **Signability** (for a team at a signing period ending season Y):
  - *Under contract* (final year > Y): the contract simply continues; nothing
    to sign.
  - *Expiring* (final year = Y): **not signable** by that team; he returns to
    the auction pool.
  - *Otherwise on the roster* (acquired at auction/draft, FAAB, or free
    agency without a live contract): **signable**.
  - *Dropped contract:* dropping a contracted player **voids the contract**.
    The dropping team owes a buyout unless it was the final year, and the
    player's signability resets, so a team that picks him up may sign him.
  - The buyout is owed by the team holding him when he's dropped (so a
    contract traded and then dropped is the new team's buyout), and it stands
    even if the same team re-claims him (decided 2026-09-26).
  - A final-year drop also voids the contract. If anyone re-claims him,
    including the team that dropped him, he's signable at his new claim price
    rather than expiring (decided 2026-09-26; e.g. Kodai Senga and Tanner
    Bibee, re-claimed at $0).
  - Today this is an honor system: drops are announced on Discord and the
    commissioner spot-checks. The app applies the season's Fantrax trades,
    drops and claims from the auction onward as events (`manage.py
    sync_fantrax`, or Sync from Fantrax on the console), each once. Moves into
    the team that already has a player (pre-auction sign-and-trades, farm
    draft picks entered as claims) are already in the sheet and change
    nothing. Anything that contradicts the records becomes an exception for
    the commissioner.
  - Processing starts when the auction does, not at midnight: drops made
    earlier on auction day belong to the previous season (2026: last
    pre-auction drop 15:11, first auction claim 18:21, cutoff 16:00).
  - Sync again before signing, because offseason trades and drops count
    toward the season just ended. After renewal they happen in the new
    Fantrax league, which the commissioner adds on the console. Trades and
    drops after signing locks apply to the newly signed contracts.
  - A farm player released and then claimed back into a Minors slot is
    flagged, because farm adds are only by draft or trade. A farm player
    promoted and sent back down in the same season can't be detected, since
    Fantrax lineup history isn't in the snapshot.
- **Contract price per year:** 1 yr = P; 2 yr = P+5; 3 yr = P+10;
  4 yr = P+15; 5+ yr = P + 4×years. (Legacy pre-2014 formula retained only for
  historical contracts.)
- **Buyouts:** 80% of annual price for the first unpaid year, then 70%, 60%,
  50%, … for each remaining year (never truncated). Final-year contracts drop
  free. Each year's penalty is rounded to whole dollars, **half up**: 50% of
  $33 is $17 (decided 2026-09-26). The sheet builds the percentage in floating
  point, so on exact halves it may round down; the app doesn't copy that.
- **Farm:** $1 per pick; retention adds $1 (no MLB appearance) or $2 (has
  appeared); promoted players can't return to the farm. An MLB appearance
  means at least 1 plate appearance or 0.1 innings pitched (confirmed
  2026-09-26); a game played as a pinch runner or defensive sub doesn't
  count. The sync reads AB + BB and IP from Fantrax. Fantrax doesn't
  show HBP or sacrifices, so a player with games but no AB, BB or out is
  flagged for a manual check rather than decided.
- **Budget:** base $400 − contracts − buyout penalties − farm − missed-IP
  penalties ± cash trades.
- **Validation:** ≤10 contracts at signing (retained farm excluded), only
  signable players, etc.
- **Signing period** (built in M4; the calls below confirmed by the
  commissioner 2026-09-26):
  - A new contract's original price is the player's **end-of-season Fantrax
    salary**, taken from the season-end snapshot even when rosters come from a
    later blackout snapshot. A roster player with no season-end salary can't be
    signed until the commissioner sets one.
  - Signable = on the team's blackout roster, not under a running contract
    with that team, not expiring, not on a farm, and not in a Fantrax minors
    slot. A player under contract to another team, or on another team's farm,
    is flagged for the commissioner instead.
  - Contracts signed now have year signed = the season just ended; buyouts
    decided now are dropped in that season (80% charged at the next auction).
  - **Sign-and-trade isn't in the app's logic.** The limit at signing is a
    flat 10. A sign-and-trade happens in Fantrax after signing, and going
    over 10 that way is a gentlemen's agreement. The contract's
    `sign_and_trade` flag is a record only.
  - Contract length is 1–10 years. The rulebook sets no
    maximum; the longest in the Year 19 sheet is 8.
  - The auction budget left after signing can't be below $0.
    There's no minimum for filling the auction roster.
  - Every farm player needs an explicit keep or release before
    a team can submit, and before the commissioner can lock.
  - A team's draft decisions are visible only to that team and
    the commissioners until signing locks. The audit log is commissioner-only.
  - Locking applies every team's saved decisions at once, submitted or not,
    and is refused while any team has a problem.

Rule constants are **per-season configuration**, since the rulebook changes a
few times a year.

**Golden tests:** the Year 19 workbook's cached values become test fixtures.
The fixture (`data/league/year19_post_signing.json`) is extracted from
a Google Sheets export by `scripts/extract_workbook_fixture.py`. Every contract
price and buyout penalty matches. Two budgets differ because of sheet bugs:
DC's farm total skips Farm 1 (+$3 of budget), and JM's buyout total only sums
4 of its 6 buyouts (+$10). Separately, the AW tab is missing two of AW's 2026
farm draft picks (Brendan Lawson, Shunpeita Yamashita), so AW's 2026 budget
was $2 too high. The sync flags them for the commissioner to add.
Where the app disagrees with the sheet, each difference is either a
documented spreadsheet bug or a bug in the app.
