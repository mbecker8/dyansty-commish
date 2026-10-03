# Discord cash trades: read future-season cash from the league's Discord

Status: design approved 2026-10-03 (branch discord-cash-trades).
Fixes #47. Part of #35. Related: #33 (scheduled syncing), #44 (Render setup).

## Problem

Cash moved in trades lives in the league's Discord, in the "Rules and Accounting" category: one channel
per budget season (`#trades-2027-assets`, `#trades-2028-assets`, ...). The app only knows the cash it
was seeded with: 37 trades for 2026 from the post-signing sheet, and three for 2027 and 2028 entered
from Fantrax trade comments. The channels hold about 30 posts for 2027 to 2030, several from 2024 and
2025 that were never in the sheet. The 2027 budgets are wrong until the app reads them.

## Decisions (from the design conversation)

1. **Discord is the only source of cash trades for budget seasons 2027 and later.** A channel's year is
   the auction its cash applies to (`#trades-2027-assets` is cash for the auction held in 2027), the
   same meaning as `CashTrade.budget_season`. Seasons up to 2026 were spent at past auctions and are
   never read.
2. **Fantrax trade comments are ignored.** They're sometimes out of date or wrong. The `CASH_COMMENT`
   rule goes away, and the three cash trades taken from comments are replaced by their Discord posts.
3. **Every message in the category is stored;** only the trade channels turn into cash trades. The
   rest are kept for later features (#35).
4. **The app follows Discord.** An edited post updates its cash trade and a deleted post removes it,
   both audited. Once a season's budget is frozen, a change becomes an exception instead.
5. **Unresolved Discord exceptions block opening signing**, like Fantrax exceptions.
6. **Read on demand over REST:** a management command and a console button. No scheduled run (#33)
   and no always-connected bot.
7. The bot can read whatever an ordinary member can read. There's no channel allowlist.

## Discord setup (done 2026-10-03)

- A separate application, **Commissioner Bot**, owns the bot. Sign-in keeps its own apps (Fantasy
  Manager locally, Fantasy Manager - Prod in production), and nothing about sign-in changes.
- Bot settings: Public Bot off; **Message Content Intent** on (without it Discord returns messages
  with empty `content`, over REST too).
- Installed in the league server with scope `bot` and permissions View Channels + Read Message
  History (`66560`), using Commissioner Bot's own client ID:
  `https://discord.com/oauth2/authorize?client_id=<bot app ID>&scope=bot&permissions=66560&guild_id=<server ID>`.
- `#rule-change-proposals` is private and the bot can't read it, which is intended.

## Settings

| Setting | Secret | Notes |
|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | The bot's token |
| `DISCORD_GUILD_ID` | no | The league server |
| `DISCORD_CATEGORY_ID` | no | "Rules and Accounting" |

Each is read from the environment, else from its line in the gitignored `secrets/discord.env` (where
all three already are; `bin/local-review.sh` also exports that file). All three are declared in
`render.yaml` (`sync: false`) and set on Render. With no token, the console
says the Discord sync isn't configured, and the button is disabled.

## Fetching

- `GET /guilds/<guild>/channels`: keep text channels whose `parent_id` is the category.
- `GET /channels/<id>/messages?limit=100&before=<id>`, paged until empty, for every such channel.
- Header `Authorization: Bot <token>`. The calls go through `DISCORD_API_BASE`, so production uses the
  Cloudflare Worker and local runs call discord.com directly, as sign-in does.
- Any error (401 bad token, 403 bot removed or missing access, 429, a network failure) stops the sync
  before anything is stored, with a plain message ("Discord bot token rejected: update
  DISCORD_BOT_TOKEN"). On a 429 that carries `retry_after`, wait and retry a few times first.
- Threads aren't read. The trade channels have none today.

### Worker

`cloudflare/discord-proxy/worker.js` forwards two more routes, both `GET`:

- `/guilds/<digits>/channels`
- `/channels/<digits>/messages`, passing through only the `limit`, `before` and `after` query
  parameters

The key check is unchanged, and `Authorization` is already forwarded. Every other method or path is
still 404, so the Worker can't be used to write to Discord. `worker.test.mjs` covers the new routes.
`docs/discord-signin.md` gets a "Commissioner Bot" section with the setup above and the Worker
redeploy.

## Data model

**`DiscordMessage`** (new): one message in the category.

| field | notes |
|---|---|
| `message_id` | unique; the store-once key |
| `channel_id`, `channel_name` | |
| `author_id`, `author_name` | |
| `posted_at`, `edited_at` | |
| `content`, `has_attachments` | |
| `deleted_at` | set when a sync no longer finds the message |
| `status` | `CASH`, `NOT_CASH`, `IGNORED`, `EXCEPTION` |
| `detail` | a readable line: "MH sends $10 to AW for 2028", or why it couldn't be read |
| `synced_at` | last sync that saw it |
| `resolved_at`, `resolved_by`, `resolved_note` | exceptions only |
| `resolved_content` | the text when it was resolved; a later edit reopens the exception |

`IGNORED` covers channels other than `trades-<year>-assets` and trade channels for seasons up to 2026.
`DiscordMessage` is read-only in the admin.

**`CashTrade`** gets `discord_message`, a nullable foreign key to `DiscordMessage` (PROTECT, related
name `cash_trades`). One post can carry more than one cash trade. Rows without it are hand-entered
history for 2026 and earlier.

## Reading a post

These rules apply to posts in channels named `trades-<year>-assets` with year ≥ 2027.

- **The season is the channel's year.**
- **No `$` amount:** `NOT_CASH` (a pick-only post, a GIF link, an empty post). Stored, no exception.
- **Cash:** exactly one `$<digits>`, an @mention (`<@id>` or `<@!id>`) before "sends", and an @mention
  after "to". Each mention maps to a team through `Manager.discord_id`. The text after the amount
  becomes the note ("in Stott trade"). The result is `CASH` and one cash trade.
- **Exception**, with the post's text and the reason, when the post:
  - has more than one `$` amount;
  - contains a four-digit year other than the channel's (e.g. "$5 2027 (not 2026)");
  - mentions someone who isn't linked to a manager ("<username> isn't linked to a manager: set their
    Discord ID in the admin"), which clears itself on the next sync once linked;
  - has no @mention on one side (names: "Kevin sends Devin $3");
  - names the same team on both sides;
  - has no text but has an attachment (a picture).

Today's history gives about 25 readable posts, most of which start as "not linked" exceptions until
every manager's Discord ID is set. About 5 posts need entering by hand: two with names, one three-season
bundle, one "(not 2026)" correction, and one picture.

## Syncing

`league/discord_sync.py` holds the fetch and the rules; the command and the console both call it.

1. **Fetch** everything (above). A failure ends the run with nothing changed.
2. **Store** in one transaction: insert or update each `DiscordMessage` by ID, and set `deleted_at`
   on stored messages that weren't returned.
3. **Follow Discord**, for each message in a trade channel for 2027 or later:
   - What it should produce comes from the rules, or from its resolution (below). A deleted message
     produces nothing.
   - Compare that with its linked cash trades:
     - Same: nothing happens.
     - Different, and that season has no `SeasonBudget` yet: create, update or delete the cash
       trades, with one audit line each ("Discord: MH → AW $10 for 2028, from #trades-2028-assets").
     - Different, and the season's budget is frozen: the message becomes an exception ("changed after
       the 2028 budget was frozen"), and nothing changes.
4. **Audit** the run: who ran it, messages read, cash trades added, changed and removed, and open
   exceptions.

A second run with no changes in Discord changes nothing. Double submits are handled as in the Fantrax
sync: the button disables itself, the run is one transaction, and `message_id` is unique.

`manage.py sync_discord [--dry-run]` prints the same summary. `--dry-run` rolls back.

### Exceptions and resolving them

The console's **Discord** panel shows:
- the **Sync from Discord** button;
- the last sync time;
- each open exception: post text, author, channel, a link to the post
  (`https://discord.com/channels/<guild>/<channel>/<message>`), and a resolve form.

The form has **sender**, **receiver** and **amount** (optional) and a **note** (required):
- With the teams and amount filled in, it creates one cash trade for the channel's season, linked to
  the post. On later syncs, that resolution is what the post "should produce".
- Left blank, it means "no cash in this post".
- Resolving stores `resolved_content`. If a later sync finds different text, the exception reopens.
  The resolved cash trade stays until it's resolved again, and signing is blocked meanwhile.
- "Not linked" exceptions aren't resolved with the form. Link the manager and sync again.

### Signing

`open_period` also requires zero unresolved Discord exceptions, and its message names which kind is
blocking ("2 Discord exception(s) are unresolved; resolve them on the console first").

## Removed

- `apply_trade_comments` in `league/events.py` and its call. The `CASH_COMMENT` kind stays for old
  rows.
- `import_league` stops loading `data/league/cash_trades_from_fantrax.json`, and the file is deleted.
- A data migration deletes the cash trades with `budget_season` ≥ 2027 and a `fantrax_tx_id`, with an
  audit entry for each. These are the three from Fantrax comments; their Discord posts replace them.
- The admin refuses to add or edit a cash trade for 2027 or later ("cash for 2027 and later comes from
  Discord: post it in #trades-<year>-assets").

## Where it shows

- **Console:** the Discord panel (above).
- **Cash page:** each Discord-sourced trade links to its post.
- **Help page:** a "Cash trades come from Discord" section in the commissioner guide: the channel
  format, linking managers' Discord IDs, and resolving exceptions.
- **Audit log:** each sync, each cash-trade change, each resolution.

## Testing

- **Reading rules:** a unit test for each shape in the real history, using made-up IDs and names.
  The real messages stay in `secrets/discord_history/`, never in the repo.
- **Sync,** with a fake Discord responder:
  - the first run creates and the second changes nothing;
  - an edit updates and a deletion removes;
  - a frozen season turns a change into an exception;
  - linking a manager clears a "not linked" exception;
  - an edit reopens a resolved exception;
  - a Discord error leaves the database untouched;
  - `--dry-run` changes nothing.
- **Signing** can't open with an open Discord exception.
- **Admin** refuses a hand-entered cash trade for 2027 or later.
- **Migration** removes only the Fantrax-comment cash trades for 2027 and later.
- **Worker:** the new routes are forwarded, other paths are 404, a missing key is 403, and extra query
  parameters are dropped.
- **e2e:** resolve a seeded exception from the console and see the team's budget change. Nothing
  calls real Discord.
- Before the PR: the full `uv run pytest`, e2e included, and one by-hand `sync_discord --dry-run`
  against the league server.

## Out of scope

- Scheduled or automatic syncing (#33).
- Reading anything but cash from the other channels: dropped contracts, rules, farm picks (#35).
- Posting to Discord or notifications (#35).
- Threads and the private `#rule-change-proposals`.
