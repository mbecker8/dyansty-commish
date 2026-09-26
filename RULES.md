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
    commissioner spot-checks. The app *detects* these by replaying the
    season's Fantrax trades, drops and claims from the auction onward
    (`manage.py reconcile`), and the commissioner accepts or rejects each
    proposal. Moves into the team that already has a player (pre-auction
    sign-and-trades, farm draft picks entered as claims) are already in the
    sheet and are skipped.
  - The replay starts when the auction does, not at midnight: drops made
    earlier on auction day belong to the previous season (2026: last
    pre-auction drop 15:11, first auction claim 18:21, cutoff 16:00).
  - Reconciliation is re-run on a fresh snapshot before signing, because
    offseason trades and drops count toward the season just ended. A player
    already decided is replayed only for moves after the snapshot that
    decision came from, starting from where the decision left him. He gets a
    new item only if something changed (a later trade, drop or MLB debut).
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
  count. Reconciliation reads AB + BB and IP from Fantrax. Fantrax doesn't
  show HBP or sacrifices, so a player with games but no AB, BB or out is
  flagged for a manual check rather than decided.
- **Budget:** base $400 − contracts − buyout penalties − farm − missed-IP
  penalties ± cash trades.
- **Validation:** ≤10 contracts (retained farm excluded; sign-and-trade
  exception, recorded per contract), only signable players, etc.

Rule constants are **per-season configuration**, since the rulebook changes a
few times a year.

**Golden tests:** the Year 19 workbook's cached values become test fixtures.
The fixture (`data/league/year19_post_signing.json`) is extracted from
a Google Sheets export by `scripts/extract_workbook_fixture.py`. Every contract
price and buyout penalty matches. Two budgets differ because of sheet bugs:
DC's farm total skips Farm 1 (+$3 of budget), and JM's buyout total only sums
4 of its 6 buyouts (+$10). Separately, the AW tab is missing two of AW's 2026
farm draft picks (Brendan Lawson, Shunpeita Yamashita), so AW's 2026 budget
was $2 too high. Reconciliation adds them.
Where the app disagrees with the sheet, each difference is either a
documented spreadsheet bug or a bug in the app.
