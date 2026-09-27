"""League state: franchises, players, contracts, buyouts, farm, picks and budget ledger.

Budget math lives in the pure `rules` package; models convert to rules objects.
"""

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.utils import timezone

import rules.contracts


class Team(models.Model):
    """A franchise. Names and managers change; the code and Fantrax ID are stable."""

    code = models.CharField(max_length=4, unique=True)
    name = models.CharField(max_length=100)
    fantrax_id = models.CharField(max_length=32, unique=True, null=True, blank=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} ({self.name})"


class TeamAlias(models.Model):
    """Other names for a franchise: old names, nicknames, old codes, manager first names."""

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="aliases")
    alias = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.alias


class Manager(models.Model):
    """A person who runs a team. The commissioner creates these and links them to Discord accounts."""

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="managers")
    name = models.CharField(max_length=100)
    discord_id = models.CharField(
        max_length=32,
        unique=True,
        null=True,
        blank=True,
        validators=[RegexValidator(r"^\d+$", "A Discord user ID is all digits (not the username).")],
        help_text="Discord user ID (numeric), not the username",
    )
    discord_username = models.CharField(max_length=100, blank=True, help_text="Last seen at sign-in")
    is_commissioner = models.BooleanField(
        default=False, help_text="Full access to the admin, on top of the manager pages"
    )
    user = models.OneToOneField("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="manager")

    class Meta:
        ordering = ["team", "name"]

    def __str__(self):
        return f"{self.name} ({self.team.code})"

    def save(self, *args, **kwargs):
        # A new Discord ID is a new person: drop the old sign-in account's link to this team.
        if self.pk and self.user_id:
            old = Manager.objects.filter(pk=self.pk).values_list("discord_id", flat=True).first()
            if old != self.discord_id:
                self.user = None
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = {*kwargs["update_fields"], "user"}
        super().save(*args, **kwargs)


class Player(models.Model):
    name = models.CharField(max_length=100)
    fantrax_id = models.CharField(max_length=16, unique=True, null=True, blank=True)
    positions = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class LiveContractManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(buyout__isnull=True, voided_in_season__isnull=True)


class Contract(models.Model):
    """A multi-year contract. `team` is the current holder; a trade moves it.

    A drop voids the contract: before the final year it gets a Buyout, in the final
    year it's just marked voided (no penalty, and the player becomes signable).
    """

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="contracts")
    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="contracts")
    original_price = models.PositiveIntegerField()
    year_signed = models.PositiveIntegerField(help_text="League season that had just ended when signed")
    length = models.PositiveIntegerField()
    sign_and_trade = models.BooleanField(default=False, help_text="Record only: acquired by sign-and-trade")
    voided_in_season = models.PositiveIntegerField(null=True, blank=True, help_text="Dropped in its final year")
    note = models.TextField(blank=True)

    objects = models.Manager()
    live = LiveContractManager()

    class Meta:
        ordering = ["team", "player__name"]

    def __str__(self):
        return f"{self.player} {self.length}yr from {self.year_signed} ({self.team.code})"

    def as_rules(self) -> rules.contracts.Contract:
        return rules.contracts.Contract(
            player_id=str(self.player_id),
            original_price=self.original_price,
            year_signed=self.year_signed,
            length=self.length,
        )

    @property
    def final_year(self) -> int:
        return self.year_signed + self.length


class Buyout(models.Model):
    """A contract dropped before its final year. The penalty belongs to `team`, which can change by trade."""

    contract = models.OneToOneField(Contract, on_delete=models.PROTECT, related_name="buyout")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="buyouts")
    dropped_in_season = models.PositiveIntegerField()
    note = models.TextField(blank=True)

    def __str__(self):
        return f"Buyout of {self.contract.player} ({self.team.code}, dropped {self.dropped_in_season})"

    def clean(self):
        # Dropped in the final year is free (the contract is just voided), so a buyout's drop
        # season runs from the signing offseason up to the year before the final year.
        if not self.contract_id:
            return
        c = self.contract
        if not c.year_signed <= self.dropped_in_season < c.final_year:
            raise ValidationError(
                {
                    "dropped_in_season": f"Must be {c.year_signed}–{c.final_year - 1} for a contract signed in "
                    f"{c.year_signed} ending {c.final_year}; a final-year drop has no buyout."
                }
            )


class FarmPlayer(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        RELEASED = "released"
        PROMOTED = "promoted"

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="farm")
    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="farm_stints")
    drafted_year = models.PositiveIntegerField()
    salary = models.PositiveIntegerField()
    salary_season = models.PositiveIntegerField(help_text="Season the salary applies to")
    has_mlb_appearance = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)

    def __str__(self):
        return f"{self.player} (farm, {self.team.code})"


class FarmPick(models.Model):
    year = models.PositiveIntegerField()
    round = models.PositiveIntegerField()
    original_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="+")
    owner = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="farm_picks")
    player = models.ForeignKey(
        Player, on_delete=models.PROTECT, null=True, blank=True, related_name="+", help_text="Who the pick was spent on"
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["year", "round", "original_team"], name="unique_farm_pick")]

    def __str__(self):
        return f"{self.year} R{self.round} ({self.original_team.code}) -> {self.owner.code}"


class FinalStanding(models.Model):
    """A team's final place in a season (1 = champion). It sets the order of the next farm draft."""

    season = models.PositiveIntegerField()
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="+")
    place = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(14)])

    class Meta:
        ordering = ["-season", "place"]
        constraints = [
            models.UniqueConstraint(fields=["season", "team"], name="one_place_per_team"),
            models.UniqueConstraint(fields=["season", "place"], name="one_team_per_place"),
        ]

    def __str__(self):
        return f"{self.season}: {self.place}. {self.team.code}"


class CashTrade(models.Model):
    """Auction budget moved between teams, applied to `budget_season`'s auction."""

    budget_season = models.PositiveIntegerField()
    from_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="cash_sent")
    to_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="cash_received")
    amount = models.PositiveIntegerField()
    note = models.CharField(max_length=200, blank=True)
    fantrax_tx_id = models.CharField(max_length=32, blank=True, help_text="Fantrax trade this cash was part of")

    def __str__(self):
        return f"${self.amount} {self.from_team.code} -> {self.to_team.code} ({self.budget_season})"


class Season(models.Model):
    """A league season: from its auction start until just before the next season's auction.

    The farm draft runs in Fantrax before the auction, so every claim from `farm_draft_starts_at`
    until the auction start is a farm pick. The commissioner starts a season on the console
    once its auction has run and been synced; until then the previous season is current.
    """

    year = models.PositiveIntegerField(unique=True)
    farm_draft_starts_at = models.DateTimeField(
        null=True, blank=True, help_text="Claims from here until the auction start are farm draft picks"
    )
    auction_starts_at = models.DateTimeField(help_text="Moves from here on belong to this season")
    started_at = models.DateTimeField(null=True, blank=True)
    started_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["year"]

    def __str__(self):
        return str(self.year)

    def clean(self):
        if self.farm_draft_starts_at and self.auction_starts_at and self.farm_draft_starts_at >= self.auction_starts_at:
            raise ValidationError({"farm_draft_starts_at": "The farm draft starts before the auction"})


class SeasonBudget(models.Model):
    """A team's auction budget, frozen when its season started. For that auction only; never recomputed."""

    season = models.PositiveIntegerField()
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="frozen_budgets")
    base = models.PositiveIntegerField()
    contracts = models.PositiveIntegerField()
    buyouts = models.PositiveIntegerField()
    farm = models.PositiveIntegerField()
    missed_ip = models.PositiveIntegerField()
    cash_net = models.IntegerField()
    remaining = models.IntegerField()
    frozen_at = models.DateTimeField()

    class Meta:
        ordering = ["season", "team"]
        constraints = [models.UniqueConstraint(fields=["season", "team"], name="one_frozen_budget_per_season")]

    def __str__(self):
        return f"{self.team.code} {self.season} auction budget ${self.remaining}"


class FantraxLeague(models.Model):
    """A Fantrax league whose moves the app applies. Renewal makes a new league for the offseason."""

    league_id = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=100, blank=True)
    season = models.PositiveIntegerField(help_text="League season its moves belong to")
    process_since = models.DateTimeField(
        null=True, blank=True, help_text="Ignore moves before this time (the league data already includes them)"
    )
    active = models.BooleanField(default=True, help_text="Read by Sync from Fantrax")

    class Meta:
        ordering = ["season", "pk"]

    def __str__(self):
        return f"{self.name or self.league_id} ({self.season})"


class FantraxEventQuerySet(models.QuerySet):
    def unresolved(self):
        return self.filter(effect=FantraxEvent.Effect.EXCEPTION, resolved_at=None)


class FantraxEvent(models.Model):
    """One Fantrax fact the app processed, stored once under `key`, with what it changed."""

    class Kind(models.TextChoices):
        TRADE = "TRADE", "Trade"
        DROP = "DROP", "Drop"
        CLAIM = "CLAIM", "Claim"
        PROMOTED = "PROMOTED", "Promotion"
        DEBUT = "DEBUT", "MLB debut"
        DEBUT_CHECK = "DEBUT_CHECK", "Possible MLB debut"
        MINORS_UNKNOWN = "MINORS_UNKNOWN", "Minors player not on a farm"
        CASH_COMMENT = "CASH_COMMENT", "Cash in a trade comment"
        ROSTER_MISMATCH = "ROSTER_MISMATCH", "Roster doesn't match"

    class Effect(models.TextChoices):
        CONTRACT_MOVED = "CONTRACT_MOVED", "Contract moved"
        BUYOUT = "BUYOUT", "Buyout"
        VOIDED = "VOIDED", "Contract voided"
        FARM_MOVED = "FARM_MOVED", "Farm moved"
        FARM_RELEASED = "FARM_RELEASED", "Farm released"
        FARM_PROMOTED = "FARM_PROMOTED", "Farm promoted"
        FARM_DEBUT = "FARM_DEBUT", "Farm debut"
        FARM_DRAFTED = "FARM_DRAFTED", "Farm drafted"
        ALREADY_REFLECTED = "ALREADY_REFLECTED", "Already reflected"
        NONE = "NONE", "No change"
        EXCEPTION = "EXCEPTION", "Exception"

    key = models.CharField(max_length=120, unique=True)
    league = models.ForeignKey(FantraxLeague, on_delete=models.PROTECT, related_name="events")
    happened_at = models.DateTimeField()
    kind = models.CharField(max_length=20, choices=Kind.choices)
    effect = models.CharField(max_length=20, choices=Effect.choices)
    fantrax_player_id = models.CharField(max_length=32, blank=True)
    player_name = models.CharField(max_length=100, blank=True)
    # PROTECT: an event is history; deleting what it touched must not erase it.
    player = models.ForeignKey(Player, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    from_team = models.ForeignKey(Team, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    to_team = models.ForeignKey(Team, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    farm_player = models.ForeignKey(FarmPlayer, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    buyout = models.ForeignKey(Buyout, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    detail = models.TextField(blank=True)
    synced_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    resolved_note = models.TextField(blank=True)

    objects = FantraxEventQuerySet.as_manager()

    class Meta:
        ordering = ["-happened_at", "pk"]

    def __str__(self):
        return f"{self.happened_at:%Y-%m-%d} {self.get_kind_display()}: {self.detail}"

    @transaction.atomic
    def resolve(self, user, note: str):
        """Mark an exception handled. The commissioner fixes the data by hand and says what they did."""
        note = note.strip()
        if not note:
            raise ValueError("Say what you did in the note")
        # Re-read under a row lock so a second tab or double submit can't resolve twice.
        current = FantraxEvent.objects.select_for_update().get(pk=self.pk)
        if current.effect != self.Effect.EXCEPTION or current.resolved_at:
            raise ValueError("This isn't an open exception")
        self.resolved_at = timezone.now()
        self.resolved_by = user if user and user.is_authenticated else None
        self.resolved_note = note
        self.save(update_fields=["resolved_at", "resolved_by", "resolved_note"])
        audit(user, "Resolved Fantrax exception", self.detail, team=self.to_team or self.from_team, note=note)


class BudgetAdjustment(models.Model):
    """Commissioner-entered charges, e.g. missed-IP penalties."""

    class Kind(models.TextChoices):
        MISSED_IP = "missed_ip", "Missed IP penalty"

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="adjustments")
    season = models.PositiveIntegerField()
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount = models.PositiveIntegerField()
    note = models.CharField(max_length=200, blank=True)


class RosterEntry(models.Model):
    """A player on a Fantrax roster at the signing blackout: the pool each team signs from.

    `salary` is the player's end-of-season Fantrax salary (his original price if signed). It
    comes from the season-end snapshot, not the blackout one, and is null when that snapshot
    doesn't have him, which makes him unsignable until the commissioner sorts it out.
    """

    season = models.PositiveIntegerField(help_text="The season that just ended")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="roster")
    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="roster_entries")
    salary = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=10, help_text="Fantrax roster slot: Active, Reserve, IR, Minors")

    class Meta:
        ordering = ["team", "player__name"]
        constraints = [models.UniqueConstraint(fields=["season", "player"], name="one_roster_spot_per_season")]

    def __str__(self):
        return f"{self.player} ({self.team.code}, {self.season})"


class SigningPeriod(models.Model):
    """The signing blackout after `season`. Managers edit while it's open; locking applies every team's decisions."""

    class Status(models.TextChoices):
        PLANNED = "planned", "Not open yet"
        OPEN = "open", "Open"
        LOCKED = "locked", "Locked"

    season = models.PositiveIntegerField(unique=True, help_text="The season that just ended")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PLANNED)
    deadline = models.DateTimeField(null=True, blank=True, help_text="Shown to managers; nothing locks automatically")
    opened_at = models.DateTimeField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Signing after {self.season} ({self.get_status_display()})"


class Submission(models.Model):
    """One team's signing decisions. Saved as a draft until the manager submits."""

    class Status(models.TextChoices):
        DRAFT = "draft"
        SUBMITTED = "submitted"

    period = models.ForeignKey(SigningPeriod, on_delete=models.PROTECT, related_name="submissions")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="submissions")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["team"]
        constraints = [models.UniqueConstraint(fields=["period", "team"], name="one_submission_per_team")]

    def __str__(self):
        return f"{self.team.code} signing after {self.period.season} ({self.status})"


class SubmissionSigning(models.Model):
    """A new contract. The price is not stored: it's the player's roster salary, read when needed."""

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="signings")
    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="+")
    length = models.PositiveIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["submission", "player"], name="sign_player_once")]


class SubmissionBuyout(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="buyouts")
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["submission", "contract"], name="buy_out_once")]


class SubmissionFarm(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="farm")
    farm_player = models.ForeignKey(FarmPlayer, on_delete=models.PROTECT, related_name="+")
    keep = models.BooleanField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["submission", "farm_player"], name="decide_farm_once")]


class AuditEntry(models.Model):
    """Who changed league state, when, and why. Written by every signing, console and admin change."""

    at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    team = models.ForeignKey(Team, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=100)
    detail = models.TextField(blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-at", "-pk"]
        verbose_name_plural = "audit entries"

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.user}: {self.action}"


def audit(user, action: str, detail: str = "", team: Team | None = None, note: str = "") -> AuditEntry:
    return AuditEntry.objects.create(
        user=user if user and user.is_authenticated else None, team=team, action=action, detail=detail, note=note
    )
