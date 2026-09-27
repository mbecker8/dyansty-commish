"""Apply Fantrax facts (trades, drops, promotions, debuts) to contracts and farm players.

Each fact is stored once as a FantraxEvent under a unique key, with what it changed, so a
re-run never applies it twice. Facts the app can't interpret become exceptions for the
commissioner, who fixes the data by hand and resolves them with a note.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from league.fantrax_data import Move
from league.models import (
    Buyout,
    CashTrade,
    Contract,
    FantraxEvent,
    FantraxLeague,
    FarmPlayer,
    Player,
    SigningPeriod,
    Team,
    TeamAlias,
    audit,
)

Effect, Kind = FantraxEvent.Effect, FantraxEvent.Kind


class UnmatchedTeam(Exception):
    pass


def match_teams(snapshot) -> dict[str, Team]:
    """Fantrax team id -> Team. A renewed league has new team ids, so fall back to names and aliases."""
    by_id = {t.fantrax_id: t for t in Team.objects.all()}
    by_name = {t.name.lower(): t for t in by_id.values()} | {a.alias.lower(): a.team for a in TeamAlias.objects.all()}
    out = {}
    for t in snapshot.teams:
        team = by_id.get(t["id"]) or by_name.get(t["name"].strip().lower())
        if team is None:
            raise UnmatchedTeam(f"Fantrax team {t['name']!r} ({t['id']}) matches no team; add an alias")
        out[t["id"]] = team
    return out


def unresolved_count() -> int:
    return FantraxEvent.objects.unresolved().count()


class Processor:
    """Applies one league's facts against the current records."""

    def __init__(self, league: FantraxLeague, teams: dict[str, Team], names: dict[str, str], known=None):
        self.league, self.season, self.teams, self.names = league, league.season, teams, names
        self.known = set(FantraxEvent.objects.values_list("key", flat=True)) if known is None else known
        period = SigningPeriod.objects.filter(season=self.season).first()
        self.locked_at = period.locked_at if period else None
        self.created: list[FantraxEvent] = []
        self._players: dict[str, Player | None] = {}

    # --- lookups -----------------------------------------------------------

    def player(self, fid: str) -> Player | None:
        if fid not in self._players:
            self._players[fid] = Player.objects.filter(fantrax_id=fid).first()
        return self._players[fid]

    def contract(self, fid: str, when: datetime) -> Contract | None:
        """The live contract a move at `when` can affect.

        A contract signed at the signing after this season doesn't exist yet for moves before the lock.
        """
        for c in Contract.live.filter(player__fantrax_id=fid).select_related("team", "player"):
            if c.final_year < self.season:
                continue
            if c.year_signed >= self.season and (self.locked_at is None or when < self.locked_at):
                continue
            return c
        return None

    def farm(self, fid: str) -> FarmPlayer | None:
        return (
            FarmPlayer.objects.filter(player__fantrax_id=fid, status=FarmPlayer.Status.ACTIVE)
            .select_related("team", "player")
            .first()
        )

    def name(self, fid: str) -> str:
        p = self.player(fid)
        return self.names.get(fid) or (p.name if p else fid)

    # --- recording ---------------------------------------------------------

    def record(self, key, kind, effect, when, detail, fid="", **links) -> FantraxEvent:
        player = links.pop("player", None) or (self.player(fid) if fid else None)
        event = FantraxEvent.objects.create(
            key=key,
            league=self.league,
            happened_at=when,
            kind=kind,
            effect=effect,
            fantrax_player_id=fid,
            player_name=self.name(fid) if fid else "",
            player=player,
            detail=detail,
            **links,
        )
        self.known.add(key)
        self.created.append(event)
        return event

    # --- transactions ------------------------------------------------------

    def apply_move(self, m: Move) -> FantraxEvent | None:
        key = f"tx:{m.tx_id}:{m.fantrax_id}:{m.kind}"
        if key in self.known or (self.league.process_since and m.when < self.league.process_since):
            return None
        frm, to = self.teams.get(m.from_team), self.teams.get(m.to_team)
        teams = {"from_team": frm, "to_team": to}
        name = self.name(m.fantrax_id)

        def rec(effect, detail, **links):
            return self.record(key, m.kind, effect, m.when, detail, m.fantrax_id, **teams, **links)

        if m.kind == "CLAIM":
            return rec(Effect.NONE, f"{to.code if to else '?'} claimed {name}")
        held = self.contract(m.fantrax_id, m.when) or self.farm(m.fantrax_id)
        if held is None:
            verb = "traded" if m.kind == "TRADE" else "dropped"
            return rec(Effect.NONE, f"{verb} {name} (no contract or farm spot)")
        is_contract = isinstance(held, Contract)
        link = {"contract": held} if is_contract else {"farm_player": held}
        what = "contract" if is_contract else "farm spot"

        if m.kind == "TRADE":
            if held.team == to:
                return rec(Effect.ALREADY_REFLECTED, f"traded {name} to {to.code}: already on their books", **link)
            if held.team != frm:
                return rec(
                    Effect.EXCEPTION,
                    f"trade of {name} from {frm.code} to {to.code}, but his {what} is with {held.team.code}",
                    **link,
                )
            held.team = to
            held.save(update_fields=["team"])
            effect = Effect.CONTRACT_MOVED if is_contract else Effect.FARM_MOVED
            return rec(effect, f"traded {name} {frm.code} → {to.code}", **link)

        # DROP
        if held.team != frm:
            return rec(
                Effect.EXCEPTION, f"drop of {name} by {frm.code}, but his {what} is with {held.team.code}", **link
            )
        if not is_contract:
            held.status = FarmPlayer.Status.RELEASED
            held.save(update_fields=["status"])
            return rec(Effect.FARM_RELEASED, f"released {name} from the {frm.code} farm", **link)
        if held.final_year > self.season:
            buyout = Buyout.objects.create(
                contract=held, team=frm, dropped_in_season=self.season, note=f"Dropped {m.when:%Y-%m-%d}"
            )
            return rec(Effect.BUYOUT, f"dropped {name} ({frm.code}): buyout owed", buyout=buyout, **link)
        held.voided_in_season = self.season
        held.save(update_fields=["voided_in_season"])
        return rec(Effect.VOIDED, f"dropped {name} ({frm.code}) in his final year: contract voided", **link)

    # --- facts from the latest rosters -------------------------------------

    def apply_rosters(self, snapshot, now: datetime):
        ends = snapshot.end_states()
        season = self.season

        def rec(key, kind, effect, detail, fid, **links):
            if key not in self.known:
                self.record(key, kind, effect, now, detail, fid, **links)

        for f in FarmPlayer.objects.filter(status=FarmPlayer.Status.ACTIVE).select_related("team", "player"):
            fid = f.player.fantrax_id
            end = ends.get(fid) if fid else None
            if end is None:
                continue  # checked with the roster mismatches below
            team = self.teams.get(end.team)
            if end.status != "Minors" and f"promoted:{fid}" not in self.known:
                f.status, f.team = FarmPlayer.Status.PROMOTED, team
                f.save(update_fields=["status", "team"])
                rec(
                    f"promoted:{fid}",
                    Kind.PROMOTED,
                    Effect.FARM_PROMOTED,
                    f"promoted {f.player.name} to {team.code}'s active roster",
                    fid,
                    farm_player=f,
                    to_team=team,
                )
            self.debut(f, end, team, rec)

        live_farm = set(
            FarmPlayer.objects.filter(status=FarmPlayer.Status.ACTIVE).values_list("player__fantrax_id", flat=True)
        )
        for fid, end in ends.items():
            if end.status != "Minors" or fid in live_farm or self.contract(fid, now):
                continue
            team = self.teams.get(end.team)
            rec(
                f"minors-unknown:{fid}:{season}",
                Kind.MINORS_UNKNOWN,
                Effect.EXCEPTION,
                f"{self.name(fid)} is in {team.code}'s Fantrax Minors but not on a farm: add him in the admin if he "
                "was drafted",
                fid,
                to_team=team,
            )

        entered = set(CashTrade.objects.exclude(fantrax_tx_id="").values_list("fantrax_tx_id", flat=True))
        for tx, when, team_ids, text in snapshot.trade_comments():
            if tx in entered or (self.league.process_since and when < self.league.process_since):
                continue
            codes = " / ".join(sorted(self.teams[t].code for t in team_ids))
            key = f"cash-comment:{tx}"
            if key not in self.known:
                self.record(
                    key,
                    Kind.CASH_COMMENT,
                    Effect.EXCEPTION,
                    when,
                    f"Trade between {codes} mentions cash: {text!r}. Enter the cash trade by hand.",
                )

        self.roster_mismatches(ends, now, rec)

    def debut(self, f: FarmPlayer, end, team, rec):
        fid = f.player.fantrax_id
        if f.has_mlb_appearance:
            return
        if end.debuted:
            f.has_mlb_appearance = True
            f.save(update_fields=["has_mlb_appearance"])
            rec(
                f"debut:{fid}",
                Kind.DEBUT,
                Effect.FARM_DEBUT,
                f"{f.player.name} made his MLB debut ({end.games_played} G, {end.plate_appearances}+ PA, "
                f"{end.outs} outs)",
                fid,
                farm_player=f,
                to_team=team,
            )
        elif end.games_played > 0:
            rec(
                f"debut-check:{fid}:{self.season}",
                Kind.DEBUT_CHECK,
                Effect.EXCEPTION,
                f"{f.player.name} ({f.team.code} farm): {end.games_played} MLB games but no plate appearance or out "
                "recorded (HBP/sacrifice?). Check, and tick 'has MLB appearance' in the admin if he debuted.",
                fid,
                farm_player=f,
                to_team=f.team,
            )

    def roster_mismatches(self, ends, now, rec):
        """Anything whose Fantrax roster disagrees with the records after every move is applied."""

        def where(fid):
            end = ends.get(fid)
            return self.teams.get(end.team) if end else None

        for c in Contract.live.exclude(player__fantrax_id=None).select_related("team", "player"):
            fid = c.player.fantrax_id
            if self.contract(fid, now) != c:
                continue
            on = where(fid)
            if on != c.team:
                rec(
                    f"roster-mismatch:c{c.pk}:{self.league.pk}",
                    Kind.ROSTER_MISMATCH,
                    Effect.EXCEPTION,
                    f"{c.player.name}'s contract is with {c.team.code}, but Fantrax has him "
                    + (f"on {on.code}" if on else "on no roster"),
                    fid,
                    contract=c,
                    from_team=c.team,
                    to_team=on,
                )
        for f in FarmPlayer.objects.filter(status=FarmPlayer.Status.ACTIVE).exclude(player__fantrax_id=None):
            fid = f.player.fantrax_id
            on = where(fid)
            if on != f.team:
                rec(
                    f"roster-mismatch:f{f.pk}:{self.league.pk}",
                    Kind.ROSTER_MISMATCH,
                    Effect.EXCEPTION,
                    f"{f.player.name} is on the {f.team.code} farm, but Fantrax has him "
                    + (f"on {on.code}" if on else "on no roster"),
                    fid,
                    farm_player=f,
                    from_team=f.team,
                    to_team=on,
                )


@dataclass
class SyncResult:
    created: list[FantraxEvent] = field(default_factory=list)

    NO_CHANGE = (Effect.NONE, Effect.ALREADY_REFLECTED)

    def counts(self) -> dict[str, int]:
        """New events that changed something (or need a look), by effect label."""
        counts = Counter(Effect(e.effect).label for e in self.created if e.effect not in self.NO_CHANGE)
        return dict(counts)

    @property
    def exceptions(self) -> list[FantraxEvent]:
        return [e for e in self.created if e.effect == Effect.EXCEPTION]

    def summary(self) -> str:
        if not self.created:
            return "No new events"
        changes = ", ".join(f"{n} {label.lower()}" for label, n in self.counts().items()) or "no changes"
        return f"{len(self.created)} new events: {changes}"


def sync(sources, user=None, source_label: str = "", dry_run: bool = False) -> SyncResult:
    """Apply every new fact from `sources` ([(FantraxLeague, Snapshot)]), all or nothing.

    Fetch before calling this: nothing here touches the network.
    """
    result = SyncResult()
    now = timezone.now()
    with transaction.atomic():
        # Two syncs at once queue here; the second then finds the first one's keys and skips them.
        list(FantraxLeague.objects.select_for_update().filter(pk__in=[lg.pk for lg, _ in sources]))
        known = set(FantraxEvent.objects.values_list("key", flat=True))
        for league, snapshot in sources:
            names = {fid: p.name for fid, p in snapshot.players().items()}
            proc = Processor(league, match_teams(snapshot), names, known)
            for m in snapshot.moves():
                proc.apply_move(m)
            proc.apply_rosters(snapshot, now)
            result.created += proc.created
        if dry_run:
            transaction.set_rollback(True)
        else:
            audit(user, "Synced Fantrax", f"{source_label or 'Fantrax'}: {result.summary()}")
    return result
