"""The two Discord OAuth2 calls sign-in needs. Scope `identify` only: we learn the user's ID, nothing else."""

import requests
from django.conf import settings

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"  # the user's browser goes here, never through the proxy
TIMEOUT = 10  # seconds


def _api(path: str) -> str:
    """A Discord API URL. In production DISCORD_API_BASE points at our Cloudflare Worker, because
    Discord's Cloudflare blocks Render's shared outbound IPs (docs/discord-signin.md)."""
    return settings.DISCORD_API_BASE.rstrip("/") + path


def _proxy_headers() -> dict:
    return {"X-Proxy-Key": settings.DISCORD_PROXY_KEY} if settings.DISCORD_PROXY_KEY else {}


class DiscordError(Exception):
    pass


class DiscordBlocked(DiscordError):
    """Discord (or its Cloudflare) is rate-limiting this server. Not the user's fault; it passes."""


def configured() -> bool:
    return bool(settings.DISCORD_CLIENT_ID and settings.DISCORD_CLIENT_SECRET)


def fetch_identity(code: str, redirect_uri: str) -> dict:
    """Trade the authorization code for a token, then read who the user is. The token isn't kept."""
    try:
        token = requests.post(
            _api("/oauth2/token"),
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            auth=(settings.DISCORD_CLIENT_ID, settings.DISCORD_CLIENT_SECRET),
            headers=_proxy_headers(),
            timeout=TIMEOUT,
        )
        if token.status_code == 429:
            raise DiscordBlocked(f"token exchange failed (429): {token.text[:300]}")
        if token.status_code != 200:
            # 401 invalid_client: wrong client ID/secret. 400 invalid_grant: stale code or redirect mismatch.
            # 429: Discord is rate-limiting this server's IP.
            raise DiscordError(f"token exchange failed ({token.status_code}): {token.text[:300]}")
        access_token = token.json()["access_token"]
        me = requests.get(
            _api("/users/@me"), headers={"Authorization": f"Bearer {access_token}", **_proxy_headers()}, timeout=TIMEOUT
        )
        if me.status_code != 200:
            raise DiscordError(f"identity lookup failed ({me.status_code}): {me.text[:300]}")
        identity = me.json()
        if not str(identity.get("id", "")).isdigit():
            raise DiscordError("identity has no user id")
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
        raise DiscordError(f"unexpected response: {e!r}") from e
    return identity
