"""The two Discord OAuth2 calls sign-in needs. Scope `identify` only: we learn the user's ID, nothing else."""

import requests
from django.conf import settings

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"
ME_URL = "https://discord.com/api/users/@me"
TIMEOUT = 10  # seconds


class DiscordError(Exception):
    pass


def configured() -> bool:
    return bool(settings.DISCORD_CLIENT_ID and settings.DISCORD_CLIENT_SECRET)


def fetch_identity(code: str, redirect_uri: str) -> dict:
    """Trade the authorization code for a token, then read who the user is. The token isn't kept."""
    try:
        token = requests.post(
            TOKEN_URL,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            auth=(settings.DISCORD_CLIENT_ID, settings.DISCORD_CLIENT_SECRET),
            timeout=TIMEOUT,
        )
        if token.status_code != 200:
            # 401 invalid_client: wrong client ID/secret. 400 invalid_grant: stale code or redirect mismatch.
            # 429: Discord is rate-limiting this server's IP.
            raise DiscordError(f"token exchange failed ({token.status_code}): {token.text[:300]}")
        access_token = token.json()["access_token"]
        me = requests.get(ME_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=TIMEOUT)
        if me.status_code != 200:
            raise DiscordError(f"identity lookup failed ({me.status_code}): {me.text[:300]}")
        identity = me.json()
        if not str(identity.get("id", "")).isdigit():
            raise DiscordError("identity has no user id")
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
        raise DiscordError(f"unexpected response: {e!r}") from e
    return identity
