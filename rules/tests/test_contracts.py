import pytest

from rules.contracts import Contract, annual_price


class TestAnnualPrice:
    @pytest.mark.parametrize(
        ("length", "expected"),
        [(1, 9), (2, 14), (3, 19), (4, 24), (5, 29), (6, 33), (8, 41)],
    )
    def test_current_formula(self, length, expected):
        # 1yr = P, 2 = P+5, 3 = P+10, 4 = P+15, 5+ = P + 4*years
        assert annual_price(original_price=9, length=length, year_signed=2024) == expected

    @pytest.mark.parametrize(
        ("length", "expected"),
        [(1, 9), (2, 13), (3, 17), (4, 21), (5, 24)],
    )
    def test_legacy_formula_before_2014(self, length, expected):
        # Pre-2014: 1yr = P, 2 = P+4, 3 = P+8, 4+ = P + 3*years
        assert annual_price(original_price=9, length=length, year_signed=2013) == expected

    def test_zero_dollar_player(self):
        assert annual_price(original_price=0, length=2, year_signed=2025) == 5

    @pytest.mark.parametrize("length", [0, -1])
    def test_rejects_non_positive_length(self, length):
        with pytest.raises(ValueError):
            annual_price(original_price=5, length=length, year_signed=2025)

    def test_rejects_negative_price(self):
        with pytest.raises(ValueError):
            annual_price(original_price=-1, length=1, year_signed=2025)


class TestContractTerm:
    # Royce Lewis: signed after the 2024 season for 3 years -> plays 2025-2027.
    lewis = Contract(player="Royce Lewis", original_price=9, year_signed=2024, length=3)

    def test_final_year_is_inclusive(self):
        assert self.lewis.final_year == 2027

    def test_price(self):
        assert self.lewis.annual_price == 19

    def test_first_season(self):
        assert self.lewis.first_season == 2025

    @pytest.mark.parametrize(("season", "covered"), [(2024, False), (2025, True), (2027, True), (2028, False)])
    def test_covers(self, season, covered):
        assert self.lewis.covers(season) is covered

    def test_years_left_after_season(self):
        # After the 2025 season ends he has 2027 - 2025 = 2 years left.
        assert self.lewis.years_left(after_season=2025) == 2
        assert self.lewis.years_left(after_season=2027) == 0

    def test_cost_for_covered_season_is_annual_price(self):
        assert self.lewis.cost_for_season(2026) == 19

    def test_cost_for_uncovered_season_is_zero(self):
        assert self.lewis.cost_for_season(2028) == 0
