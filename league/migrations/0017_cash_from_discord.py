"""Cash for 2027 on comes only from Discord: drop the trades entered from Fantrax trade comments."""

from django.db import migrations


def drop_fantrax_comment_cash(apps, schema_editor):
    CashTrade = apps.get_model("league", "CashTrade")
    AuditEntry = apps.get_model("league", "AuditEntry")
    future = CashTrade.objects.filter(budget_season__gte=2027).exclude(fantrax_tx_id="")
    for c in future.select_related("from_team", "to_team"):
        detail = f"${c.amount} {c.from_team.code} -> {c.to_team.code} ({c.budget_season}), from a Fantrax comment"
        for team in (c.from_team, c.to_team):
            AuditEntry.objects.create(
                team=team, action="Removed cash trade", detail=detail, note="Discord is the source for 2027 on"
            )
        c.delete()


class Migration(migrations.Migration):
    dependencies = [("league", "0016_discord_message")]
    operations = [migrations.RunPython(drop_fantrax_comment_cash, migrations.RunPython.noop)]
