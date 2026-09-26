"""Minimal authenticated Fantrax request helper for snapshot scripts.

Reads the browser Cookie header from secrets/fantrax_cookie.txt (gitignored).
"""

from pathlib import Path

import requests

COOKIE_FILE = Path(__file__).resolve().parent.parent / "secrets" / "fantrax_cookie.txt"
URL = "https://www.fantrax.com/fxpa/req"


def session() -> requests.Session:
    raw = COOKIE_FILE.read_text().strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    s = requests.Session()
    for part in raw.split(";"):
        if "=" in part:
            name, value = part.strip().split("=", 1)
            s.cookies.set(name, value, domain=".fantrax.com")
    return s


def call(s: requests.Session, league_id: str, *msgs: tuple[str, dict]) -> dict:
    """POST one or more methods; returns the full JSON response."""
    body = {"msgs": [{"method": m, "data": {"leagueId": league_id, **d}} for m, d in msgs]}
    r = s.post(URL, params={"leagueId": league_id}, json=body, timeout=30)
    r.raise_for_status()
    j = r.json()
    if "pageError" in j:
        raise RuntimeError(f"Fantrax error: {j['pageError'].get('code')}")
    return j
