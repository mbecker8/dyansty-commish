import pytest


@pytest.fixture(autouse=True)
def _plain_static_storage(settings):
    # Tests run with DEBUG off and no collectstatic, so skip the hashed-manifest lookup.
    settings.STORAGES = settings.STORAGES | {
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
    }
