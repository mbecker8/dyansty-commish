"""Who may see league pages: the commissioner (staff), or a manager whose Discord link is still current."""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.shortcuts import render


def discord_username(discord_id: str) -> str:
    """The Django username for a Discord account. One account per Discord ID."""
    return f"discord-{discord_id}"


def is_league_member(user) -> bool:
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_staff:
        return True
    manager = getattr(user, "manager", None)
    # Checked on every request, so unlinking or re-pointing a Manager takes effect at once.
    return bool(manager and manager.discord_id and user.get_username() == discord_username(manager.discord_id))


def member_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_league_member(request.user):
            return render(request, "league/not_a_member.html", status=403)
        return view(request, *args, **kwargs)

    return wrapper
