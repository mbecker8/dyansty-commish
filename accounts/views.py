import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from accounts import discord
from league.access import discord_username, sync_commissioner_rights
from league.models import Manager

SESSION_STATE = "discord_oauth_state"
SESSION_NEXT = "discord_oauth_next"


def _callback_uri(request):
    return request.build_absolute_uri(reverse("discord_callback"))


def _safe_next(request, url):
    if url and url_has_allowed_host_and_scheme(url, {request.get_host()}, require_https=request.is_secure()):
        return url
    return "/"


@require_GET
def login_page(request):
    return render(request, "accounts/login.html", {"configured": discord.configured(), "next": request.GET.get("next")})


@require_GET
def discord_login(request):
    if not discord.configured():
        return redirect("login")
    state = secrets.token_urlsafe(32)
    request.session[SESSION_STATE] = state
    request.session[SESSION_NEXT] = _safe_next(request, request.GET.get("next"))
    query = urlencode(
        {
            "client_id": discord.settings.DISCORD_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": _callback_uri(request),
            "scope": "identify",
            "state": state,
            "prompt": "none",
        }
    )
    return redirect(f"{discord.AUTHORIZE_URL}?{query}")


@require_GET
def discord_callback(request):
    expected = request.session.pop(SESSION_STATE, None)
    next_url = request.session.pop(SESSION_NEXT, "/")
    given = request.GET.get("state", "")
    if not expected or not secrets.compare_digest(expected, given):
        return render(request, "accounts/error.html", {"message": "That sign-in link expired. Try again."}, status=400)
    if "error" in request.GET or "code" not in request.GET:
        return redirect(reverse("login"))
    try:
        identity = discord.fetch_identity(request.GET["code"], _callback_uri(request))
    except discord.DiscordError:
        return render(
            request, "accounts/error.html", {"message": "Couldn't reach Discord. Try again in a minute."}, status=502
        )

    manager = Manager.objects.select_related("user", "team").filter(discord_id=identity["id"]).first()
    if manager is None and identity["id"] not in settings.COMMISSIONER_DISCORD_IDS:
        return render(
            request,
            "accounts/not_linked.html",
            {"discord_id": identity["id"], "discord_username": identity.get("username", "")},
            status=403,
        )
    # The account belongs to the Discord ID, not the team: handing a team to someone else
    # gives them their own account, never the previous manager's (or its admin rights).
    user, created = User.objects.get_or_create(
        username=discord_username(identity["id"]),
        defaults={"first_name": manager.name if manager else identity.get("username", "")},
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    if manager is not None:
        Manager.objects.filter(user=user).exclude(pk=manager.pk).update(user=None)
        manager.user = user
        manager.discord_username = str(identity.get("username", ""))[:100]
        manager.save(update_fields=["user", "discord_username"])
    sync_commissioner_rights(user)
    login(request, user, backend=f"{ModelBackend.__module__}.{ModelBackend.__name__}")
    return redirect(_safe_next(request, next_url))


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")
