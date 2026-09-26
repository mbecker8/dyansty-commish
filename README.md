# Dynasty Commish

The league office for our dynasty baseball league. It replaces the contract-signing
spreadsheet: managers sign in with Discord and see every team's contracts, buyouts, farm
systems, farm picks, cash trades and auction budget. Contract signing moves here for
February 2027. Fantrax still runs trades, the auction, drafts, lineups and scoring.

- **Managers and commissioners:** the in-app **Help** page (`/help/`) explains the pages and
  the rules. Commissioners also see a commissioner guide there (linking managers,
  reviewing the season's moves, manual entries, command line).
- **The rules as implemented:** [RULES.md](RULES.md)
- **Why and what:** [VISION.md](VISION.md) · **How it's built:** [ARCHITECTURE.md](ARCHITECTURE.md)

## Run it locally

Needs [uv](https://docs.astral.sh/uv/) (Python 3.14 is installed automatically).

```sh
bin/local-review.sh
```

The script:
- sets up `review.sqlite3`,
- loads the league from the committed data (`data/`),
- proposes the 2026 season's changes,
- asks you to create an admin login,
- and serves http://127.0.0.1:8000/.

Re-running it keeps your decisions.

For Discord sign-in, create an app at https://discord.com/developers/applications. Add the
OAuth2 redirect `http://127.0.0.1:8000/auth/discord/callback`, then put the credentials in
`secrets/discord.env` (the whole `secrets/` folder is gitignored):

```sh
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
```

Then link yourself: sign in once to see your Discord ID, then in the admin add a
**Manager** with your team and that ID. Tick **Is commissioner** if you run the league.

## Develop

```sh
uv run pytest -q                 # tests (SQLite locally; CI uses Postgres)
uv run ruff check . && uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
```

Work happens on a branch per milestone, with a PR into `main`. CI must pass, and the PR
lists the issues it fixes. Never commit anything from `secrets/`: the Fantrax cookie,
Discord credentials, or the workbook with managers' contact details.

## Deploy

Render Blueprint (`render.yaml`): https://dynasty-commish.onrender.com. It runs on the free
tier for now. Before the league beta, upgrade to paid Postgres with backups, and set
`DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` in the dashboard.
