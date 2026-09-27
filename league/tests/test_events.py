from datetime import datetime

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from league.fantrax_data import EASTERN
from league.models import AuditEntry, FantraxEvent, FantraxLeague

pytestmark = pytest.mark.django_db


def test_import_seeds_the_2026_league():
    call_command("import_league", verbosity=0)
    league = FantraxLeague.objects.get()
    assert (league.league_id, league.season, league.active) == ("p3z8zy75mgdm460o", 2026, True)
    assert league.process_since == datetime(2026, 2, 25, 16, 0, tzinfo=EASTERN)


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
    assert AuditEntry.objects.get(action="Resolved Fantrax exception").note == "entered it"
    with pytest.raises(ValueError):
        e.resolve(user, "again")
