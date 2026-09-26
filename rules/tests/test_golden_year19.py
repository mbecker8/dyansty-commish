"""Golden tests: the engine vs. the Year 19 (2026 season) post-signing workbook.

The fixture is extracted from a Google Sheets export by
scripts/extract_workbook_fixture.py (team tabs only). Where the engine
disagrees with the sheet, the difference must be a documented sheet bug below.
"""

import json
from pathlib import Path

import pytest

from rules.budget import compute_budget
from rules.buyouts import buyout_schedule
from rules.contracts import Contract

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "year19_post_signing.json").read_text())
TEAMS = FIXTURE["teams"]

# (team, budget component) -> (engine minus sheet, explanation)
SHEET_BUGS = {
    ("DC", "farm"): (3, "Farm Contracts sums F39:F57, skipping Farm 1 (Ethan Salas, $3)"),
    ("JM", "buyouts"): (10, "Bad Contracts sums only the first 4 buyout blocks; Arrighetti $4 and Francis $6 missed"),
}


def contract(row) -> Contract:
    return Contract(row["player"], row["original_price"], row["year_signed"], row["length"])


def real_buyouts(team):
    # Skip blank buyout blocks (a name typed in with no contract details).
    return [b for b in team["buyouts"] if b["original_price"] is not None]


def engine_budget(team):
    season = team["after_season"] + 1
    return compute_budget(
        season=season,
        contracts=[contract(c) for c in team["contracts"]],
        buyouts=[(contract(b), b["dropped_in"]) for b in real_buyouts(team)],
        # Post-signing, the farm list holds only retained players at next season's salary.
        farm_salaries={f["player"]: f["salary"] or 0 for f in team["farm"]},
        missed_ip_penalties=[team["budget"]["missed_ip"] or 0],
        cash_net=team["budget"]["cash_in"],
    )


def expected(code, component):
    return TEAMS[code]["budget"][component] + SHEET_BUGS.get((code, component), (0, ""))[0]


def test_fixture_covers_all_14_teams():
    assert len(TEAMS) == 14


@pytest.mark.parametrize("code", sorted(TEAMS))
def test_player_names_unique_within_team(code):
    # The fixture has no player IDs, so names stand in for them; they must not collide.
    t = TEAMS[code]
    names = [r["player"] for r in t["contracts"] + real_buyouts(t) + t["farm"]]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("code", sorted(TEAMS))
def test_each_contract_price_matches_sheet(code):
    season = TEAMS[code]["after_season"] + 1
    for row in TEAMS[code]["contracts"]:
        assert contract(row).cost_for_season(season) == row["sheet_price"], row["player"]


@pytest.mark.parametrize("code", sorted(TEAMS))
def test_each_buyout_penalty_matches_sheet(code):
    season = TEAMS[code]["after_season"] + 1
    for row in real_buyouts(TEAMS[code]):
        c = contract(row)
        assert c.annual_price == row["sheet_orig_sign"], row["player"]
        assert buyout_schedule(c, row["dropped_in"]).get(season, 0) == row["sheet_penalty"], row["player"]


@pytest.mark.parametrize("code", sorted(TEAMS))
@pytest.mark.parametrize("component", ["contracts", "buyouts", "farm"])
def test_budget_component_matches_sheet(code, component):
    assert getattr(engine_budget(TEAMS[code]), component) == expected(code, component)


@pytest.mark.parametrize("code", sorted(TEAMS))
def test_remaining_budget_matches_sheet(code):
    overcharge = sum(delta for (team, _), (delta, _) in SHEET_BUGS.items() if team == code)
    assert engine_budget(TEAMS[code]).remaining == TEAMS[code]["budget"]["remaining"] - overcharge
