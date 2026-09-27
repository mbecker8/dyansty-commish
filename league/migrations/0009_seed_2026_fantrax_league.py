"""Databases loaded before Fantrax events existed get the 2026 league that import_league now seeds."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import migrations


def seed(apps, schema_editor):
    Team, FantraxLeague = apps.get_model("league", "Team"), apps.get_model("league", "FantraxLeague")
    if Team.objects.exists() and not FantraxLeague.objects.exists():
        FantraxLeague.objects.create(
            league_id="p3z8zy75mgdm460o",
            name="Dynasty Yr 19",
            season=2026,
            process_since=datetime(2026, 2, 25, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        )


class Migration(migrations.Migration):
    dependencies = [("league", "0008_remove_reconciliation")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
