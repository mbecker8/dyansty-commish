from rules.contracts import Contract
from rules.validation import validate_signing


def _contracts(n, year_signed=2025, length=2):
    return [Contract(f"P{i}", 1, year_signed, length) for i in range(n)]


def test_ten_contracts_is_ok():
    assert validate_signing(existing=_contracts(4, 2024, 3), new=_contracts(6), after_season=2025) == []


def test_eleven_contracts_is_an_error():
    errors = validate_signing(existing=_contracts(5, 2024, 3), new=_contracts(6), after_season=2025)
    assert any("11 contracts" in e for e in errors)


def test_expiring_existing_contracts_do_not_count():
    expiring = _contracts(5, 2022, 3)  # final year 2025
    assert validate_signing(existing=expiring, new=_contracts(10), after_season=2025) == []


def test_sign_and_trade_exception_raises_limit():
    errors = validate_signing(existing=_contracts(5, 2024, 3), new=_contracts(6), after_season=2025, extra_allowed=1)
    assert errors == []


def test_new_contract_must_be_signed_this_period():
    errors = validate_signing(existing=[], new=[Contract("Late", 1, 2024, 2)], after_season=2025)
    assert any("Late" in e for e in errors)


def test_player_cannot_be_signed_twice():
    errors = validate_signing(
        existing=[], new=[Contract("Dup", 1, 2025, 1), Contract("Dup", 1, 2025, 2)], after_season=2025
    )
    assert any("Dup" in e for e in errors)
