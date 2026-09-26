# Dynasty Commish — Vision

> Status: **Draft v0.1** (2026-09-26). Living document — iterate freely.

## 1. Why this exists

Our 14-team dynasty baseball league (Year 19, running since 2007) plays on
**Fantrax**, but everything Fantrax doesn't model — multi-year contracts,
buyouts, farm systems, auction-budget math, cash trades — lives in a Google
Sheet. Every offseason the commissioner resets that sheet, sends it to all
managers, collects their contract decisions, and validates and aggregates them
by hand.

That process is slow and fragile. A review of the Year 19 workbook
(`docs/reference/`) found no errors in this year's budgets, but it did find
latent problems of the kind that eventually cause one:

- The **"Total Penalty Left"** projection for buyouts stops after two future
  years, so managers see an understated remaining cost for long buyouts. This
  year's charge is still correct, because it's recalculated every season.
- The team tabs have drifted apart. One tab's buyout formula still uses the
  pre-2014 escalation (harmless today, since its buyouts are all ≤3 years),
  and the tabs' buyout blocks sit on different rows, so the budget formulas
  differ from tab to tab.
- The aggregate sheet has copy/paste errors (a wrong row reference, a
  `#REF!`). These affect what's displayed, not budgets.
- Farm "Keep?" decisions don't feed the budget formula. Released players and
  the $1/$2 retention bump are handled by editing salaries by hand.
- Some rules live only in cell colors (signable / not signable) or side tabs.
  The "Special Waivers" section the rulebook refers to wasn't in the exported
  document.

Each year the commissioner has to rebuild and re-check every one of these by
hand.

**Dynasty Commish replaces the contract spreadsheet with a web app** where
managers make their own contract decisions, the rules are enforced by code,
and the whole league can see the state of every team.

## 2. Goals

1. **Managers self-serve.** Each manager signs in, makes their contract,
   buyout, and farm-retention decisions, and sees their resulting auction
   budget update live. No more emailing spreadsheets.
2. **The rules are code, not cells.** One tested rules engine computes
   contract prices, buyout penalties, farm retention costs, and budgets.
3. **Transparency.** Every manager can see every team's contracts, buyouts,
   farm, budget, and pick ownership.
4. **Commissioner stays in control.** Review, override (with a note),
   lock periods, and make manual adjustments — all audited.
5. **Parity with the spreadsheet.** Everything the workbook does today, the
   app does (correctly).

**Hard deadline:** in production for the **mid-February 2027 contract
signing period**.

## 3. Users & roles

| Role | Who | Can |
|---|---|---|
| **Manager** | One (or more) per team, ~14 teams | View whole league; edit *their own team's* signing decisions while the signing window is open; submit. |
| **Commissioner** | League commish (possibly a co-commish) | Everything managers can, plus: open/lock periods, edit any team, run Fantrax sync, enter trades/penalties/farm picks, season rollover. |

Sign-in: **email magic link** (no passwords). Managers are invited by the
commissioner and linked to a team. (Alternative under consideration: **Sign in
with Discord**, since the league already lives there — see open questions.)

League communication happens on **Discord**, not email.

## 4. The league year (lifecycle)

```
Season ends (Fantrax) ──► Offseason trade window ──► SIGNING BLACKOUT ──► Trades reopen ──► Auction ──► Snake draft ──► Farm draft ──► In-season
      (Sept/Oct)              (Oct → Feb)            (mid-Feb, ≤1 wk)    (until auction)                               (Discord, late Feb)
```

The **signing blackout** is the heart of the MVP:

0. **At season end (now):** snapshot every player's end-of-year salary from
   Fantrax. These become the original prices, and they're fixed from that
   point on.
1. **At the blackout:** sync current rosters from Fantrax, which reflect
   offseason trades.
2. Commissioner opens signing.
3. Each manager: chooses up to 10 contracts (player + length), decides
   buyouts on existing contracts, decides which farm players to retain.
4. App validates continuously and shows the resulting auction budget.
5. Manager submits; commissioner reviews, fixes, and **locks** the period.
6. Resulting budgets and contract list are published to the league.

## 5. Domain model (first cut)

- **League / Season** — league year (e.g. Year 19 = the 2026 season; its
  signing in Feb 2026 records contracts as year signed S=2025), rule
  parameters for that season (base budget, contract limit, formula constants).
- **Team / Manager** — team names change over time; managers can change.
- **Player** — Fantrax player ID, name, positions, MLB-debut status.
- **Contract** — player, team, original price, year signed, length →
  annual price and final year.
- **Buyout** — a contract dropped early, with its year-by-year penalty
  schedule charged against future budgets.
- **Farm player** — drafted year, pick salary, MLB-experience flag, retained
  each year or released.
- **Farm draft pick** — season, original owner, current owner (tradeable up to
  2 years out offseason, 5 years in-season).
- **Budget ledger** — per team per season: base budget, contract costs,
  buyout penalties, farm costs, missed-IP penalties, cash traded in/out →
  remaining auction budget. Every line traceable to its source.
- **Dropped contracts** — reference list of contracted players dropped
  in-season (affects signability).
- **Audit log** — who changed what, when, and why.

## 6. Rules engine

The league rules as the app implements them, including decisions the
rulebook doesn't spell out, live in **[RULES.md](RULES.md)**. The engine is a
pure-Python package (`rules/`) with no web or database dependencies, checked
by golden tests against the Year 19 workbook.

## 7. Fantrax integration

- Uses the open-source `FantraxAPI` Python library (sibling project). It is
  **read-only** and needs the commissioner's logged-in **cookie** for private
  data.
- Fantrax is the source of truth for **rosters** and **end-of-season
  salaries**, which become each signable player's *original price*.
- The app is the source of truth for **contract history** (lengths, buyouts,
  farm) — seeded once from the Year 19 workbook.
- The sync is **on demand** (a commissioner button or management command), not
  scheduled.
- What Fantrax does and doesn't record (from the 2026 snapshot):
  - Rosters, salaries, claims, drops and trades, with player IDs. Salary
    equals the contract's annual price for contracted players.
  - Farm-pick ownership for future years (`draftPicksData`), which the app
    imports as the source of truth.
  - Cash in trades appears only as a free-text commissioner comment. The app
    lists these comments, and the commissioner enters the cash trade.
  - Its "Year Signed" and "Contract Expires" columns are hand-kept and
    unreliable; the app ignores them.
- Franchises are keyed by code and Fantrax ID, with every name the sheet uses
  kept as an alias (`data/league/teams.json`). AG (Big Beautiful Baseball
  Team) is the same franchise as EP (Winning DeLautery).
- **Long-term direction:** a live, largely automatic link to the Fantrax
  league. Transactions, trades, roster moves and weekly stats would flow in on
  a schedule, so contract status, buyouts, penalties and cap checks update
  without the commissioner doing anything. The MVP doesn't need this, but the
  design should allow it: keep the Fantrax client behind one sync interface,
  and store Fantrax IDs on every player, team and transaction.

## 8. MVP scope — February 2027 signing

**In:**
- Magic-link auth; manager↔team linking; commissioner role.
- One-time **import of the Year 19 workbook** (contracts, buyouts, farm,
  dropped contracts, cash trades, farm-pick ownership), plus a commissioner
  reconciliation screen for the known spreadsheet errors.
- **Fantrax sync:** final rosters + salaries → each team's signable pool.
- **Manager signing screen:** signable players with a price preview for each
  length, buyout decisions with full penalty schedules, farm retention,
  live budget, validation, save draft, submit.
- **Commissioner console:** submission status for every team, edit/override
  with notes, open/lock signing, enter offseason cash trades and
  farm-pick trades, missed-IP penalties, manual farm-player adds, season
  rollover.
- **League views:** all contracts, budgets, buyouts, farm systems, cash-trade
  ledger, farm-pick ownership / farm draft order.
- **Export** (CSV/xlsx) of the final league state.
- **Audit log.**

**Out of the MVP (planned later):**
- Running the farm draft in the app. It stays on Discord and Fantrax for now,
  and the commissioner enters picks by hand.

## 9. Roadmap beyond MVP (unordered)

All of these are admin work done by hand outside Fantrax today:

- **Live Fantrax link:** scheduled sync of rosters and transactions, so the
  app picks up drops, pickups and trades itself (for example, a contract
  becomes a buyout when a player is dropped, or cash moves when a trade
  processes). Most items below depend on it.
- **Farm draft in the app:** the rulebook's pick order, pick ownership,
  default picks (highest available Baseball America Top 100), and picks
  recorded automatically.
- Automated missed-IP penalties and deadbeat-deposit tracking from Fantrax
  weekly data.
- League history: import past years; contract and trade archive.
- **Discord integration:** post to the league server when signing opens, a
  deadline approaches, a team submits, a period locks, or a trade is pending.
  Later, a bot for lookups (`/contract <player>`, `/budget <team>`) and farm
  draft picks made in a channel.
- Dues and payout tracking.

## 10. Non-goals

- **Anything Fantrax already does.** Trade proposals and vetoes, the auction,
  the snake draft, lineups, waivers/FAAB, scoring, and in-season roster and
  salary-cap enforcement all stay in Fantrax. This app only covers the extra
  administration the league does by hand outside Fantrax: contracts,
  buyouts, the farm system, and the auction-budget math. It *records the
  effects* of Fantrax activity (e.g. cash or farm picks moving in a trade)
  but never replaces it.
- Handling money (dues stay on PayPal via the banker).
- Multi-league SaaS. The app is built for one league, though nothing should
  make a second league impossible.

## 11. Architecture (initial)

- **Django + Postgres**, server-rendered pages with **HTMX** for the live
  signing screen; Django admin as the commissioner's escape hatch.
- Hosted on **Render** (web service + managed Postgres with daily backups).
- **Transactional email** provider (Postmark / Resend / SES) for magic links.
- `FantraxAPI` as a dependency; the commissioner's Fantrax cookie is stored as
  a secret.
- Rules engine as an isolated package with its own test suite.

## 12. Success criteria

- The February 2027 signing period runs entirely in the app, with no
  spreadsheet round-trip.
- All teams submit through the app; the commissioner only reviews and locks.
- Zero budget-math disputes caused by the tool.
- The app's results match the workbook for Year 19 except for the documented
  spreadsheet bugs.

## 13. Milestones

These assume we build together (human + Claude Code) in focused sessions. The
MVP is small: one Django app, one rules module, about a dozen screens. So the
build fits in about a month, and the time after that goes to real-world
testing with the league.

| Target | Milestone | Done when |
|---|---|---|
| **Week of Sep 28** | **Foundations.** Fantrax spike against the just-ended league (before it rolls over), with end-of-season rosters + salaries saved to disk. Django project skeleton, CI, deployed to Render. Rules engine + golden tests from the Year 19 workbook. | Salary snapshot saved; rules tests pass; "hello" page live on Render |
| **Week of Oct 5** | **Data in.** Domain models, workbook importer, Fantrax import, reconciliation report against the spreadsheet. | Year 19 state fully loaded; every difference from the sheet explained |
| **Week of Oct 12** | **Read side.** Magic-link auth, manager↔team linking, league-wide views (contracts, budgets, buyouts, farms, pick ownership). | Every manager can log in and see their team |
| **Week of Oct 19** | **Signing flow.** Manager signing screen with live budget + validation; commissioner console (status, override, open/lock, manual adjustments); audit log; export. | Full signing period runs start-to-finish on test data |
| **Late Oct – Nov** | **Dry run + league beta.** Commissioner replays the Year 19→20 signing; then invite managers to click around and report issues. | League has seen it; no open correctness bugs |
| **Dec – Jan** | **Buffer + first extras.** Fix beta feedback; optionally start Discord notifications or the farm draft. Final data refresh plan for February. | MVP frozen by Jan 31 |
| **Mid-Feb 2027** | **Live signing period.** | All teams signed in-app; budgets published |

## 14. Open questions

1. **Fantrax data:** does the API expose end-of-season salaries per player?
   Does each season get a new Fantrax league ID?
2. ~~Signability rules~~ — **resolved** (see RULES.md).
3. **Buyout timing:** precise definition of "first unpaid year" relative to
   the season a player is dropped, including in-season drops. (The sheet
   charges 80% in the first signing after the drop, then 70%, 60%, …)
4. ~~Contract years~~ — **resolved**: final year = S+L, inclusive (see RULES.md).
5. **Special Waivers:** the rulebook refers to a section that wasn't in the exported doc (another tab?) —
   write it, or drop it?
6. **Sign-and-trade:** how does the >10-contract exception get recorded?
7. **Legacy contracts:** are any pre-2014 contracts still active, or can the
   old formula be retired?
8. **Missed-IP penalties:** commissioner-entered for MVP, or pulled from
   Fantrax?
9. **Co-commissioners and the banker:** do they need special roles?
10. **Sign-in:** email magic link, or Sign in with Discord? Discord OAuth needs
    no email provider and matches where the league already talks, but every
    manager needs a Discord account linked to their team.
11. **Farm draft channel:** the farm draft now happens on Discord, not email.
    Confirm, and decide whether the MVP should import picks from a channel or
    stick with manual entry.
12. **Rulebook ownership:** keep the Google Doc as the source and snapshot it
    here, or move the rulebook into the app?
