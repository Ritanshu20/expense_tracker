"""
Serializers for the REST API.

What lives here: how expenses, dashboards and reports look as JSON, and checks on API input.
Who uses it: expenses/api_views.py.
What it must never do: repeat business rules. Splits and balances come from services.py, and all
numbers come from selectors.py. Money is sent as strings, so it stays exact.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import (
    MONEY_DECIMAL_PLACES,
    MONEY_MAX_DIGITS,
    ZERO_AMOUNT,
    Category,
    Expense,
    ExpenseParticipant,
    ExpenseType,
    SettlementPayment,
)
from .selectors import find_user_by_username_or_email, get_categories_for_user
from .services import ParticipantRow, SplitType, save_personal_expense, save_shared_expense


def _total_field(**kwargs) -> serializers.DecimalField:
    """Return a field for a total. A total across many expenses can exceed one expense's limit."""
    return serializers.DecimalField(max_digits=None, decimal_places=MONEY_DECIMAL_PLACES, **kwargs)


class ParticipantInputSerializer(serializers.Serializer):
    """One person on a shared expense, as sent by an API client."""

    person = serializers.CharField(max_length=254, help_text="Exact username or email.")
    paid_amount = serializers.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        min_value=ZERO_AMOUNT,
        default=ZERO_AMOUNT,
    )
    share_value = serializers.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        min_value=ZERO_AMOUNT,
        default=ZERO_AMOUNT,
        help_text="An amount for a custom split, a percent for a percentage split, else unused.",
    )

    def validate_person(self, value: str):
        # RULE: no user directory. An exact match is the only way to reach another account.
        user = find_user_by_username_or_email(value)
        if user is None:
            raise serializers.ValidationError("No user found with this exact username or email.")
        return user


class ParticipantSerializer(serializers.ModelSerializer):
    """One person on an expense, as sent to an API client. Emails stay private."""

    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = ExpenseParticipant
        fields = ("username", "paid_amount", "share_amount")


class ExpenseSerializer(serializers.ModelSerializer):
    """An expense. Reading shows its participants, and writing goes through the services."""

    category_name = serializers.CharField(source="category.name", read_only=True)
    created_by = serializers.CharField(source="created_by.username", read_only=True)
    my_share = serializers.SerializerMethodField()
    participants = ParticipantSerializer(many=True, read_only=True)
    split_type = serializers.ChoiceField(
        choices=SplitType.choices, default=SplitType.EQUAL, write_only=True
    )
    participant_rows = ParticipantInputSerializer(many=True, required=False, write_only=True)

    class Meta:
        model = Expense
        fields = (
            "id",
            "title",
            "description",
            "amount",
            "date",
            "category",
            "category_name",
            "payment_method",
            "expense_type",
            "created_by",
            "my_share",
            "participants",
            "split_type",
            "participant_rows",
        )

    def get_fields(self) -> dict:
        fields = super().get_fields()
        user = self.context["request"].user
        # SAFETY: a user can only pick a default category or one of their own. The API docs
        # build this serializer with no logged-in user, so they get an empty list.
        fields["category"].queryset = (
            get_categories_for_user(user) if user.is_authenticated else Category.objects.none()
        )
        return fields

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, allow_null=True
        )
    )
    def get_my_share(self, expense: Expense) -> str | None:
        """Return the requesting user's share as a string, or None if they do not take part."""
        for participant in expense.participants.all():
            if participant.user_id == self.context["request"].user.pk:
                # RULE: never a Decimal here. The JSON encoder would turn it into a float.
                return str(participant.share_amount)
        return None

    def create(self, validated_data: dict) -> Expense:
        return self._save(Expense(), validated_data)

    def update(self, instance: Expense, validated_data: dict) -> Expense:
        return self._save(instance, validated_data)

    def _save(self, expense: Expense, validated_data: dict) -> Expense:
        """Save through the same services as the website, so the money rules are the same."""
        user = self.context["request"].user
        split_type = validated_data.pop("split_type")
        rows = validated_data.pop("participant_rows", [])
        for field, value in validated_data.items():
            setattr(expense, field, value)
        try:
            if expense.expense_type == ExpenseType.PERSONAL:
                return save_personal_expense(expense, user)
            participant_rows = [
                ParticipantRow(row["person"], row["paid_amount"], row["share_value"])
                for row in rows
            ]
            return save_shared_expense(expense, user, split_type, participant_rows)
        except DjangoValidationError as error:
            raise serializers.ValidationError({"non_field_errors": error.messages}) from error


class ExpenseSummarySerializer(serializers.ModelSerializer):
    """A short line about an expense, for the dashboard's recent list."""

    category_name = serializers.CharField(source="category.name", read_only=True)
    my_share = serializers.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, allow_null=True
    )

    class Meta:
        model = Expense
        fields = ("id", "title", "date", "amount", "category_name", "my_share")


class TotalRowSerializer(serializers.Serializer):
    """One line of a breakdown: a category, a payment method or a person."""

    label = serializers.CharField()
    total = _total_field()


class SettlementSerializer(serializers.Serializer):
    """One debt: from_user owes to_user this amount."""

    from_user = serializers.CharField()
    to_user = serializers.CharField()
    amount = _total_field()


class SettlementSummarySerializer(serializers.Serializer):
    """Who the user owes and who owes the user, after recorded payments."""

    you_owe = SettlementSerializer(many=True)
    owed_to_you = SettlementSerializer(many=True)
    total_you_owe = _total_field()
    total_owed_to_you = _total_field()


class BudgetStatusSerializer(serializers.Serializer):
    """How much of one monthly budget has been used."""

    year = serializers.IntegerField()
    month = serializers.IntegerField()
    amount = _total_field()
    spent = _total_field(source="usage.spent")
    remaining = _total_field(source="usage.remaining")
    usage_percent = _total_field(source="usage.usage_percent")
    is_warning = serializers.BooleanField(source="usage.is_warning")
    is_over = serializers.BooleanField(source="usage.is_over")


class SpendingSummarySerializer(serializers.Serializer):
    """My spending for the main periods. Paid by me is separate and is never mixed in."""

    total = _total_field()
    paid_by_me = _total_field()
    today = _total_field()
    this_week = _total_field()
    this_month = _total_field()
    personal = _total_field()
    shared = _total_field()


class DashboardSerializer(serializers.Serializer):
    """Everything the dashboard page shows."""

    summary = SpendingSummarySerializer()
    payment_method_totals = TotalRowSerializer(many=True)
    category_totals = TotalRowSerializer(many=True)
    person_totals = TotalRowSerializer(many=True)
    recent_expenses = ExpenseSummarySerializer(many=True)
    settlements = SettlementSummarySerializer()
    budget = BudgetStatusSerializer(allow_null=True)


class ReportSummarySerializer(serializers.Serializer):
    """My spending in one period."""

    total = _total_field()
    paid_by_me = _total_field()
    personal = _total_field()
    shared = _total_field()


class MonthlyReportQuerySerializer(serializers.Serializer):
    """The optional query parameter of the monthly report."""

    date = serializers.DateField(required=False, help_text="Any day in the month. Default: today.")


class MonthlyReportSerializer(serializers.Serializer):
    """Everything a monthly report shows."""

    start = serializers.DateField()
    end = serializers.DateField()
    summary = ReportSummarySerializer()
    expense_count = serializers.IntegerField()
    category_totals = TotalRowSerializer(many=True)
    payment_method_totals = TotalRowSerializer(many=True)
    person_totals = TotalRowSerializer(many=True)
    settlements = SettlementSummarySerializer()


class SettlementPaymentSerializer(serializers.ModelSerializer):
    """A recorded payment, as sent to an API client."""

    from_user = serializers.CharField(source="from_user.username", read_only=True)
    to_user = serializers.CharField(source="to_user.username", read_only=True)
    created_by = serializers.CharField(source="created_by.username", read_only=True)

    class Meta:
        model = SettlementPayment
        fields = ("id", "from_user", "to_user", "amount", "date", "note", "created_by")


class SettlementsSerializer(serializers.Serializer):
    """Who owes whom, plus the payments behind those numbers."""

    settlements = SettlementSummarySerializer()
    payments = SettlementPaymentSerializer(many=True)
