from unittest import mock

import pytest
import requests

from league import discord_client
from league.discord_client import DiscordReadError


@pytest.fixture
def bot(settings):
    settings.DISCORD_BOT_TOKEN, settings.DISCORD_GUILD_ID, settings.DISCORD_CATEGORY_ID = "tok", "1", "10"
    settings.DISCORD_API_BASE, settings.DISCORD_PROXY_KEY = "https://discord.com/api", ""


def reply(status, body):
    return mock.Mock(status_code=status, json=lambda: body, text=str(body))


def channels(*extra):
    return [
        {"id": "11", "name": "trades-2027-assets", "type": 0, "parent_id": "10", "position": 2},
        {"id": "12", "name": "league-rules", "type": 0, "parent_id": "10", "position": 1},
        {"id": "13", "name": "general", "type": 0, "parent_id": "99", "position": 0},
        {"id": "14", "name": "Voice", "type": 2, "parent_id": "10", "position": 3},
        *extra,
    ]


def routed(routes):
    """requests.get stand-in: the reply for the first route whose path fragment is in the URL."""

    def get(url, params=None, headers=None, timeout=None):
        assert headers["Authorization"] == "Bot tok" and timeout
        for fragment, answer in routes.items():
            if fragment in url:
                return answer if isinstance(answer, mock.Mock) else answer(params or {})
        raise AssertionError(url)

    return get


def test_reads_every_text_channel_in_the_category_with_all_pages(bot):
    page1 = [{"id": str(i)} for i in range(300, 200, -1)]
    page2 = [{"id": "150"}]

    def history(params):
        assert params.get("before") != "150", "a short page is the last one"
        return reply(200, {None: page1, "201": page2}[params.get("before")])

    routes = {"/guilds/1/channels": reply(200, channels()), "/channels/11/": history, "/channels/12/": reply(200, [])}
    with mock.patch("league.discord_client.requests.get", side_effect=routed(routes)) as get:
        out = discord_client.fetch_category()
    assert [c.name for c in out] == ["league-rules", "trades-2027-assets"]
    assert len(out[1].messages) == 101 and out[1].readable
    assert get.call_args_list[0].args[0] == "https://discord.com/api/v10/guilds/1/channels"


def test_private_channel_is_skipped(bot):
    routes = {
        "/guilds/1/channels": reply(200, channels()),
        "/channels/11/": reply(200, []),
        "/channels/12/": reply(403, {"message": "Missing Access", "code": 50001}),
    }
    with mock.patch("league.discord_client.requests.get", side_effect=routed(routes)):
        out = {c.name: c for c in discord_client.fetch_category()}
    assert not out["league-rules"].readable and out["trades-2027-assets"].readable


@pytest.mark.parametrize(
    "status, body, says",
    [
        (401, {"message": "401: Unauthorized"}, "DISCORD_BOT_TOKEN"),
        (403, {"message": "Missing Access", "code": 50001}, "still in the league server"),
        (403, "Forbidden", "DISCORD_PROXY_KEY"),
        (404, "Not found", "redeploy the Worker"),
        (500, {"message": "boom"}, "500"),
    ],
)
def test_errors_say_what_to_fix(bot, status, body, says):
    r = reply(status, body)
    if isinstance(body, str):
        r.json = mock.Mock(side_effect=ValueError("not json"))
    with mock.patch("league.discord_client.requests.get", return_value=r):
        with pytest.raises(DiscordReadError, match=says):
            discord_client.fetch_category()


def test_rate_limit_waits_then_retries(bot):
    answers = iter([reply(429, {"retry_after": 0.01}), reply(200, channels()[:1]), reply(200, [])])
    with mock.patch("league.discord_client.requests.get", side_effect=lambda *a, **k: next(answers)):
        with mock.patch("league.discord_client.time.sleep") as sleep:
            (ch,) = discord_client.fetch_category()
    sleep.assert_called_once()
    assert ch.name == "trades-2027-assets"


def test_network_failure_is_a_read_error(bot):
    with mock.patch("league.discord_client.requests.get", side_effect=requests.ConnectionError("down")):
        with pytest.raises(DiscordReadError, match="Couldn't reach Discord"):
            discord_client.fetch_category()


def test_empty_category_is_an_error(bot):
    with mock.patch("league.discord_client.requests.get", return_value=reply(200, channels()[2:3])):
        with pytest.raises(DiscordReadError, match="DISCORD_CATEGORY_ID"):
            discord_client.fetch_category()


def test_not_configured(settings):
    settings.DISCORD_BOT_TOKEN = ""
    assert not discord_client.configured()
    with pytest.raises(DiscordReadError, match="DISCORD_BOT_TOKEN"):
        discord_client.fetch_category()


def test_goes_through_the_proxy_when_set(bot, settings):
    settings.DISCORD_API_BASE, settings.DISCORD_PROXY_KEY = "https://proxy.example", "k"
    with mock.patch("league.discord_client.requests.get", return_value=reply(200, [])) as get:
        with pytest.raises(DiscordReadError):
            discord_client.fetch_category()
    assert get.call_args.args[0] == "https://proxy.example/v10/guilds/1/channels"
    assert get.call_args.kwargs["headers"]["X-Proxy-Key"] == "k"
