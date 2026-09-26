from unittest import mock

import pytest
import requests

from accounts import discord


def test_fetch_identity_exchanges_the_code_then_reads_the_user(settings):
    settings.DISCORD_CLIENT_ID, settings.DISCORD_CLIENT_SECRET = "id", "secret"
    token = mock.Mock(status_code=200, json=lambda: {"access_token": "tok", "token_type": "Bearer"})
    me = mock.Mock(status_code=200, json=lambda: {"id": "42", "username": "x"})
    with mock.patch("accounts.discord.requests") as http:
        http.RequestException = requests.RequestException
        http.post.return_value, http.get.return_value = token, me
        assert discord.fetch_identity("code", "http://127.0.0.1:8000/auth/discord/callback") == {
            "id": "42",
            "username": "x",
        }
    post = http.post.call_args
    assert post.kwargs["data"]["code"] == "code" and post.kwargs["auth"] == ("id", "secret")
    assert post.kwargs["timeout"] and http.get.call_args.kwargs["timeout"]
    assert http.get.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"


def test_fetch_identity_raises_on_a_refused_code(settings):
    with mock.patch("accounts.discord.requests") as http:
        http.RequestException = requests.RequestException
        http.post.return_value = mock.Mock(status_code=400, json=lambda: {"error": "invalid_grant"})
        with pytest.raises(discord.DiscordError):
            discord.fetch_identity("bad", "http://127.0.0.1:8000/auth/discord/callback")


@pytest.mark.parametrize(
    "token_json, me_json",
    [
        ({"no_token": 1}, {"id": "1"}),
        ({"access_token": "t"}, {"username": "no id"}),
        ({"access_token": "t"}, ValueError),
    ],
)
def test_odd_discord_responses_raise_discord_error(settings, token_json, me_json):
    def body(value):
        def parse():
            if value is ValueError:
                raise ValueError("not json")
            return value

        return parse

    with mock.patch("accounts.discord.requests") as http:
        http.RequestException = requests.RequestException
        http.post.return_value = mock.Mock(status_code=200, json=body(token_json))
        http.get.return_value = mock.Mock(status_code=200, json=body(me_json))
        with pytest.raises(discord.DiscordError):
            discord.fetch_identity("code", "http://127.0.0.1:8000/auth/discord/callback")
