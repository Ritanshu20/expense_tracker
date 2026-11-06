"""Small helpers that keep the tests short and readable."""

from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User

from expenses.models import Category, Expense, ExpenseParticipant, ExpenseType, PaymentMethod


def create_user(username: str) -> User:
    """Create a user with no usable password, because most tests use force_login."""
    return User.objects.create_user(username=username, email=f"{username}@example.com")


def default_category(name: str = "Food") -> Category:
    """Return one of the default categories created by the migration."""
    return Category.objects.get(name=name, owner=None)


def create_expense(created_by: User, **overrides) -> Expense:
    """Create an expense row with sensible values. Participants are not added."""
    values = {
        "title": "Dinner",
        "amount": Decimal("100.00"),
        "date": date(2026, 11, 1),
        "payment_method": PaymentMethod.CASH,
        "expense_type": ExpenseType.PERSONAL,
    }
    values.update(overrides)
    if "category" not in values:
        values["category"] = default_category()
    return Expense.objects.create(created_by=created_by, **values)


def create_personal_expense(created_by: User, **overrides) -> Expense:
    """Create a personal expense with its single participant row, as the app stores it."""
    expense = create_expense(created_by, expense_type=ExpenseType.PERSONAL, **overrides)
    ExpenseParticipant.objects.create(
        expense=expense,
        user=created_by,
        paid_amount=expense.amount,
        share_amount=expense.amount,
    )
    return expense
