"""
Database models for expenses.

What lives here: Category, Expense, ExpenseParticipant and Budget, plus the rules the
database itself must enforce (positive amounts, no duplicates).
Who uses it: services, selectors, forms and the admin.
What it must never do: hold split or settlement logic. That belongs in services.py.
"""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.functions import Lower

MONEY_MAX_DIGITS = 12
MONEY_DECIMAL_PLACES = 2
SMALLEST_POSITIVE_AMOUNT = Decimal("0.01")
ZERO_AMOUNT = Decimal("0.00")
FIRST_MONTH = 1
LAST_MONTH = 12


class TimeStampedModel(models.Model):
    """Adds created_at and updated_at to a model."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class PaymentMethod(models.TextChoices):
    CASH = "cash", "Cash"
    UPI = "upi", "UPI"
    DEBIT_CARD = "debit_card", "Debit card"
    CREDIT_CARD = "credit_card", "Credit card"
    BANK_TRANSFER = "bank_transfer", "Bank transfer"
    OTHER = "other", "Other"


class ExpenseType(models.TextChoices):
    PERSONAL = "personal", "Personal"
    SHARED = "shared", "Shared"


class CategoryQuerySet(models.QuerySet):
    """Reusable category filters, so the visibility rule is written in only one place."""

    def visible_to(self, user: AbstractBaseUser) -> "CategoryQuerySet":
        """Return the default categories plus the user's own."""
        return self.filter(Q(owner=None) | Q(owner=user))

    def owned_by(self, user: AbstractBaseUser) -> "CategoryQuerySet":
        """Return only the user's own categories, which are the ones they may change."""
        return self.filter(owner=user)


class Category(TimeStampedModel):
    """A spending category. Default categories have no owner and are visible to everyone."""

    name = models.CharField(max_length=100)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="categories",
    )

    objects = CategoryQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"
        constraints = [
            # RULE: "Food" and "food" are the same category for one owner. PostgreSQL compares
            # text case-sensitively, so the constraint uses Lower(name). nulls_distinct=False
            # also covers default categories, because by default NULL owners never conflict.
            models.UniqueConstraint(
                "owner",
                Lower("name"),
                nulls_distinct=False,
                name="unique_category_name_per_owner",
                violation_error_message="A category with this name already exists.",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class ExpenseQuerySet(models.QuerySet):
    """Reusable expense filters, so the permission rule is written in only one place."""

    def visible_to(self, user: AbstractBaseUser) -> "ExpenseQuerySet":
        """Return the expenses a user may view: the ones they created or take part in."""
        # SAFETY: this is the data isolation rule for expenses. Exists is used instead of a
        # join, because a join would return one row per participant.
        participation = ExpenseParticipant.objects.filter(expense=OuterRef("pk"), user=user)
        return self.filter(Q(created_by=user) | Exists(participation))


class Expense(TimeStampedModel):
    """One bill. Who paid and who consumed what is stored in ExpenseParticipant."""

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        validators=[MinValueValidator(SMALLEST_POSITIVE_AMOUNT)],
    )
    date = models.DateField()
    # RULE: a category in use cannot be deleted, so old expenses never lose their category.
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="expenses")
    payment_method = models.CharField(max_length=20, choices=PaymentMethod)
    expense_type = models.CharField(max_length=20, choices=ExpenseType)
    # RULE: deleting a user must not silently change other people's shared balances.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_expenses"
    )

    objects = ExpenseQuerySet.as_manager()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="expense_amount_greater_than_zero",
            ),
        ]
        # The category column needs no entry here: Django indexes foreign keys automatically.
        indexes = [
            models.Index(fields=["created_by", "date"], name="expense_creator_date_idx"),
            # The expense list sorts by these. On 50,000 expenses the page went from about
            # 140 ms to about 60 ms.
            models.Index(fields=["-date", "-id"], name="expense_newest_first_idx"),
            models.Index(fields=["payment_method"], name="expense_payment_method_idx"),
            models.Index(fields=["expense_type"], name="expense_type_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.amount})"


class ExpenseParticipant(TimeStampedModel):
    """
    One person's part in an expense: what they paid and what they consumed.

    RULE: a personal expense has exactly one row, for its creator, with
    paid_amount = share_amount = amount. Reports can then use the same query for both types.
    """

    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="expense_participations"
    )
    paid_amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        default=ZERO_AMOUNT,
        validators=[MinValueValidator(ZERO_AMOUNT)],
    )
    share_amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        default=ZERO_AMOUNT,
        validators=[MinValueValidator(ZERO_AMOUNT)],
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["expense", "user"],
                name="unique_participant_per_expense",
            ),
            models.CheckConstraint(
                condition=Q(paid_amount__gte=0),
                name="participant_paid_amount_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(share_amount__gte=0),
                name="participant_share_amount_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} in {self.expense}"


class Budget(TimeStampedModel):
    """A user's spending limit for one calendar month."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="budgets"
    )
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(FIRST_MONTH), MaxValueValidator(LAST_MONTH)]
    )
    amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        validators=[MinValueValidator(SMALLEST_POSITIVE_AMOUNT)],
    )

    class Meta:
        ordering = ["-year", "-month"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "year", "month"],
                name="unique_budget_per_user_and_month",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="budget_amount_greater_than_zero",
            ),
            models.CheckConstraint(
                condition=Q(month__gte=FIRST_MONTH, month__lte=LAST_MONTH),
                name="budget_month_between_1_and_12",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} {self.year}-{self.month:02d}: {self.amount}"


class SettlementPaymentQuerySet(models.QuerySet):
    """Reusable payment filters, so the permission rule is written in only one place."""

    def involving(self, user: AbstractBaseUser) -> "SettlementPaymentQuerySet":
        """Return the payments a user may view: the ones they made or received."""
        return self.filter(Q(from_user=user) | Q(to_user=user))


class SettlementPayment(TimeStampedModel):
    """
    Real money that one person paid another to settle up.

    It lowers what the payer owes. It is not spending, so it never changes "my spending",
    budgets or category totals.
    """

    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments_made"
    )
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments_received"
    )
    amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        validators=[MinValueValidator(SMALLEST_POSITIVE_AMOUNT)],
    )
    date = models.DateField()
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments_recorded"
    )

    objects = SettlementPaymentQuerySet.as_manager()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="settlement_payment_amount_greater_than_zero",
            ),
            models.CheckConstraint(
                condition=~Q(from_user=F("to_user")),
                name="settlement_payment_between_different_users",
            ),
            # SAFETY: nobody can record a payment between two other people.
            models.CheckConstraint(
                condition=Q(created_by=F("from_user")) | Q(created_by=F("to_user")),
                name="settlement_payment_created_by_a_party",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.from_user} paid {self.to_user} {self.amount}"
