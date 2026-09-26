# Dynasty Commish — Architecture

> Status: reflects the code as of **M3 (Read side)**, in progress 2026-09-26.
> Sections marked **(planned)** describe where M2+ is headed. They're design
> intent, not code yet. For goals, scope and the rules themselves, see
> [VISION.md](VISION.md) and [the rulebook](docs/reference/rulebook-year19.md).

## 1. The big picture

```
                 ┌──────────────────────────── Render ─────────────────────────────┐
                 │                                                                 │
  Managers ────► │  gunicorn ─► Django (config/)                                   │
  Commissioner   │               ├─ core/      pages, healthz                      │
                 │               ├─ league/    domain models, views   (planned)    │
                 │               └─ rules/     pure-Python engine ◄── called by ─┐ │
                 │                                                               │ │
                 │               Postgres (managed) ◄── Django ORM ──────────────┘ │
                 └─────────────────────────────────────────────────────────────────┘
                                        ▲
                         one-off / on-demand imports
                                        │
          ┌─────────────────────────────┴──────────────────────────────┐
          │                                                            │
  Fantrax (read-only, cookie auth)                  Year 19 contract workbook (.xlsx)
  scripts/fantrax_snapshot.py ─► data/fantrax/      scripts/extract_workbook_fixture.py
```

The app has three layers, and the dependencies only point downward:

1. **Web layer** (`config/`, `core/`, future apps): Django views, templates
   and HTMX. It owns requests, auth, permissions and the audit trail.
2. **Persistence** (Django models on Postgres): league state, meaning
   contracts, buyouts, farm, picks, cash trades and signing decisions.
3. **Rules engine** (`rules/`): pure functions over plain dataclasses. It
   never imports Django and never touches the database.

Everything that talks to the outside world (Fantrax, the workbook) enters
through scripts or import commands, never from inside a request.

## 2. Repository layout

| Path | What lives there |
|---|---|
| `config/` | Django project: settings, root URLs, WSGI/ASGI. |
| `core/` | Site shell: base template, CSS, `/healthz`. |
| `league/` | Domain models, import and reconcile commands, budget adapter, league pages. |
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
`compute_budget(base=…)`, `validate_signing(limit=…, extra_allowed=…)`).
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
  - `DATABASE_URL` (via `dj-database-url`). Falls back to SQLite locally.
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
  - `/teams/`, `/teams/<code>/`, `/contracts/`, `/buyouts/`, `/farm/`, `/picks/` and `/cash/` are league pages that need sign-in.
  - `/auth/…` is Discord sign-in, `/healthz` is the Render health check, and `/admin/` is the Django admin.
- League pages show the *next* auction's committed money (`LEAGUE_SEASON` + 1), using
  `league.budget.team_budget`. While reconciliation items are pending, a banner says the numbers
  may still change.

### (planned) Domain app and screens

- A domain app holding the models from VISION §5: `Season`, `Team`,
  `Manager`, `Player`, `Contract`, `Buyout`, `FarmPlayer`, `FarmPick`,
  budget adjustments (cash trades, missed-IP penalties), `SigningDecision`
  and `AuditLog`. Every external entity stores its **Fantrax ID**.
- **Model ↔ engine boundary:** a thin adapter turns ORM rows into
  `rules.Contract` values and back. Views never re-implement rule math. They
  call the engine and render its output.
- **Signing screen:** server-rendered Django templates, with HTMX partials
  re-rendering the budget and validation panel on each change.
- **Commissioner console:** custom views for the common workflows, with
  Django admin as the escape hatch. Every write that changes league state
  writes an audit entry (who, what, when, note).
- **Auth (built in M3):** Sign in with Discord.
  - `accounts/views.py` stores a single-use `state` in the session and compares it in constant time.
  - It exchanges the code on the server and reads `/users/@me`. The token isn't kept.
  - Only a Discord ID that the commissioner has put on a `Manager` (in the admin) gets in. A User is
    created on that manager's first sign-in. Anyone else sees their Discord ID and is asked to send it
    to the commissioner.
  - Commissioner = Django staff. Permissions: managers
  edit only their own team while signing is open. The commissioner can edit
  everything.

## 5. External data

### Fantrax

- **Read-only.** The app never writes to Fantrax.
- Auth is the commissioner's browser cookie, kept in
  `secrets/fantrax_cookie.txt` locally (gitignored). **(planned)** An env var
  secret on Render.
- `scripts/fantrax_client.py` is a minimal helper that POSTs to Fantrax's
  `fxpa/req` endpoint. `scripts/fantrax_snapshot.py` saves league info, teams,
  every roster, the transaction history and a flattened `rosters.csv` into
  `data/fantrax/<name>/`.
- Raw JSON is committed so imports are reproducible and don't depend on
  Fantrax staying up. Fantrax renewal creates a **new league ID** and leaves the
  old one frozen, so an end-of-season snapshot isn't a race against rollover.
- **Source-of-truth split:** Fantrax owns rosters and end-of-season salaries
  (salary = original price). The app owns contract history (lengths,
  buyouts, farm). When the sheet and Fantrax disagree on a salary, Fantrax wins.
- **(planned)** All Fantrax access goes behind one sync interface (on-demand
  command / commissioner button for the MVP). This leaves room for the
  scheduled live sync on the roadmap without touching callers.

### Contract workbook

- The Year 19 workbook is the one-time seed for contract history.
  `docs/reference/` holds the pre-signing export.
- **(planned)** An import command loads it into the models, plus a
  reconciliation report explaining every difference from the sheet (the same
  discipline as the golden tests).

## 6. Deployment and operations

- **Render Blueprint** (`render.yaml`): one Python web service plus managed
  Postgres. Free tier for M1. Upgrade to paid plans with backups **before the
  league beta** (free Postgres expires).
- **Build** (`bin/render-build.sh`): `uv sync --frozen --no-dev` →
  `collectstatic` → `migrate`. Migrations run on every deploy.
- **Run:** `uv run --frozen --no-dev gunicorn config.wsgi:application`.
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
  `DATABASE_URL` is wired from the managed database.

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
| Render with config from env vars | Simple managed hosting. The same settings file works in every environment. |
