"""League state: franchises, players, contracts, buyouts, farm, picks and budget ledger.

Budget math lives in the pure `rules` package; models convert to rules objects.
"""

from django.db import models

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


class Player(models.Model):
    name = models.CharField(max_length=100)
    fantrax_id = models.CharField(max_length=16, unique=True, null=True, blank=True)
    positions = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Contract(models.Model):
    """A multi-year contract. `team` is the current holder; a trade moves it."""

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="contracts")
    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="contracts")
    original_price = models.PositiveIntegerField()
    year_signed = models.PositiveIntegerField(help_text="League season that had just ended when signed")
    length = models.PositiveIntegerField()
    sign_and_trade = models.BooleanField(default=False, help_text="Counts under the sign-and-trade exception")
    note = models.TextField(blank=True)

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
