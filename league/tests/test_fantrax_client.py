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

    def post(self, url, **kwargs):
        if not self.logged_in:
            return FakeResponse({"pageError": {"code": "WARNING_NOT_LOGGED_IN"}})
        msg = kwargs["json"]["msgs"][0]
        method, data = msg["method"], msg["data"]
        if method == "getFantasyLeagueInfo":
            body = json.loads((SNAP / "league_info.json").read_text())
        elif method == "getTeamRosterInfo":
            team = data.get("teamId", self.teams[0]["id"])
            body = json.loads((SNAP / f"roster_{team}.json").read_text())
            body.setdefault("fantasyTeams", self.teams)
        elif data.get("view") == "TRADE":
            body = json.loads((SNAP / "trades.json").read_text())
        else:
            pages = json.loads((SNAP / "transactions.json").read_text())
            body = pages[int(data["pageNumber"]) - 1]
        return FakeResponse({"responses": [{"data": body}]})


def test_fetch_raw_matches_the_saved_snapshot():
    live = Snapshot.from_raw(fantrax_client.fetch_raw(FakeSession(), "p3z8zy75mgdm460o"))
    saved = Snapshot(SNAP)
    assert live.moves() == saved.moves()
    assert live.end_states() == saved.end_states()
    assert live.season == saved.season


def test_league_season_is_the_year_fantrax_gives_the_league():
    assert fantrax_client.league_season(FakeSession(), "p3z8zy75mgdm460o") == 2026


def test_not_logged_in_raises_login_expired():
    with pytest.raises(fantrax_client.FantraxLoginExpired):
        fantrax_client.fetch_raw(FakeSession(logged_in=False), "p3z8zy75mgdm460o")


def test_session_parses_a_cookie_header():
    s = fantrax_client.session("Cookie: a=1; b=two")
    assert s.cookies.get("a") == "1" and s.cookies.get("b") == "two"


def test_listing_leagues_needs_the_secret_id():
    with pytest.raises(fantrax_client.FantraxError, match="FANTRAX_SECRET_ID"):
        fantrax_client.list_leagues("")


def test_listing_leagues_reports_a_fantrax_error(monkeypatch):
    monkeypatch.setattr(
        fantrax_client.requests,
        "get",
        lambda *a, **k: FakeResponse({"error": {"code": "WARNING", "message": "bad id"}}),
    )
    with pytest.raises(fantrax_client.FantraxError, match="bad id"):
        fantrax_client.list_leagues("nope")
