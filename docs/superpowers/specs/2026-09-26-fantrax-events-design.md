# Fantrax events: apply the season's moves automatically

Status: implemented 2026-09-26 (branch fantrax-events).
Replaces the reconciliation queue. Related: #42 (renewed league, moves after lock), #45 (Fantrax on
Render), #33 (live sync), #22 (dry run), #35 (Discord).

## Problem

`reconcile` replays a season's Fantrax moves and saves every conclusion as a `ReconciliationItem` for the
commissioner to accept or reject, one by one. That's the wrong model. Trades, drops, promotions and
debuts are facts that already happened in Fantrax; the app should process them and update the contract
and farm state itself. The 2026 review copy has 235 pending items, 157 of which ("continues",
"expiring", "farm continues") aren't events at all.

## Decisions (from the design conversation)

1. Events the app understands are applied automatically. Only the ones it can't interpret become
   **exceptions**; signing can't open while any exception is unresolved.
2. Each Fantrax transaction is **stored once**, with what it did. Re-running never applies it twice.
   Rare wrong applications (a reversed trade, an agreed undo) are fixed **by hand in the admin**; no
   undo button.
3. Processing runs from **both** a management command (saved snapshot or live) and a **"Sync from
   Fantrax"** button on the commissioner console.
4. Cash trades stay **manual** for the beta. Reading them from the league's Discord channel is a later,
   separate project (#35).

## What goes away

- `ReconciliationItem` (model, accept/reject, admin page, `pending_reconciliation`), the
  `reconcile` command, and `league/reconcile.py`'s approval-oriented outcomes.
- The kinds CONTINUES, EXPIRING and FARM_CONTINUES. A contract expires because of its end year; no
  event is needed.
- The "numbers may still change" banner driven by pending items.
- The special case where reconcile skips contracts signed at the signing after the replayed season
  (see "Moves after lock" below).

The review database's 235 items are all pending, so the migration drops them with no loss of
decisions. Production has no league data yet.

## Data model

**`FantraxLeague`**: a Fantrax league the app reads.

| field | notes |
|---|---|
| `league_id` | Fantrax league ID, unique |
| `season` | league season its moves belong to (e.g. 2026) |
| `process_since` | ignore moves before this time (the 2026 league: 2026-02-25 16:00 Eastern, when the post-signing sheet's state ends) |
| `active` | the sync reads only active leagues |

The renewed league for offseason trades is a second row with the same `season` (#42). Its team IDs
differ, so teams are matched by Fantrax ID first, then by name and `TeamAlias`, the way
`sync_rosters` already does.

**`FantraxEvent`**: one processed fact.

| field | notes |
|---|---|
| `key` | unique. Transactions: `tx:<txSetId>:<playerId>:<kind>` (a trade set has several legs, one per player). Roster-derived facts: `promoted:<playerId>`, `debut:<playerId>`, `minors-unknown:<playerId>:<season>`, `cash-comment:<txSetId>`, `roster-mismatch:<contractId>:<season>` |
| `league` | FK FantraxLeague |
| `happened_at` | Fantrax time, or sync time for roster-derived facts |
| `kind` | TRADE, DROP, CLAIM, PROMOTED, DEBUT, MINORS_UNKNOWN, CASH_COMMENT, ROSTER_MISMATCH |
| `player`, `from_team`, `to_team` | as known |
| `effect` | CONTRACT_MOVED, BUYOUT, VOIDED, FARM_MOVED, FARM_RELEASED, FARM_PROMOTED, FARM_DEBUT, ALREADY_REFLECTED, NONE, EXCEPTION |
| `contract`, `farm_player`, `buyout` | what it touched (PROTECT, nullable) |
| `detail` | human-readable line, e.g. "traded Juan Soto MB → AC" |
| `resolved_at`, `resolved_by`, `resolved_note` | exceptions only |

`sync` of a whole batch runs in one transaction. Events are read-only in the admin.

**Running twice at once** (a double-click): the button disables itself; the league rows are locked
(`select_for_update`), and SQLite takes its write lock at the start of every transaction, so a second
sync waits and then skips what the first stored. A run that still loses the race on a unique key
rolls back and retries.

## Processing rules

Transactions are processed oldest first (drops before claims at the same time, as today). A key
already in `FantraxEvent` is skipped. Moves before the league's `process_since` are ignored.

| event | contract player | farm player | neither |
|---|---|---|---|
| TRADE | contract moves to the receiving team (CONTRACT_MOVED). If it's already on the receiving team: ALREADY_REFLECTED | moves to the receiving team's farm (FARM_MOVED) | NONE |
| DROP | before the final year: buyout owed by the dropping team, `dropped_in_season` = league season (BUYOUT). In the final year: contract voided (VOIDED) | released (FARM_RELEASED) | NONE |
| CLAIM | NONE (a claimed player has no contract until signed) | NONE | NONE |

A move that contradicts the current records (a trade or drop from a team that doesn't hold the
contract or farm player) is an EXCEPTION, and nothing changes.

Only **live** contracts are touched. A contract already bought out or voided is no longer affected by
later moves of the same player.

**Roster-derived facts**, from the latest roster fetch of the season's newest active league (after
renewal the old league is frozen, so its rosters are stale), stored once each:

- A farm player now in a non-Minors slot: FARM_PROMOTED (status promoted; can't return).
- A farm player without an MLB appearance whose stats show a debut: FARM_DEBUT. The existing
  "games played but no PA or out" note becomes an EXCEPTION for a look.
- A Minors-slot player who isn't a farm player: EXCEPTION ("add him in the admin if he was drafted").
- A trade with a commissioner comment, in any league: EXCEPTION ("enter the cash trade by hand").
- After applying, a live contract whose team doesn't match the player's Fantrax roster team:
  EXCEPTION.

**Moves after lock.** Because events are processed incrementally against the current records, a trade
or drop after the signing locks just affects the newly signed contracts. The buyout season comes from
the league's `season`. This closes #42's second gap.

## Fetching Fantrax

- `scripts/fantrax_client.py` moves to `league/fantrax_client.py`; `scripts/fantrax_snapshot.py`
  imports it from there.
- One source interface yields what processing needs: moves (claims, drops, trade legs, trade
  comments), roster rows with slot status, and farm-player stats. Two implementations: a saved
  snapshot directory (today's `Snapshot`), and a live fetch returning the same shapes in memory.
- **Auth:** transactions, trades and stats come only from the logged-in `fxpa/req` API, so the live
  fetch needs the browser cookie: `FANTRAX_COOKIE` (a Render secret; locally
  `secrets/fantrax_cookie.txt`). Checked 2026-09-26: the documented public API
  (https://www.fantrax.com/developer, v1.8 beta) has no transaction, trade or stats endpoint, and the
  Secret ID (`userSecretId`) is accepted only by `getLeagues`; `fxpa/req` answers
  `WARNING_NOT_LOGGED_IN` with it.
- **Secret ID use:** `FANTRAX_SECRET_ID` (locally `secrets/fantrax_secret_id.txt`) lists the
  commissioner's leagues through `getLeagues`. The console uses it to offer the renewed league when it
  appears, so adding the second `FantraxLeague` row is a pick, not a copy-paste of an ID.
- An expired cookie (`WARNING_NOT_LOGGED_IN`) or any Fantrax error stops the sync before anything is
  applied, and the console says so plainly ("Fantrax login expired: update FANTRAX_COOKIE").
  The cookie lives only in the environment or the secrets file; it's never stored in the database,
  logged or shown back. (A console form to paste a fresh cookie can come with #33 if expiry turns out
  to be frequent.)

## Running it

- **`manage.py sync_fantrax`** replaces `reconcile`.
  - `--snapshot <dir>`: process a saved snapshot instead of fetching.
  - `--dry-run`: print what would be applied, change nothing.
  - Prints a summary: new events by effect, and the open exceptions.
- **Console button "Sync from Fantrax"**: runs the live sync for every active league and shows the
  same summary, linking to the event list.
- `bin/local-review.sh` runs `sync_fantrax --snapshot data/fantrax/2026-final` instead of `reconcile`.
- Opening signing requires zero unresolved exceptions (replaces the pending-items check).

## Where it shows

- **Console:** "Fantrax exceptions" with a resolve form (note required) and the sync button.
- **Team page:** "Moves this season": that team's events with an effect other than NONE, newest
  first.
- **Audit log:** each sync (who, source, counts) and each resolved exception.
- **Admin:** FantraxLeague is editable; FantraxEvent is read-only. Corrections go through the existing
  audited admin pages for contracts, buyouts and farm players.
- **Help page:** the reconciliation section is rewritten for events and exceptions.

## Testing

- The replay unit tests become tests of the event rules (trade, drop before/in the final year, claim,
  contradictions, dead contracts ignored, farm moves).
- Sync on `data/fantrax/2026-final`:
  - running it twice creates nothing the second time;
  - the resulting contracts, buyouts, voids and farm state equal what accepting every 2026
    reconciliation item produced (captured before removal as a golden expectation);
  - a trade after lock moves a newly signed contract.
- Live fetch: tested with a fake `fxpa/req` responder, including the not-logged-in error leaving the
  database untouched.
- Signing can't open with an unresolved exception.

## Out of scope

- Reading cash trades from Discord (#35, after this).
- Scheduled/automatic syncing (#33). The button and command are on-demand.
- Moving `sync_rosters` to the public API (#45 part 1).
