from types import SimpleNamespace

import pytest

from league.discord_cash import CASH, EXCEPTION, NOT_CASH, read_post, readable_text

A, B = SimpleNamespace(code="AA"), SimpleNamespace(code="BB")
TEAMS = {"1": A, "2": B}
NAMES = {"1": "alice", "2": "bob", "3": "carol"}


def read(text, attachments=False, season=2027):
    return read_post(text, attachments, season, TEAMS, NAMES)


@pytest.mark.parametrize(
    "text",
    [
        "<@1> sends $10 to <@2> in Stott trade",
        "<@!1>  sends $ 10 to <@!2> in Stott trade",
        "<@1> Sends $10 (2027 auction budget) TO <@2> in Stott trade",
        "<@1> sends $10 2027 cash and 2027 1st Rd FYPD pick to <@2> in Stott trade",
    ],
)
def test_cash_shapes(text):
    r = read(text)
    assert r.status == CASH and r.detail == "AA sends $10 to BB for 2027"
    (t,) = r.trades
    assert (t.from_team, t.to_team, t.amount) == (A, B, 10)
    assert t.note.endswith("in Stott trade")


@pytest.mark.parametrize("text", ["<@1> sends 2027 second to <@2> in a trade", "https://klipy.com/gifs/shimmy", ""])
def test_no_amount_is_not_cash(text):
    assert read(text).status == NOT_CASH


@pytest.mark.parametrize(
    "text, attachments, why",
    [
        ("", True, "picture"),
        ("<@1> sends $25 (2025 cash), $10 (2026) and $5 (2027) to <@2>", False, "More than one amount"),
        ("<@1> sends $5 2027 (not 2026) cash to <@2>", False, "Mentions 2026"),
        ("Kevin sends Devin $3 2027 in Imanaga acquisition", False, "@mentions"),
        ("<@3> sends $3 to <@2> in a trade", False, "@carol isn't linked"),
        ("<@1> sends $3 to <@1>", False, "same team"),
    ],
)
def test_exceptions(text, attachments, why):
    r = read(text, attachments)
    assert r.status == EXCEPTION and why in r.detail and not r.trades


@pytest.mark.parametrize(
    "text", ["<@1> sends 10 auction dollars to <@2>", "<@1> sends 10$ to <@2>", "<@1> sends ten bucks to <@2>"]
)
def test_cash_without_a_dollar_amount_is_an_exception(text):
    r = read(text)
    assert r.status == EXCEPTION and "$" in r.detail


def test_only_an_unlinked_mention_clears_itself_once_linked():
    assert read("<@3> sends $3 to <@2> in a trade").needs_link
    assert not read("Kevin sends Devin $3 2027").needs_link
    assert not read("<@1> sends $3 to <@2>").needs_link


def test_readable_text_names_mentions():
    assert readable_text("<@1> sends $3 to <@!9>", NAMES) == "@alice sends $3 to @9"
