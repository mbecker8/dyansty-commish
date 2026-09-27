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


def _count(text: str | None) -> int:
    return int(float(text)) if text else 0


def _outs(innings: str | None) -> int:
    """Baseball innings notation: '32.1' is 32 1/3 innings = 97 outs."""
    if not innings:
        return 0
    whole, _, thirds = innings.partition(".")
    return int(whole) * 3 + int(thirds or 0)


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


@dataclass(frozen=True)
class EndState:
    team: str
    status: str  # Fantrax roster status: Active, Reserve, IR, Minors
    games_played: int
    # MLB debut = 1 plate appearance or 0.1 innings pitched. Fantrax gives AB and BB but not
    # HBP or sacrifices, so plate_appearances is a lower bound.
    plate_appearances: int = 0
    outs: int = 0

    @property
    def debuted(self) -> bool:
        return self.plate_appearances > 0 or self.outs > 0


class Snapshot:
    """A saved snapshot directory, or the same data fetched live (`from_raw`)."""

    def __init__(self, directory: Path | None = None, *, raw: dict | None = None):
        if raw is None:
            teams = json.loads((directory / "teams.json").read_text())
            raw = {
                "league_info": json.loads((directory / "league_info.json").read_text()),
                "teams": teams,
                "rosters": {t["id"]: json.loads((directory / f"roster_{t['id']}.json").read_text()) for t in teams},
                "transactions": json.loads((directory / "transactions.json").read_text()),
                "trades": json.loads((directory / "trades.json").read_text()),
            }
        self.dir = directory
        self.league_info, self.teams, self.rosters = raw["league_info"], raw["teams"], raw["rosters"]
        self.transactions, self.trades = raw["transactions"], raw["trades"]

    @classmethod
    def from_raw(cls, raw: dict) -> Snapshot:
        return cls(raw=raw)

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

    @property
    def season(self) -> int:
        return int(self.league_info["fantasySettings"]["season"]["displayYear"])

    STATUS = {"1": "Active", "2": "Reserve", "3": "IR", "9": "Minors"}

    def end_states(self) -> dict[str, EndState]:
        """Fantrax player id -> where he ended the season and his MLB playing time."""
        out = {}
        for team_id, roster in self.rosters.items():
            for table in roster["tables"]:
                headers = [c.get("name") for c in table["header"]["cells"]]
                for row in table["rows"]:
                    if "scorer" not in row:
                        continue
                    stats = {h: row["cells"][i].get("content") for i, h in enumerate(headers)}
                    out[row["scorer"]["scorerId"]] = EndState(
                        team=team_id,
                        status=self.STATUS.get(row.get("statusId"), row.get("statusId")),
                        games_played=_count(stats.get("Games Played")),
                        plate_appearances=_count(stats.get("At Bats")) + _count(stats.get("Walks")),
                        outs=_outs(stats.get("Innings Pitched")),
                    )
        return out

    def salaries(self) -> dict[str, int]:
        """Fantrax player id -> salary on the roster he's on."""
        out = {}
        for roster in self.rosters.values():
            for table in roster["tables"]:
                headers = [c.get("name") for c in table["header"]["cells"]]
                if "Salary" not in headers:
                    continue
                col = headers.index("Salary")
                for row in table["rows"]:
                    if "scorer" in row and (text := row["cells"][col].get("content")):
                        out[row["scorer"]["scorerId"]] = int(float(text))
        return out

    def trade_comments(self) -> list[tuple[str, datetime, set[str], str]]:
        """(trade id, date, team ids involved, comment) for trades with a commissioner comment."""
        teams, dates, comments = defaultdict(set), {}, {}
        for row in self.trades["table"]["rows"]:
            cells = {c["key"]: c for c in row["cells"]}
            tx = row["txSetId"]
            teams[tx] |= {cells[k]["teamId"] for k in ("from", "to") if k in cells}
            if "date" in cells:
                dates[tx] = parse_fantrax_date(cells["date"]["content"])
            if row["result"].get("props", {}).get("comment"):
                text = re.sub(r"<[^>]+>", " ", row["result"]["content"])
                comments[tx] = re.sub(r"\s+", " ", text.replace("Executed", "", 1)).strip()
        return [(tx, dates[tx], teams[tx], text) for tx, text in comments.items()]

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
