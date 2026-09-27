# Season Rollover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `LEAGUE_SEASON` setting with `Season` records, take each Fantrax move's season from its date, read farm draft picks from Fantrax claims, and let the commissioner start the next season from the console (freezing auction budgets and adding the next year of farm picks).

**Architecture:** A `Season` model holds each year's farm draft start and auction start. `league/seasons.py` answers "which season is current" and "which season does this moment belong to", and holds the rollover (checklist, set dates, start). `league/events.Processor` asks it per move instead of trusting `FantraxLeague.season`. The console gets a Season section; the team page shows the frozen budget.

**Tech Stack:** Django 6.1, Python 3.14, pytest-django, uv. Run everything with `uv run`.

**Spec:** `docs/superpowers/specs/2026-09-26-season-rollover-design.md`

## Global Constraints

- Money is whole dollars (`int`); seasons are calendar years (`int`).
- Farm draft picks cost `rules.farm.DRAFT_PICK_SALARY` ($1), whatever Fantrax shows.
- Farm picks: 2 rounds per team; rollover adds `max(existing year) + 1`.
- Every state change is audited with `league.models.audit`.
- The 2026 golden tests (`league/tests/test_sync_2026.py`) pass unchanged.
- Lint: `uv run ruff check . && uv run ruff format --check .`; migrations: `uv run python manage.py makemigrations --check --dry-run`.
- One deviation from the spec, decided while planning: the 12-month guard makes **the sync refuse** (nothing saved, a message to enter the next auction start) rather than storing an exception event, because a stored event is never re-processed after the date is entered.

---

### Task 1: Season model, `current_season()`, and the setting's replacement

**Files:**
- Modify: `league/models.py` (add `Season` after `CashTrade`)
- Create: `league/migrations/0010_season.py` (generated, plus a seed)
- Create: `league/seasons.py`
- Modify: `config/settings.py:118-120` (remove `LEAGUE_SEASON`)
- Modify: `league/context_processors.py`, `league/views.py`, `league/signing_views.py:39-40`, `league/management/commands/sync_rosters.py`, `league/admin.py`
- Test: `league/tests/test_seasons.py` (new), `league/tests/test_views.py:161-166`

**Interfaces:**
- Produces: `Season` (fields `year`, `farm_draft_starts_at`, `auction_starts_at`, `started_at`, `started_by`); `seasons.current() -> Season`; `seasons.current_season() -> int`; `seasons.season_at(when, seasons=None) -> Season | None`; `seasons.farm_draft_at(when, seasons=None) -> Season | None`; `seasons.window(season: Season) -> tuple[datetime, datetime | None]`.

- [ ] **Step 1: Write the failing tests** in `league/tests/test_seasons.py`:

```python
from datetime import datetime

import pytest

from league import seasons
from league.fantrax_data import EASTERN
from league.models import Season

pytestmark = pytest.mark.django_db
AUCTION_2026 = datetime(2026, 2, 25, 16, 0, tzinfo=EASTERN)


def add_2027(started=False):
    return Season.objects.create(
        year=2027,
        farm_draft_starts_at=datetime(2027, 2, 20, 18, 0, tzinfo=EASTERN),
        auction_starts_at=datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN),
        started_at=datetime(2027, 2, 25, tzinfo=EASTERN) if started else None,
    )


def test_2026_is_seeded_and_current():
    s = Season.objects.get()
    assert (s.year, s.auction_starts_at, s.farm_draft_starts_at) == (2026, AUCTION_2026, None)
    assert s.started_at is not None
    assert seasons.current_season() == 2026


def test_a_season_is_current_only_once_started():
    add_2027()
    assert seasons.current_season() == 2026
    Season.objects.filter(year=2027).update(started_at=datetime(2027, 2, 25, tzinfo=EASTERN))
    assert seasons.current_season() == 2027


def test_season_at_uses_the_auction_start():
    add_2027()
    assert seasons.season_at(AUCTION_2026).year == 2026
    assert seasons.season_at(datetime(2027, 2, 24, 18, 59, tzinfo=EASTERN)).year == 2026
    assert seasons.season_at(datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN)).year == 2027
    assert seasons.season_at(datetime(2026, 2, 25, 15, 59, tzinfo=EASTERN)) is None


def test_farm_draft_window_ends_at_the_auction():
    add_2027()
    assert seasons.farm_draft_at(datetime(2027, 2, 20, 18, 0, tzinfo=EASTERN)).year == 2027
    assert seasons.farm_draft_at(datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN)) is None
    assert seasons.farm_draft_at(datetime(2026, 7, 1, tzinfo=EASTERN)) is None  # 2026 has no window


def test_window_runs_to_the_next_auction():
    s27 = add_2027()
    assert seasons.window(Season.objects.get(year=2026)) == (AUCTION_2026, s27.auction_starts_at)
    assert seasons.window(s27) == (s27.auction_starts_at, None)
```

- [ ] **Step 2: Run** `uv run pytest league/tests/test_seasons.py -q` — expect ImportError (`seasons` / `Season` missing).

- [ ] **Step 3: Add the model** to `league/models.py` after `CashTrade`:

```python
class Season(models.Model):
    """A league season: from its auction start until just before the next season's auction.

    The farm draft runs in Fantrax before the auction, so every claim from `farm_draft_starts_at`
    until the auction start is a farm pick. The commissioner starts a season on the console
    once its auction has run and been synced; until then the previous season is current.
    """

    year = models.PositiveIntegerField(unique=True)
    farm_draft_starts_at = models.DateTimeField(
        null=True, blank=True, help_text="Claims from here until the auction start are farm draft picks"
    )
    auction_starts_at = models.DateTimeField(help_text="Moves from here on belong to this season")
    started_at = models.DateTimeField(null=True, blank=True)
    started_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["year"]

    def __str__(self):
        return str(self.year)

    def clean(self):
        if self.farm_draft_starts_at and self.auction_starts_at and self.farm_draft_starts_at >= self.auction_starts_at:
            raise ValidationError({"farm_draft_starts_at": "The farm draft starts before the auction"})
```

- [ ] **Step 4: Generate the migration** `uv run python manage.py makemigrations league --name season`, then append the seed to `league/migrations/0010_season.py` (above `class Migration`, and add the op after `CreateModel`):

```python
from datetime import datetime
from zoneinfo import ZoneInfo


def seed(apps, schema_editor):
    """2026 is under way: its boundary is the cutoff the imported sheet already reflects."""
    Season = apps.get_model("league", "Season")
    start = datetime(2026, 2, 25, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    Season.objects.get_or_create(year=2026, defaults={"auction_starts_at": start, "started_at": start})
```

`operations = [migrations.CreateModel(...), migrations.RunPython(seed, migrations.RunPython.noop)]`

- [ ] **Step 5: Create `league/seasons.py`:**

```python
"""Which league season it is, which season a moment belongs to, and starting the next one.

Season S runs from the S auction start until just before the S+1 auction. The current season
is the latest one the commissioner has started; a Fantrax move's season comes from its date.
"""

from datetime import datetime

from league.models import Season


def current() -> Season:
    season = Season.objects.exclude(started_at=None).order_by("-year").first()
    if season is None:
        raise Season.DoesNotExist("No season has started")
    return season


def current_season() -> int:
    return current().year


def season_at(when: datetime, seasons=None) -> Season | None:
    """The season `when` belongs to: the latest whose auction had started. None before the first one."""
    found = None
    for s in Season.objects.all() if seasons is None else seasons:
        if s.auction_starts_at <= when and (found is None or s.year > found.year):
            found = s
    return found


def farm_draft_at(when: datetime, seasons=None) -> Season | None:
    """The season whose farm draft window (farm draft start up to the auction start) holds `when`."""
    for s in Season.objects.all() if seasons is None else seasons:
        if s.farm_draft_starts_at and s.farm_draft_starts_at <= when < s.auction_starts_at:
            return s
    return None


def window(season: Season) -> tuple[datetime, datetime | None]:
    """[auction start, next season's auction start); open-ended until the next one is entered."""
    following = Season.objects.filter(year=season.year + 1).first()
    return season.auction_starts_at, following.auction_starts_at if following else None
```

- [ ] **Step 6: Replace the setting.** Delete `LEAGUE_SEASON` and its comment from `config/settings.py`. Then:

`league/context_processors.py`:
```python
from league.access import is_league_member
from league.models import SigningPeriod
from league.seasons import current_season


def league(request):
    season = current_season()
    context = {"league_season": season, "next_season": season + 1}
    if is_league_member(request.user):
        period = SigningPeriod.objects.filter(season=season).first()
        context["signing_status"] = period.status if period else None
    return context
```

`league/views.py`: import `from league.seasons import current_season`; `next_season()` returns `current_season() + 1`; in `team()` replace both `settings.LEAGUE_SEASON` uses with `current_season()` (the moves filter is replaced properly in Task 5; for now `league__season=current_season()`). Drop the `settings` import if unused.

`league/signing_views.py`: `def season(): return current_season()` (import it; drop `settings` only if unused — `FANTRAX_COOKIE`/`FANTRAX_SECRET_ID` still use it).

`league/management/commands/sync_rosters.py`: `parser.add_argument("--season", type=int, default=None, help="The season that just ended (default: the current season)")`, and at the top of `handle`: `season = season or current_season()` (import from `league.seasons`).

`league/admin.py`: register `models.Season` with `AuditedAdmin`:
```python
@admin.register(models.Season)
class SeasonAdmin(AuditedAdmin):
    """Set a season's dates on the console; change a started season's dates here."""

    list_display = ["year", "farm_draft_starts_at", "auction_starts_at", "started_at"]
    readonly_fields = ["started_at", "started_by"]
```

- [ ] **Step 7: Update `test_ended_contracts_are_only_last_seasons`** in `league/tests/test_views.py` — replace `settings.LEAGUE_SEASON = 2027` with starting a 2027 season (drop the `settings` fixture argument):

```python
    Season.objects.create(year=2027, auction_starts_at="2027-02-24T19:00-05:00", started_at="2027-02-25T00:00-05:00")
```
(import `Season` from `league.models`).

- [ ] **Step 8: Run** `uv run pytest -q` — all pass. `grep -rn LEAGUE_SEASON --include='*.py' --include='*.html' --include='*.md' .` finds only docs (fixed in Task 6).

- [ ] **Step 9: Commit** `git add -A && git commit -m "Season model replaces the LEAGUE_SEASON setting"` (with the session's attribution trailer).

---

### Task 2: A move's season comes from its date

**Files:**
- Modify: `league/events.py` (`Processor.__init__`, `contract`, drop branch, `_sync_once`)
- Test: `league/tests/test_events.py`

**Interfaces:**
- Consumes: `seasons.season_at`, `seasons.current_season`, `Season`.
- Produces: `events.SeasonMissing(Exception)` raised by `sync()`; `Processor.seasons: list[Season]`, `Processor.season_of(when) -> int`.

- [ ] **Step 1: Write failing tests** (append to `league/tests/test_events.py`; import `Season`, `SigningPeriod` from `league.models` and `SeasonMissing` from `league.events`):

```python
# --- the season of a move comes from its date ---------------------------------


def season_2027():
    return Season.objects.create(year=2027, auction_starts_at=datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN))


def at(kind, fid, frm=None, to=None, when=None, tx="t9"):
    return Move(when, kind, fid, frm, to, tx)


def test_final_year_drop_after_the_next_auction_is_free(world):
    league, proc, a, b, c, f = world
    Contract.objects.filter(pk=c.pk).update(year_signed=2025, length=2)  # final year 2027
    season_2027()
    proc = Processor(league, proc.teams, proc.names)
    e = proc.apply_move(at("DROP", "p1", "ta", when=datetime(2027, 6, 1, tzinfo=EASTERN)))
    c.refresh_from_db()
    assert (e.effect, c.voided_in_season) == ("VOIDED", 2027)


def test_drop_after_the_next_auction_is_bought_out_in_that_season(world):
    league, proc, a, b, c, f = world  # final year 2028
    season_2027()
    proc = Processor(league, proc.teams, proc.names)
    proc.apply_move(at("DROP", "p1", "ta", when=datetime(2027, 6, 1, tzinfo=EASTERN)))
    assert Buyout.objects.get(contract=c).dropped_in_season == 2027


def test_drop_before_the_next_auction_is_the_old_season(world):
    league, proc, a, b, c, f = world
    season_2027()
    proc = Processor(league, proc.teams, proc.names)
    proc.apply_move(at("DROP", "p1", "ta", when=datetime(2027, 2, 24, 18, 0, tzinfo=EASTERN)))
    assert Buyout.objects.get(contract=c).dropped_in_season == 2026


def test_new_contract_is_visible_to_moves_after_its_lock(world):
    league, proc, a, b, c, f = world
    Contract.objects.filter(pk=c.pk).update(year_signed=2026)
    SigningPeriod.objects.create(season=2026, status="locked", locked_at=datetime(2026, 7, 5, tzinfo=EASTERN))
    proc = Processor(league, proc.teams, proc.names)
    assert proc.apply_move(move("TRADE", "p1", "ta", "tb")).effect == "CONTRACT_MOVED"


def test_sync_refuses_moves_a_year_past_the_last_auction(world):
    league, proc, a, b, c, f = world
    snap = FakeSnapshot(
        {"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Minors", 0)},
        moves_=[at("DROP", "p1", "ta", when=datetime(2027, 3, 1, tzinfo=EASTERN))],
    )
    with pytest.raises(SeasonMissing, match="2027"):
        sync([(league, snap)])
    assert not FantraxEvent.objects.exists()
```

- [ ] **Step 2: Run** `uv run pytest league/tests/test_events.py -q` — the new tests fail (ImportError for `SeasonMissing`, then wrong seasons).

- [ ] **Step 3: Implement in `league/events.py`.** Imports: `from datetime import datetime, timedelta`; `from league import seasons`; add `Season` to the models import.

```python
class SeasonMissing(Exception):
    """A move is over a year past the last auction: the next season's auction start isn't entered."""


SEASON_LENGTH = timedelta(days=365)
```

`Processor.__init__`: replace the `self.season` / period lines with

```python
        self.league, self.teams, self.names = league, teams, names
        self.season = seasons.current_season()  # roster facts are about now
        self.seasons = list(Season.objects.all())
        self.locks = dict(
            SigningPeriod.objects.exclude(locked_at=None).values_list("season", "locked_at")
        )
```

Add:

```python
    def season_of(self, when: datetime) -> int:
        s = seasons.season_at(when, self.seasons)
        if s is None:
            raise SeasonMissing(f"{when:%Y-%m-%d} is before the first season's auction")
        return s.year
```

`contract()`:

```python
        season = self.season_of(when)
        lock = self.locks.get(season)
        for c in Contract.live.filter(player__fantrax_id=fid).select_related("team", "player"):
            if c.final_year < season:
                continue
            if c.year_signed >= season and (lock is None or when < lock):
                continue
            return c
        return None
```

Drop branch: `season = self.season_of(m.when)` then use `season` in place of `self.season` in `held.final_year > season`, `dropped_in_season=season`, and `held.voided_in_season = season`.

`_sync_once`: right after `known = known_keys()`, before the loop:

```python
        latest = max(Season.objects.all(), key=lambda s: s.year)
        for league, snapshot in sources:
            late = [m for m in snapshot.moves() if m.when - latest.auction_starts_at > SEASON_LENGTH]
            if late:
                raise SeasonMissing(
                    f"Fantrax has moves from {late[0].when:%b %-d, %Y}, over a year after the {latest.year} auction. "
                    f"Enter the {latest.year + 1} auction start on the console, then sync again."
                )
```

`SeasonMissing` raised inside the atomic block rolls everything back. In `league/signing_views.py`, add `events.SeasonMissing` to the console's caught exceptions tuple; in `league/management/commands/sync_fantrax.py`, catch it and re-raise as `CommandError(str(e))` wherever `UnmatchedTeam` is handled.

- [ ] **Step 4: Run** `uv run pytest league -q` — all pass, including `test_sync_2026.py`.

- [ ] **Step 5: Commit** "Take a Fantrax move's season from its date".

---

### Task 3: Farm draft picks from Fantrax claims

**Files:**
- Modify: `league/models.py` (`FarmPick.player`, `FantraxEvent.Effect.FARM_DRAFTED`)
- Create: `league/migrations/0011_farm_draft.py` (generated)
- Modify: `league/events.py` (`apply_move`, new `draft`)
- Test: `league/tests/test_events.py`

**Interfaces:**
- Consumes: `seasons.farm_draft_at`, `Processor.seasons`, `rules.farm.DRAFT_PICK_SALARY`.
- Produces: `FarmPick.player` (nullable FK, `related_name="+"`); `Effect.FARM_DRAFTED`.

- [ ] **Step 1: Write failing tests** (append; import `FarmPick`):

```python
# --- farm draft ---------------------------------------------------------------


@pytest.fixture
def draft(world):
    league, proc, a, b, c, f = world
    Season.objects.create(
        year=2027,
        farm_draft_starts_at=datetime(2027, 2, 20, 18, 0, tzinfo=EASTERN),
        auction_starts_at=datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN),
    )
    for r in (1, 2):
        FarmPick.objects.create(year=2027, round=r, original_team=a, owner=a)
    return league, Processor(league, proc.teams, {"p7": "Rook", "p8": "Kid", "p9": "Late"}), a, b


def in_draft(fid, to, day=21, tx=None):
    return Move(datetime(2027, 2, day, 20, 0, tzinfo=EASTERN), "CLAIM", fid, None, to, tx or f"d{fid}")


def test_draft_claim_makes_a_one_dollar_farm_player(draft):
    league, proc, a, b = draft
    e = proc.apply_move(in_draft("p7", "ta"))
    fp = FarmPlayer.objects.get(player__fantrax_id="p7")
    assert (e.effect, fp.team, fp.salary, fp.salary_season, fp.drafted_year) == ("FARM_DRAFTED", a, 1, 2027, 2027)
    assert FarmPick.objects.get(year=2027, round=1, owner=a).player == fp.player
    proc.apply_move(in_draft("p8", "ta", day=22))
    assert FarmPick.objects.get(year=2027, round=2, owner=a).player.name == "Kid"


def test_draft_claim_without_a_pick_is_an_exception(draft):
    league, proc, a, b = draft
    e = proc.apply_move(in_draft("p7", "tb"))
    assert e.effect == "EXCEPTION" and "no 2027 pick left" in e.detail
    assert not FarmPlayer.objects.filter(player__fantrax_id="p7").exists()


def test_claim_after_the_auction_start_is_not_a_draft_pick(draft):
    league, proc, a, b = draft
    late = Move(datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN), "CLAIM", "p9", None, "ta", "late")
    assert proc.apply_move(late).effect == "NONE"
    assert not FarmPick.objects.exclude(player=None).exists()


def test_draft_claim_of_a_contracted_player_is_an_exception(draft):
    league, proc, a, b = draft
    assert proc.apply_move(in_draft("p1", "tb")).effect == "EXCEPTION"
```

- [ ] **Step 2: Run** — fail (`FarmPick` has no `player`).

- [ ] **Step 3: Models.** On `FarmPick` add

```python
    player = models.ForeignKey(
        Player, on_delete=models.PROTECT, null=True, blank=True, related_name="+", help_text="Who the pick was spent on"
    )
```

and in `FantraxEvent.Effect` add `FARM_DRAFTED = "FARM_DRAFTED", "Farm drafted"` after `FARM_DEBUT`. Run `uv run python manage.py makemigrations league --name farm_draft`.

- [ ] **Step 4: Processor.** Import `FarmPick`; `from rules.farm import DRAFT_PICK_SALARY`. In `apply_move`, directly after the `rec` helper and before the `if m.kind == "CLAIM" or ...` line:

```python
        if m.kind == "CLAIM" and (draft := seasons.farm_draft_at(m.when, self.seasons)):
            return self.draft(m, key, draft.year, to, name)
```

New method:

```python
    def draft(self, m: Move, key: str, year: int, to: Team, name: str) -> FantraxEvent:
        """A claim in the farm draft window is a farm pick: $1, spending the team's lowest unused pick."""

        def rec(effect, detail, **links):
            return self.record(key, m.kind, effect, m.when, detail, m.fantrax_id, to_team=to, **links)

        if held := self.farm(m.fantrax_id):
            if held.team == to:
                return rec(Effect.ALREADY_REFLECTED, f"drafted {name}: already on the {to.code} farm", farm_player=held)
            return rec(Effect.EXCEPTION, f"{to.code} drafted {name}, but he's on the {held.team.code} farm", farm_player=held)
        if held := self.contract(m.fantrax_id, m.when):
            return rec(Effect.EXCEPTION, f"{to.code} drafted {name}, but he's under contract to {held.team.code}", contract=held)
        pick = FarmPick.objects.filter(year=year, owner=to, player=None).order_by("round", "pk").first()
        if pick is None:
            return rec(
                Effect.EXCEPTION,
                f"{to.code} claimed {name} in the farm draft but has no {year} pick left: add him in the admin if "
                "he's a farm player",
            )
        player = self.player(m.fantrax_id)
        if player is None:
            player = Player.objects.create(name=name, fantrax_id=m.fantrax_id)
            self._players[m.fantrax_id] = player
        farm = FarmPlayer.objects.create(
            team=to, player=player, drafted_year=year, salary=DRAFT_PICK_SALARY, salary_season=year
        )
        pick.player = player
        pick.save(update_fields=["player"])
        self.tracked.add(m.fantrax_id)
        return rec(
            Effect.FARM_DRAFTED,
            f"drafted {name} to the {to.code} farm ({year} round {pick.round}, ${DRAFT_PICK_SALARY})",
            farm_player=farm,
        )
```

- [ ] **Step 5: Run** `uv run pytest league -q` — pass.

- [ ] **Step 6: Commit** "Read farm draft picks from Fantrax claims".

---

### Task 4: Rollover — dates, checklist, start

**Files:**
- Modify: `league/models.py` (add `SeasonBudget` after `Season`)
- Create: `league/migrations/0012_seasonbudget.py` (generated)
- Modify: `league/seasons.py` (rollover functions), `league/admin.py` (read-only `SeasonBudget`), `league/management/commands/import_league.py` (refuse `--replace` after a rollover)
- Test: `league/tests/test_seasons.py`

**Interfaces:**
- Consumes: `league.budget.team_budget`, `current()`, `Season`.
- Produces: `SeasonBudget` (`season`, `team`, `base`, `contracts`, `buyouts`, `farm`, `missed_ip`, `cash_net`, `remaining`, `frozen_at`); `seasons.RolloverError`; `seasons.Check(label, ok)`; `seasons.upcoming() -> Season | None`; `seasons.set_dates(farm_draft_starts_at, auction_starts_at, user) -> Season`; `seasons.checklist(now=None) -> list[Check]`; `seasons.start(year, user) -> Season`; `seasons.FARM_ROUNDS = (1, 2)`.

- [ ] **Step 1: Write failing tests** (append to `league/tests/test_seasons.py`; imports: `from django.core.management import call_command`, `from league.budget import team_budget`, `from league.models import AuditEntry, FarmPick, SeasonBudget, SigningPeriod, Team`, `from league.tests.test_signing import settle_2026`):

```python
# --- rollover -------------------------------------------------------------------


@pytest.fixture
def league_2026():
    call_command("import_league", verbosity=0)
    settle_2026()


def ready_to_start():
    """Signing locked, the 2027 draft day entered and past, synced since.

    The checklist compares with the real clock, so the "2027" draft day is set in the past (mid-2026).
    """
    SigningPeriod.objects.create(season=2026, status="locked", locked_at=datetime(2026, 5, 1, tzinfo=EASTERN))
    seasons.set_dates(datetime(2026, 5, 20, tzinfo=EASTERN), datetime(2026, 6, 1, 19, 0, tzinfo=EASTERN), None)
    AuditEntry.objects.create(action="Synced Fantrax")  # `at` is now, after that auction start


def test_set_dates_creates_the_next_season_and_checks_order():
    with pytest.raises(seasons.RolloverError):
        seasons.set_dates(None, datetime(2026, 1, 1, tzinfo=EASTERN), None)
    with pytest.raises(seasons.RolloverError):
        seasons.set_dates(datetime(2027, 3, 1, tzinfo=EASTERN), datetime(2027, 2, 24, tzinfo=EASTERN), None)
    s = seasons.set_dates(None, datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN), None)
    assert (s.year, s.started_at, seasons.upcoming()) == (2027, None, s)


def test_checklist_lists_what_is_missing(league_2026):
    assert [c.ok for c in seasons.checklist()] == [False, False, False, True]
    ready_to_start()
    assert all(c.ok for c in seasons.checklist())


def test_start_is_refused_until_ready(league_2026):
    seasons.set_dates(None, datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN), None)
    with pytest.raises(seasons.RolloverError, match="Signing after 2026 is locked"):
        seasons.start(2027, None)
    assert seasons.current_season() == 2026


def test_start_freezes_budgets_adds_picks_and_advances(league_2026):
    ready_to_start()
    want = {t.code: team_budget(t, 2027) for t in Team.objects.all()}
    seasons.start(2027, None)
    assert seasons.current_season() == 2027
    for sb in SeasonBudget.objects.filter(season=2027).select_related("team"):
        b = want.pop(sb.team.code)
        assert (sb.remaining, sb.contracts, sb.buyouts, sb.farm, sb.cash_net) == (
            b.remaining, b.contracts, b.buyouts, b.farm, b.cash_net
        )
    assert want == {}
    new = FarmPick.objects.filter(year=2031)
    assert new.count() == 2 * Team.objects.count()
    assert all(p.owner_id == p.original_team_id for p in new)
    assert AuditEntry.objects.filter(action="Started season").exists()


def test_start_happens_once(league_2026):
    ready_to_start()
    seasons.start(2027, None)
    with pytest.raises(seasons.RolloverError, match="already started"):
        seasons.start(2027, None)
    assert FarmPick.objects.filter(year=2032).count() == 0
```

- [ ] **Step 2: Run** — fail (no `SeasonBudget`).

- [ ] **Step 3: Model** after `Season`:

```python
class SeasonBudget(models.Model):
    """A team's auction budget, frozen when its season started. For that auction only; never recomputed."""

    season = models.PositiveIntegerField()
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="frozen_budgets")
    base = models.PositiveIntegerField()
    contracts = models.PositiveIntegerField()
    buyouts = models.PositiveIntegerField()
    farm = models.PositiveIntegerField()
    missed_ip = models.PositiveIntegerField()
    cash_net = models.IntegerField()
    remaining = models.IntegerField()
    frozen_at = models.DateTimeField()

    class Meta:
        ordering = ["season", "team"]
        constraints = [models.UniqueConstraint(fields=["season", "team"], name="one_frozen_budget_per_season")]

    def __str__(self):
        return f"{self.team.code} {self.season} auction budget ${self.remaining}"
```

Run `uv run python manage.py makemigrations league --name seasonbudget`.

- [ ] **Step 4: Rollover in `league/seasons.py`.** Add imports (`from dataclasses import dataclass`, `from django.db import transaction`, `from django.db.models import Max`, `from django.utils import timezone`, `from league.budget import team_budget`, models `AuditEntry, FantraxEvent, FarmPick, SeasonBudget, SigningPeriod, Team, audit`) and:

```python
FARM_ROUNDS = (1, 2)


class RolloverError(Exception):
    """Starting a season, or setting its dates, isn't allowed right now."""


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool


def upcoming() -> Season | None:
    return Season.objects.filter(year=current_season() + 1).first()


@transaction.atomic
def set_dates(farm_draft_starts_at, auction_starts_at, user) -> Season:
    """Enter the next season's farm draft and auction start. Allowed until that season starts."""
    now = current()
    if auction_starts_at <= now.auction_starts_at:
        raise RolloverError(f"The {now.year + 1} auction starts after the {now.year} one")
    if farm_draft_starts_at and farm_draft_starts_at >= auction_starts_at:
        raise RolloverError("The farm draft starts before the auction")
    season, _ = Season.objects.select_for_update().get_or_create(
        year=now.year + 1, defaults={"auction_starts_at": auction_starts_at}
    )
    if season.started_at:
        raise RolloverError(f"The {season.year} season has started; change its dates in the admin")
    season.farm_draft_starts_at, season.auction_starts_at = farm_draft_starts_at, auction_starts_at
    season.save(update_fields=["farm_draft_starts_at", "auction_starts_at"])
    when = lambda t: f"{timezone.localtime(t):%b %-d %Y %-I:%M %p}" if t else "not set"  # noqa: E731
    audit(user, "Set season dates", f"{season.year}: farm draft {when(farm_draft_starts_at)}, auction {when(auction_starts_at)}")
    return season


def checklist(now=None) -> list[Check]:
    """What must be true before the next season starts. Everything is required."""
    now = now or timezone.now()
    year = current_season()
    period = SigningPeriod.objects.filter(season=year).first()
    nxt = Season.objects.filter(year=year + 1).first()
    auction_ran = bool(nxt and nxt.auction_starts_at <= now)
    synced = auction_ran and AuditEntry.objects.filter(action="Synced Fantrax", at__gte=nxt.auction_starts_at).exists()
    return [
        Check(f"Signing after {year} is locked", bool(period and period.status == SigningPeriod.Status.LOCKED)),
        Check(f"The {year + 1} auction start is entered and has passed", auction_ran),
        Check("Fantrax synced since the auction started", synced),
        Check("No unresolved Fantrax exceptions", not FantraxEvent.objects.unresolved().exists()),
    ]


@transaction.atomic
def start(year: int, user) -> Season:
    """Start `year`: freeze every team's auction budget, add the next year of farm picks, advance."""
    season = Season.objects.select_for_update().filter(year=year).first()
    if season is None:
        raise RolloverError(f"Enter the {year} auction start first")
    if season.started_at:
        raise RolloverError(f"The {year} season has already started")
    if year != current_season() + 1:
        raise RolloverError(f"The next season is {current_season() + 1}")
    if missing := [c.label for c in checklist() if not c.ok]:
        raise RolloverError("Not ready: " + "; ".join(missing))
    now, teams = timezone.now(), list(Team.objects.all())
    for team in teams:
        b = team_budget(team, year)
        SeasonBudget.objects.create(
            season=year, team=team, base=b.base, contracts=b.contracts, buyouts=b.buyouts, farm=b.farm,
            missed_ip=b.missed_ip, cash_net=b.cash_net, remaining=b.remaining, frozen_at=now,
        )
    pick_year = (FarmPick.objects.aggregate(y=Max("year"))["y"] or year) + 1
    FarmPick.objects.bulk_create(
        FarmPick(year=pick_year, round=r, original_team=t, owner=t) for t in teams for r in FARM_ROUNDS
    )
    season.started_at, season.started_by = now, user if user and user.is_authenticated else None
    season.save(update_fields=["started_at", "started_by"])
    audit(user, "Started season", f"The {year} season: froze {len(teams)} auction budgets, added {pick_year} farm picks")
    return season
```

(Run `uv run ruff format .` afterwards; it reflows the long calls.)

- [ ] **Step 5: Admin and import.** In `league/admin.py`:

```python
@admin.register(models.SeasonBudget)
class SeasonBudgetAdmin(admin.ModelAdmin):
    """Frozen when a season starts. Read-only: it's what each team brought to that auction."""

    list_display = ["season", "team", "remaining", "frozen_at"]
    list_filter = ["season"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
```

In `import_league.handle`, with the other `--replace` refusals:

```python
            if SeasonBudget.objects.exists():
                raise CommandError("A season has been started; --replace would erase it.")
```

- [ ] **Step 6: Run** `uv run pytest league -q` — pass.

- [ ] **Step 7: Commit** "Start the next season: freeze budgets, add farm picks".

---

### Task 5: Console and team page

**Files:**
- Modify: `league/signing_views.py` (`console`), `league/templates/league/console.html`
- Modify: `league/views.py` (`team`), `league/templates/league/team.html`
- Test: `league/tests/test_signing_views.py`, `league/tests/test_views.py`

**Interfaces:**
- Consumes: `seasons.set_dates`, `seasons.checklist`, `seasons.start`, `seasons.upcoming`, `seasons.window`, `seasons.current`, `SeasonBudget`.

- [ ] **Step 1: Failing tests.** In `league/tests/test_signing_views.py` (import `datetime`, `EASTERN`, `Season`, `SeasonBudget`, `seasons`):

```python
def test_console_sets_dates_and_starts_the_season(ready, commish):
    r = commish.post(
        "/commish/",
        {"action": "season_dates", "farm_draft_starts_at": "2027-02-20T18:00", "auction_starts_at": "2027-02-24T19:00"},
        follow=True,
    )
    s = Season.objects.get(year=2027)
    assert s.auction_starts_at == datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN)
    assert "Start the 2027 season" in r.content.decode()
    r = commish.post("/commish/", {"action": "start_season", "year": "2027"}, follow=True)
    assert "Not ready" in r.content.decode() and seasons.current_season() == 2026


def test_manager_cannot_start_a_season(ready, mb):
    assert mb.post("/commish/", {"action": "start_season", "year": "2027"}).status_code in (302, 403)
    assert not Season.objects.filter(year=2027).exists()
```

In `league/tests/test_views.py` (import `SeasonBudget`, `Season`):

```python
def test_team_page_shows_the_frozen_auction_budget(manager_client):
    Season.objects.create(year=2027, auction_starts_at="2027-02-24T19:00-05:00", started_at="2027-02-25T00:00-05:00")
    team = Team.objects.get(code="MB")
    SeasonBudget.objects.create(
        season=2027, team=team, base=400, contracts=200, buyouts=10, farm=4, missed_ip=0, cash_net=5,
        remaining=191, frozen_at="2027-02-25T00:00-05:00",
    )
    html = manager_client.get("/teams/MB/").content.decode()
    assert "2027 auction budget: $191" in html
    assert "Contracts that ended" not in html  # the 2027 season is under way
```

- [ ] **Step 2: Run** — fail.

- [ ] **Step 3: Console view.** In `console()`'s POST branch add (parse `datetime-local` values as local time with `django.utils.dateparse.parse_datetime` + `timezone.make_aware`):

```python
            elif action == "season_dates":
                seasons.set_dates(
                    _local(request.POST.get("farm_draft_starts_at")),
                    _local(request.POST.get("auction_starts_at")) or _required("the auction start"),
                    request.user,
                )
                messages.success(request, "Season dates saved.")
            elif action == "start_season":
                s = seasons.start(int(request.POST.get("year", 0)), request.user)
                messages.success(request, f"The {s.year} season has started. Auction budgets are frozen.")
```

with module-level helpers

```python
def _local(text):
    """A datetime-local form value, in the league's time zone. Blank is None."""
    value = parse_datetime(text or "")
    return timezone.make_aware(value) if value else None


def _required(what):
    raise ValueError(f"Enter {what}")
```

Add `seasons.RolloverError` to the caught tuple. Add to the render context: `"upcoming": seasons.upcoming()`, `"checklist": seasons.checklist()`, `"next_year": s + 1`.

- [ ] **Step 4: Console template.** Insert before `<h2>Enter things by hand</h2>`:

```html
<h2>The {{ next_year }} season</h2>
<p class="muted">Draft day runs in Fantrax: the farm draft, then the auction. Every claim from the farm draft start until
  the auction start is a farm pick ($1). After the auction, sync, then start the season: that freezes each team's auction
  budget and adds the next year of farm picks.</p>
<form method="post" class="season-dates">{% csrf_token %}<input type="hidden" name="action" value="season_dates">
  <label>Farm draft starts <input type="datetime-local" name="farm_draft_starts_at" value="{{ upcoming.farm_draft_starts_at|date:'Y-m-d\TH:i' }}"></label>
  <label>Auction starts <input type="datetime-local" name="auction_starts_at" required value="{{ upcoming.auction_starts_at|date:'Y-m-d\TH:i' }}"></label>
  <button type="submit">Save dates</button> <span class="muted">Eastern time</span></form>
{% if upcoming %}
<ul class="checklist">{% for c in checklist %}<li class="{% if c.ok %}ok{% else %}todo{% endif %}">{{ c.label }}</li>{% endfor %}</ul>
<form method="post">{% csrf_token %}<input type="hidden" name="year" value="{{ upcoming.year }}">
  <button type="submit" name="action" value="start_season" class="primary">Start the {{ upcoming.year }} season</button></form>
{% endif %}
```

- [ ] **Step 5: Team page.** In `views.team()`:

```python
    current = seasons.current()
    start, end = seasons.window(current)
    moves = FantraxEvent.objects.filter(Q(from_team=team) | Q(to_team=team), happened_at__gte=start)
    if end:
        moves = moves.filter(happened_at__lt=end)
```

(replace the old `league__season=` filter with `moves`, keeping the existing `.exclude(...)`), and add `"frozen": SeasonBudget.objects.filter(team=team, season=current.year).first()` to the context. In `team.html`, after the summary paragraph:

```html
{% if frozen %}<p><strong>{{ frozen.season }} auction budget: ${{ frozen.remaining }}</strong> <span class="muted">(frozen
  {{ frozen.frozen_at|date:"M j" }}: base ${{ frozen.base }} − contracts ${{ frozen.contracts }} − buyouts ${{ frozen.buyouts }}
  − farm ${{ frozen.farm }}{% if frozen.missed_ip %} − missed IP ${{ frozen.missed_ip }}{% endif %}{% if frozen.cash_net %} ± cash ${{ frozen.cash_net }}{% endif %})</span></p>{% endif %}
```

and change the ended-contracts heading to

```html
<h2>{% if signing_status %}Contracts that ended with the {{ league_season }} season{% else %}Final year: {{ league_season }}{% endif %}</h2>
```

- [ ] **Step 6: Run** `uv run pytest -q` — pass.

- [ ] **Step 7: Commit** "Console: season dates and Start; team page shows the frozen budget".

---

### Task 6: Docs

**Files:** `RULES.md`, `ARCHITECTURE.md`, `league/templates/league/help.html`, `league/management/commands/import_league.py` (comment only)

- [ ] **Step 1: RULES.md.**
  - In the Fantrax processing bullets: season S runs from the S auction start to just before the S+1 auction, and a move's season comes from its date. The 2026 cutoff note: the 18:21 claim was a farm pick (Eduardo Quintero), so 16:00 is really the start of the 2026 farm draft; the imported sheet covers it.
  - Farm: the farm draft runs in Fantrax before the auction; every claim in the window is a farm pick at $1 whatever Fantrax shows, spending the team's lowest unused pick; a team with no pick left is flagged.
  - Golden tests paragraph: Lawson and Yamashita are entered at $1 for 2026, so they're $2 in 2027; there's no 2026 correction.
  - New **Rollover** bullet: enter dates, draft day in Fantrax, sync, Start (checklist), frozen budget, next year of picks (four-year horizon).
- [ ] **Step 2: ARCHITECTURE.md.** Status line → this branch. §4 "Today": league pages use `seasons.current_season()`. New "Seasons and rollover" subsection (models `Season`, `SeasonBudget`, `FarmPick.player`; `league/seasons.py`; Processor by date; `SeasonMissing`). Fantrax events: farm draft claims, `FARM_DRAFTED`. §3 "(planned)" per-season rules config: keep. Remove the `LEAGUE_SEASON` mention.
- [ ] **Step 3: help.html** commissioner guide: a "Draft day and the new season" `<h3>` with the four steps (enter dates on the console, run the farm draft and auction in Fantrax, sync, Start the season).
- [ ] **Step 4: import_league comment** at the `process_since` line: "the first claim at 18:21 was a farm pick; 16:00 is the start of draft day".
- [ ] **Step 5: Verify** `uv run ruff check . && uv run ruff format --check . && uv run python manage.py makemigrations --check --dry-run && uv run pytest -q`.
- [ ] **Step 6: Commit** "Docs for seasons, the farm draft and rollover".
