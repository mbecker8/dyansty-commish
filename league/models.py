"""League state: franchises, players, contracts, buyouts, farm, picks and budget ledger.

Budget math lives in the pure `rules` package; models convert to rules objects.
"""

from django.db import models, transaction

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
        max_length=32, unique=True, null=True, blank=True, help_text="Discord user ID (numeric), not the username"
    )
    discord_username = models.CharField(max_length=100, blank=True, help_text="Last seen at sign-in")
    user = models.OneToOneField("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="manager")

    class Meta:
        ordering = ["team", "name"]

    def __str__(self):
        return f"{self.name} ({self.team.code})"


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
    sign_and_trade = models.BooleanField(default=False, help_text="Counts under the sign-and-trade exception")
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

    class Meta:
        constraints = [models.UniqueConstraint(fields=["year", "round", "original_team"], name="unique_farm_pick")]

    def __str__(self):
        return f"{self.year} R{self.round} ({self.original_team.code}) -> {self.owner.code}"


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


class BudgetAdjustment(models.Model):
    """Commissioner-entered charges, e.g. missed-IP penalties."""

    class Kind(models.TextChoices):
        MISSED_IP = "missed_ip", "Missed IP penalty"

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="adjustments")
    season = models.PositiveIntegerField()
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount = models.PositiveIntegerField()
    note = models.CharField(max_length=200, blank=True)


class ReconciliationItem(models.Model):
    """Something the season's Fantrax moves imply for a contract or farm player, awaiting the commissioner."""

    class Kind(models.TextChoices):
        CONTINUES = "continues"
        EXPIRING = "expiring"
        TRADED = "traded", "Traded (contract moves)"
        DROPPED = "dropped", "Dropped (buyout owed)"
        DROPPED_FREE = "dropped_free", "Dropped in final year (no penalty)"
        INCONSISTENT = "inconsistent", "Needs a look"
        FARM_CONTINUES = "farm_continues", "Farm: still on the farm"
        FARM_TRADED = "farm_traded", "Farm: traded"
        FARM_PROMOTED = "farm_promoted", "Farm: promoted (can't return)"
        FARM_RELEASED = "farm_released", "Farm: released"
        FARM_INCONSISTENT = "farm_inconsistent", "Farm: needs a look"
        FARM_UNKNOWN = "farm_unknown", "Farm: in Fantrax minors but not on the sheet"
        CASH_COMMENT = "cash_comment", "Trade comment mentioning cash (enter a CashTrade by hand)"

    class Status(models.TextChoices):
        PENDING = "pending"
        ACCEPTED = "accepted"
        REJECTED = "rejected"

    season = models.PositiveIntegerField()
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # PROTECT: a decision is history; deleting what it refers to must not erase it.
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    farm_player = models.ForeignKey(FarmPlayer, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    player = models.ForeignKey(Player, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    mlb_debut = models.BooleanField(default=False, help_text="Farm player appeared in MLB this season")
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="New holder (trade/promotion) or team owing the buyout (drop)",
    )
    detail = models.TextField(blank=True)
    fantrax_tx_ids = models.CharField(max_length=500, blank=True)
    start_team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Team the replay started from (holder before the season's moves)",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_note = models.TextField(blank=True)

    class Meta:
        ordering = ["kind", "player__name"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.contract or self.farm_player or self.player or self.detail}"

    def _lock_pending(self):
        """Re-read status under a row lock so a stale copy (second tab, double submit) can't decide twice."""
        status = ReconciliationItem.objects.select_for_update().values_list("status", flat=True).get(pk=self.pk)
        if status != self.Status.PENDING:
            raise ValueError(f"Item {self.pk} is already {status}")

    @transaction.atomic
    def accept(self, note: str = ""):
        """Apply the proposed change, all or nothing."""
        self._lock_pending()
        if self.kind == self.Kind.DROPPED:
            self.contract.team = self.team  # the team holding him when he was dropped
            self.contract.save(update_fields=["team"])
            Buyout.objects.create(
                contract=self.contract, team=self.team, dropped_in_season=self.season, note=self.detail
            )
        elif self.kind == self.Kind.DROPPED_FREE:
            self.contract.voided_in_season = self.season
            self.contract.save(update_fields=["voided_in_season"])
        elif self.kind == self.Kind.TRADED:
            self.contract.team = self.team
            self.contract.save(update_fields=["team"])
        elif self.kind == self.Kind.FARM_UNKNOWN:
            # A farm draft pick the sheet missed.
            self.farm_player = FarmPlayer.objects.create(
                team=self.team, player=self.player, drafted_year=self.season, salary=1, salary_season=self.season
            )
        elif self.kind in (self.Kind.INCONSISTENT, self.Kind.FARM_INCONSISTENT, self.Kind.CASH_COMMENT):
            raise ValueError("Fix this by hand, write what you did in the note, then reject it")
        elif self.farm_player_id:
            farm = self.farm_player
            if self.mlb_debut:
                farm.has_mlb_appearance = True
            if self.kind in (self.Kind.FARM_TRADED, self.Kind.FARM_PROMOTED):
                farm.team = self.team
            if self.kind == self.Kind.FARM_PROMOTED:
                farm.status = FarmPlayer.Status.PROMOTED
            elif self.kind == self.Kind.FARM_RELEASED:
                farm.status = FarmPlayer.Status.RELEASED
            farm.save()
        self.status = self.Status.ACCEPTED
        self.decided_note = note
        self.save(update_fields=["status", "decided_note", "farm_player"])

    @transaction.atomic
    def reject(self, note: str):
        self._lock_pending()
        self.status = self.Status.REJECTED
        self.decided_note = note
        self.save(update_fields=["status", "decided_note"])
