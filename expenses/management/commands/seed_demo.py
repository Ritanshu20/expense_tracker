"""
Creates demo data for trying the app and for the 5-minute demo.

What lives here: the seed_demo management command. It saves through the same services as the app.
Who calls it: a developer, with python manage.py seed_demo.
What it must never do: run in production, or create anything twice. Running it again changes
nothing, unless --password asks for a new password.
"""

import secrets
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from expenses.models import Budget, Category, Expense, PaymentMethod
from expenses.services import (
    ParticipantRow,
    SplitType,
    save_personal_expense,
    save_shared_expense,
)

DEMO_USERNAMES = ("Rahul", "Aman", "Rohit")
DEMO_BUDGET_AMOUNT = Decimal("5000.00")
ZERO_AMOUNT = Decimal("0.00")


def _recent_day(today: date, days_ago: int) -> date:
    """Return a recent day that is still in this month, so it counts on the dashboard."""
    return max(today - timedelta(days=days_ago), today.replace(day=1))


class Command(BaseCommand):
    help = "Creates demo users, expenses, a custom category and a budget. Safe to run twice."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--password",
            help="Password for the demo users. Default: a random one, printed once when created.",
        )

    def handle(self, *args, **options) -> None:
        # SAFETY: demo accounts have a known password, so they must never reach a real site.
        if not settings.DEBUG:
            raise CommandError("seed_demo only runs with DEBUG on.")
        users = self._create_users(options["password"])
        self._create_data(users["Rahul"], users["Aman"], users["Rohit"], timezone.localdate())
        self.stdout.write(self.style.SUCCESS("Demo data is ready. Log in as Rahul."))

    def _create_users(self, password: str | None) -> dict[str, User]:
        """Create the demo users if needed. Only a given or a new user changes a password."""
        new_password = password or secrets.token_urlsafe(12)
        users = {}
        created_any = False
        for username in DEMO_USERNAMES:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"first_name": username, "email": f"{username.lower()}@example.com"},
            )
            if created or password:
                user.set_password(new_password)
                user.save()
            created_any = created_any or created
            users[username] = user
        if created_any and not password:
            self.stdout.write(f"Password for the new demo users: {new_password}")
        return users

    def _create_data(self, rahul: User, aman: User, rohit: User, today: date) -> None:
        office, _ = Category.objects.get_or_create(name="Office", owner=rahul)
        self._add_personal_expense(
            rahul,
            title="Groceries for the week",
            amount=Decimal("1200.00"),
            day=_recent_day(today, 2),
            category=Category.objects.get(name="Groceries", owner=None),
            payment_method=PaymentMethod.UPI,
        )
        self._add_personal_expense(
            rahul,
            title="Office lunch",
            amount=Decimal("350.00"),
            day=_recent_day(today, 1),
            category=office,
            payment_method=PaymentMethod.CASH,
        )
        self._add_dinner(rahul, aman, rohit, today)
        Budget.objects.get_or_create(
            user=rahul,
            year=today.year,
            month=today.month,
            defaults={"amount": DEMO_BUDGET_AMOUNT},
        )

    def _add_personal_expense(
        self,
        owner: User,
        title: str,
        amount: Decimal,
        day: date,
        category: Category,
        payment_method: str,
    ) -> None:
        if Expense.objects.filter(created_by=owner, title=title).exists():
            return
        expense = Expense(
            title=title, amount=amount, date=day, category=category, payment_method=payment_method
        )
        save_personal_expense(expense, owner)

    def _add_dinner(self, rahul: User, aman: User, rohit: User, today: date) -> None:
        """The dinner of 3000: paid 1500/1000/500, shared equally, so Rohit owes Rahul 500."""
        title = "Dinner at Saffron"
        if Expense.objects.filter(created_by=rahul, title=title).exists():
            return
        expense = Expense(
            title=title,
            amount=Decimal("3000.00"),
            date=today,
            category=Category.objects.get(name="Food", owner=None),
            payment_method=PaymentMethod.UPI,
        )
        rows = [
            ParticipantRow(rahul, Decimal("1500.00"), ZERO_AMOUNT),
            ParticipantRow(aman, Decimal("1000.00"), ZERO_AMOUNT),
            ParticipantRow(rohit, Decimal("500.00"), ZERO_AMOUNT),
        ]
        save_shared_expense(expense, rahul, SplitType.EQUAL, rows)
