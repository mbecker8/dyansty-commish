# Discord Cash Trades Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read cash trades for budget seasons 2027+ from the league Discord's `#trades-<year>-assets` channels, on demand, and keep `CashTrade` equal to what the posts say.

**Architecture:** `league/discord_client.py` fetches the "Rules and Accounting" category over REST, through the Cloudflare Worker in production. `league/discord_cash.py` holds the pure rules that turn a post into wanted cash trades. `league/discord_sync.py` stores every message as a `DiscordMessage` and follows Discord in one transaction. A `sync_discord` command and a console panel (sync button, exception resolve form) drive it. Unresolved Discord exceptions block opening signing.

**Tech Stack:** Django 6.1, requests, pytest/pytest-django, Playwright (e2e), Cloudflare Worker (plain JS, node test).

**Spec:** `docs/superpowers/specs/2026-10-03-discord-cash-trades-design.md`

## Global Constraints

- Discord is the only source of cash trades for budget seasons ≥ 2027 (`FIRST_SEASON = 2027`). A channel's year is the auction its cash applies to (`CashTrade.budget_season`).
- Fantrax trade comments are ignored: the `CASH_COMMENT` rule goes away, and the `CASH_COMMENT` kind stays for old rows.
- Every message in the category is stored. Only `trades-<year>-assets` channels with year ≥ 2027 make cash trades.
- The app follows Discord: an edit updates and a deletion removes, both audited. Once a season has a `SeasonBudget` (frozen), a change becomes an exception and nothing changes.
- Unresolved Discord exceptions block `open_period`.
- Read-only REST, on demand: `GET /v10/guilds/<id>/channels` and `GET /v10/channels/<id>/messages`. No gateway, no schedule.
- Settings `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID` and `DISCORD_CATEGORY_ID`: read from the environment, else from their line in `secrets/discord.env`.
- Tests never call real Discord. Real messages stay in `secrets/`, so tests use made-up IDs and names.
- Every PR: full `uv run pytest` (e2e included) locally. Ruff check and format must pass.

## Review Focus

1. **A private channel in the category** (`#rule-change-proposals`; the bot gets 403 "Missing Access", code 50001) must be skipped, not end the sync or mark its stored messages deleted. Tests: Task 2 `test_private_channel_is_skipped`, Task 5 `test_unreadable_channel_keeps_its_messages`.
2. **A trade channel moved out of the category** (e.g. into "archived" after its auction) must not have its posts treated as deleted. Test: Task 5 `test_channel_moved_out_of_the_category_keeps_its_cash`.
3. **The nickname mention form `<@!id>`, extra spaces, and "sends"/"to" in any case** must read the same as `<@id>`. Test: Task 4 parametrized shapes.
4. **Wrong or missing configuration:** with no token, the console button is disabled and the command names the missing setting. A category ID that matches no channel must fail loudly, not "succeed" with zero messages. Tests: Task 2 `test_empty_category_is_an_error`, Task 6 `test_console_discord_not_set_up`.
5. **A post edited after you resolved it with hand-entered cash** reopens the exception, keeps the cash trade, and blocks signing until it's resolved again. Test: Task 5 `test_an_edit_reopens_a_resolved_exception`.

---

### Task 1: Worker forwards the bot's two read-only routes

**Files:**
- Modify: `cloudflare/discord-proxy/worker.js`
- Test: `cloudflare/discord-proxy/worker.test.mjs`

**Interfaces:** Produces `GET /v10/guilds/<digits>/channels` and `GET /v10/channels/<digits>/messages?limit&before&after`, forwarded to `https://discord.com/api` + path. Everything else is unchanged.

- [ ] **Step 1: Failing test.** Append before `console.log("worker ok")`:

```js
// Commissioner Bot's reads: GET only, digits only, and only the paging parameters reach Discord.
calls.length = 0;
globalThis.fetch = async (url, init) => {
  calls.push({ url, init });
  return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
};
const bot = { "x-proxy-key": "k3y", authorization: "Bot t0k" };
assert.equal((await call("GET", "/v10/guilds/123/channels", bot)).status, 200);
assert.equal(calls[0].url, "https://discord.com/api/v10/guilds/123/channels");
assert.equal(calls[0].init.headers.get("authorization"), "Bot t0k");
assert.equal((await call("GET", "/v10/channels/45/messages?limit=100&before=9&with=x", bot)).status, 200);
assert.equal(calls[1].url, "https://discord.com/api/v10/channels/45/messages?limit=100&before=9");
assert.equal((await call("POST", "/v10/channels/45/messages", bot, "{}")).status, 404);
assert.equal((await call("GET", "/v10/channels/abc/messages", bot)).status, 404);
assert.equal((await call("GET", "/v10/guilds/123/members", bot)).status, 404);
assert.equal((await call("GET", "/v10/guilds/123/channels")).status, 403);
assert.equal(calls.length, 2);
```

- [ ] **Step 2:** `node cloudflare/discord-proxy/worker.test.mjs`. Expected: AssertionError (404 ≠ 200).
- [ ] **Step 3: Implement.** In `worker.js`, replace the `ALLOWED` check and the upstream URL:

```js
const ALLOWED = new Set(["POST /oauth2/token", "GET /users/@me"]);
// Commissioner Bot reads the league's Rules and Accounting channels (league/discord_client.py). Read-only.
const BOT_ROUTES = [/^\/v10\/guilds\/\d+\/channels$/, /^\/v10\/channels\/\d+\/messages$/];
const PAGING = ["limit", "before", "after"];

function allowed(method, path) {
  return ALLOWED.has(`${method} ${path}`) || (method === "GET" && BOT_ROUTES.some((r) => r.test(path)));
}

function pagingQuery(url) {
  const params = new URLSearchParams();
  for (const name of PAGING) if (url.searchParams.has(name)) params.set(name, url.searchParams.get(name));
  const query = params.toString();
  return query ? "?" + query : "";
}
```

In `fetch`, use `if (!allowed(request.method, url.pathname))` and `fetch(DISCORD_API + url.pathname + pagingQuery(url), …)`. Update the header comment: the Worker now also forwards the bot's two reads.
- [ ] **Step 4:** Run the test. Expected: `worker ok`.
- [ ] **Step 5:** Commit "Let the Discord Worker forward Commissioner Bot's channel reads", then push.

### Task 2: Settings and the Discord reader

**Files:**
- Modify: `config/settings.py`, `accounts/discord.py` (make `api_url` and `proxy_headers` public), `render.yaml`, `conftest.py`
- Create: `league/discord_client.py`
- Test: `league/tests/test_discord_client.py`

**Interfaces:**
- Produces: `discord_client.configured() -> bool`, `discord_client.fetch_category() -> list[Channel]`, `Channel(id: str, name: str, messages: list[dict], readable: bool)`, `DiscordReadError(Exception)`.
- Produces: `accounts.discord.api_url(path)` and `accounts.discord.proxy_headers()`, the renamed `_api` and `_proxy_headers`.

- [ ] **Step 1: Failing tests** in `league/tests/test_discord_client.py`:

```python
from unittest import mock

import pytest
import requests

from league import discord_client
from league.discord_client import DiscordReadError


@pytest.fixture
def bot(settings):
    settings.DISCORD_BOT_TOKEN, settings.DISCORD_GUILD_ID, settings.DISCORD_CATEGORY_ID = "tok", "1", "10"
    settings.DISCORD_API_BASE, settings.DISCORD_PROXY_KEY = "https://discord.com/api", ""


def reply(status, body):
    return mock.Mock(status_code=status, json=lambda: body, text=str(body))


def channels(*extra):
    return [
        {"id": "11", "name": "trades-2027-assets", "type": 0, "parent_id": "10", "position": 2},
        {"id": "12", "name": "league-rules", "type": 0, "parent_id": "10", "position": 1},
        {"id": "13", "name": "general", "type": 0, "parent_id": "99", "position": 0},
        {"id": "14", "name": "Voice", "type": 2, "parent_id": "10", "position": 3},
        *extra,
    ]


def routed(routes):
    """requests.get stand-in: the reply for the first route whose path fragment is in the URL."""

    def get(url, params=None, headers=None, timeout=None):
        assert headers["Authorization"] == "Bot tok" and timeout
        for fragment, answer in routes.items():
            if fragment in url:
                return answer(params or {}) if callable(answer) else answer
        raise AssertionError(url)

    return get


def test_reads_every_text_channel_in_the_category_with_all_pages(bot):
    page1 = [{"id": str(i)} for i in range(300, 200, -1)]
    page2 = [{"id": "150"}]

    def history(params):
        return reply(200, {None: page1, "201": page2, "150": []}[params.get("before")])

    routes = {"/guilds/1/channels": reply(200, channels()), "/channels/11/": history, "/channels/12/": reply(200, [])}
    with mock.patch("league.discord_client.requests.get", side_effect=routed(routes)) as get:
        out = discord_client.fetch_category()
    assert [c.name for c in out] == ["league-rules", "trades-2027-assets"]
    assert len(out[1].messages) == 101 and out[1].readable
    assert get.call_args_list[0].args[0] == "https://discord.com/api/v10/guilds/1/channels"


def test_private_channel_is_skipped(bot):
    routes = {
        "/guilds/1/channels": reply(200, channels()),
        "/channels/11/": reply(200, []),
        "/channels/12/": reply(403, {"message": "Missing Access", "code": 50001}),
    }
    with mock.patch("league.discord_client.requests.get", side_effect=routed(routes)):
        out = {c.name: c for c in discord_client.fetch_category()}
    assert not out["league-rules"].readable and out["trades-2027-assets"].readable


@pytest.mark.parametrize(
    "status, body, says",
    [
        (401, {"message": "401: Unauthorized"}, "DISCORD_BOT_TOKEN"),
        (403, {"message": "Missing Access", "code": 50001}, "still in the league server"),
        (403, "Forbidden", "DISCORD_PROXY_KEY"),
        (500, {"message": "boom"}, "500"),
    ],
)
def test_errors_say_what_to_fix(bot, status, body, says):
    with mock.patch("league.discord_client.requests.get", return_value=reply(status, body)):
        with pytest.raises(DiscordReadError, match=says):
            discord_client.fetch_category()


def test_rate_limit_waits_then_retries(bot):
    answers = iter([reply(429, {"retry_after": 0.01}), reply(200, channels()[:1]), reply(200, [])])
    with mock.patch("league.discord_client.requests.get", side_effect=lambda *a, **k: next(answers)):
        with mock.patch("league.discord_client.time.sleep") as sleep:
            (ch,) = discord_client.fetch_category()
    sleep.assert_called_once()
    assert ch.name == "trades-2027-assets"


def test_network_failure_is_a_read_error(bot):
    with mock.patch("league.discord_client.requests.get", side_effect=requests.ConnectionError("down")):
        with pytest.raises(DiscordReadError, match="Couldn't reach Discord"):
            discord_client.fetch_category()


def test_empty_category_is_an_error(bot):
    with mock.patch("league.discord_client.requests.get", return_value=reply(200, channels()[2:3])):
        with pytest.raises(DiscordReadError, match="DISCORD_CATEGORY_ID"):
            discord_client.fetch_category()


def test_not_configured(settings):
    settings.DISCORD_BOT_TOKEN = ""
    assert not discord_client.configured()
    with pytest.raises(DiscordReadError, match="DISCORD_BOT_TOKEN"):
        discord_client.fetch_category()


def test_goes_through_the_proxy_when_set(bot, settings):
    settings.DISCORD_API_BASE, settings.DISCORD_PROXY_KEY = "https://proxy.example", "k"
    with mock.patch("league.discord_client.requests.get", return_value=reply(200, [])) as get:
        with pytest.raises(DiscordReadError):
            discord_client.fetch_category()
    assert get.call_args.args[0] == "https://proxy.example/v10/guilds/1/channels"
    assert get.call_args.kwargs["headers"]["X-Proxy-Key"] == "k"
```

- [ ] **Step 2:** `uv run pytest league/tests/test_discord_client.py -q`. Expected: ImportError.
- [ ] **Step 3: Implement.**

`accounts/discord.py`: rename `_api` → `api_url` and `_proxy_headers` → `proxy_headers`, including their uses.

`config/settings.py`, after `DISCORD_PROXY_KEY`:

```python
def _discord_env(name: str) -> str:
    """Env var, else its line in the gitignored secrets/discord.env (local runs)."""
    if value := os.environ.get(name, "").strip():
        return value
    path = BASE_DIR / "secrets" / "discord.env"
    if path.exists():
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip("'\"")
    return ""


# Commissioner Bot (its own Discord app) reads the league server's "Rules and Accounting" channels.
# Cash for the 2027 auction on comes from its #trades-<year>-assets channels (league/discord_sync.py).
DISCORD_BOT_TOKEN = _discord_env("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = _discord_env("DISCORD_GUILD_ID")
DISCORD_CATEGORY_ID = _discord_env("DISCORD_CATEGORY_ID")
```

`conftest.py`: add an autouse fixture so no test reaches real Discord through a local `secrets/discord.env`:

```python
@pytest.fixture(autouse=True)
def _no_real_discord_bot(settings):
    # A local secrets/discord.env holds the real bot token; tests that need one set their own.
    settings.DISCORD_BOT_TOKEN = settings.DISCORD_GUILD_ID = settings.DISCORD_CATEGORY_ID = ""
```

`render.yaml`, after `COMMISSIONER_DISCORD_IDS`:

```yaml
      # Commissioner Bot reads the league Discord's Rules and Accounting channels (cash trades for
      # 2027 on): its bot token, the server ID and the category ID (docs/discord-signin.md).
      - key: DISCORD_BOT_TOKEN
        sync: false
      - key: DISCORD_GUILD_ID
        sync: false
      - key: DISCORD_CATEGORY_ID
        sync: false
```

`league/discord_client.py`:

```python
"""Read the league Discord's "Rules and Accounting" channels as Commissioner Bot. Read-only GETs.

Production goes through our Cloudflare Worker (DISCORD_API_BASE), like sign-in: Discord's Cloudflare
blocks Render's shared outbound IPs (docs/discord-signin.md).
"""

import time
from dataclasses import dataclass, field

import requests
from django.conf import settings

from accounts.discord import api_url, proxy_headers

TIMEOUT = 20  # seconds
TEXT_CHANNELS = {0, 5}  # text and announcement channels
RETRIES = 3
MISSING_ACCESS = 50001


class DiscordReadError(Exception):
    """Discord couldn't be read. Nothing was changed."""


@dataclass
class Channel:
    id: str
    name: str
    messages: list[dict] = field(default_factory=list)  # newest first, as Discord returns them
    readable: bool = True  # False: a private channel the bot can't see


def configured() -> bool:
    return bool(settings.DISCORD_BOT_TOKEN and settings.DISCORD_GUILD_ID and settings.DISCORD_CATEGORY_ID)


def fetch_category() -> list[Channel]:
    """Every text channel in the category, with all its messages."""
    if not configured():
        raise DiscordReadError(
            "The Discord sync isn't set up: it needs DISCORD_BOT_TOKEN, DISCORD_GUILD_ID and DISCORD_CATEGORY_ID"
        )
    listed = _json(_ok(_get(f"/guilds/{settings.DISCORD_GUILD_ID}/channels"), "the server's channel list"))
    try:
        inside = [
            c for c in listed if c.get("parent_id") == settings.DISCORD_CATEGORY_ID and c.get("type") in TEXT_CHANNELS
        ]
        channels = [Channel(c["id"], c["name"]) for c in sorted(inside, key=lambda c: c.get("position", 0))]
    except (AttributeError, KeyError, TypeError) as e:
        raise DiscordReadError(f"Unexpected channel list from Discord: {e!r}") from e
    if not channels:
        raise DiscordReadError("No text channels in that category: check DISCORD_CATEGORY_ID")
    for channel in channels:
        _read_history(channel)
    return channels


def _read_history(channel: Channel) -> None:
    before = None
    while True:
        r = _get(f"/channels/{channel.id}/messages", {"limit": 100} | ({"before": before} if before else {}))
        if r.status_code == 403 and _code(r) == MISSING_ACCESS:
            channel.readable, channel.messages = False, []
            return
        page = _json(_ok(r, f"#{channel.name}"))
        if not page:
            return
        channel.messages.extend(page)
        before = page[-1]["id"]


def _get(path: str, params: dict | None = None):
    headers = {
        "Authorization": f"Bot {settings.DISCORD_BOT_TOKEN}",
        "User-Agent": "DiscordBot (https://dynasty-commish.onrender.com, 1.0)",
        **proxy_headers(),
    }
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(api_url("/v10" + path), params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise DiscordReadError(f"Couldn't reach Discord: {e}") from e
        if r.status_code != 429 or attempt == RETRIES:
            return r
        time.sleep(min(_retry_after(r), 10))
    raise AssertionError("unreachable")


def _ok(r, what: str):
    if r.status_code == 200:
        return r
    if r.status_code == 401:
        raise DiscordReadError("Discord rejected the bot token: update DISCORD_BOT_TOKEN")
    if r.status_code == 403 and _code(r) is None:
        raise DiscordReadError("Our Discord proxy refused the key: DISCORD_PROXY_KEY must match the Worker's PROXY_KEY")
    if r.status_code in (403, 404):
        raise DiscordReadError(
            f"Discord refused {what} ({r.status_code}): is Commissioner Bot still in the league server, "
            "and are DISCORD_GUILD_ID and DISCORD_CATEGORY_ID right?"
        )
    if r.status_code == 429:
        raise DiscordReadError("Discord is rate-limiting this server: try again in a few minutes")
    raise DiscordReadError(f"Discord error reading {what} ({r.status_code}): {r.text[:200]}")


def _json(r):
    try:
        return r.json()
    except ValueError as e:
        raise DiscordReadError(f"Unexpected answer from Discord: {r.text[:200]}") from e


def _code(r):
    try:
        return r.json().get("code")
    except ValueError, AttributeError:
        return None


def _retry_after(r) -> float:
    try:
        return float(r.json().get("retry_after", 1))
    except ValueError, AttributeError, TypeError:
        return 1.0
```

- [ ] **Step 4:** Run the new tests, then `uv run pytest accounts -q`. Expected: all pass.
- [ ] **Step 5:** Commit "Read the league Discord's category as Commissioner Bot", then push.

### Task 3: `DiscordMessage`, the `CashTrade` link, and dropping Fantrax trade comments

**Files:**
- Modify: `league/models.py`, `league/events.py` (delete `apply_trade_comments` and its call), `league/fantrax_data.py` (delete `trade_comments`), `league/management/commands/import_league.py` (delete `import_cash_from_fantrax`, its call and docstring line), `league/tests/test_events.py` (delete `test_cash_comments_in_the_old_league_are_still_listed`, the FakeSnapshot `comments` field and the `trade_comments` method)
- Delete: `data/league/cash_trades_from_fantrax.json`
- Create: `league/migrations/0016_discord_message.py` (makemigrations), `league/migrations/0017_cash_from_discord.py` (data)
- Test: `league/tests/test_discord_models.py`

**Interfaces:**
- Produces: `DiscordMessage` (fields as in the spec, plus `readable`), `DiscordMessage.Status` with CASH, NOT_CASH, IGNORED, EXCEPTION, `DiscordMessage.objects.unresolved()`, `DiscordMessage.season` (int | None), `DiscordMessage.url`, and `CashTrade.discord_message` (related name `cash_trades`).
- Produces: `league.discord_cash.FIRST_SEASON` and `channel_season(name)`. The model needs `channel_season`, so this task creates `league/discord_cash.py` with just these two; Task 4 adds the reading rules.

- [ ] **Step 1: Failing tests** in `league/tests/test_discord_models.py`:

```python
from importlib import import_module

import pytest
from django.apps import apps

from league.discord_cash import channel_season
from league.models import AuditEntry, CashTrade, DiscordMessage, Team

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "name, season",
    [("trades-2027-assets", 2027), ("trades-2031-assets", 2031), ("trades-2026-assets", None), ("league-rules", None)],
)
def test_channel_season(name, season):
    assert channel_season(name) == season


def test_message_url_and_unresolved(settings):
    settings.DISCORD_GUILD_ID = "1"
    m = DiscordMessage.objects.create(
        message_id="5", channel_id="2", channel_name="trades-2027-assets", author_id="9",
        posted_at="2026-07-01T00:00Z", synced_at="2026-07-01T00:00Z", status="EXCEPTION",
    )  # fmt: skip
    assert m.url == "https://discord.com/channels/1/2/5"
    assert m.season == 2027
    assert list(DiscordMessage.objects.unresolved()) == [m]


def test_migration_drops_only_future_cash_from_fantrax_comments():
    a = Team.objects.create(code="AA", name="A")
    b = Team.objects.create(code="BB", name="B")
    keep_old = CashTrade.objects.create(budget_season=2026, from_team=a, to_team=b, amount=1, fantrax_tx_id="t1")
    keep_hand = CashTrade.objects.create(budget_season=2027, from_team=a, to_team=b, amount=2)
    CashTrade.objects.create(budget_season=2027, from_team=a, to_team=b, amount=5, fantrax_tx_id="t2")
    import_module("league.migrations.0017_cash_from_discord").drop_fantrax_comment_cash(apps, None)
    assert set(CashTrade.objects.all()) == {keep_old, keep_hand}
    assert AuditEntry.objects.filter(action="Removed cash trade").count() == 1
```

- [ ] **Step 2:** Run it. Expected: ImportError.
- [ ] **Step 3: Implement.**

`league/discord_cash.py` (first part):

```python
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
```

`league/models.py`: add `from django.conf import settings`. On `CashTrade` add:

```python
    discord_message = models.ForeignKey(
        "DiscordMessage", on_delete=models.PROTECT, null=True, blank=True, related_name="cash_trades",
        help_text="The #trades-<year>-assets post this came from (Sync from Discord)",
    )  # fmt: skip
```

After `FantraxEvent`:

```python
class DiscordMessageQuerySet(models.QuerySet):
    def unresolved(self):
        return self.filter(status=DiscordMessage.Status.EXCEPTION, resolved_at=None)


class DiscordMessage(models.Model):
    """One message in the league Discord's "Rules and Accounting" channels, stored once by its ID.

    Posts in #trades-<year>-assets (2027 on) are the only source of those auctions' cash trades; each
    sync keeps a post's cash trades equal to what it says now (league/discord_sync.py).
    """

    class Status(models.TextChoices):
        CASH = "CASH", "Cash trade"
        NOT_CASH = "NOT_CASH", "No cash"
        IGNORED = "IGNORED", "Not a trade channel"
        EXCEPTION = "EXCEPTION", "Exception"

    message_id = models.CharField(max_length=32, unique=True)
    channel_id = models.CharField(max_length=32)
    channel_name = models.CharField(max_length=100)
    author_id = models.CharField(max_length=32)
    author_name = models.CharField(max_length=100, blank=True)
    posted_at = models.DateTimeField()
    edited_at = models.DateTimeField(null=True, blank=True)
    content = models.TextField(blank=True)
    readable = models.TextField(blank=True, help_text="The text with @mentions shown as usernames")
    has_attachments = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    detail = models.TextField(blank=True)
    synced_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    resolved_note = models.TextField(blank=True)
    resolved_content = models.TextField(blank=True, help_text="The text when resolved; an edit reopens it")

    objects = DiscordMessageQuerySet.as_manager()

    class Meta:
        ordering = ["-posted_at"]

    def __str__(self):
        return f"#{self.channel_name} {self.posted_at:%Y-%m-%d} {self.author_name}: {self.readable[:60]}"

    @property
    def season(self) -> int | None:
        from league.discord_cash import channel_season

        return channel_season(self.channel_name)

    @property
    def url(self) -> str:
        return f"https://discord.com/channels/{settings.DISCORD_GUILD_ID}/{self.channel_id}/{self.message_id}"
```

`uv run python manage.py makemigrations league -n discord_message`.

`league/migrations/0017_cash_from_discord.py`:

```python
"""Cash for 2027 on comes only from Discord: drop the three trades entered from Fantrax trade comments."""

from django.db import migrations


def drop_fantrax_comment_cash(apps, schema_editor):
    CashTrade = apps.get_model("league", "CashTrade")
    AuditEntry = apps.get_model("league", "AuditEntry")
    for c in (
        CashTrade.objects.filter(budget_season__gte=2027)
        .exclude(fantrax_tx_id="")
        .select_related("from_team", "to_team")
    ):
        detail = f"${c.amount} {c.from_team.code} -> {c.to_team.code} ({c.budget_season}), from a Fantrax comment"
        for team in (c.from_team, c.to_team):
            AuditEntry.objects.create(
                team=team, action="Removed cash trade", detail=detail, note="Discord is the source for 2027 on"
            )
        c.delete()


class Migration(migrations.Migration):
    dependencies = [("league", "0016_discord_message")]
    operations = [migrations.RunPython(drop_fantrax_comment_cash, migrations.RunPython.noop)]
```

Then remove the trade-comment code and the JSON file as listed under Files.
- [ ] **Step 4:** `uv run pytest league -q`. Expected: all pass. `test_events`, `test_import_league` and `test_signing` still pass without the comment exceptions, because `settle_2026` resolves whatever is open.
- [ ] **Step 5:** Commit "Store Discord messages and stop reading cash from Fantrax comments", then push.

### Task 4: Reading a trade post

**Files:**
- Modify: `league/discord_cash.py`
- Test: `league/tests/test_discord_cash.py`

**Interfaces:**
- Produces: `Wanted(from_team, to_team, amount: int, note: str)`, `Reading(status: str, detail: str, trades: list[Wanted])`, `read_post(content: str, has_attachments: bool, season: int, teams: dict[str, Team], names: dict[str, str]) -> Reading`, `readable_text(content, names) -> str`, and the constants `CASH`, `NOT_CASH`, `EXCEPTION`. `teams` maps Discord user ID → Team; `names` maps a mentioned user ID → username.

- [ ] **Step 1: Failing tests**:

```python
from types import SimpleNamespace

import pytest

from league.discord_cash import CASH, EXCEPTION, NOT_CASH, read_post, readable_text

A, B = SimpleNamespace(code="AA"), SimpleNamespace(code="BB")
TEAMS = {"1": A, "2": B}
NAMES = {"1": "alice", "2": "bob", "3": "carol"}


def read(text, attachments=False, season=2027):
    return read_post(text, attachments, season, TEAMS, NAMES)


@pytest.mark.parametrize(
    "text",
    [
        "<@1> sends $10 to <@2> in Stott trade",
        "<@!1>  sends $ 10 to <@!2> in Stott trade",
        "<@1> Sends $10 (2027 auction budget) TO <@2> in Stott trade",
        "<@1> sends $10 2027 cash and 2027 1st Rd FYPD pick to <@2> in Stott trade",
    ],
)
def test_cash_shapes(text):
    r = read(text)
    assert r.status == CASH and r.detail == "AA sends $10 to BB for 2027"
    (t,) = r.trades
    assert (t.from_team, t.to_team, t.amount) == (A, B, 10)
    assert t.note.endswith("in Stott trade")


@pytest.mark.parametrize("text", ["<@1> sends 2027 second to <@2> in a trade", "https://klipy.com/gifs/shimmy", ""])
def test_no_amount_is_not_cash(text):
    assert read(text).status == NOT_CASH


@pytest.mark.parametrize(
    "text, attachments, why",
    [
        ("", True, "picture"),
        ("<@1> sends $25 (2025 cash), $10 (2026) and $5 (2027) to <@2>", False, "More than one amount"),
        ("<@1> sends $5 2027 (not 2026) cash to <@2>", False, "Mentions 2026"),
        ("Kevin sends Devin $3 2027 in Imanaga acquisition", False, "@mentions"),
        ("<@3> sends $3 to <@2> in a trade", False, "@carol isn't linked"),
        ("<@1> sends $3 to <@1>", False, "same team"),
    ],
)
def test_exceptions(text, attachments, why):
    r = read(text, attachments)
    assert r.status == EXCEPTION and why in r.detail and not r.trades


def test_readable_text_names_mentions():
    assert readable_text("<@1> sends $3 to <@!9>", NAMES) == "@alice sends $3 to @9"
```

- [ ] **Step 2:** Run them. Expected: ImportError.
- [ ] **Step 3: Implement.** Append to `league/discord_cash.py`:

```python
CASH, NOT_CASH, EXCEPTION = "CASH", "NOT_CASH", "EXCEPTION"  # DiscordMessage.Status values
MENTION = re.compile(r"<@!?(\d+)>")
AMOUNT = re.compile(r"\$\s?(\d+)")
YEAR = re.compile(r"\b(20\d\d)\b")
SENDS = re.compile(r"<@!?(\d+)>\s*sends\b.*?\bto\b\s*<@!?(\d+)>(.*)", re.IGNORECASE | re.DOTALL)
NOTE_LENGTH = 200  # CashTrade.note


@dataclass(frozen=True)
class Wanted:
    """One cash trade a post asks for."""

    from_team: object
    to_team: object
    amount: int
    note: str


@dataclass
class Reading:
    status: str
    detail: str
    trades: list[Wanted] = field(default_factory=list)


def readable_text(content: str, names: dict[str, str]) -> str:
    return MENTION.sub(lambda m: "@" + names.get(m[1], m[1]), content)


def read_post(content: str, has_attachments: bool, season: int, teams: dict, names: dict[str, str]) -> Reading:
    """What a post in the `season` trade channel says. `teams`: Discord user ID -> Team (linked managers)."""
    text = content.strip()
    amounts = AMOUNT.findall(text)
    if not amounts:
        if not text and has_attachments:
            return Reading(EXCEPTION, "A picture with no text: if it moves cash, enter the trade here")
        return Reading(NOT_CASH, "No cash amount")
    if len(amounts) > 1:
        return Reading(EXCEPTION, f"More than one amount: enter the {season} cash here")
    if other := sorted({int(y) for y in YEAR.findall(text)} - {season}):
        years = ", ".join(map(str, other))
        return Reading(EXCEPTION, f"Mentions {years} in the {season} channel: check which auction the cash is for")
    m = SENDS.search(text)
    if not m:
        return Reading(
            EXCEPTION, "Couldn't tell who sends and who receives: posts need @mentions (“@A sends $5 to @B”)"
        )
    for uid in (m[1], m[2]):
        if uid not in teams:
            name = names.get(uid, uid)
            return Reading(
                EXCEPTION, f"@{name} isn't linked to a manager: set their Discord ID in the admin, then sync again"
            )
    sender, receiver, amount = teams[m[1]], teams[m[2]], int(amounts[0])
    if sender == receiver:
        return Reading(EXCEPTION, "Sender and receiver are the same team")
    note = readable_text(m[3], names).strip(" .,-–—")[:NOTE_LENGTH]
    return Reading(
        CASH, f"{sender.code} sends ${amount} to {receiver.code} for {season}", [Wanted(sender, receiver, amount, note)]
    )
```

Add `from dataclasses import dataclass, field` to the imports.
- [ ] **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit "Read cash trades from trade-channel posts", then push.

### Task 5: The sync, resolving, and the command

**Files:**
- Create: `league/discord_sync.py`, `league/management/commands/sync_discord.py`
- Modify: `league/signing.py` (`open_period`)
- Test: `league/tests/test_discord_sync.py`

**Interfaces:**
- Consumes: `fetch_category`, `Channel` and `DiscordReadError` (Task 2); `DiscordMessage` (Task 3); `read_post`, `readable_text`, `channel_season` and the statuses (Task 4).
- Produces:
  - `DiscordSyncResult` (`read`, `added`, `changed`, `removed`, `skipped: list[str]`, `open: list[str]`), with `.summary() -> str`.
  - `sync(channels, user=None, dry_run=False) -> DiscordSyncResult`.
  - `fetch_and_sync(user, dry_run=False)`.
  - `unresolved_count() -> int`.
  - `resolve(message_id: int, user, note: str, from_code="", to_code="", amount="") -> DiscordMessage`. The arguments are form strings; `message_id` is the pk.

- [ ] **Step 1: Failing tests** in `league/tests/test_discord_sync.py`:

```python
import re

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from league import discord_sync, signing
from league.discord_client import Channel
from league.models import AuditEntry, CashTrade, DiscordMessage, Manager, SeasonBudget, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def teams():
    a = Team.objects.create(code="AA", name="A")
    b = Team.objects.create(code="BB", name="B")
    Manager.objects.create(team=a, name="Al", discord_id="1")
    Manager.objects.create(team=b, name="Bo", discord_id="2")
    return a, b


def post(mid, content, edited=None, attachments=()):
    ids = re.findall(r"<@!?(\d+)>", content)
    return {
        "id": mid, "content": content, "timestamp": "2026-07-01T12:00:00+00:00", "edited_timestamp": edited,
        "author": {"id": "1", "username": "alice"}, "attachments": list(attachments),
        "mentions": [{"id": i, "username": f"user{i}"} for i in ids],
    }  # fmt: skip


def trades(*posts, name="trades-2027-assets", cid="27"):
    return Channel(cid, name, list(posts))


def cash():
    return sorted((c.budget_season, c.from_team.code, c.to_team.code, c.amount) for c in CashTrade.objects.all())


def test_first_sync_creates_cash_and_a_second_changes_nothing(teams):
    channels = [trades(post("m1", "<@1> sends $10 to <@2> in Stott trade"), post("m2", "a GIF"))]
    first = discord_sync.sync(channels)
    assert cash() == [(2027, "AA", "BB", 10)]
    assert (first.read, first.added) == (2, 1)
    assert CashTrade.objects.get().note == "in Stott trade"
    assert CashTrade.objects.get().discord_message.message_id == "m1"
    second = discord_sync.sync(channels)
    assert (second.added, second.changed, second.removed) == (0, 0, 0)
    assert cash() == [(2027, "AA", "BB", 10)]
    assert AuditEntry.objects.filter(action="Synced Discord").count() == 2


def test_other_channels_and_past_auctions_are_stored_but_never_cash(teams):
    discord_sync.sync(
        [
            trades(post("o1", "<@1> sends $5 to <@2>"), name="trades-2026-assets", cid="26"),
            Channel("9", "league-rules", [post("r1", "Rule 1: $400 budget")]),
        ]
    )
    assert cash() == []
    assert set(DiscordMessage.objects.values_list("status", flat=True)) == {"IGNORED"}


def test_an_edit_updates_the_cash_trade(teams):
    discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))])
    result = discord_sync.sync([trades(post("m1", "<@2> sends $4 to <@1>", edited="2026-07-02T00:00:00+00:00"))])
    assert cash() == [(2027, "BB", "AA", 4)] and result.changed == 1
    assert AuditEntry.objects.filter(action="Discord: removed cash trade").count() == 2  # one per team


def test_a_deletion_removes_the_cash_trade(teams):
    discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))])
    result = discord_sync.sync([trades()])
    assert cash() == [] and result.removed == 1
    assert DiscordMessage.objects.get().deleted_at is not None


def test_a_change_after_the_budget_froze_is_an_exception(teams):
    a, b = teams
    discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))])
    for t in teams:
        SeasonBudget.objects.create(season=2027, team=t, base=400, contracts=0, buyouts=0, farm=0, missed_ip=0,
                                    cash_net=0, remaining=400, frozen_at="2027-03-01T00:00Z")  # fmt: skip
    discord_sync.sync([trades(post("m1", "<@1> sends $3 to <@2>"))])
    assert cash() == [(2027, "AA", "BB", 10)]
    (m,) = DiscordMessage.objects.unresolved()
    assert "frozen" in m.detail
    discord_sync.sync([trades()])  # deleting it doesn't change frozen money either
    assert cash() == [(2027, "AA", "BB", 10)]


def test_linking_a_manager_clears_a_not_linked_exception(teams):
    channels = [trades(post("m1", "<@3> sends $2 to <@2>"))]
    discord_sync.sync(channels)
    assert "@user3 isn't linked" in DiscordMessage.objects.unresolved().get().detail
    Manager.objects.create(team=Team.objects.create(code="CC", name="C"), name="Cy", discord_id="3")
    discord_sync.sync(channels)
    assert not DiscordMessage.objects.unresolved().exists()
    assert cash() == [(2027, "CC", "BB", 2)]


def test_resolving_enters_the_cash_and_an_edit_reopens_it(teams):
    user = User.objects.create(username="c")
    discord_sync.sync([trades(post("m1", "Al sends Bo $3"))])
    m = DiscordMessage.objects.get()
    with pytest.raises(ValueError, match="note"):
        discord_sync.resolve(m.pk, user, "  ", "AA", "BB", "3")
    with pytest.raises(ValueError, match="both teams"):
        discord_sync.resolve(m.pk, user, "names", "AA", "", "3")
    discord_sync.resolve(m.pk, user, "names, not mentions", "AA", "BB", "3")
    assert cash() == [(2027, "AA", "BB", 3)] and discord_sync.unresolved_count() == 0
    with pytest.raises(ValueError, match="open exception"):
        discord_sync.resolve(m.pk, user, "again")
    discord_sync.sync([trades(post("m1", "Al sends Bo $3"))])
    assert discord_sync.unresolved_count() == 0 and cash() == [(2027, "AA", "BB", 3)]
    discord_sync.sync([trades(post("m1", "Al sends Bo $4"))])
    assert discord_sync.unresolved_count() == 1 and cash() == [(2027, "AA", "BB", 3)]


def test_an_edit_reopens_a_resolved_exception(teams):
    test_resolving_enters_the_cash_and_an_edit_reopens_it(teams)
    with pytest.raises(signing.SigningError, match="Discord exception"):
        signing.open_period(2026, None)


def test_resolving_blank_means_no_cash(teams):
    discord_sync.sync([trades(post("m1", "", attachments=[{"id": "x"}]))])
    m = DiscordMessage.objects.get()
    discord_sync.resolve(m.pk, None, "a meme")
    assert cash() == [] and discord_sync.unresolved_count() == 0


def test_unreadable_channel_keeps_its_messages(teams):
    discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))])
    result = discord_sync.sync([Channel("27", "trades-2027-assets", [], readable=False)])
    assert result.skipped == ["trades-2027-assets"]
    assert cash() == [(2027, "AA", "BB", 10)] and DiscordMessage.objects.get().deleted_at is None


def test_channel_moved_out_of_the_category_keeps_its_cash(teams):
    discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))])
    discord_sync.sync([Channel("9", "league-rules", [])])
    assert cash() == [(2027, "AA", "BB", 10)] and DiscordMessage.objects.get(message_id="m1").deleted_at is None


def test_dry_run_changes_nothing(teams):
    result = discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))], dry_run=True)
    assert result.added == 1
    assert not CashTrade.objects.exists() and not DiscordMessage.objects.exists()


def test_command_reports_and_lists_exceptions(teams, monkeypatch, capsys):
    monkeypatch.setattr(discord_sync, "fetch_category", lambda: [trades(post("m1", "Al sends Bo $3"))])
    call_command("sync_discord", dry_run=True)
    out = capsys.readouterr().out
    assert "Dry run" in out and "Couldn't tell who sends" in out
```

- [ ] **Step 2:** Run. Expected: ImportError.
- [ ] **Step 3: Implement** `league/discord_sync.py`:

```python
"""Sync from Discord: store every message in Rules and Accounting, and keep each trade post's cash trades
equal to what it says now. Fetch first (league.discord_client), then apply in one transaction.

Cash for the auctions from 2027 on comes only from #trades-<year>-assets. An edited post changes its cash
trade and a deleted one removes it, audited, until that season's auction budgets are frozen; after that
a change is an exception and nothing moves.
"""

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from league.discord_cash import CASH, EXCEPTION, NOT_CASH, channel_season, read_post, readable_text
from league.discord_client import Channel, fetch_category
from league.models import CashTrade, DiscordMessage, Manager, SeasonBudget, Team, audit

Status = DiscordMessage.Status


@dataclass
class DiscordSyncResult:
    read: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    skipped: list[str] = field(default_factory=list)  # channels the bot can't see
    open: list[str] = field(default_factory=list)  # unresolved exceptions after the run

    def summary(self) -> str:
        text = (
            f"{self.read} Discord messages read; cash trades: {self.added} added, {self.changed} changed, "
            f"{self.removed} removed"
        )
        if self.skipped:
            text += f"; can't read #{', #'.join(self.skipped)}"
        return text + (f"; {len(self.open)} exception(s) to resolve" if self.open else "")


def unresolved_count() -> int:
    return DiscordMessage.objects.unresolved().count()


def fetch_and_sync(user, dry_run: bool = False) -> DiscordSyncResult:
    return sync(fetch_category(), user, dry_run)


def sync(channels: list[Channel], user=None, dry_run: bool = False) -> DiscordSyncResult:
    result, now = DiscordSyncResult(), timezone.now()
    with transaction.atomic():
        # Every cash trade involves teams: holding their rows makes a second sync (a double-click) wait here.
        list(Team.objects.select_for_update())
        ctx = _Context(user, result)
        read = [c for c in channels if c.readable]
        result.skipped = [c.name for c in channels if not c.readable]
        stored = {m.message_id: m for m in DiscordMessage.objects.filter(channel_id__in=[c.id for c in read])}
        for channel in read:
            season = channel_season(channel.name)
            for raw in channel.messages:
                result.read += 1
                msg, names = _store(stored.pop(raw["id"], None), channel, raw, now)
                if season is None:
                    msg.status, msg.detail = Status.IGNORED, ""
                else:
                    ctx.follow(msg, season, names)
                msg.save()
        for msg in stored.values():  # gone from a channel read this run: deleted in Discord
            if msg.deleted_at is None:
                msg.deleted_at = now
                if (season := msg.season) is not None:
                    ctx.follow_deletion(msg, season)
                msg.save()
        result.open = [
            f"#{m.channel_name} {m.author_name}: {m.readable[:80]!r}: {m.detail}"
            for m in DiscordMessage.objects.unresolved()
        ]
        if dry_run:
            transaction.set_rollback(True)
        else:
            audit(user, "Synced Discord", result.summary())
    return result


def _store(msg, channel, raw, now):
    """Insert or update the message from Discord's JSON. Returns it (saved) and its mentions' usernames."""
    msg = msg or DiscordMessage(message_id=raw["id"])
    author = raw.get("author") or {}
    names = {u["id"]: u.get("username") or u["id"] for u in raw.get("mentions") or []}
    msg.channel_id, msg.channel_name = channel.id, channel.name
    msg.author_id, msg.author_name = author.get("id", ""), author.get("username", "")
    msg.posted_at = parse_datetime(raw["timestamp"])
    msg.edited_at = parse_datetime(raw["edited_timestamp"]) if raw.get("edited_timestamp") else None
    msg.content = raw.get("content") or ""
    msg.readable = readable_text(msg.content, names)
    msg.has_attachments = bool(raw.get("attachments"))
    msg.deleted_at, msg.synced_at = None, now
    msg.status = msg.status or Status.NOT_CASH
    msg.save()
    return msg, names


class _Context:
    def __init__(self, user, result):
        self.user, self.result = user, result
        self.teams = {m.discord_id: m.team for m in Manager.objects.exclude(discord_id=None).select_related("team")}
        self.frozen = set(SeasonBudget.objects.values_list("season", flat=True))

    def follow(self, msg, season, names):
        if msg.resolved_at:
            if msg.content == msg.resolved_content:
                return  # resolved and unchanged: what the commissioner decided stands
            msg.resolved_at, msg.resolved_by, msg.resolved_note = None, None, ""
            msg.status, msg.detail = Status.EXCEPTION, "Edited after it was resolved: check it again"
            return
        reading = read_post(msg.content, msg.has_attachments, season, self.teams, names)
        msg.status, msg.detail = reading.status, reading.detail
        if reading.status != EXCEPTION:
            self.make(msg, season, reading.trades, reading.detail)

    def follow_deletion(self, msg, season):
        if self.make(msg, season, [], "Deleted in Discord"):
            msg.status, msg.detail = Status.NOT_CASH, "Deleted in Discord"

    def make(self, msg, season, wanted, now_says) -> bool:
        """Make the message's cash trades equal `wanted`. False (and an exception) if the season is frozen."""
        current = list(msg.cash_trades.select_related("from_team", "to_team"))
        have = sorted((c.from_team_id, c.to_team_id, c.amount) for c in current)
        if have == sorted((w.from_team.pk, w.to_team.pk, w.amount) for w in wanted):
            return True
        if season in self.frozen:
            msg.status = Status.EXCEPTION
            msg.detail = f"Changed after the {season} auction budgets were frozen, so nothing changed. Now: {now_says}"
            return False
        replace(msg, season, wanted, self.user, self.result)
        return True


def replace(msg, season, wanted, user, result=None):
    """Delete the message's cash trades and create `wanted` ones, auditing each for both teams."""
    current = list(msg.cash_trades.select_related("from_team", "to_team"))
    where = f"#{msg.channel_name}"
    for c in current:
        c.delete()
        _audit(user, "Discord: removed cash trade", c, where)
    for w in wanted:
        c = CashTrade.objects.create(
            budget_season=season, from_team=w.from_team, to_team=w.to_team, amount=w.amount, note=w.note,
            discord_message=msg,
        )  # fmt: skip
        _audit(user, "Discord: added cash trade", c, where)
    if result is not None:
        if current and wanted:
            result.changed += 1
        else:
            result.added += len(wanted)
            result.removed += len(current)


def _audit(user, action, c, where):
    for team in (c.from_team, c.to_team):
        audit(
            user,
            action,
            f"{c.from_team.code} → {c.to_team.code} ${c.amount} for {c.budget_season}, from {where}",
            team=team,
        )


@transaction.atomic
def resolve(message_id: int, user, note: str, from_code: str = "", to_code: str = "", amount: str = ""):
    """Close an exception. With teams and an amount, that's the post's cash trade; all blank means no cash."""
    note = note.strip()
    if not note:
        raise ValueError("Say what you did in the note")
    msg = DiscordMessage.objects.select_for_update().get(pk=message_id)
    if msg.status != Status.EXCEPTION or msg.resolved_at:
        raise ValueError("This isn't an open exception")
    season = msg.season
    frozen = SeasonBudget.objects.filter(season=season).exists()
    entered = [x.strip() for x in (from_code, to_code, amount)]
    if any(entered):
        if not all(entered):
            raise ValueError("Pick both teams and an amount, or leave all three blank")
        sender, receiver = (Team.objects.get(code=code.upper()) for code in entered[:2])
        if sender == receiver:
            raise ValueError("Sender and receiver are the same team")
        if not entered[2].isdigit() or int(entered[2]) <= 0:
            raise ValueError("The amount is a whole number of dollars")
        if frozen:
            raise ValueError(f"The {season} auction budgets are frozen: cash for it can't change now")
        from league.discord_cash import Wanted

        replace(msg, season, [Wanted(sender, receiver, int(entered[2]), "Entered from the console")], user)
    elif not frozen:
        replace(msg, season, [], user)
    msg.resolved_at, msg.resolved_by = timezone.now(), (user if user and user.is_authenticated else None)
    msg.resolved_note, msg.resolved_content = note, msg.content
    msg.save()
    audit(user, "Resolved Discord exception", f"#{msg.channel_name} {msg.author_name}: {msg.readable[:150]}", note=note)
    return msg
```

(Move the `Wanted` import to the top of the file with the other `discord_cash` imports; it's inline here only for clarity.)

`league/management/commands/sync_discord.py`:

```python
"""Read the league Discord's Rules and Accounting channels. Cash for 2027 on comes from #trades-<year>-assets.

Needs DISCORD_BOT_TOKEN, DISCORD_GUILD_ID and DISCORD_CATEGORY_ID (environment or secrets/discord.env).
"""

from django.core.management.base import BaseCommand, CommandError

from league import discord_sync
from league.discord_client import DiscordReadError


class Command(BaseCommand):
    help = "Sync from Discord: store the category's messages and follow the trade channels' cash"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would change; save nothing")

    def handle(self, *args, dry_run=False, **options):
        try:
            result = discord_sync.fetch_and_sync(None, dry_run=dry_run)
        except DiscordReadError as e:
            raise CommandError(str(e)) from e
        self.stdout.write(("Dry run, nothing saved. " if dry_run else "") + result.summary())
        for line in result.open:
            self.stdout.write(f"  {line}")
```

`league/signing.py` `open_period`: after the Fantrax check, add the import `from league import discord_sync` and:

```python
    if n := discord_sync.unresolved_count():
        raise SigningError(f"{n} Discord exception(s) are unresolved; resolve them on the console first")
```

- [ ] **Step 4:** `uv run pytest league -q`. Expected: pass.
- [ ] **Step 5:** Commit "Sync cash trades from Discord, with exceptions that block signing", then push.

### Task 6: Console panel, cash page link, admin rules

**Files:**
- Modify: `league/signing_views.py` (console actions and context), `league/templates/league/console.html`, `league/views.py` (`cash`), `league/templates/league/cash.html`, `league/admin.py`
- Test: `league/tests/test_discord_console.py`

**Interfaces:**
- Consumes: `discord_sync.fetch_and_sync`, `resolve` and `unresolved_count`; `discord_client.configured`; `DiscordReadError`.
- Produces: console POST actions `discord_sync` and `discord_resolve` (fields `message`, `note`, `from_team`, `to_team`, `amount`), and `CashTradeAdmin` with `CashTradeForm`.

- [ ] **Step 1: Failing tests**:

```python
import pytest
from django.contrib.auth.models import User
from django.test import Client

from league import discord_sync
from league.admin import CashTradeForm
from league.discord_client import Channel, DiscordReadError
from league.models import CashTrade, DiscordMessage, Manager, Team
from league.tests.test_discord_sync import post

pytestmark = pytest.mark.django_db


@pytest.fixture
def commish():
    a = Team.objects.create(code="AA", name="A")
    Team.objects.create(code="BB", name="B")
    user = User.objects.create_user("discord-1")
    Manager.objects.create(team=a, name="Al", discord_id="1", user=user, is_commissioner=True)
    client = Client()
    client.force_login(user)
    client.get("/teams/")  # the middleware grants admin rights on the first page
    return client


def test_console_discord_not_set_up(commish):
    page = commish.get("/commish/").content.decode()
    assert "Sync from Discord" in page and "Not set up" in page


def test_console_syncs_and_resolves(commish, monkeypatch, settings):
    settings.DISCORD_BOT_TOKEN, settings.DISCORD_GUILD_ID, settings.DISCORD_CATEGORY_ID = "t", "1", "10"
    monkeypatch.setattr(
        discord_sync, "fetch_category", lambda: [Channel("27", "trades-2027-assets", [post("m1", "Al sends Bo $3")])]
    )
    page = commish.post("/commish/", {"action": "discord_sync"}, follow=True).content.decode()
    assert "1 Discord messages read" in page and "Discord exceptions (1)" in page and "Al sends Bo $3" in page
    m = DiscordMessage.objects.get()
    commish.post("/commish/", {"action": "discord_resolve", "message": m.pk, "note": "names", "from_team": "AA",
                               "to_team": "BB", "amount": "3"})  # fmt: skip
    assert CashTrade.objects.get().amount == 3
    cash_page = commish.get("/cash/").content.decode()
    assert "https://discord.com/channels/1/27/m1" in cash_page


def test_console_reports_a_discord_error(commish, monkeypatch):
    def fail():
        raise DiscordReadError("Discord rejected the bot token: update DISCORD_BOT_TOKEN")

    monkeypatch.setattr(discord_sync, "fetch_category", fail)
    page = commish.post("/commish/", {"action": "discord_sync"}, follow=True).content.decode()
    assert "update DISCORD_BOT_TOKEN" in page


def test_admin_refuses_hand_entered_cash_from_2027():
    a, b = Team.objects.create(code="AA", name="A"), Team.objects.create(code="BB", name="B")
    form = CashTradeForm(data={"budget_season": "2027", "from_team": a.pk, "to_team": b.pk, "amount": "5"})
    assert not form.is_valid() and "trades-2027-assets" in str(form.errors)
    form = CashTradeForm(data={"budget_season": "2026", "from_team": a.pk, "to_team": b.pk, "amount": "5"})
    assert form.is_valid(), form.errors
```

The 2026 case relies on the year dropdown including 2026. `YEARS_AHEAD` starts at the current year, so it does in 2026. To keep the test valid in later years, build `CashTradeForm` with `budget_season` choices that include past years: see Step 3.
- [ ] **Step 2:** Run. Expected: ImportError (`CashTradeForm`).
- [ ] **Step 3: Implement.**

`league/admin.py`: remove `models.CashTrade` from the `admin.site.register([...])` list, then add:

```python
class CashTradeForm(YearChoicesForm):
    class Meta:
        model = models.CashTrade
        fields = ["budget_season", "from_team", "to_team", "amount", "note", "fantrax_tx_id"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only auctions before FIRST_SEASON are entered here; offer the few before it.
        years = list(range(FIRST_SEASON - 4, FIRST_SEASON + 1))
        self.fields["budget_season"].choices = [(y, y) for y in years]

    def clean_budget_season(self):
        season = self.cleaned_data["budget_season"]
        if season is not None and season >= FIRST_SEASON:
            raise forms.ValidationError(
                f"Cash for {FIRST_SEASON} and later comes from Discord: post it in #trades-{season}-assets, "
                "then Sync from Discord on the console."
            )
        return season


@admin.register(models.CashTrade)
class CashTradeAdmin(AuditedAdmin):
    """Cash for 2027 on comes from the Discord trade channels; earlier auctions' cash is entered here."""

    form = CashTradeForm
    list_display = ["budget_season", "from_team", "to_team", "amount", "note", "discord_message"]
    list_filter = ["budget_season"]

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and obj.discord_message_id)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.discord_message_id)


@admin.register(models.DiscordMessage)
class DiscordMessageAdmin(admin.ModelAdmin):
    """What Sync from Discord read. Read-only: fix cash by editing the post, or on the console."""

    list_display = ["posted_at", "channel_name", "author_name", "readable", "status", "detail", "resolved_at"]
    list_filter = ["status", "channel_name"]
    search_fields = ["readable", "author_name", "detail"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
```

Also add `from league.discord_cash import FIRST_SEASON`.

`league/signing_views.py`: import `discord_client`, `discord_sync` and `DiscordMessage`. Add these console actions before `except`:

```python
            elif action == "discord_sync":
                messages.success(request, discord_sync.fetch_and_sync(request.user).summary())
            elif action == "discord_resolve":
                discord_sync.resolve(
                    int(request.POST.get("message") or 0), request.user, request.POST.get("note", ""),
                    request.POST.get("from_team", ""), request.POST.get("to_team", ""), request.POST.get("amount", ""),
                )  # fmt: skip
                messages.success(request, "Discord exception resolved.")
```

Add `discord_client.DiscordReadError` and `DiscordMessage.DoesNotExist` to the caught exceptions, and `Team.DoesNotExist` too (a bad team code). Context additions:

```python
            "discord_configured": discord_client.configured(),
            "discord_exceptions": DiscordMessage.objects.unresolved().prefetch_related(
                "cash_trades__from_team", "cash_trades__to_team"
            ),
            "discord_last_sync": AuditEntry.objects.filter(action="Synced Discord").first(),
            "teams": Team.objects.all(),
```

`console.html`, after the Fantrax exceptions block (before `{% if not period ... %}`):

```html
<h2>Discord</h2>
<form method="post" onsubmit="const b = this.querySelector('button'); b.disabled = true; b.textContent = 'Syncing…';">{% csrf_token %}
  <input type="hidden" name="action" value="discord_sync"><button type="submit" class="primary"{% if not discord_configured %} disabled{% endif %}>Sync from Discord</button>
  <span class="muted">{% if discord_configured %}Reads Rules and Accounting. Cash for the 2027 auction on comes only from the #trades-&lt;year&gt;-assets channels.{% if discord_last_sync %} Last synced {{ discord_last_sync.at|date:"M j, g:i a" }}.{% endif %}{% else %}Not set up: it needs DISCORD_BOT_TOKEN, DISCORD_GUILD_ID and DISCORD_CATEGORY_ID (docs/discord-signin.md).{% endif %}</span></form>
{% if discord_exceptions %}
<h3>Discord exceptions ({{ discord_exceptions|length }})</h3>
<p class="muted">Enter the cash the post moves, or leave the teams and amount blank if it moves none. Signing can't open while any are listed.</p>
<div class="table-wrap"><table class="panel"><tbody>
{% for m in discord_exceptions %}<tr><td>{{ m.posted_at|date:"M j, Y" }}<br><span class="muted">#{{ m.channel_name }}</span></td>
  <td class="wrap"><a href="{{ m.url }}">{{ m.author_name }}</a>: “{{ m.readable|default:"(no text)" }}”<br><span class="error-text">{{ m.detail }}</span>
    {% for c in m.cash_trades.all %}<br><span class="muted">Now: {{ c.from_team.code }} → {{ c.to_team.code }} ${{ c.amount }}</span>{% endfor %}</td>
  <td><form method="post">{% csrf_token %}<input type="hidden" name="message" value="{{ m.pk }}">
    <select name="from_team" aria-label="From"><option value="">From</option>{% for t in teams %}<option>{{ t.code }}</option>{% endfor %}</select>
    <select name="to_team" aria-label="To"><option value="">To</option>{% for t in teams %}<option>{{ t.code }}</option>{% endfor %}</select>
    <input name="amount" inputmode="numeric" size="4" placeholder="$" aria-label="Amount">
    <input name="note" required placeholder="What you did" aria-label="Note"> <button type="submit" name="action" value="discord_resolve">Resolve</button></form></td></tr>
{% endfor %}
</tbody></table></div>
{% endif %}
```

In the "Before you open signing" checklist, after the Fantrax line:

```html
  <li class="{% if discord_exceptions %}todo{% else %}ok{% endif %}">{% if discord_exceptions %}{{ discord_exceptions|length }} Discord exception{{ discord_exceptions|pluralize }} to resolve (above).{% else %}Discord cash posts are read and nothing needs a look.{% endif %}</li>
```

`league/views.py` `cash`: add `"discord_message"` to the `select_related`. In `cash.html`, the note cell becomes:

```html
<td class="muted">{{ t.note }}{% if t.discord_message %} <a href="{{ t.discord_message.url }}">post</a>{% endif %}</td>
```

- [ ] **Step 4:** `uv run pytest league -q`. Expected: pass.
- [ ] **Step 5:** Commit "Add the Discord panel to the console and keep 2027+ cash out of the admin", then push.

### Task 7: e2e, then docs and the PR

**Files:**
- Modify: `e2e/pages.py` (replace `add_cash_trade` with `give_cash`), `e2e/test_signing_decisions.py`, `e2e/test_admin_entries.py`
- Modify: `league/templates/league/help.html`, `league/templates/league/runbook.html`, `docs/discord-signin.md`, `ARCHITECTURE.md`

**Interfaces:** Consumes the console form from Task 6 (selects labelled "From" and "To", inputs "Amount" and "Note", the "Resolve" button).

- [ ] **Step 1: e2e changes.**

In `e2e/pages.py`, replace `add_cash_trade` with:

```python
def give_cash(sender, receiver, amount):
    """Cash for the 2027 auction, as a synced Discord post would make it (the admin refuses 2027 cash)."""
    CashTrade.objects.create(
        budget_season=2027, from_team=Team.objects.get(code=sender), to_team=Team.objects.get(code=receiver),
        amount=amount,
    )  # fmt: skip
```

(import `CashTrade`). In `test_signing_decisions.py`, use `give_cash("MB", "JJ", before - player.entry.salary)` and `give_cash("MB", "JJ", 1)`.

In `test_admin_entries.py`, replace `test_cash_trade_moves_both_budgets` with:

```python
def test_resolving_a_discord_post_moves_both_budgets(commish_page):
    page = commish_page
    mb, jj = left_before_signings(page, "MB"), left_before_signings(page, "JJ")
    discord_sync.sync([Channel("27", "trades-2027-assets", [post("m1", "Becker sends JJ $7 in the e2e trade")])])
    page.goto("/commish/")
    row = page.locator("tr").filter(has_text="Becker sends JJ $7")
    row.get_by_label("From").select_option("MB")
    row.get_by_label("To").select_option("JJ")
    row.get_by_label("Amount").fill("7")
    row.get_by_label("Note").fill("names, not mentions")
    row.get_by_role("button", name="Resolve").click()
    expect(page.locator(".messages")).to_contain_text("Discord exception resolved.")
    assert left_before_signings(page, "MB") == mb - 7
    assert left_before_signings(page, "JJ") == jj + 7
    assert AuditEntry.objects.filter(action="Discord: added cash trade", team__code="JJ").exists()
```

(imports: `from league import discord_sync`, `from league.discord_client import Channel`, `from league.tests.test_discord_sync import post`; drop `add_cash_trade`).
- [ ] **Step 2:** `uv run pytest e2e -q`. Expected: pass.
- [ ] **Step 3: Docs.**
  - `help.html`: in the Fantrax exceptions bullet, drop "a trade comment that mentions cash". Replace the "Cash trades" bullet under "Enter things by hand" with: earlier auctions only; from 2027 they come from Discord. Add an `<h3>Cash trades (Discord)</h3>` section after "Fantrax moves (sync)" covering:
    - the `@A sends $N to @B in … trade` format in `#trades-<year>-assets`, where the channel's year is the auction;
    - that **Sync from Discord** reads the channels and follows edits and deletions, until the season's budgets are frozen;
    - linking each manager's Discord ID clears "isn't linked";
    - resolving: enter the teams and amount, or leave them blank for no cash;
    - that signing can't open while any exception is listed.
  - `runbook.html`: line 29 becomes "Discord: cash trades are posted in #trades-<year>-assets; press Sync from Discord on the console", and line 64 drops "cash trades". Also add "Sync from Discord and resolve its exceptions" wherever the runbook says to Sync from Fantrax before opening signing.
  - `docs/discord-signin.md`: add a "## 5. Commissioner Bot (reading the trade channels)" section covering:
    - the separate app;
    - Public Bot off and the Message Content intent on;
    - the install link pattern with the **bot app's own** client ID and permissions `66560`;
    - the server and category IDs from Developer Mode;
    - the three settings (locally in `secrets/discord.env`);
    - redeploying the Worker for the two new routes;
    - kicking a sign-in app's bot if it was installed by mistake.

    Add troubleshooting rows: 401 → token, 403 with code 50001 → bot not in server / wrong IDs, "No text channels" → category ID.
  - `ARCHITECTURE.md`: add `discord_client.py`, `discord_cash.py` and `discord_sync.py` to the `league` module table, plus `/commish/` Discord actions. Line 191 drops "cash comment".
- [ ] **Step 4: Full verification.** `uv run ruff check . && uv run ruff format --check . && uv run python manage.py makemigrations --check --dry-run && uv run pytest -q && node cloudflare/discord-proxy/worker.test.mjs`. Expected: all green.
- [ ] **Step 5:** Commit "Document cash trades from Discord", push, and open the PR: "Fixes #47", "Part of #35", "Related: #33".

The by-hand `sync_discord --dry-run` against the real server runs from the main checkout, where `secrets/discord.env` lives (`uv run python manage.py sync_discord --dry-run` with this branch checked out). The commissioner runs it, or approves it.
