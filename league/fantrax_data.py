"""Read the committed Fantrax snapshot (data/fantrax/<dir>/) into plain Python structures."""

import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def normalize_name(name: str) -> str:
    """Lowercase ASCII letters only: 'Agustín Ramírez' -> 'agustinramirez'."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", ascii_name.lower())


def parse_fantrax_date(text: str) -> datetime:
    """'Sat Sep 19, 2026, 1:04PM' (Eastern) -> aware datetime."""
    return datetime.strptime(text, "%a %b %d, %Y, %I:%M%p").replace(tzinfo=EASTERN)


@dataclass(frozen=True)
class FantraxPlayer:
    fantrax_id: str
    name: str
    positions: str


@dataclass(frozen=True)
class Move:
    """One player movement: a claim, drop, or one side of a trade."""

    when: datetime
    kind: str  # CLAIM, DROP, TRADE
    fantrax_id: str
    from_team: str | None  # Fantrax team id
    to_team: str | None
    tx_id: str


class Snapshot:
    def __init__(self, directory: Path):
        self.dir = directory
        self.teams = json.loads((directory / "teams.json").read_text())
        self.rosters = {t["id"]: json.loads((directory / f"roster_{t['id']}.json").read_text()) for t in self.teams}
        self.transactions = json.loads((directory / "transactions.json").read_text())
        self.trades = json.loads((directory / "trades.json").read_text())

    def roster_rows(self):
        """(fantrax team id, row) for every rostered player."""
        for team_id, roster in self.rosters.items():
            for table in roster["tables"]:
                for row in table["rows"]:
                    if "scorer" in row:
                        yield team_id, row

    def players(self) -> dict[str, FantraxPlayer]:
        """Every player seen anywhere in the snapshot, by Fantrax ID."""
        found = {}

        def add(scorer):
            if scorer.get("scorerId") and scorer.get("name"):
                found.setdefault(
                    scorer["scorerId"],
                    FantraxPlayer(scorer["scorerId"], scorer["name"], scorer.get("posShortNames", "")),
                )

        for _, row in self.roster_rows():
            add(row["scorer"])
        for page in self.transactions:
            for row in page["table"]["rows"]:
                add(row["scorer"])
        for row in self.trades["table"]["rows"]:
            add(row["scorer"])
        return found

    def ids_by_name(self) -> dict[str, set[str]]:
        index = defaultdict(set)
        for p in self.players().values():
            index[normalize_name(p.name)].add(p.fantrax_id)
        return index

    STATUS = {"1": "Active", "2": "Reserve", "3": "IR", "9": "Minors"}

    def end_states(self) -> dict[str, tuple[str, str, int]]:
        """Fantrax player id -> (team id, roster status, MLB games played this season)."""
        out = {}
        for team_id, roster in self.rosters.items():
            for table in roster["tables"]:
                headers = [c.get("name") for c in table["header"]["cells"]]
                gp = headers.index("Games Played")
                for row in table["rows"]:
                    if "scorer" in row:
                        games = row["cells"][gp].get("content") or "0"
                        status = self.STATUS.get(row.get("statusId"), row.get("statusId"))
                        out[row["scorer"]["scorerId"]] = (team_id, status, int(float(games)))
        return out

    def rostered(self) -> dict[str, tuple[str, dict]]:
        """Fantrax player id -> (team id, roster row) at snapshot time."""
        return {row["scorer"]["scorerId"]: (team_id, row) for team_id, row in self.roster_rows()}

    def moves(self) -> list[Move]:
        """All claims, drops and trade legs, oldest first."""
        out = []
        group_cells = {}  # rows after the first in a set omit rowspan'd cells (team, date)
        for page in self.transactions:
            for row in page["table"]["rows"]:
                cells = group_cells.setdefault(row["txSetId"], {}) | {c["key"]: c for c in row["cells"]}
                group_cells[row["txSetId"]] = cells
                if not row.get("executed"):
                    continue
                team = cells["team"]["teamId"]
                kind = row["transactionCode"]
                out.append(
                    Move(
                        when=parse_fantrax_date(cells["date"]["content"]),
                        kind=kind,
                        fantrax_id=row["scorer"]["scorerId"],
                        from_team=team if kind == "DROP" else None,
                        to_team=team if kind == "CLAIM" else None,
                        tx_id=row["txSetId"],
                    )
                )
        dates = {}
        for row in self.trades["table"]["rows"]:
            cells = {c["key"]: c for c in row["cells"]}
            if "date" in cells:
                dates[row["txSetId"]] = parse_fantrax_date(cells["date"]["content"])
        for row in self.trades["table"]["rows"]:
            if not row.get("executed") or not row["scorer"].get("scorerId"):
                continue  # draft picks have no scorer
            cells = {c["key"]: c for c in row["cells"]}
            out.append(
                Move(
                    when=dates[row["txSetId"]],
                    kind="TRADE",
                    fantrax_id=row["scorer"]["scorerId"],
                    from_team=cells["from"]["teamId"],
                    to_team=cells["to"]["teamId"],
                    tx_id=row["txSetId"],
                )
            )
        # Within a claim/drop set, drops happen before claims.
        return sorted(out, key=lambda m: (m.when, m.kind != "DROP"))
