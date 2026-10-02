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


def league_season(s, league_id: str) -> int:
    """The year Fantrax gives a league. Renewal makes next year's league while this season's offseason runs."""
    try:
        info = _data(call(s, league_id, ("getFantasyLeagueInfo", {})))
        return int(info["fantasySettings"]["season"]["displayYear"])
    except (KeyError, TypeError, ValueError) as e:
        raise FantraxError(f"Fantrax didn't give a season for league {league_id}") from e


def fetch_raw(s, league_id: str) -> dict:
    """Everything a Snapshot needs, in memory."""
    info = _data(call(s, league_id, ("getFantasyLeagueInfo", {})))
    teams = _data(call(s, league_id, ("getTeamRosterInfo", {"view": "STATS"})))["fantasyTeams"]
    rosters = {
        t["id"]: _data(call(s, league_id, ("getTeamRosterInfo", {"teamId": t["id"], "view": "STATS"}))) for t in teams
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
    if not secret_id:
        raise FantraxError("FANTRAX_SECRET_ID isn't set (it's on your Fantrax user profile)")
    try:
        r = requests.get(LEAGUES_URL, params={"userSecretId": secret_id}, timeout=30)
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        raise FantraxError(f"Couldn't list Fantrax leagues: {e}") from e
    if "leagues" not in body:
        raise FantraxError(f"Fantrax didn't list your leagues: {body.get('error', body)}")
    return body["leagues"]
