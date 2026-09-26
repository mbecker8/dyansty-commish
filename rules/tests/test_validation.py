from rules.contracts import Contract
from rules.validation import validate_signing


def _contracts(n, year_signed=2025, length=2):
    return [Contract(f"P{year_signed}-{i}", 1, year_signed, length) for i in range(n)]


def test_ten_contracts_is_ok():
    assert validate_signing(existing=_contracts(4, 2024, 3), new=_contracts(6), after_season=2025) == []


def test_eleven_contracts_is_an_error():
    errors = validate_signing(existing=_contracts(5, 2024, 3), new=_contracts(6), after_season=2025)
    assert any("11 contracts" in e for e in errors)


def test_expiring_existing_contracts_do_not_count():
    expiring = _contracts(5, 2022, 3)  # final year 2025
    assert validate_signing(existing=expiring, new=_contracts(10), after_season=2025) == []


def test_new_contract_must_be_signed_this_period():
    errors = validate_signing(existing=[], new=[Contract("Late", 1, 2024, 2)], after_season=2025)
    assert any("Late" in e for e in errors)


def test_player_cannot_be_signed_twice():
    errors = validate_signing(
        existing=[], new=[Contract("Dup", 1, 2025, 1), Contract("Dup", 1, 2025, 2)], after_season=2025
    )
    assert any("Dup" in e for e in errors)


def test_cannot_sign_player_already_under_contract():
    lewis = Contract("Lewis", 9, 2024, 3)  # through 2027
    errors = validate_signing(existing=[lewis], new=[Contract("Lewis", 5, 2025, 2)], after_season=2025)
    assert any("Lewis" in e for e in errors)


def test_expiring_contract_does_not_collide_with_new_signings():
    # An expiring contract doesn't cover next season, so it doesn't collide here;
    # signability (not validation) keeps the team from re-signing him.
    expiring = Contract("Winn", 1, 2022, 3)
    assert validate_signing(existing=[expiring], new=[Contract("Other", 1, 2025, 1)], after_season=2025) == []


def test_length_above_the_maximum_is_an_error():
    errors = validate_signing(existing=[], new=[Contract("Long", 1, 2025, 11)], after_season=2025)
    assert any("11 years" in e for e in errors)
    assert validate_signing(existing=[], new=[Contract("Long", 1, 2025, 10)], after_season=2025) == []


def test_messages_use_player_names():
    errors = validate_signing(
        existing=[],
        new=[Contract("42", 1, 2025, 1), Contract("42", 1, 2025, 2)],
        after_season=2025,
        names={"42": "Royce Lewis"},
    )
    assert errors == ["Royce Lewis is signed 2 times"]
