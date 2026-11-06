"""
Read queries for expenses.

What lives here: the queries that pages use to read data.
Who calls it: views and forms.
What it must never do: change data, or return rows the given user may not see.
"""

import calendar
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from itertools import groupby
from operator import itemgetter
from typing import Any, NamedTuple

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import User
from django.db.models import Count, F, OuterRef, Q, QuerySet, Subquery, Sum
from django.db.models.functions import ExtractMonth, ExtractYear, Lower

from .models import (
    ZERO_AMOUNT,
    Budget,
    Category,
    Expense,
    ExpenseParticipant,
    ExpenseType,
    PaymentMethod,
    SettlementPayment,
)
from .services import Settlement, calculate_budget_usage, calculate_pairwise_debts

RECENT_EXPENSE_COUNT = 5
DAYS_IN_WEEK = 7
LAST_MONTH_OF_YEAR = 12
LAST_DAY_OF_YEAR = 31


class DateRange(NamedTuple):
    """A period of local dates. Both days are included."""

    start: date
    end: date


class SettlementSummary(NamedTuple):
    """Who the user owes and who owes the user, netted per person."""

    you_owe: list[Settlement]
    owed_to_you: list[Settlement]
    total_you_owe: Decimal
    total_owed_to_you: Decimal


def get_day_range(day: date) -> DateRange:
    """Return one day as a range."""
    return DateRange(day, day)


def get_week_range(day: date) -> DateRange:
    """
    Return the week containing a day.

    Why: RULE: a week runs from Monday to Sunday, and both days count.

    Example:
        Wednesday 11 Nov 2026 gives Monday 9 Nov to Sunday 15 Nov.
    """
    monday = day - timedelta(days=day.weekday())
    return DateRange(monday, monday + timedelta(days=DAYS_IN_WEEK - 1))


def get_month_range(day: date) -> DateRange:
    """Return the calendar month containing a day, from its first day to its last."""
    last_day = calendar.monthrange(day.year, day.month)[1]
    return DateRange(day.replace(day=1), day.replace(day=last_day))


def get_year_range(day: date) -> DateRange:
    """Return the calendar year containing a day, from 1 Jan to 31 Dec."""
    return DateRange(
        date(day.year, 1, 1),
        date(day.year, LAST_MONTH_OF_YEAR, LAST_DAY_OF_YEAR),
    )


def get_categories_for_user(user: AbstractBaseUser) -> QuerySet[Category]:
    """
    Return the categories a user can see, sorted by name without regard to letter case.

    Why: RULE: a user sees the default categories plus their own, never anyone else's.

    Args:
        user: The logged-in user.

    Returns:
        A queryset of the default categories and the user's own.
    """
    return Category.objects.visible_to(user).order_by(Lower("name"))


def get_expenses_for_user(user: AbstractBaseUser) -> QuerySet[Expense]:
    """
    Return the expenses a user may view, newest first, ready for display.

    Why: RULE: a user sees the expenses they created or take part in, never anyone else's.
    The related rows are loaded in one query, so a list page does not run one query per row.

    Args:
        user: The logged-in user.

    Returns:
        A queryset of expenses with their category and creator loaded. Each has my_share, the
        user's own share, which is None if they created the expense but do not take part in it.
    """
    my_row = ExpenseParticipant.objects.filter(expense=OuterRef("pk"), user=user)
    return (
        Expense.objects.visible_to(user)
        .select_related("category", "created_by")
        .annotate(my_share=Subquery(my_row.values("share_amount")[:1]))
    )


def find_user_by_username_or_email(identifier: str) -> User | None:
    """
    Find the one active user whose username or email exactly matches, ignoring letter case.

    Why: RULE: there is no user directory. Participants are added by exact match only, so
    nobody can browse or search the list of accounts.

    Args:
        identifier: The username or email that was typed.

    Returns:
        The user, or None if nobody matches or the match is ambiguous.

    Example:
        "RAHUL@example.com" finds rahul@example.com, but "rah" finds nobody.
    """
    matches = list(
        User.objects.filter(
            Q(username__iexact=identifier) | Q(email__iexact=identifier), is_active=True
        )[:2]
    )
    return matches[0] if len(matches) == 1 else None


def filter_expenses(expenses: QuerySet[Expense], filters: Mapping[str, Any]) -> QuerySet[Expense]:
    """
    Narrow a queryset of expenses by search text and filters. Empty values are ignored.

    Why: the list page and the exports must apply the same filters in the same way.

    Args:
        expenses: The expenses to narrow. The caller has already limited them to one user.
        filters: The cleaned data of ExpenseFilterForm.

    Returns:
        The narrowed queryset. Both dates are inclusive.

    Example:
        {"q": "dinner", "date_from": date(2026, 11, 1)} keeps expenses from 1 Nov onwards
        with "dinner" in the title or description, in any letter case.
    """
    search_text = filters.get("q")
    if search_text:
        expenses = expenses.filter(
            Q(title__icontains=search_text) | Q(description__icontains=search_text)
        )
    lookups = {
        "date__gte": filters.get("date_from"),
        "date__lte": filters.get("date_to"),
        "category": filters.get("category"),
        "payment_method": filters.get("payment_method"),
        "expense_type": filters.get("expense_type"),
    }
    active_lookups = {lookup: value for lookup, value in lookups.items() if value}
    return expenses.filter(**active_lookups)


def get_expenses_for_export(
    user: AbstractBaseUser, filters: Mapping[str, Any]
) -> QuerySet[Expense]:
    """
    Return the expenses to put in an export file, using the same filters as the list page.

    Why: RULE: an export contains exactly what the user can see on the list, so it can never
    include someone else's data.

    Args:
        user: The logged-in user.
        filters: The cleaned data of ExpenseFilterForm.

    Returns:
        The filtered expenses. Each has my_share and my_paid, which are None if the user
        created the expense but does not take part in it.
    """
    my_row = ExpenseParticipant.objects.filter(expense=OuterRef("pk"), user=user)
    return filter_expenses(get_expenses_for_user(user), filters).annotate(
        my_paid=Subquery(my_row.values("paid_amount")[:1])
    )


def _within_range(
    rows: QuerySet[ExpenseParticipant], date_range: DateRange | None
) -> QuerySet[ExpenseParticipant]:
    """Keep the participant rows whose expense date is in the range. None means all dates."""
    if date_range is None:
        return rows
    return rows.filter(expense__date__range=date_range)


def _my_rows(
    user: AbstractBaseUser, date_range: DateRange | None = None
) -> QuerySet[ExpenseParticipant]:
    """
    Return the user's own participant rows.

    Why: RULE: only expenses where I am a participant count for me. A shared expense that I
    created without taking part in it has no row here, so it adds nothing to my spending.
    """
    return _within_range(ExpenseParticipant.objects.filter(user=user), date_range)


def _spending_by(rows: QuerySet[ExpenseParticipant], field: str) -> list[dict[str, Any]]:
    """Group rows by a field and add up the shares, largest first. Zero totals are left out."""
    return list(
        rows.values(label=F(field))
        .annotate(total=Sum("share_amount"))
        .filter(total__gt=0)
        .order_by("-total", "label")
    )


def get_spending_summary(user: AbstractBaseUser, today: date) -> dict[str, Decimal]:
    """
    Return a user's main spending totals, all from one query.

    Why: RULE: "my spending" is the sum of my share_amount, which is what I consumed.
    "Paid by me" is the sum of my paid_amount, which is the cash that left my pocket. They
    are separate numbers and are never mixed.

    Args:
        user: The logged-in user.
        today: The local date that "today", "this week" and "this month" are based on.

    Returns:
        A dict with total, paid_by_me, today, this_week, this_month, personal and shared.
        A total with no rows is 0.00.

    Example:
        Paying 1500 for a dinner where my share is 1000 adds 1000 to total and 1500 to
        paid_by_me.
    """
    totals = _my_rows(user).aggregate(
        total=Sum("share_amount"),
        paid_by_me=Sum("paid_amount"),
        today=Sum("share_amount", filter=Q(expense__date=today)),
        this_week=Sum("share_amount", filter=Q(expense__date__range=get_week_range(today))),
        this_month=Sum("share_amount", filter=Q(expense__date__range=get_month_range(today))),
        personal=Sum("share_amount", filter=Q(expense__expense_type=ExpenseType.PERSONAL)),
        shared=Sum("share_amount", filter=Q(expense__expense_type=ExpenseType.SHARED)),
    )
    return {name: value or ZERO_AMOUNT for name, value in totals.items()}


def get_category_totals(
    user: AbstractBaseUser, date_range: DateRange | None = None
) -> list[dict[str, Any]]:
    """Return my spending per category, largest first, as dicts with label and total."""
    return _spending_by(_my_rows(user, date_range), "expense__category__name")


def get_payment_method_totals(
    user: AbstractBaseUser, date_range: DateRange | None = None
) -> list[dict[str, Any]]:
    """Return my spending per payment method, largest first, with readable labels."""
    totals = _spending_by(_my_rows(user, date_range), "expense__payment_method")
    return [{"label": PaymentMethod(row["label"]).label, "total": row["total"]} for row in totals]


def get_person_totals(
    user: AbstractBaseUser, date_range: DateRange | None = None
) -> list[dict[str, Any]]:
    """
    Return what each person consumed in the expenses I take part in, largest first.

    Why: RULE: person totals use share_amount, and only expenses where I am a participant
    count. So my own line always equals my spending, and an expense I am not part of adds
    nothing to anyone.

    Example:
        A dinner of 3000 shared equally by Rahul, Aman and Rohit gives each of them 1000.
    """
    rows = ExpenseParticipant.objects.filter(expense__participants__user=user)
    return _spending_by(_within_range(rows, date_range), "user__username")


def _shared_rows_per_expense(
    user: AbstractBaseUser, date_range: DateRange | None
) -> list[list[tuple[str, Decimal, Decimal]]]:
    """Return (username, paid, share) rows, one list per shared expense the user takes part in."""
    rows = ExpenseParticipant.objects.filter(
        expense__expense_type=ExpenseType.SHARED, expense__participants__user=user
    )
    rows = (
        _within_range(rows, date_range)
        .order_by("expense_id", "id")
        .values_list("expense_id", "user__username", "paid_amount", "share_amount")
    )
    expenses = []
    for _, expense_rows in groupby(rows, key=itemgetter(0)):
        expenses.append([row[1:] for row in expense_rows])
    return expenses


def get_payments_for_user(user: AbstractBaseUser) -> QuerySet[SettlementPayment]:
    """
    Return the settlement payments a user may view, newest first, ready for display.

    Why: RULE: both people on a payment can view it, and nobody else can.

    Args:
        user: The logged-in user.

    Returns:
        A queryset of payments with the people on them loaded.
    """
    return SettlementPayment.objects.involving(user).select_related(
        "from_user", "to_user", "created_by"
    )


def _payment_rows(
    user: AbstractBaseUser, date_range: DateRange | None
) -> list[tuple[str, str, Decimal]]:
    """Return (from username, to username, amount) for the user's payments in the period."""
    payments = SettlementPayment.objects.involving(user)
    if date_range is not None:
        payments = payments.filter(date__range=date_range)
    return list(payments.values_list("from_user__username", "to_user__username", "amount"))


def get_settlement_summary(
    user: AbstractBaseUser, date_range: DateRange | None = None
) -> SettlementSummary:
    """
    Return who the user owes and who owes the user, after the payments already recorded.

    Why: the balance between two people comes only from expenses they shared, minus what
    they have paid each other. Debts between other people are not this user's business, so
    they are left out.

    Args:
        user: The logged-in user.
        date_range: Only count expenses and payments in this period. None means all dates.

    Returns:
        A SettlementSummary, netted per person.

    Example:
        After the dinner of 3000 paid 1500/1000/500, Rahul sees that Rohit owes him 500.
        Once Rohit records a payment of 200, Rahul sees that Rohit owes him 300.
    """
    debts = calculate_pairwise_debts(
        _shared_rows_per_expense(user, date_range), _payment_rows(user, date_range)
    )
    you_owe = [debt for debt in debts if debt.from_user == user.username]
    owed_to_you = [debt for debt in debts if debt.to_user == user.username]
    return SettlementSummary(
        you_owe=you_owe,
        owed_to_you=owed_to_you,
        total_you_owe=sum((debt.amount for debt in you_owe), ZERO_AMOUNT),
        total_owed_to_you=sum((debt.amount for debt in owed_to_you), ZERO_AMOUNT),
    )


def get_report_data(user: AbstractBaseUser, date_range: DateRange) -> dict[str, Any]:
    """
    Collect everything a report shows for one period.

    Why: RULE: a report uses the same definitions as the dashboard. My spending is my share,
    paid by me is my cash, and only expenses where I am a participant count. Both ends of the
    period are included.

    Args:
        user: The logged-in user.
        date_range: The period to report on.

    Returns:
        A dict with the totals for the period, its breakdowns and its settlement summary.

    Example:
        A report for November counts expenses dated 1 Nov and 30 Nov, but not 31 Oct or 1 Dec.
    """
    totals = _my_rows(user, date_range).aggregate(
        total=Sum("share_amount"),
        paid_by_me=Sum("paid_amount"),
        personal=Sum("share_amount", filter=Q(expense__expense_type=ExpenseType.PERSONAL)),
        shared=Sum("share_amount", filter=Q(expense__expense_type=ExpenseType.SHARED)),
        expense_count=Count("id"),
    )
    expense_count = totals.pop("expense_count")
    return {
        "summary": {name: value or ZERO_AMOUNT for name, value in totals.items()},
        "expense_count": expense_count,
        "category_totals": get_category_totals(user, date_range),
        "payment_method_totals": get_payment_method_totals(user, date_range),
        "person_totals": get_person_totals(user, date_range),
        "settlements": get_settlement_summary(user, date_range),
    }


def _attach_usage(budget: Budget, spent: Decimal) -> Budget:
    """Add usage, a BudgetUsage, to a budget."""
    budget.usage = calculate_budget_usage(
        budget.amount, spent, settings.BUDGET_WARNING_THRESHOLD_PERCENT
    )
    return budget


def _spending_per_month(user: AbstractBaseUser) -> dict[tuple[int, int], Decimal]:
    """Return my spending for every month that has any, as {(year, month): total}."""
    rows = (
        _my_rows(user)
        .annotate(year=ExtractYear("expense__date"), month=ExtractMonth("expense__date"))
        .values("year", "month")
        .annotate(total=Sum("share_amount"))
    )
    return {(row["year"], row["month"]): row["total"] for row in rows}


def get_budgets_for_user(user: AbstractBaseUser) -> list[Budget]:
    """
    Return a user's budgets, newest month first, each with its usage.

    Why: spending is added up once per month in a single query. A subquery per budget would
    scan all of the user's rows again for every budget, which measured about 5 times slower
    with 12 budgets and 20,000 rows, even though it is one query less.

    Args:
        user: The logged-in user.

    Returns:
        A list of budgets. Each has usage, a BudgetUsage.
    """
    budgets = list(Budget.objects.filter(user=user))
    spent_per_month = _spending_per_month(user)
    return [
        _attach_usage(budget, spent_per_month.get((budget.year, budget.month), ZERO_AMOUNT))
        for budget in budgets
    ]


def get_budget_for_month(user: AbstractBaseUser, year: int, month: int) -> Budget | None:
    """Return a user's budget for one month with its usage, or None if they did not set one."""
    budget = Budget.objects.filter(user=user, year=year, month=month).first()
    if budget is None:
        return None
    month_range = get_month_range(date(year, month, 1))
    spent = _my_rows(user, month_range).aggregate(total=Sum("share_amount"))["total"]
    return _attach_usage(budget, spent or ZERO_AMOUNT)


def get_dashboard_data(user: AbstractBaseUser, today: date) -> dict[str, Any]:
    """
    Collect everything the dashboard shows.

    Args:
        user: The logged-in user.
        today: The local date that the periods are based on.

    Returns:
        A dict that can be passed directly to the dashboard template.
    """
    return {
        "summary": get_spending_summary(user, today),
        "payment_method_totals": get_payment_method_totals(user),
        "category_totals": get_category_totals(user),
        "person_totals": get_person_totals(user),
        "recent_expenses": get_expenses_for_user(user)[:RECENT_EXPENSE_COUNT],
        "settlements": get_settlement_summary(user),
        "budget": get_budget_for_month(user, today.year, today.month),
    }
