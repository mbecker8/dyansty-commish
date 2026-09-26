"""Who may see league pages: the commissioner (staff), or a manager whose Discord link is still current."""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.shortcuts import render


def discord_username(discord_id: str) -> str:
    """The Django username for a Discord account. One account per Discord ID."""
    return f"discord-{discord_id}"


def is_discord_account(user) -> bool:
    return user.get_username().startswith(discord_username(""))


def linked_manager(user):
    """The Manager this Discord account currently signs in for, or None if the link is gone."""
    manager = getattr(user, "manager", None)
    if manager and manager.discord_id and user.get_username() == discord_username(manager.discord_id):
        return manager
    return None


def sync_commissioner_rights(user) -> None:
    """A Discord account has admin rights exactly while its Manager is marked commissioner."""
    manager = linked_manager(user)
    commissioner = bool(manager and manager.is_commissioner)
    if user.is_staff != commissioner or user.is_superuser != commissioner:
        user.is_staff = user.is_superuser = commissioner
        user.save(update_fields=["is_staff", "is_superuser"])


def is_league_member(user) -> bool:
    if not user.is_authenticated or not user.is_active:
        return False
    if is_discord_account(user):
        return linked_manager(user) is not None
    return user.is_staff  # the commissioner's password account for the admin


def member_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_league_member(request.user):
            return render(request, "league/not_a_member.html", status=403)
        return view(request, *args, **kwargs)

    return wrapper
