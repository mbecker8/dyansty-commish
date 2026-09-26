import pytest

from rules.contracts import Contract
from rules.signability import Signability, signability


@pytest.mark.parametrize(
    ("contract", "expected"),
    [
        (Contract("Lewis", 9, 2024, 3), Signability.UNDER_CONTRACT),  # final 2027
        (Contract("Baz", 12, 2025, 1), Signability.UNDER_CONTRACT),  # final 2026
        (Contract("Winn", 1, 2022, 3), Signability.EXPIRING),  # final 2025
        (None, Signability.SIGNABLE),  # auction/draft/FA pickup, or a voided (dropped) contract
    ],
)
def test_signability_at_2025_signing(contract, expected):
    assert signability(contract, after_season=2025) is expected


def test_contract_already_over_is_treated_as_no_contract():
    assert signability(Contract("Old", 1, 2020, 2), after_season=2025) is Signability.SIGNABLE


def test_only_signable_players_can_be_signed():
    assert Signability.SIGNABLE.can_sign
    assert not Signability.EXPIRING.can_sign
    assert not Signability.UNDER_CONTRACT.can_sign
