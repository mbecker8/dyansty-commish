# Dynasty Commish — Architecture

> Status: reflects the code as of **Season rollover** (#21, 2026-09-26).
> Sections marked **(planned)** describe design intent, not code yet. For goals, scope and the rules themselves, see
> [VISION.md](VISION.md) and [the rulebook](docs/reference/rulebook-year19.md).

## 1. The big picture

```
                 ┌──────────────────────────── Render ─────────────────────────────┐
                 │                                                                 │
  Managers ────► │  gunicorn ─► Django (config/)                                   │
  Commissioner   │               ├─ core/      pages, healthz                      │
                 │               ├─ league/    models, views, Fantrax events       │
                 │               └─ rules/     pure-Python engine ◄── called by ─┐ │
                 │                                                               │ │
                 │               Postgres (managed) ◄── Django ORM ──────────────┘ │
                 └─────────────────────────────────────────────────────────────────┘
                                        ▲
             imports, and on-demand Fantrax syncs (command or console button)
                                        │
          ┌─────────────────────────────┴──────────────────────────────┐
          │                                                            │
  Fantrax (read-only, cookie auth)                  Year 19 contract workbook (.xlsx)
  league/fantrax_client.py (live sync)              scripts/extract_workbook_fixture.py
  scripts/fantrax_snapshot.py ─► data/fantrax/
```

The app has three layers, and the dependencies only point downward:

1. **Web layer** (`config/`, `core/`, future apps): Django views, templates
   and HTMX. It owns requests, auth, permissions and the audit trail.
2. **Persistence** (Django models on Postgres): league state, meaning
   contracts, buyouts, farm, picks, cash trades and signing decisions.
3. **Rules engine** (`rules/`): pure functions over plain dataclasses. It
   never imports Django and never touches the database.

The workbook enters only through import commands. Fantrax enters through
`league.events.sync`: from a saved snapshot, from `manage.py sync_fantrax`, or
from the console's Sync from Fantrax button, which fetches inside that request
before opening the transaction. Nothing ever writes to Fantrax.

## 2. Repository layout

| Path | What lives there |
|---|---|
| `config/` | Django project: settings, root URLs, WSGI/ASGI. |
| `core/` | Site shell: base template, CSS, `/healthz`. |
| `league/` | Domain models, import and Fantrax sync commands, event processor, budget adapter, league pages. |
| `accounts/` | Sign in with Discord (OAuth2, `identify` scope). |
| `rules/` | League rules engine plus its tests (`rules/tests/`), including golden tests. |
| `scripts/` | Standalone tools run by hand: the Fantrax snapshot and the workbook fixture extractor. |
| `data/fantrax/<snapshot>/` | Raw Fantrax JSON saved to disk (e.g. `2026-final`, the end-of-season snapshot). Committed. |
| `docs/reference/` | Source documents: the Year 19 rulebook and the pre-signing workbook. |
| `bin/render-build.sh` | Render build step. |
| `render.yaml` | Render Blueprint (web service + Postgres). |
| `.github/workflows/ci.yml` | CI: lint, format, migration check, tests against Postgres. |
| `VISION.md` | Product vision, scope, milestones, open questions. |

## 3. Rules engine (`rules/`)

The core of the product. VISION §6 says *what* the rules are. This section
covers how the code is shaped.

### Principles

- **No framework imports.** `rules` depends only on the standard library, so
  it can be tested exhaustively and quickly, and reused from views, import
  commands and scripts.
- **Immutable values in, values out.** Inputs are frozen dataclasses
  (`Contract`) or plain ints/mappings. Outputs are values (`Budget`,
  `Signability`, `dict[int, int]` schedules, `list[str]` errors). Nothing is
  mutated or stored.
- **Money is whole dollars (`int`); seasons are calendar years (`int`).** No
  floats anywhere. Percentages round half-up in integer arithmetic
  (`round_half_up_pct`) to match the spreadsheet's `ROUND()`.
- **Players are keyed by Fantrax player ID**, never by name, because names
  aren't unique.
- **Traceable results.** `Budget` keeps each charge as a
  `(kind, player_id, amount)` line, so every dollar can be explained in the UI.

### Modules

| Module | Responsibility | Key API |
|---|---|---|
| `contracts.py` | Contract pricing and term (S+1 … S+L inclusive); legacy pre-2014 escalation. | `annual_price()`, `Contract` (`final_year`, `covers()`, `years_left()`) |
| `buyouts.py` | Penalty schedule for a dropped contract: 80%, 70%, 60%, … per unpaid season. | `buyout_schedule(contract, dropped_in_season)` |
| `farm.py` | Farm retention salary bump (+$1, or +$2 with an MLB appearance). | `retained_salary()` |
| `signability.py` | Classifies a rostered player at a signing period. | `signability()` → `Signability` |
| `validation.py` | Checks a team's signing decisions (limit, duplicates, overlaps, year signed). | `validate_signing()` → `list[str]` |
| `budget.py` | Auction budget: base − contracts − buyouts − farm − missed IP ± cash. | `compute_budget()` → `Budget` |

### Rule constants

Constants (`BASE_BUDGET`, `CONTRACT_LIMIT`, buyout percentages, farm bumps,
`CURRENT_FORMULA_SINCE`) are module-level today. Functions that a commissioner
might tune take them as keyword arguments with those defaults (e.g.
`compute_budget(base=…)`, `validate_signing(limit=…)`).
**(planned)** A per-season rules configuration stored with the `Season`
record, passed into the engine by the caller. The engine stays unaware of
where the numbers come from.

### Testing

- **Unit tests** for each module (`rules/tests/test_*.py`).
- **Golden tests** (`test_golden_year19.py`) run the engine against cached
  values from the Year 19 post-signing workbook. The fixture
  (`rules/tests/fixtures/year19_post_signing.json`) is produced by
  `scripts/extract_workbook_fixture.py`, which reads team tabs only and finds
  blocks by their labels, because the tabs have drifted apart. Every known
  disagreement is listed in `SHEET_BUGS` with its explanation. Any new
  difference fails the test.

## 4. Web application

### Today

- `config/settings.py` reads everything from environment variables, so one
  file serves local dev, CI and Render:
  - `DATABASE_URL` (via `dj-database-url`). Falls back to SQLite locally, where
    transactions take the write lock up front (`IMMEDIATE`), so overlapping
    writes such as a double-clicked sync wait instead of failing.
  - `FANTRAX_COOKIE` and `FANTRAX_SECRET_ID`, else the matching files in `secrets/`.
  - `DJANGO_DEBUG` defaults to on locally and **off on Render** (detected by
    `RENDER`), so production fails safe.
  - `DJANGO_SECRET_KEY` is required whenever DEBUG is off.
  - `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` come from
    `DJANGO_ALLOWED_HOSTS` plus Render's `RENDER_EXTERNAL_HOSTNAME`.
  - With DEBUG off: HTTPS redirect (except `/healthz`), secure cookies,
    proxy SSL header.
- Static files are served by WhiteNoise (compressed, manifest-hashed).
- Routes:
  - `/` sends you to your team, or to sign-in.
  - `/teams/`, `/teams/<code>/`, `/contracts/`, `/buyouts/`, `/farm/`, `/picks/`, `/picks/order/` (farm draft order), `/cash/`, `/export/` and `/help/` are league pages that need sign-in.
  - `/signing/<code>/` is a team's signing page; `/commish/`, `/commish/audit/` and `/commish/runbook/` (the season runbook) are for commissioners.
  - `/auth/…` is Discord sign-in, `/healthz` is the Render health check, and `/admin/` is the Django admin.
- **Layout** (`core/templates/base.html`): a left sidebar grouped into League, More and Commissioner, plus
  breadcrumbs built by `league.context_processors.breadcrumbs` from the URL name.
- **Grids:** a `<table class="grid">` gets click-to-sort headers and a filter box per column from
  `core/static/core/grid.js`, which works on the rendered cells, so the page still works without JS. The
  league-wide Contracts, Buyouts, Farm, Farm picks and Cash tables use it.
- **Fantrax roster links:** `FantraxTeam` holds each team's ID per Fantrax league, because renewal gives
  every team a new ID. `import_league` (and migration 0015) seeds the 2026 league. Every sync records the
  IDs it matched, and the link uses the newest league the team has been matched in.
- **Farm draft order:** `FinalStanding` (season, team, place) is entered in the admin, and `import_league`
  seeds it from `data/league/final_standings.json`. Pick numbers (1–28) are computed from the previous
  season's places by `rules.farm.pick_number`, never stored. Places aren't unique, so two teams can swap
  in the admin, and the draft order page flags a duplicate.
- League pages show the *next* auction's committed money (`seasons.current_season()` + 1),
  using `league.budget.team_budget`.

### Signing, console, audit and export (built in M4)

- **Models** (`league/models.py`): `RosterEntry` (the blackout roster and each player's
  season-end salary, loaded by `manage.py sync_rosters`), `SigningPeriod` (planned → open →
  locked), `Submission` per team with `SubmissionSigning` / `SubmissionBuyout` /
  `SubmissionFarm` rows, and `AuditEntry` (who, what, when, team, note).
- **`league/signing.py`** holds the logic; views only render it.
  - `pool()` classifies a team's roster with `rules.signability`.
  - `plan_from_form()` parses the form and trusts nothing. `evaluate()` checks every id
    against the team and prices the plan.
  - `league.budget.team_budget(team, season, Changes(...))` does the pricing: the pending
    decisions are passed as `Changes`, so a preview and the post-lock budget take the same
    code path. The end-to-end test asserts they're equal for every team.
  - `lock_period()` applies every team's plan in one transaction, with `select_for_update`
    on the period.
- **Model ↔ engine boundary:** ORM rows become `rules.Contract` values through
  `Contract.as_rules()`. Views never re-implement rule math.
- **Signing screen** (`/signing/<code>/`): a server-rendered form. HTMX (vendored in
  `core/static/core/htmx.min.js`) posts it to `/signing/<code>/preview` on every change and
  swaps in the budget panel. The page works without JavaScript, minus the live panel.
  - Drafts are private to the team and commissioners.
  - A manager can't change a submitted plan until they withdraw it.
  - A commissioner can edit any team, with a required note.
- **Commissioner console** (`/commish/`): open and lock signing, see every team's status,
  budget and problems, jump to any team's page, and follow links to the admin for manual
  entries (cash trades before 2027, missed-IP penalties, farm picks and farm players), and the
  Discord panel (Sync from Discord, Discord exceptions).
- **Audit log** (`/commish/audit/`): signing saves, submits, withdrawals, open, lock,
  roster syncs, Fantrax syncs, resolved exceptions, and every admin add, change or delete (through
  `league.admin.AuditedAdmin`).
- **Export** (`/export/`): CSV downloads of budgets, contracts, buyouts, farm, picks and
  cash, built from the same row helpers as the league pages.

### Fantrax events

- **Models:** `FantraxLeague` (a league the sync reads: Fantrax ID, season, start time, active)
  and `FantraxEvent` (one processed fact, unique `key`, with the effect and what it touched).
  `import_league` seeds the 2026 league.
- **`league/events.py`:** `sync([(league, snapshot)])` applies everything new in one
  transaction. Transactions are keyed `tx:<txSetId>:<player>:<kind>`; roster-derived facts
  (promotion, debut, unknown Minors player, roster mismatch) have their own keys. Fantrax trade
  comments are ignored: cash comes from Discord.
  A key already stored is skipped, so re-running is safe and hand fixes in the admin stick.
- Moves are applied against current records, so a trade after the signing locks moves the new
  contract. A contract signed at the signing after a season ignores that season's moves from
  before the lock.
- A move's season comes from its date (`seasons.season_at`), not from `FantraxLeague.season`,
  which is now only a label and picks the newest league for roster facts. `sync` raises
  `SeasonMissing` (nothing saved) for moves over a year past the last auction start.
- Claims in a season's farm draft window become `FARM_DRAFTED` farm players at $1, spending the
  team's lowest unused `FarmPick` (recorded on `FarmPick.player`); no pick left is an exception.
- Facts the app can't interpret are `EXCEPTION` events. `signing.open_period` refuses while
  any are unresolved; the console resolves them with a required note.
- **Entry points:** `manage.py sync_fantrax` (live, or `--snapshot`, `--dry-run`) and the
  console's Sync from Fantrax. Both fetch before the transaction starts.
- The golden test (`league/tests/test_sync_2026.py`) checks the 2026 result against the state
  the old reconciliation queue produced when everything was accepted.

### Discord cash trades

- Cash for the 2027 auction on comes only from the league Discord's "Rules and Accounting"
  category, channels `#trades-<year>-assets` (the year is `CashTrade.budget_season`). Commissioner
  Bot (its own Discord app) reads them over REST, on demand; production goes through the
  Cloudflare Worker (`docs/discord-signin.md`).
- **`league/discord_client.py`:** `fetch_category()` lists the category's text channels and pages
  through each one's messages. A private channel is skipped, not an error.
- **`league/discord_cash.py`:** pure rules. `read_post()` turns a post into a `Reading` (CASH,
  NOT_CASH or EXCEPTION, with the wanted cash trades); mentions map to teams through
  `Manager.discord_id`.
- **`league/discord_sync.py`:** `sync(channels)` stores every message once as a `DiscordMessage`
  (by its Discord ID) and makes each post's `CashTrade`s equal what it says now: edits update,
  deletions remove, both audited, unless that season has a `SeasonBudget` (then it's an
  exception). Only channels read in the run can mark messages deleted. `resolve()` closes an
  exception, optionally entering the cash; an edit to a resolved post reopens it.
  `signing.open_period` refuses while any are unresolved.
- **Freeze:** the Fantrax sync that would freeze the next auction's budgets raises `FreezeBlocked`
  (nothing saved) until Discord has synced since the auction start with no open exceptions
  (`seasons.discord_blocks_freeze`); the season checklist has the same two checks. Only when the
  bot is configured.
- **Entry points:** `manage.py sync_discord [--dry-run]` and the console's Sync from Discord.
  The admin refuses hand-entered cash for 2027 on; `DiscordMessage` is read-only there.

### Seasons and rollover

- **Models:** `Season` (year, farm draft start, auction start, started at/by) replaces the old
  `LEAGUE_SEASON` setting; a migration seeds 2026. `SeasonBudget` is each team's auction budget,
  frozen as of the auction start and never recomputed. A started season can't be deleted.
- **`league/seasons.py`:** `current_season()` (the latest started season), `season_at(when)`,
  `farm_draft_at(when)`, `window(season)` (draft day to draft day, for the team page), and the
  rollover: `set_dates` (refuses dates in time already synced), `budgets_due`/`freeze_budgets`,
  `checklist` and `start(year, user)`.
- **Freeze in the sync:** `events.check_seasons` refuses a sync once signing is locked and the
  next draft day isn't entered. When `budgets_due`, the sync applies every move before the auction
  start, freezes the budgets (`frozen_at` = the auction start), then applies the rest.
- **Start** locks the `Season` row, re-checks the checklist, freezes the budgets only if no sync
  did, adds the next year of farm picks and audits it.
- **Console:** a season section to enter dates, see the checklist and start the season. The team
  page shows the frozen budget.

### Sign-in and permissions

- **Auth (built in M3):** Sign in with Discord.
  - `accounts/views.py` stores a single-use `state` in the session and compares it in constant time.
  - It exchanges the code on the server and reads `/users/@me`. The token isn't kept.
  - Those two server calls go to `DISCORD_API_BASE`. In production that's our Cloudflare Worker
    (`cloudflare/discord-proxy/`), which forwards only those two calls and only with the
    `X-Proxy-Key` header, because Discord's Cloudflare blocks Render's shared outbound IPs with 429s.
    A 429 is `DiscordBlocked` and gets its own message. Every failure is logged with Discord's status.
    Setup: `docs/discord-signin.md`.
  - Only a Discord ID that the commissioner has put on a `Manager` (in the admin) gets in. A User is
    created on that manager's first sign-in. Anyone else sees their Discord ID and is asked to send it
    to the commissioner.
  - The account is keyed by Discord ID (`discord-<id>`), so handing a team to someone else never
    hands over the old account.
  - `accounts.middleware.DiscordAccountMiddleware` re-checks the link on every request. It turns
    admin rights (staff + superuser) on exactly while the Manager is marked **Is commissioner**, so
    unlinking or removing the flag applies immediately, the admin included.
  - `COMMISSIONER_DISCORD_IDS` (env, comma-separated) makes those Discord accounts commissioners
    with or without a Manager. It's how the first commissioner signs in on an empty database.
  - League pages use `league.access.member_required`: a linked Discord manager, a listed commissioner,
    or a local password account. `commissioner_required` adds staff. `manages(user, team)` decides who edits
    which team's signing.

## 5. External data

### Fantrax

- **Read-only.** The app never writes to Fantrax.
- Transactions, trades and stats need the logged-in `fxpa/req` API, so auth is the
  commissioner's browser cookie: `FANTRAX_COOKIE` (a Render secret), else
  `secrets/fantrax_cookie.txt` locally (gitignored). The Fantrax Secret ID
  (`FANTRAX_SECRET_ID`, else `secrets/fantrax_secret_id.txt`) only works for the
  public `getLeagues` call, which the console uses to find the renewed league.
- `league/fantrax_client.py` fetches a league into memory; `Snapshot.from_raw`
  reads it exactly like a saved snapshot. `scripts/fantrax_snapshot.py` uses the
  same client to save league info, teams, every roster, the transaction history
  and a flattened `rosters.csv` into `data/fantrax/<name>/`.
- Raw JSON is committed so imports are reproducible and don't depend on
  Fantrax staying up. Fantrax renewal creates a **new league ID** and leaves the
  old one frozen, so an end-of-season snapshot isn't a race against rollover.
- **Source-of-truth split:** Fantrax owns rosters and end-of-season salaries
  (salary = original price). The app owns contract history (lengths,
  buyouts, farm). When the sheet and Fantrax disagree on a salary, Fantrax wins.
- All transaction access goes through `league.events.sync` (on-demand command
  and console button). A scheduled live sync (#33) can call the same code.

### Contract workbook

- The Year 19 workbook is the one-time seed for contract history.
  `docs/reference/` holds the pre-signing export.
- `manage.py import_league` loads it (plus the 2026 Fantrax snapshot and
  `data/league/`) into the models and seeds the 2026 `FantraxLeague`. It refuses
  `--replace` once anyone has made changes, including applied Fantrax events.
- In production, the console's **Load league** (`league.setup.load_league`, empty database
  only) runs `import_league` and then the 2026 sync from the committed snapshot, and **Load
  rosters** runs `sync_rosters` on a committed snapshot in `data/fantrax/`.

## 6. Deployment and operations

- **Render Blueprint** (`render.yaml`): one Python web service plus managed
  Postgres. Postgres is on the paid basic plan (daily backups). The web service
  is free, so it sleeps when idle and has no Shell: move it to Starter before
  signing opens to avoid cold starts.
- **No shell in production.** Every task runs from the commissioner console or the
  admin; the first commissioner comes from `COMMISSIONER_DISCORD_IDS`.
- **Build** (`bin/render-build.sh`): `uv sync --frozen --no-dev` →
  `collectstatic` → `migrate`. Migrations run on every deploy.
- **Run:** `uv run --frozen --no-dev gunicorn config.wsgi:application --timeout 120`. The long
  timeout is for Sync from Fantrax, which makes about 15 Fantrax calls (about 25s).
- **Health check:** `GET /healthz` returns `ok` and is exempt from the HTTPS
  redirect.
- Python 3.14 and Django 6.1, pinned with `uv.lock`. Every install uses
  `--frozen`, so the lockfile is authoritative.

## 7. CI and quality gates

GitHub Actions (`.github/workflows/ci.yml`) runs on every PR and on pushes to
`main`, against a Postgres 17 service:

1. `ruff check .` (rules E, F, I, UP, B; line length 120)
2. `ruff format --check .`
3. `makemigrations --check --dry-run`, so model changes must ship with migrations
4. `pytest -q` over `core/`, `rules/`, `league/` and `accounts/`

## 8. Secrets and sensitive data

- `secrets/`, `.env` and `*.cookie` are gitignored. The Fantrax cookie never
  gets committed.
- The workbook's **Contact Info** tab holds personal data. The fixture
  extractor reads team tabs only and never touches it.
- On Render, `DJANGO_SECRET_KEY` is generated by the Blueprint and
  `DATABASE_URL` is wired from the managed database. `FANTRAX_COOKIE`,
  `FANTRAX_SECRET_ID` and the Discord credentials are set in the dashboard
  (`sync: false`). The cookie is a login to the commissioner's whole Fantrax
  account: it's never stored in the database, logged or shown.

## 9. Key decisions

| Decision | Why |
|---|---|
| Django + Postgres + HTMX, server-rendered | One small team, ~14 users, a dozen screens. Admin comes free, and there's no SPA to maintain. |
| Rules engine as a pure package | The rules are the product. They need exhaustive, fast tests and must not drift between screens. |
| Integer dollars, half-up rounding | Matches the spreadsheet exactly and avoids float drift in budgets. |
| Key everything by Fantrax player ID | Names collide and change. IDs are also the join key for syncs. |
| Golden tests against the real workbook | Parity with the sheet is a success criterion. Every difference must be explained. |
| Commit raw Fantrax snapshots | Reproducible imports. Protects against API changes or outages. |
| Fantrax read-only, on-demand sync (MVP) | Keeps the scope out of anything Fantrax already does (VISION §10). |
| Fantrax moves applied as events, not approved one by one | They're facts that already happened. Each is stored once by key, so re-runs are safe and hand fixes stick; only what the app can't interpret waits for the commissioner. |
| Render with config from env vars | Simple managed hosting. The same settings file works in every environment. |
