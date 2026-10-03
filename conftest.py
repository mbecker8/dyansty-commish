import pytest


@pytest.fixture(autouse=True)
def _plain_static_storage(settings):
    # Tests run with DEBUG off and no collectstatic, so skip the hashed-manifest lookup.
    settings.STORAGES = settings.STORAGES | {
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
    }


@pytest.fixture(autouse=True)
def _no_real_discord_bot(settings):
    # A local secrets/discord.env holds the real bot token; tests that need one set their own.
    settings.DISCORD_BOT_TOKEN = settings.DISCORD_GUILD_ID = settings.DISCORD_CATEGORY_ID = ""
