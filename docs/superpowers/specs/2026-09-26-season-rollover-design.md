# Season rollover: advance the league year on draft day

Status: implemented 2026-09-26 (branch season-rollover). Changes after review:
- The 12-month guard refuses the sync rather than storing an exception, and once signing is locked the sync
  also refuses until the next draft day is entered.
- Budgets freeze in the first sync after the auction start, exactly as of that minute (moves before it are
  applied first); Start only freezes if no sync did.
- Dates can't be set into time already synced; a started season can't be deleted; the team page's moves run
  from draft day to draft day. Issue #21. Related: #22 (dry run), #34 (farm draft in
the app), #37 (history archive), #48 (Fantrax events).

## Problem

The current season is a setting (`LEAGUE_SEASON = 2026`), and each Fantrax league has one `season` that
the event processor stamps on every drop. The renewed Fantrax league holds the 2026 offseason *and* the
whole 2027 season, so once the 2027 auction has run its drops would be filed under 2026: a final-year
drop would get a buyout, and the 80% year would land on a budget that was already final.

Most of "rollover" already happens by itself: contracts end by `year_signed + length`, buyouts carry
forward by their schedule, budgets are computed, and locking signing moves kept farm players to next
season's salary. What's missing is a season record with its draft-day times, a deliberate moment to
advance, a frozen auction budget, the next year of farm picks, and farm draft picks read from Fantrax.

## Decisions (from the design conversation)

1. Season S runs from the start of the S auction until just before the S+1 auction. A move's season is
   taken from its **date**, not from the Fantrax league.
2. The commissioner advances the season with a **button on the console**, behind a checklist, after
   draft day has run in Fantrax and been synced.
3. Advancing **freezes each team's auction budget**. The frozen figure is only for that auction;
   nothing recomputes it.
4. Advancing adds the **next year of farm picks**, keeping a four-year horizon (today 2027–2030, so
   the 2027 rollover adds 2031).
5. The **farm draft runs before the auction**. Every claim between the farm draft start and the auction
   start is a farm draft pick, charged **$1** whatever Fantrax shows.
6. Past $0 farm pickups need no correction: entered at $1, they're $2 the next season (Lawson and
   Yamashita, 2026).

## Data model

**`Season`** (new)

| Field | |
|---|---|
| `year` | unique; the league season (2027 = Year 20) |
| `farm_draft_starts_at` | nullable; start of the farm draft window |
| `auction_starts_at` | the season boundary |
| `started_at`, `started_by` | set by Start; null until then |

- `current_season()` is the latest season with `started_at` set. It replaces `settings.LEAGUE_SEASON`
  everywhere (pages, context processor, console, signing views, `sync_rosters` default). Meaning is
  unchanged: the current season is S, the next auction is S+1, the next signing is "after S".
- `season_at(when)` is the latest season whose `auction_starts_at <= when`. Before the first season
  it raises.
- A data migration seeds **2026**: auction start 2026-02-25 16:00 Eastern (today's `process_since`),
  no farm draft window, started. The 2026 boundary stays where it is even though the 18:21 claim that
  set it was a farm pick; the imported sheet already covers those moves.

**`SeasonBudget`** (new): `season`, `team`, `base`, `contracts`, `buyouts`, `farm`, `missed_ip`,
`cash`, `remaining`, `frozen_at`. Unique on (season, team). Filled from `team_budget(team, S+1)` at
Start.

**`FarmPick.player`** (new, nullable FK to `Player`): who a used pick was spent on. A pick with a
player is used.

**`FantraxLeague.season`** stays, as a label (the season whose offseason the league starts in) and to
choose the newest league per season for roster facts. The processor no longer uses it for drops.

## Draft day

1. Before draft day the commissioner enters the S+1 farm draft start and auction start on the console.
   That creates the `Season` row, not started. An auction start earlier than the current season's is
   refused. Once a season has started, its times change only in the admin (audited).
2. The farm draft and the auction run in Fantrax.
3. The commissioner syncs.
4. The commissioner presses **Start the S+1 season**.

### Checklist (all required)

- Signing after S is locked.
- The S+1 auction start has been entered and has passed.
- A Fantrax sync has run since the auction start.
- No unresolved Fantrax exceptions.

### What Start does, in one transaction

The Season row is locked with `select_for_update` and the checklist re-checked, so a double click can't
run it twice; an already-started season is refused.

1. A `SeasonBudget` per team from `team_budget(team, S+1)`. This includes the farm draft's $1 picks.
2. Farm picks for `max(existing year) + 1`: every team, rounds 1–2, owned by its original team.
3. `started_at` / `started_by` set; audit entry "Started the S+1 season".

## Fantrax events by date

In `Processor`, per move:

- `season = season_at(move.when)` for `dropped_in_season`, the final-year check (`final_year > season`
  means buyout, else voided) and the debut-check key.
- `contract()`: a contract signed at the signing after `season` is invisible to moves before that
  signing's lock, using that season's `SigningPeriod.locked_at`.
- **Guard:** a drop more than 12 months after its season's auction start raises an exception instead of
  guessing (the next season's auction start was probably never entered).

Roster facts (promotion, debut, unknown Minors) use the current season.

### Farm draft picks

A `CLAIM` dated in [farm_draft_starts_at, auction_starts_at) of season S+1:

- If the claiming team holds an unused S+1 pick: create a `FarmPlayer` (drafted_year S+1, salary $1,
  salary_season S+1, active) and set the pick's `player`. With several picks, earlier claims use the
  lower round. Effect `FARM_DRAFTED` (new).
- If the player is already on that team's farm: `ALREADY_REFLECTED`.
- Otherwise: an exception ("claimed in the farm draft but TEAM has no S+1 pick left").

A Minors player not on a farm and not drafted this way stays a `MINORS_UNKNOWN` exception, as today.

## Pages

- League pages keep showing next-auction (S+1) commitments, now from `current_season()`.
- The team page shows "S auction budget: $X (frozen DATE)" when a `SeasonBudget` exists for the
  current season, with its ledger.
- "Contracts that ended with the S season" becomes "Final year: S" while that season is running
  (after Start, before the signing after S opens).
- The team page's moves list filters by the current season's date window, not `league__season`.
- Console: a Season section with the entry form, the checklist and the Start button. "Add renewed
  league" keeps labelling the new league with the current season.

## Docs

RULES.md: the 2026 cutoff note (18:21 was a farm pick), the farm draft window, $1 regardless of
Fantrax, Lawson and Yamashita ($2 in 2027, no 2026 correction), rollover. ARCHITECTURE.md: `Season`
replaces the setting; drop season by date; Start. Help page: the commissioner's draft-day steps.

## Tests

- `season_at`: exactly at the auction start, just before it, before the first season.
- Drops by date: with a 2027 auction start, a June 2027 drop of a contract ending 2027 is voided; a
  June 2027 drop of one ending 2028 is a buyout dropped in 2027; a drop in the farm draft window is
  season 2026. The 12-month guard raises.
- Farm draft: a window claim with a pick left makes a $1 farm player and uses the lowest round; a
  second team's claim with no pick left is an exception; a claim after the auction start is not a
  draft pick.
- Start: refused for each failing checklist item; frozen budgets equal `team_budget` at that moment;
  28 picks for the next year; `current_season()` advances; a second Start is refused.
- `test_sync_2026.py` passes unchanged.

## Out of scope

Per-season rule constants, in-app farm draft (#34), in-season page redesign, auction results in the
app (auction players matter only at the next signing, through the season-end salary).
