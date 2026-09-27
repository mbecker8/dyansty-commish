#!/usr/bin/env bash
# Run the commissioner's review copy on this laptop: http://127.0.0.1:8000/admin/
#
# Uses its own database (review.sqlite3) so dev and test runs never touch your
# decisions. Safe to re-run: it loads the league only into an empty database,
# and sync_fantrax applies each Fantrax move only once.
set -euo pipefail
cd "$(dirname "$0")/.."

export DATABASE_URL="sqlite:///$PWD/review.sqlite3"
export DJANGO_DEBUG=1
# Discord sign-in: DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET (gitignored).
if [ -f secrets/discord.env ]; then set -a; . secrets/discord.env; set +a; fi

uv sync --quiet
uv run python manage.py migrate --verbosity 0

if [ "$(uv run python manage.py shell --no-imports -c 'from league.models import Team; print(Team.objects.exists())')" = "False" ]; then
  uv run python manage.py import_league
fi
uv run python manage.py sync_fantrax --snapshot data/fantrax/2026-final --league p3z8zy75mgdm460o
# The signing pool. Rosters are fixed once signing opens, so load them only the first time.
if [ "$(uv run python manage.py shell --no-imports -c 'from league.models import RosterEntry; print(RosterEntry.objects.exists())')" = "False" ]; then
  uv run python manage.py sync_rosters
fi

if [ "$(uv run python manage.py shell --no-imports -c 'from django.contrib.auth.models import User; print(User.objects.filter(is_superuser=True).exists())')" = "False" ]; then
  echo "Create your admin login:"
  uv run python manage.py createsuperuser
fi

echo "League pages: http://127.0.0.1:8000/   Commissioner console: http://127.0.0.1:8000/commish/"
exec uv run python manage.py runserver 127.0.0.1:8000
