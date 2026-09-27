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
        http.post.return_value = mock.Mock(
            status_code=400, json=lambda: {"error": "invalid_grant"}, text='{"error": "invalid_grant"}'
        )
        with pytest.raises(discord.DiscordError, match=r"\(400\).*invalid_grant"):
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


def test_calls_go_through_the_proxy_with_its_key_when_configured(settings):
    settings.DISCORD_API_BASE, settings.DISCORD_PROXY_KEY = "https://proxy.example.workers.dev/", "k3y"
    token = mock.Mock(status_code=200, json=lambda: {"access_token": "tok"})
    me = mock.Mock(status_code=200, json=lambda: {"id": "42", "username": "x"})
    with mock.patch("accounts.discord.requests") as http:
        http.RequestException = requests.RequestException
        http.post.return_value, http.get.return_value = token, me
        discord.fetch_identity("code", "https://dynasty-commish.onrender.com/auth/discord/callback")
    assert http.post.call_args.args[0] == "https://proxy.example.workers.dev/oauth2/token"
    assert http.post.call_args.kwargs["headers"] == {"X-Proxy-Key": "k3y"}
    assert http.get.call_args.args[0] == "https://proxy.example.workers.dev/users/@me"
    assert http.get.call_args.kwargs["headers"] == {"Authorization": "Bearer tok", "X-Proxy-Key": "k3y"}


def test_without_a_proxy_calls_go_straight_to_discord_with_no_key(settings):
    settings.DISCORD_API_BASE, settings.DISCORD_PROXY_KEY = "https://discord.com/api", ""
    with mock.patch("accounts.discord.requests") as http:
        http.RequestException = requests.RequestException
        http.post.return_value = mock.Mock(status_code=200, json=lambda: {"access_token": "tok"})
        http.get.return_value = mock.Mock(status_code=200, json=lambda: {"id": "42"})
        discord.fetch_identity("code", "http://127.0.0.1:8000/auth/discord/callback")
    assert http.post.call_args.args[0] == "https://discord.com/api/oauth2/token"
    assert http.post.call_args.kwargs["headers"] == {}


def test_a_rate_limit_is_reported_as_blocked(settings):
    with mock.patch("accounts.discord.requests") as http:
        http.RequestException = requests.RequestException
        http.post.return_value = mock.Mock(status_code=429, text="<!doctype html> cloudflare")
        with pytest.raises(discord.DiscordBlocked, match=r"\(429\)"):
            discord.fetch_identity("code", "http://127.0.0.1:8000/auth/discord/callback")
