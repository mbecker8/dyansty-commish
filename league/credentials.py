"""The Fantrax cookie and Secret ID, pasted on the console and stored encrypted.

Read order: the saved value, else the env var or secrets/ file (settings.FANTRAX_*), so local runs and the
Render env vars keep working. Values are read when used, never at startup, so a pasted cookie works at once.
Nothing here logs or renders a value.
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from league.models import Credential, audit

FANTRAX_COOKIE = "fantrax_cookie"
FANTRAX_SECRET_ID = "fantrax_secret_id"
LABELS = {FANTRAX_COOKIE: "Fantrax cookie", FANTRAX_SECRET_ID: "Fantrax Secret ID"}


def _fernet() -> Fernet:
    """FANTRAX_CREDENTIALS_KEY if set, else a key derived from SECRET_KEY (regenerating it loses saved values)."""
    secret = os.environ.get("FANTRAX_CREDENTIALS_KEY") or settings.SECRET_KEY
    digest = hashlib.sha256(f"dynasty-commish credentials:{secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _saved(name: str) -> str | None:
    """The decrypted value, "" if saved but unreadable, None if nothing is saved."""
    row = Credential.objects.filter(name=name).first()
    if row is None:
        return None
    try:
        return _fernet().decrypt(row.ciphertext.encode()).decode()
    except InvalidToken:
        return ""


def _setting(name: str) -> str:
    return getattr(settings, name.upper(), "")


def get(name: str) -> str:
    return _saved(name) or _setting(name)


def fantrax_cookie() -> str:
    return get(FANTRAX_COOKIE)


def fantrax_secret_id() -> str:
    return get(FANTRAX_SECRET_ID)


def save(name: str, value: str, user) -> None:
    Credential.objects.update_or_create(
        name=name,
        defaults={"ciphertext": _fernet().encrypt(value.strip().encode()).decode(), "updated_by": user},
    )
    audit(user, f"Updated {LABELS[name]}")


def status(name: str) -> dict:
    """What the console shows: where the value comes from, never the value itself."""
    row = Credential.objects.filter(name=name).select_related("updated_by").first()
    if row is not None:
        state = "saved" if _saved(name) else "unreadable"
        return {"state": state, "updated_at": row.updated_at, "updated_by": row.updated_by}
    return {"state": "env" if _setting(name) else "unset"}
