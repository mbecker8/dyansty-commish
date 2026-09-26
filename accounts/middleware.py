from league.access import is_discord_account, sync_commissioner_rights


class DiscordAccountMiddleware:
    """Re-check a Discord account's team link and commissioner flag on every request, so changes
    the commissioner makes in the admin (unlinking, removing the flag) apply immediately, the
    admin included."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and is_discord_account(user):
            sync_commissioner_rights(user)
        return self.get_response(request)
