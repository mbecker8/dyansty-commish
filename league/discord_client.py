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
    if r.status_code == 404 and _code(r) is None:
        raise DiscordReadError(
            "Our Discord proxy doesn't forward this yet: redeploy the Worker (docs/discord-signin.md, section 5)"
        )
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
