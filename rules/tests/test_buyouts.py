import pytest

from rules.buyouts import buyout_schedule, round_half_up_pct
from rules.contracts import Contract


class TestRounding:
    # Google Sheets ROUND is half away from zero; money is never negative.
    @pytest.mark.parametrize(
        ("amount", "pct", "expected"),
        [(6, 80, 5), (5, 70, 4), (15, 70, 11), (13, 80, 10), (11, 70, 8), (10, 50, 5), (1, 40, 0)],
    )
    def test_half_up(self, amount, pct, expected):
        assert round_half_up_pct(amount, pct) == expected


class TestBuyoutSchedule:
    def test_single_unpaid_year_is_80_percent(self):
        # Jurickson Profar: $1, signed 2024 for 2 yrs ($6/yr, 2025-2026), dropped in 2025.
        profar = Contract("Jurickson Profar", original_price=1, year_signed=2024, length=2)
        assert buyout_schedule(profar, dropped_in_season=2025) == {2026: 5}

    def test_two_unpaid_years_step_down(self):
        # Braxton Garrett: $1, signed 2023 for 3 yrs ($11/yr, 2024-2026), dropped in 2024.
        garrett = Contract("Braxton Garrett", original_price=1, year_signed=2023, length=3)
        assert buyout_schedule(garrett, dropped_in_season=2024) == {2025: 9, 2026: 8}

    def test_final_year_drop_is_free(self):
        lux = Contract("Gavin Lux", original_price=3, year_signed=2023, length=3)
        assert buyout_schedule(lux, dropped_in_season=2026) == {}

    def test_long_buyout_is_never_truncated(self):
        # $10, 6 yrs -> $34/yr for 2026-2031; dropped in 2026 leaves 5 unpaid years.
        c = Contract("Long", original_price=10, year_signed=2025, length=6)
        assert buyout_schedule(c, dropped_in_season=2026) == {2027: 27, 2028: 24, 2029: 20, 2030: 17, 2031: 14}

    def test_percentage_floors_at_zero(self):
        # 10 yrs from 2026: dropped after the first season leaves 9 unpaid years, 80% down to 0%.
        c = Contract("Ten", original_price=0, year_signed=2025, length=10)
        schedule = buyout_schedule(c, dropped_in_season=2026)
        assert list(schedule) == list(range(2027, 2036))
        assert schedule[2035] == 0

    def test_offseason_buyout_before_first_season(self):
        # Bought out at the signing right after it was signed: every season is unpaid.
        c = Contract("Fresh", original_price=0, year_signed=2025, length=2)  # $5/yr, 2026-2027
        assert buyout_schedule(c, dropped_in_season=2025) == {2026: 4, 2027: 4}

    @pytest.mark.parametrize("season", [2024, 2028])
    def test_drop_outside_contract_rejected(self, season):
        c = Contract("X", original_price=1, year_signed=2025, length=2)
        with pytest.raises(ValueError):
            buyout_schedule(c, dropped_in_season=season)
