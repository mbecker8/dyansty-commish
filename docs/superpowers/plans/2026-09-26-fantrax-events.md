# Fantrax Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the reconciliation approval queue with automatic, once-only processing of Fantrax events, plus exceptions the commissioner resolves.

**Architecture:** `league/fantrax_client.py` fetches raw Fantrax JSON (cookie auth) and `Snapshot.from_raw` wraps it, so a saved snapshot and a live fetch look the same. `league/events.py` applies moves and roster-derived facts to contracts and farm players, storing one `FantraxEvent` per fact under a unique key. `sync_fantrax` (command) and a console button drive it. `ReconciliationItem` and `reconcile` are removed.

**Tech Stack:** Django 5, pytest-django, requests, SQLite locally / Postgres on Render.

**Spec:** `docs/superpowers/specs/2026-09-26-fantrax-events-design.md`

## Global Constraints

- Run tests with `uv run pytest -q`; lint with `uv run ruff check . && uv run ruff format --check .`; migrations check with `uv run python manage.py makemigrations --check --dry-run`.
- The 2026 league: Fantrax ID `p3z8zy75mgdm460o`, season 2026, process moves from `2026-02-25T16:00` America/New_York.
- Secrets: `FANTRAX_COOKIE` (env, else `secrets/fantrax_cookie.txt`), `FANTRAX_SECRET_ID` (env, else `secrets/fantrax_secret_id.txt`). Never log, store in the DB, or render either.
- Order at setup: `import_league` → `sync_fantrax --snapshot` → `sync_rosters` (the golden state was captured before `sync_rosters` links extra players).
- Golden expectation: `league/tests/data/events_2026_golden.json` (committed with this plan; generated from the old code by accepting every 2026 item except FARM_UNKNOWN / INCONSISTENT / FARM_INCONSISTENT / CASH_COMMENT). Its `unknown_minors` are Brendan Lawson and Shunpeita Yamashita.
- Copy style: plain English, no em-dash-heavy prose; match the existing templates.
- Every admin change stays audited (`AuditedAdmin`).

---

### Task 1: Fantrax client in the app, `Snapshot.from_raw`, secrets settings

**Files:**
- Create: `league/fantrax_client.py`
- Modify: `league/fantrax_data.py` (Snapshot constructor), `scripts/fantrax_snapshot.py`, `config/settings.py`
- Delete: `scripts/fantrax_client.py`
- Test: `league/tests/test_fantrax_client.py`

**Interfaces:**
- Produces: `class FantraxError(Exception)`, `class FantraxLoginExpired(FantraxError)`; `session(cookie: str) -> requests.Session`; `call(s, league_id, *msgs) -> dict`; `fetch_raw(s, league_id) -> dict` with keys `league_info, teams, rosters (team id -> roster), transactions (list of pages), trades`; `list_leagues(secret_id) -> list[dict]` (getLeagues rows); `Snapshot.from_raw(raw: dict) -> Snapshot`; settings `FANTRAX_COOKIE`, `FANTRAX_SECRET_ID`.

- [ ] **Step 1: Failing tests** in `league/tests/test_fantrax_client.py`:

```python
import json
from pathlib import Path

import pytest

from league import fantrax_client
from league.fantrax_data import Snapshot

SNAP = Path(__file__).resolve().parents[2] / "data" / "fantrax" / "2026-final"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    """Answers fxpa/req calls from the saved 2026 snapshot."""

    def __init__(self, logged_in=True):
        self.logged_in = logged_in
        self.teams = json.loads((SNAP / "teams.json").read_text())

    def post(self, url, params=None, json=None, timeout=None):
        if not self.logged_in:
            return FakeResponse({"pageError": {"code": "WARNING_NOT_LOGGED_IN"}})
        msg = json["msgs"][0]
        method, data = msg["method"], msg["data"]
        if method == "getFantasyLeagueInfo":
            body = __import__("json").loads((SNAP / "league_info.json").read_text())
        elif method == "getTeamRosterInfo":
            team = data.get("teamId", self.teams[0]["id"])
            body = __import__("json").loads((SNAP / f"roster_{team}.json").read_text())
            body.setdefault("fantasyTeams", self.teams)
        elif data.get("view") == "TRADE":
            body = __import__("json").loads((SNAP / "trades.json").read_text())
        else:
            pages = __import__("json").loads((SNAP / "transactions.json").read_text())
            body = pages[int(data["pageNumber"]) - 1]
        return FakeResponse({"responses": [{"data": body}]})


def test_fetch_raw_matches_the_saved_snapshot():
    live = Snapshot.from_raw(fantrax_client.fetch_raw(FakeSession(), "p3z8zy75mgdm460o"))
    saved = Snapshot(SNAP)
    assert live.moves() == saved.moves()
    assert live.end_states() == saved.end_states()
    assert live.season == saved.season


def test_not_logged_in_raises_login_expired():
    with pytest.raises(fantrax_client.FantraxLoginExpired):
        fantrax_client.fetch_raw(FakeSession(logged_in=False), "p3z8zy75mgdm460o")


def test_session_parses_a_cookie_header():
    s = fantrax_client.session("Cookie: a=1; b=two")
    assert s.cookies.get("a") == "1" and s.cookies.get("b") == "two"
```

- [ ] **Step 2:** `uv run pytest -q league/tests/test_fantrax_client.py` → fails (no module).

- [ ] **Step 3: Implement** `league/fantrax_client.py` by moving `session`/`call` from `scripts/fantrax_client.py` and the fetch loop from `scripts/fantrax_snapshot.py`:

```python
"""Read-only access to Fantrax.

Transactions, trades and stats come only from the logged-in `fxpa/req` API, which needs the
commissioner's browser cookie. The Secret ID works only for the public `getLeagues` call.
"""

import requests

URL = "https://www.fantrax.com/fxpa/req"
LEAGUES_URL = "https://www.fantrax.com/fxea/general/getLeagues"


class FantraxError(Exception):
    pass


class FantraxLoginExpired(FantraxError):
    def __init__(self):
        super().__init__("Fantrax login expired: update FANTRAX_COOKIE with a fresh browser cookie")


def session(cookie: str) -> requests.Session:
    raw = cookie.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    s = requests.Session()
    for part in raw.split(";"):
        if "=" in part:
            name, value = part.strip().split("=", 1)
            s.cookies.set(name, value, domain=".fantrax.com")
    return s


def call(s, league_id: str, *msgs: tuple[str, dict]) -> dict:
    """POST one or more methods; returns the full JSON response."""
    body = {"msgs": [{"method": m, "data": {"leagueId": league_id, **d}} for m, d in msgs]}
    try:
        r = s.post(URL, params={"leagueId": league_id}, json=body, timeout=30)
        r.raise_for_status()
        j = r.json()
    except (requests.RequestException, ValueError) as e:
        raise FantraxError(f"Couldn't reach Fantrax: {e}") from e
    if "pageError" in j:
        if j["pageError"].get("code") == "WARNING_NOT_LOGGED_IN":
            raise FantraxLoginExpired()
        raise FantraxError(f"Fantrax error: {j['pageError'].get('code')}")
    return j


def _data(resp: dict) -> dict:
    return resp["responses"][0]["data"]


def fetch_raw(s, league_id: str) -> dict:
    """Everything a Snapshot needs, in memory."""
    info = _data(call(s, league_id, ("getFantasyLeagueInfo", {})))
    teams = _data(call(s, league_id, ("getTeamRosterInfo", {"view": "STATS"})))["fantasyTeams"]
    rosters = {
        t["id"]: _data(call(s, league_id, ("getTeamRosterInfo", {"teamId": t["id"], "view": "STATS"})))
        for t in teams
    }
    pages, page = [], 1
    while True:
        tx = _data(
            call(s, league_id, ("getTransactionDetailsHistory", {"maxResultsPerPage": "200", "pageNumber": str(page)}))
        )
        pages.append(tx)
        if page >= int(tx.get("paginatedResultSet", {}).get("totalNumPages", 1)):
            break
        page += 1
    # Trades (players, draft picks; cash only appears as a commissioner comment).
    trades = _data(call(s, league_id, ("getTransactionDetailsHistory", {"view": "TRADE", "maxResultsPerPage": "500"})))
    if int(trades["paginatedResultSet"].get("totalNumPages", 1)) > 1:
        raise FantraxError("More than one page of trades; add pagination.")
    return {"league_info": info, "teams": teams, "rosters": rosters, "transactions": pages, "trades": trades}


def list_leagues(secret_id: str) -> list[dict]:
    """The commissioner's leagues (leagueId, leagueName, ...), via the public API and the Secret ID."""
    try:
        r = requests.get(LEAGUES_URL, params={"userSecretId": secret_id}, timeout=30)
        r.raise_for_status()
        return r.json().get("leagues", [])
    except (requests.RequestException, ValueError) as e:
        raise FantraxError(f"Couldn't list Fantrax leagues: {e}") from e
```

In `league/fantrax_data.py`, keep `Snapshot(directory)` and add `from_raw`:

```python
class Snapshot:
    def __init__(self, directory: Path | None = None, *, raw: dict | None = None):
        if raw is None:
            raw = {
                "league_info": json.loads((directory / "league_info.json").read_text()),
                "teams": json.loads((directory / "teams.json").read_text()),
            }
            raw["rosters"] = {
                t["id"]: json.loads((directory / f"roster_{t['id']}.json").read_text()) for t in raw["teams"]
            }
            raw["transactions"] = json.loads((directory / "transactions.json").read_text())
            raw["trades"] = json.loads((directory / "trades.json").read_text())
        self.dir = directory
        self.league_info, self.teams, self.rosters = raw["league_info"], raw["teams"], raw["rosters"]
        self.transactions, self.trades = raw["transactions"], raw["trades"]

    @classmethod
    def from_raw(cls, raw: dict) -> "Snapshot":
        return cls(raw=raw)
```

`scripts/fantrax_snapshot.py`: `sys.path` insert of the repo root, import `session, fetch_raw` from `league.fantrax_client`, read the cookie file, then write `raw` to the files it writes today (same names) and build `rosters.csv` from `raw["rosters"]`. Delete `scripts/fantrax_client.py`.

`config/settings.py`, after `LEAGUE_SEASON`:

```python
def _secret(name: str, filename: str) -> str:
    """Env var, else the gitignored secrets/ file (local runs)."""
    if value := os.environ.get(name):
        return value
    path = BASE_DIR / "secrets" / filename
    return path.read_text().strip() if path.exists() else ""


# Fantrax (read-only). The cookie is the commissioner's browser session; the Secret ID only lists leagues.
FANTRAX_COOKIE = _secret("FANTRAX_COOKIE", "fantrax_cookie.txt")
FANTRAX_SECRET_ID = _secret("FANTRAX_SECRET_ID", "fantrax_secret_id.txt")
```

- [ ] **Step 4:** the new tests and the full suite pass.
- [ ] **Step 5: Commit** "Move the Fantrax client into the app; Snapshot from raw data".

---

### Task 2: `FantraxLeague` and `FantraxEvent` models, admin, seed

**Files:**
- Modify: `league/models.py`, `league/admin.py`, `league/management/commands/import_league.py`
- Create: migration (via makemigrations)
- Test: `league/tests/test_events.py` (new file, first tests)

**Interfaces:**
- Produces:
  - `FantraxLeague(league_id: str unique, season: int, process_since: datetime | None, active: bool, name: str)`; `__str__` → `"{name or league_id} ({season})"`.
  - `FantraxEvent` fields: `key` (unique, max 120), `league` FK PROTECT, `happened_at`, `kind` (TextChoices `Kind`: TRADE, DROP, CLAIM, PROMOTED, DEBUT, MINORS_UNKNOWN, CASH_COMMENT, ROSTER_MISMATCH, DEBUT_CHECK), `effect` (TextChoices `Effect`: CONTRACT_MOVED, BUYOUT, VOIDED, FARM_MOVED, FARM_RELEASED, FARM_PROMOTED, FARM_DEBUT, ALREADY_REFLECTED, NONE, EXCEPTION), `fantrax_player_id`, `player_name`, `player` FK null, `from_team`/`to_team` FK null, `contract`/`farm_player`/`buyout` FK null (all PROTECT, `related_name="+"`), `detail`, `resolved_at`, `resolved_by` FK User null SET_NULL, `resolved_note`, `synced_at` auto_now_add. Meta ordering `["-happened_at", "pk"]`.
  - `FantraxEvent.objects.unresolved()` → exceptions with `resolved_at=None` (custom manager/queryset method).
  - `FantraxEvent.resolve(user, note)`: raises `ValueError` if not an exception, already resolved, or blank note; sets fields; calls `audit(user, "Resolved Fantrax exception", detail, team=to_team or from_team, note=note)`.

- [ ] **Step 1: Failing tests** (`league/tests/test_events.py`):

```python
import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from league.models import FantraxEvent, FantraxLeague

pytestmark = pytest.mark.django_db


def test_import_seeds_the_2026_league():
    call_command("import_league", verbosity=0)
    league = FantraxLeague.objects.get()
    assert (league.league_id, league.season, league.active) == ("p3z8zy75mgdm460o", 2026, True)
    assert league.process_since.isoformat().startswith("2026-02-25T16:00")


def test_resolve_needs_a_note_and_happens_once():
    league = FantraxLeague.objects.create(league_id="x", season=2026)
    e = FantraxEvent.objects.create(
        key="k", league=league, happened_at="2026-07-01T00:00Z", kind="CASH_COMMENT", effect="EXCEPTION", detail="d"
    )
    user = User.objects.create(username="c")
    with pytest.raises(ValueError):
        e.resolve(user, "  ")
    e.resolve(user, "entered it")
    assert not FantraxEvent.objects.unresolved().exists()
    with pytest.raises(ValueError):
        e.resolve(user, "again")
```

- [ ] **Step 2:** run → fails.
- [ ] **Step 3: Implement** the models (after `CashTrade`), `makemigrations league -n fantrax_events`. In `import_league`: after `import_farm_picks()`, create the league:

```python
FantraxLeague.objects.create(
    league_id="p3z8zy75mgdm460o",
    name="Dynasty Yr 19",
    season=2026,
    # The post-signing sheet reflects every move up to the auction. The last pre-auction drop was
    # 15:11 on Feb 25 and the first auction claim 18:21; earlier drops belong to the previous season.
    process_since=datetime(2026, 2, 25, 16, 0, tzinfo=EASTERN),
)
```

and in the `--replace` path: refuse when `FantraxEvent.objects.exists()` ("Fantrax events have been applied; --replace would erase them."), and add `FantraxEvent, FantraxLeague` at the front of the delete list. Admin: `FantraxLeagueAdmin(AuditedAdmin)` with `list_display = ["league_id", "name", "season", "process_since", "active"]`; `FantraxEventAdmin(admin.ModelAdmin)` read-only (no add/change/delete), `list_display = ["happened_at", "kind", "effect", "player_name", "from_team", "to_team", "detail", "resolved_at"]`, `list_filter = ["effect", "kind", "league"]`, `search_fields = ["player_name", "detail"]`.
- [ ] **Step 4:** tests pass, `makemigrations --check` clean.
- [ ] **Step 5: Commit** "Add FantraxLeague and FantraxEvent".

---

### Task 3: Event processor, transaction rules

**Files:**
- Create: `league/events.py`
- Test: `league/tests/test_events.py`

**Interfaces:**
- Consumes: models from Task 2, `Move` from `league.fantrax_data`.
- Produces:
  - `class UnmatchedTeam(Exception)`
  - `match_teams(snapshot) -> dict[str, Team]` (moved from `sync_rosters.Command.match_teams`, raising `UnmatchedTeam`).
  - `class Processor` with `__init__(self, league: FantraxLeague, teams: dict[str, Team], players: dict[str, str])` (Fantrax player id → name) and `apply_move(self, m: Move) -> FantraxEvent | None` (None if the key exists or `m.when < league.process_since`). New events are appended to `self.created`.

Rules (spec "Processing rules"):
- live contract for a player = `Contract.live` rows with `player__fantrax_id == m.fantrax_id` and `final_year >= league.season`, excluding `year_signed >= league.season` when the season's `SigningPeriod.locked_at` is None or `m.when < locked_at` (a contract signed at the signing after the season doesn't exist yet for earlier moves).
- active farm player = `FarmPlayer` with `status=ACTIVE` and that Fantrax id.
- TRADE: holder == to → ALREADY_REFLECTED; holder == from → move (`CONTRACT_MOVED` / `FARM_MOVED`); else EXCEPTION.
- DROP: holder == from → contract: `final_year > season` → `Buyout(contract, team=from, dropped_in_season=season)` BUYOUT, else `voided_in_season=season` VOIDED; farm → `status=RELEASED` FARM_RELEASED. Holder != from → EXCEPTION.
- CLAIM, or no contract/farm: NONE.
- Detail strings use team codes: `"traded Juan Soto MB → AC"`, `"dropped Spencer Torkelson (SM): buyout"`, `"dropped X (AW) in his final year: contract voided"`, `"released X from the MB farm"`, `"trade of X from MB, but his contract is with AW"`.

- [ ] **Step 1: Failing tests** (add to `test_events.py`), building tiny states with the ORM:

```python
from datetime import datetime

from league.events import Processor
from league.fantrax_data import EASTERN, Move
from league.models import Buyout, Contract, FarmPlayer, Player, Team


@pytest.fixture
def world():
    league = FantraxLeague.objects.create(league_id="L", season=2026, process_since=datetime(2026, 3, 1, tzinfo=EASTERN))
    a = Team.objects.create(code="AA", name="A", fantrax_id="ta")
    b = Team.objects.create(code="BB", name="B", fantrax_id="tb")
    p = Player.objects.create(name="Pat", fantrax_id="p1")
    q = Player.objects.create(name="Quinn", fantrax_id="p2")
    c = Contract.objects.create(player=p, team=a, original_price=10, year_signed=2024, length=4)  # final 2028
    f = FarmPlayer.objects.create(team=a, player=q, drafted_year=2025, salary=1, salary_season=2026)
    return league, Processor(league, {"ta": a, "tb": b}, {"p1": "Pat", "p2": "Quinn"}), a, b, c, f


def move(kind, fid, frm=None, to=None, tx="t1", day=10):
    return Move(datetime(2026, 7, day, tzinfo=EASTERN), kind, fid, frm, to, tx)


def test_trade_moves_the_contract(world):
    league, proc, a, b, c, f = world
    e = proc.apply_move(move("TRADE", "p1", "ta", "tb"))
    c.refresh_from_db()
    assert (c.team, e.effect, e.contract) == (b, "CONTRACT_MOVED", c)


def test_same_move_twice_is_skipped(world):
    league, proc, *_ = world
    proc.apply_move(move("TRADE", "p1", "ta", "tb"))
    assert proc.apply_move(move("TRADE", "p1", "ta", "tb")) is None


def test_trade_into_the_holder_is_already_reflected(world):
    league, proc, *_ = world
    assert proc.apply_move(move("TRADE", "p1", "tb", "ta")).effect == "ALREADY_REFLECTED"


def test_drop_before_final_year_creates_buyout(world):
    league, proc, a, b, c, f = world
    e = proc.apply_move(move("DROP", "p1", "ta"))
    buyout = Buyout.objects.get(contract=c)
    assert (buyout.team, buyout.dropped_in_season, e.effect, e.buyout) == (a, 2026, "BUYOUT", buyout)


def test_drop_in_final_year_voids(world):
    league, proc, a, b, c, f = world
    Contract.objects.filter(pk=c.pk).update(length=2)  # final 2026
    e = proc.apply_move(move("DROP", "p1", "ta"))
    c.refresh_from_db()
    assert (c.voided_in_season, e.effect) == (2026, "VOIDED")


def test_moves_after_a_buyout_do_nothing(world):
    league, proc, *_ = world
    proc.apply_move(move("DROP", "p1", "ta", tx="t1"))
    proc.apply_move(move("CLAIM", "p1", to="tb", tx="t2", day=11))
    assert proc.apply_move(move("DROP", "p1", "tb", tx="t3", day=12)).effect == "NONE"
    assert Buyout.objects.count() == 1


def test_drop_by_a_team_that_does_not_hold_him_is_an_exception(world):
    league, proc, a, b, c, f = world
    e = proc.apply_move(move("DROP", "p1", "tb"))
    c.refresh_from_db()
    assert (e.effect, c.team, Buyout.objects.count()) == ("EXCEPTION", a, 0)


def test_farm_trade_and_release(world):
    league, proc, a, b, c, f = world
    assert proc.apply_move(move("TRADE", "p2", "ta", "tb")).effect == "FARM_MOVED"
    assert proc.apply_move(move("DROP", "p2", "tb", tx="t2", day=11)).effect == "FARM_RELEASED"
    f.refresh_from_db()
    assert (f.team, f.status) == (b, FarmPlayer.Status.RELEASED)


def test_moves_before_process_since_are_ignored(world):
    league, proc, *_ = world
    early = Move(datetime(2026, 2, 1, tzinfo=EASTERN), "DROP", "p1", "ta", None, "t0")
    assert proc.apply_move(early) is None
    assert not FantraxEvent.objects.exists()


def test_contract_signed_at_this_signing_ignores_earlier_moves(world):
    league, proc, a, b, c, f = world
    Contract.objects.filter(pk=c.pk).update(year_signed=2026)
    assert proc.apply_move(move("DROP", "p1", "ta")).effect == "NONE"
```

- [ ] **Step 2:** run → fails.
- [ ] **Step 3: Implement** `league/events.py` (Processor with `_record(key, kind, effect, when, m, detail, **links)` that creates and appends the event; `_contract(m)`, `_farm(m)` lookups; `_trade`, `_drop`; `match_teams`). `sync_rosters` imports `match_teams` and converts `UnmatchedTeam` into `CommandError`.
- [ ] **Step 4:** tests pass.
- [ ] **Step 5: Commit** "Apply Fantrax trades and drops as events".

---

### Task 4: Roster-derived facts, cash comments, roster check, `sync()`

**Files:**
- Modify: `league/events.py`
- Test: `league/tests/test_events.py`, `league/tests/test_sync_2026.py` (new)

**Interfaces:**
- Produces:
  - `Processor.apply_rosters(self, snapshot, now)`: promotions, debuts, debut checks, unknown Minors, cash comments, roster mismatches (contracts and active farm players).
  - `@dataclass SyncResult(created: list[FantraxEvent])` with `counts() -> dict[str, int]` (by effect label), `exceptions` (created EXCEPTION events), `summary() -> str` e.g. `"14 new events: 3 contract moved, 2 buyout, 1 exception"` or `"No new events"`.
  - `sync(sources: list[tuple[FantraxLeague, Snapshot]], user=None, source_label="", dry_run=False) -> SyncResult`: one `transaction.atomic()`; `select_for_update` on the league rows first (two clicks serialize; the second finds the keys and skips); for each league, match teams, apply `snapshot.moves()` oldest first, then `apply_rosters`; `audit(user, "Synced Fantrax", f"{source_label}: {summary}")`; `dry_run` rolls back via `transaction.set_rollback(True)` and returns the result (events are unsaved-by-rollback but still in the list for printing).
  - `unresolved_count() -> int`.

Roster rules (keys from the spec):
- `promoted:<fid>`: active farm player whose end state is rostered and `status != "Minors"` → `status=PROMOTED`, `team` = rostered team, FARM_PROMOTED.
- `debut:<fid>`: farm player (active or just promoted) with `has_mlb_appearance=False` and `end.debuted` → set True, FARM_DEBUT, detail `"MLB debut (G games, PA+ PA, outs outs)"`.
- `debut-check:<fid>:<season>`: not debuted but `end.games_played > 0` → EXCEPTION ("N MLB games but no plate appearance or out recorded (HBP/sacrifice?): check").
- `minors-unknown:<fid>:<season>`: `status == "Minors"`, no ACTIVE farm player and no live contract for him → EXCEPTION "In a Fantrax Minors slot but not on a farm: add him in the admin if he was drafted".
- `cash-comment:<tx>`: each `snapshot.trade_comments()` at/after `process_since`, skipped when a `CashTrade` has that `fantrax_tx_id` → EXCEPTION "Trade between AA / BB mentions cash: 'text'. Enter the cash trade by hand".
- `roster-mismatch:c<contract pk>:<league pk>` for each live contract (per Task 3's visibility rule, evaluated at `now`) whose player has a Fantrax id and whose rostered team (mapped) differs from `contract.team` → EXCEPTION "Contract is with AA, but Fantrax has him on BB" / "... on no roster". Same for active farm players: `roster-mismatch:f<farm pk>:<league pk>`.

- [ ] **Step 1: Failing unit tests** (in `test_events.py`, using the `world` fixture and a tiny fake snapshot):

```python
from dataclasses import dataclass, field

from league.events import sync
from league.fantrax_data import EndState


@dataclass
class FakeSnapshot:
    ends: dict
    moves_: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    teams: list = field(default_factory=lambda: [{"id": "ta", "name": "A"}, {"id": "tb", "name": "B"}])

    def moves(self):
        return self.moves_

    def end_states(self):
        return self.ends

    def trade_comments(self):
        return self.comments

    def players(self):
        return {}


def test_promotion_and_debut(world):
    league, proc, a, b, c, f = world
    snap = FakeSnapshot({"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Active", 5, plate_appearances=9)})
    result = sync([(league, snap)])
    f.refresh_from_db()
    assert (f.status, f.has_mlb_appearance) == (FarmPlayer.Status.PROMOTED, True)
    assert result.counts() == {"Farm promoted": 1, "Farm debut": 1}
    assert sync([(league, snap)]).created == []


def test_unknown_minors_player_is_an_exception(world):
    league, *_ = world
    Player.objects.create(name="Rookie", fantrax_id="p9")
    snap = FakeSnapshot({"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Minors", 0), "p9": EndState("tb", "Minors", 0)})
    (e,) = sync([(league, snap)]).exceptions
    assert e.key == "minors-unknown:p9:2026"


def test_contract_on_the_wrong_roster_is_an_exception(world):
    league, *_ = world
    snap = FakeSnapshot({"p1": EndState("tb", "Active", 0), "p2": EndState("ta", "Minors", 0)})
    (e,) = sync([(league, snap)]).exceptions
    assert "BB" in e.detail


def test_dry_run_changes_nothing(world):
    league, proc, a, b, c, f = world
    snap = FakeSnapshot({"p1": EndState("tb", "Active", 0), "p2": EndState("ta", "Minors", 0)},
                        moves_=[move("TRADE", "p1", "ta", "tb")])
    result = sync([(league, snap)], dry_run=True)
    assert result.counts() == {"Contract moved": 1}
    assert not FantraxEvent.objects.exists()
    c.refresh_from_db()
    assert c.team == a
```

- [ ] **Step 2: Failing golden tests** in `league/tests/test_sync_2026.py`:

```python
import json
from pathlib import Path

import pytest
from django.core.management import call_command

from league.events import sync
from league.fantrax_data import Snapshot
from league.models import Buyout, Contract, FantraxEvent, FantraxLeague, FarmPlayer

pytestmark = pytest.mark.django_db
SNAP = Path(__file__).resolve().parents[2] / "data" / "fantrax" / "2026-final"
GOLDEN = json.loads((Path(__file__).parent / "data" / "events_2026_golden.json").read_text())


@pytest.fixture
def synced():
    call_command("import_league", verbosity=0)
    return sync([(FantraxLeague.objects.get(), Snapshot(SNAP))])


def state():
    return {
        "contracts": sorted([c.player.name, c.year_signed, c.team.code, c.voided_in_season] for c in Contract.objects.select_related("player", "team")),
        "buyouts": sorted([b.contract.player.name, b.contract.year_signed, b.team.code, b.dropped_in_season] for b in Buyout.objects.select_related("contract__player", "team")),
        "farm": sorted([f.player.name, f.drafted_year, f.team.code, f.status, f.has_mlb_appearance] for f in FarmPlayer.objects.select_related("player", "team")),
    }


def test_2026_matches_accepting_every_reconciliation_item(synced):
    got = json.loads(json.dumps(state()))  # tuples → lists, like the golden file
    assert got == {k: GOLDEN[k] for k in ("contracts", "buyouts", "farm")}


def test_2026_exceptions_are_the_two_unknown_minors(synced):
    assert sorted(e.player_name for e in synced.exceptions) == GOLDEN["unknown_minors"]


def test_second_sync_adds_nothing(synced):
    n = FantraxEvent.objects.count()
    assert sync([(FantraxLeague.objects.get(), Snapshot(SNAP))]).created == []
    assert FantraxEvent.objects.count() == n
```

- [ ] **Step 3:** run both → fail. Implement `apply_rosters`, `SyncResult`, `sync`, `unresolved_count`. If the golden test disagrees, print the diff and decide per case: a real rule bug gets fixed; an intended difference (per spec) gets explained in the test with a targeted adjustment, never a blanket regeneration of the golden file.
- [ ] **Step 4:** tests pass.
- [ ] **Step 5: Commit** "Roster-derived Fantrax events and sync()".

---

### Task 5: `sync_fantrax` command; remove `reconcile` and `ReconciliationItem`

**Files:**
- Create: `league/management/commands/sync_fantrax.py`
- Delete: `league/management/commands/reconcile.py`, `league/reconcile.py`, `league/tests/test_replay.py`, `league/tests/test_farm_replay.py`, `league/tests/test_reconcile_command.py`, `league/tests/test_reconcile_farm.py`, `league/tests/test_rerun.py`, `league/tests/test_decisions.py`
- Modify: `league/models.py` (remove ReconciliationItem), `league/admin.py`, `league/signing.py`, `league/signing_views.py`, `league/context_processors.py`, `core/templates/base.html`, `league/management/commands/import_league.py`, `league/tests/test_signing.py`, `league/tests/test_signing_views.py`, `league/tests/test_views.py`, `bin/local-review.sh`, `render.yaml`
- Create: migration removing ReconciliationItem

**Interfaces:**
- Consumes: `sync`, `unresolved_count`, `FantraxEvent.resolve`.
- Produces: command `sync_fantrax [--snapshot DIR] [--league ID] [--dry-run]`; `signing.open_period` refuses with `"N Fantrax exception(s) are unresolved; resolve them on the console first"`; test helper `league.tests.test_signing.settle_2026()` = sync the 2026 snapshot, add Lawson and Yamashita as $1 farm players drafted 2026 on their Fantrax teams, resolve both exceptions with note "added by hand".

Command behavior:
- `--snapshot DIR`: `Snapshot(Path(DIR))`; league = `--league` or the single `FantraxLeague` with `season == snapshot.season` (CommandError if none or several).
- no `--snapshot`: every active league, live: `fantrax_client.session(settings.FANTRAX_COOKIE)`, `fetch_raw` for each **before** calling `sync`; `FantraxError` → `CommandError(str(e))`; empty cookie → CommandError "FANTRAX_COOKIE isn't set".
- prints `result.summary()`, then each new exception's detail, then `N unresolved exception(s)` from `unresolved_count()`; `--dry-run` prefixes "Dry run, nothing saved:".

- [ ] **Step 1: Failing tests**: in `test_signing.py`, replace `decide_everything` and the `ready` fixture:

```python
def settle_2026():
    """What the commissioner does after the 2026 sync: add the two drafted rookies Fantrax shows in Minors."""
    call_command("sync_fantrax", snapshot=str(SNAPSHOT), verbosity=0)
    for e in FantraxEvent.objects.unresolved():
        FarmPlayer.objects.create(team=e.to_team, player=e.player, drafted_year=2026, salary=1, salary_season=2026)
        e.resolve(None, "added by hand")


@pytest.fixture
def ready():
    """The league after the 2026 season: events applied, rosters loaded, signing not open yet."""
    call_command("import_league", verbosity=0)
    settle_2026()
    call_command("sync_rosters", verbosity=0)
```

(`SNAPSHOT = Path(settings.BASE_DIR) / "data" / "fantrax" / "2026-final"`; minors-unknown events must set `player` and `to_team` = the rostered team for this to work, so Task 4's implementation records them.) Replace `test_open_refuses_while_reconciliation_is_pending` with:

```python
def test_open_refuses_while_exceptions_are_unresolved():
    call_command("import_league", verbosity=0)
    call_command("sync_fantrax", snapshot=str(SNAPSHOT), verbosity=0)
    call_command("sync_rosters", verbosity=0)
    with pytest.raises(signing.SigningError, match="unresolved"):
        signing.open_period(2026, None)
```

Replace `test_reconcile_after_lock_proposes_nothing` with a post-lock trade test:

```python
def test_trade_after_lock_moves_a_new_contract(opened):
    ...  # lock with MB signing someone (reuse this file's existing lock helpers), then
    league = FantraxLeague.objects.create(league_id="renewed", season=2026, process_since=None)
    new = Contract.objects.filter(year_signed=2026).select_related("player", "team").first()
    other = Team.objects.exclude(pk=new.team_id).first()
    snap = FakeSnapshot(  # from test_events
        ends={new.player.fantrax_id: EndState(other.fantrax_id, "Active", 0)},
        moves_=[Move(timezone.now(), "TRADE", new.player.fantrax_id, new.team.fantrax_id, other.fantrax_id, "post-lock")],
        teams=[{"id": t.fantrax_id, "name": t.name} for t in Team.objects.all()],
    )
    sync([(league, snap)])
    new.refresh_from_db()
    assert new.team == other
```

(Read the existing `opened`/lock tests in `test_signing.py` first and build the lock the same way they do.) In `test_signing_views.py`: `ready` calls `settle_2026()`; the `unreconciled` fixture syncs without settling; `test_console_open_refuses_with_pending_reconciliation` asserts `b"unresolved"`. In `test_views.py`: delete both banner tests, and make line ~191's setup use `settle_2026()`. Add a command test in `test_sync_2026.py`:

```python
def test_command_dry_run_saves_nothing(capsys):
    call_command("import_league", verbosity=0)
    call_command("sync_fantrax", snapshot=str(SNAP), dry_run=True)
    assert "Dry run" in capsys.readouterr().out
    assert not FantraxEvent.objects.exists()


def test_live_sync_needs_a_cookie(settings):
    call_command("import_league", verbosity=0)
    settings.FANTRAX_COOKIE = ""
    with pytest.raises(CommandError, match="FANTRAX_COOKIE"):
        call_command("sync_fantrax")
```

- [ ] **Step 2:** run → fail.
- [ ] **Step 3: Implement** the command, then remove ReconciliationItem everywhere (`grep -rn "ReconciliationItem\|pending_reconciliation\|pending_review\|reconcile" league core bin`), `makemigrations league -n remove_reconciliation`. `signing.open_period` uses `events.unresolved_count()`. Console context `pending` → `exceptions` (list of unresolved events). Remove the base.html banner and the context processor query. `import_league`: drop the ReconciliationItem guard/delete. `bin/local-review.sh`: replace the `reconcile` line with `uv run python manage.py sync_fantrax --snapshot data/fantrax/2026-final >/dev/null` (before `sync_rosters`, as today). `render.yaml`: `gunicorn config.wsgi:application --timeout 120` (a live sync makes ~15 Fantrax calls).
- [ ] **Step 4:** full suite passes, ruff clean, `makemigrations --check` clean.
- [ ] **Step 5: Commit** "sync_fantrax replaces reconcile; drop ReconciliationItem".

---

### Task 6: Console (sync button, exceptions, find leagues) and team page moves

**Files:**
- Modify: `league/signing_views.py`, `league/urls.py`, `league/templates/league/console.html`, `league/views.py`, `league/templates/league/team.html`
- Test: `league/tests/test_signing_views.py`, `league/tests/test_views.py`

**Interfaces:**
- Consumes: `sync`, `fantrax_client.session/fetch_raw/list_leagues`, `FantraxEvent.resolve`.
- Produces: console POST actions `sync`, `resolve` (`event` id + `note`), `add_league` (`league_id`, `name`); console GET `?find_leagues=1` lists `list_leagues(settings.FANTRAX_SECRET_ID)` rows not yet in `FantraxLeague`; team view context `moves` = that team's events (`from_team` or `to_team`) with effect not in (NONE, EXCEPTION), league season == `LEAGUE_SEASON`.

Console layout, top of the page (all statuses):

```html
<h2>Fantrax</h2>
<form method="post">{% csrf_token %}<button type="submit" name="action" value="sync" class="primary">Sync from Fantrax</button>
  <span class="muted">Reads {{ fantrax_leagues|join:", " }} and applies new trades, drops, promotions and debuts.</span></form>
{% if exceptions %}<h3>Fantrax exceptions ({{ exceptions|length }})</h3>
<p class="muted">Fix each by hand (usually in the admin), then note what you did. Signing can't open while any are listed.</p>
<div class="table-wrap"><table class="panel"><tbody>
{% for e in exceptions %}<tr><td>{{ e.happened_at|date:"M j" }}</td><td class="wrap">{{ e.detail }}</td>
  <td><form method="post" class="note">{% csrf_token %}<input type="hidden" name="event" value="{{ e.pk }}">
  <input name="note" required placeholder="What you did"> <button name="action" value="resolve">Resolve</button></form></td></tr>{% endfor %}
</tbody></table></div>{% endif %}
<p><a href="?find_leagues=1">Find new Fantrax leagues</a></p>
{% if found_leagues is not None %}...list with an "Add for the {{ league_season }} season" button per league, or "No new leagues."{% endif %}
```

In the "Before you open signing" checklist, the first item becomes: exceptions → `todo` "N Fantrax exception(s) to resolve (above)", else `ok` "Fantrax events are applied and nothing needs a look."

View code for `sync`: 

```python
elif action == "sync":
    if not settings.FANTRAX_COOKIE:
        raise FantraxError("FANTRAX_COOKIE isn't set")
    s = fantrax_client.session(settings.FANTRAX_COOKIE)
    leagues = list(FantraxLeague.objects.filter(active=True))
    sources = [(lg, Snapshot.from_raw(fantrax_client.fetch_raw(s, lg.league_id))) for lg in leagues]
    result = events.sync(sources, request.user, source_label="Fantrax (live)")
    messages.success(request, result.summary())
```

with `except (FantraxError, UnmatchedTeam) as e: messages.error(request, str(e))` next to the existing `SigningError` handler. `resolve`: `get_object_or_404(FantraxEvent, pk=..., effect=EXCEPTION)`, `.resolve(request.user, note)`, ValueError → messages.error. `add_league`: create `FantraxLeague(league_id, name, season=LEAGUE_SEASON, process_since=None)` and audit "Added Fantrax league".

Team page, after the summary `<details>`:

```html
{% if moves %}<h2>Moves this season</h2>
<ul>{% for e in moves %}<li>{{ e.happened_at|date:"M j" }}: {{ e.detail }}</li>{% endfor %}</ul>{% endif %}
```

- [ ] **Step 1: Failing tests** (`test_signing_views.py`, reusing `ready`/`commish` fixtures and monkeypatching the network):

```python
def test_console_sync_applies_events(ready, commish, monkeypatch, settings):
    from league import fantrax_client
    from league.tests.test_fantrax_client import FakeSession

    settings.FANTRAX_COOKIE = "a=1"
    monkeypatch.setattr(fantrax_client, "session", lambda cookie: FakeSession())
    response = commish.post("/commish/", {"action": "sync"}, follow=True)
    assert b"No new events" in response.content  # ready already synced the same data


def test_console_sync_reports_expired_login(ready, commish, monkeypatch, settings):
    from league import fantrax_client
    from league.tests.test_fantrax_client import FakeSession

    settings.FANTRAX_COOKIE = "a=1"
    monkeypatch.setattr(fantrax_client, "session", lambda cookie: FakeSession(logged_in=False))
    response = commish.post("/commish/", {"action": "sync"}, follow=True)
    assert b"Fantrax login expired" in response.content


def test_resolve_exception_from_the_console(unreconciled, commish):
    e = FantraxEvent.objects.unresolved().first()
    commish.post("/commish/", {"action": "resolve", "event": e.pk, "note": "added him"})
    e.refresh_from_db()
    assert e.resolved_note == "added him"
    assert AuditEntry.objects.filter(action="Resolved Fantrax exception").exists()


def test_find_and_add_a_league(ready, commish, monkeypatch, settings):
    from league import fantrax_client

    settings.FANTRAX_SECRET_ID = "s"
    monkeypatch.setattr(fantrax_client, "list_leagues", lambda secret: [
        {"leagueId": "p3z8zy75mgdm460o", "leagueName": "Dynasty Yr 19"},
        {"leagueId": "newone", "leagueName": "Dynasty Yr 20"},
    ])
    page = commish.get("/commish/?find_leagues=1").content.decode()
    assert "newone" in page and "p3z8zy75mgdm460o" not in page.split("Find new Fantrax leagues")[1]
    commish.post("/commish/", {"action": "add_league", "league_id": "newone", "name": "Dynasty Yr 20"})
    assert FantraxLeague.objects.filter(league_id="newone", season=2026).exists()
```

and in `test_views.py`, a team page test: after `settle_2026()`, `/teams/SM/` contains "Spencer Torkelson" under "Moves this season".
- [ ] **Step 2:** run → fail. **Step 3:** implement. **Step 4:** full suite + ruff. **Step 5: Commit** "Console sync, exceptions and league finder; team moves".

---

### Task 7: Docs, local run, issue and PR

**Files:**
- Modify: `league/templates/league/help.html`, `ARCHITECTURE.md`, `README.md` (if it mentions reconcile), `docs/superpowers/specs/2026-09-26-fantrax-events-design.md` (status line → "Implemented"), memory file `project-dynasty-commish.md`

- [ ] **Step 1:** Rewrite help's "Review the season's moves (reconciliation)" as "Fantrax events": what sync does, exceptions and how to resolve, fixing a wrong application in the admin, the cookie and how to refresh it, finding the renewed league; update the command-line list (`sync_fantrax`, `--snapshot`, `--dry-run`). Update ARCHITECTURE (models, `league/events.py`, `league/fantrax_client.py`, the flow, the removed special case). `grep -rn "reconcil" *.md league core bin` and fix every stale mention.
- [ ] **Step 2:** Restart the local app with a fresh review DB state: `bin/local-review.sh` uses `review.sqlite3`, which holds the old items; run migrations, then `sync_fantrax --snapshot data/fantrax/2026-final` (the script does this), and check the console and a team page load (curl with a logged-in session or Django test client against `review.sqlite3`).
- [ ] **Step 3:** Full suite, ruff, `makemigrations --check`.
- [ ] **Step 4:** Open an issue "Read cash trades from the league's Discord channel" (label `future`, "Part of #35"; body: bot with message-content read, parse posts into CashTrade, exceptions for unparsed posts, reuse the event-key pattern; manual entry until then).
- [ ] **Step 5:** Push, open the PR: "Fixes #42", "Related: #45, #33, #22"; summary of behavior change, the golden test, the review-DB note. Update memory.
