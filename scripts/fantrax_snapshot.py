"""Save a raw end-of-season snapshot of a Fantrax league to disk.

Usage: uv run python scripts/fantrax_snapshot.py <league_id> <out_dir>

Writes league info, every team's roster (raw getTeamRosterInfo JSON), the full
claim/drop history and trade history, then a flattened rosters.csv for quick checks.
"""

import csv
import json
import sys
from pathlib import Path

from fantrax_client import call, session

STATUS = {"1": "Active", "2": "Reserve", "3": "Inj Res", "9": "Minors"}


def data(resp: dict) -> dict:
    return resp["responses"][0]["data"]


def main(league_id: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    s = session()

    info = data(call(s, league_id, ("getFantasyLeagueInfo", {})))
    (out / "league_info.json").write_text(json.dumps(info, indent=1))

    first = data(call(s, league_id, ("getTeamRosterInfo", {"view": "STATS"})))
    teams = first["fantasyTeams"]
    (out / "teams.json").write_text(json.dumps(teams, indent=1))

    rows = []
    for team in teams:
        roster = data(call(s, league_id, ("getTeamRosterInfo", {"teamId": team["id"], "view": "STATS"})))
        (out / f"roster_{team['id']}.json").write_text(json.dumps(roster, indent=1))
        for table in roster["tables"]:
            headers = [c.get("name") for c in table["header"]["cells"]]
            for row in table["rows"]:
                if "scorer" not in row:
                    continue  # empty lineup slot
                cells = dict(zip(headers, (c.get("content") for c in row["cells"]), strict=False))
                p = row["scorer"]
                rows.append(
                    {
                        "team_id": team["id"],
                        "team": team["name"],
                        "player_id": p["scorerId"],
                        "player": p["name"],
                        "mlb_team": p.get("teamShortName", ""),
                        "positions": p.get("posShortNames", ""),
                        "status": STATUS.get(row.get("statusId"), row.get("statusId")),
                        "salary": cells.get("Salary", ""),
                        "year_signed": cells.get("Year Signed", ""),
                        "contract_expires": cells.get("Contract Expires", ""),
                        "rookie": p.get("rookie"),
                        "minors_eligible": p.get("minorsEligible"),
                    }
                )
        print(f"{team['name']}: {sum(r['team_id'] == team['id'] for r in rows)} players")

    if not rows:
        raise SystemExit("No rostered players found; Fantrax response shape may have changed.")
    with (out / "rosters.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    pages, page = [], 1
    while True:
        tx = data(
            call(s, league_id, ("getTransactionDetailsHistory", {"maxResultsPerPage": "200", "pageNumber": str(page)}))
        )
        pages.append(tx)
        pi = tx.get("paginatedResultSet", {})
        if page >= int(pi.get("totalNumPages", 1)):
            break
        page += 1
    (out / "transactions.json").write_text(json.dumps(pages, indent=1))

    # Trades (players, draft picks; cash only appears as a commissioner comment).
    trades = data(call(s, league_id, ("getTransactionDetailsHistory", {"view": "TRADE", "maxResultsPerPage": "500"})))
    if int(trades["paginatedResultSet"].get("totalNumPages", 1)) > 1:
        raise SystemExit("More than one page of trades; add pagination.")
    (out / "trades.json").write_text(json.dumps(trades, indent=1))
    print(f"trades: {trades['paginatedResultSet'].get('totalNumResults')} results")
    print(
        f"transactions: {len(pages)} page(s), {pages[0].get('paginatedResultSet', {}).get('totalNumResults')} results"
    )


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
