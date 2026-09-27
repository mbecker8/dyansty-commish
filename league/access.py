"""Who may see league pages: the commissioner (staff), or a manager whose Discord link is still current."""

from functools import wraps

from django.conf import settings
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


def is_listed_commissioner(user) -> bool:
    """The Discord account is in COMMISSIONER_DISCORD_IDS, so it's a commissioner even without a Manager."""
    prefix = discord_username("")
    username = user.get_username()
    return username.startswith(prefix) and username.removeprefix(prefix) in settings.COMMISSIONER_DISCORD_IDS


def sync_commissioner_rights(user) -> None:
    """A Discord account has admin rights exactly while its Manager is marked commissioner,
    or while its ID is in COMMISSIONER_DISCORD_IDS."""
    manager = linked_manager(user)
    commissioner = bool(manager and manager.is_commissioner) or is_listed_commissioner(user)
    if user.is_staff != commissioner or user.is_superuser != commissioner:
        user.is_staff = user.is_superuser = commissioner
        user.save(update_fields=["is_staff", "is_superuser"])


def is_league_member(user) -> bool:
    if not user.is_authenticated or not user.is_active:
        return False
    if is_discord_account(user):
        return linked_manager(user) is not None or is_listed_commissioner(user)
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


def manages(user, team) -> bool:
    """The user signs in as one of this team's managers."""
    manager = linked_manager(user) if is_discord_account(user) else None
    return manager is not None and manager.team_id == team.pk


def commissioner_required(view):
    @wraps(view)
    @member_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            return render(request, "league/forbidden.html", {"why": "This page is for commissioners."}, status=403)
        return view(request, *args, **kwargs)

    return wrapper
