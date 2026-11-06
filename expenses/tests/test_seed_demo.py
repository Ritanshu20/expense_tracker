"""Tests for the seed_demo management command."""

from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from expenses.models import Budget, Category, Expense, ExpenseParticipant
from expenses.selectors import get_settlement_summary


def run_seed_demo(**options) -> str:
    output = StringIO()
    call_command("seed_demo", stdout=output, **options)
    return output.getvalue()


@override_settings(DEBUG=True)
class SeedDemoTests(TestCase):
    def test_creates_the_three_demo_users(self):
        """Rahul, Aman and Rohit can all log in afterwards."""
        run_seed_demo(password="a-demo-password-42")

        for username in ("Rahul", "Aman", "Rohit"):
            with self.subTest(username=username):
                self.assertTrue(
                    User.objects.get(username=username).check_password("a-demo-password-42")
                )

    def test_dinner_gives_the_expected_settlement(self):
        """Paid 1500/1000/500 with an equal split: Rohit owes Rahul 500."""
        run_seed_demo()
        rahul = User.objects.get(username="Rahul")

        self.assertEqual(
            get_settlement_summary(rahul).owed_to_you, [("Rohit", "Rahul", Decimal("500.00"))]
        )

    def test_creates_a_personal_expense_a_category_and_a_budget(self):
        """Everything the demo needs is there."""
        run_seed_demo()
        rahul = User.objects.get(username="Rahul")

        self.assertTrue(Expense.objects.filter(created_by=rahul, expense_type="personal").exists())
        self.assertTrue(Category.objects.filter(name="Office", owner=rahul).exists())
        self.assertEqual(Budget.objects.filter(user=rahul).count(), 1)

    def test_running_it_twice_creates_no_duplicates(self):
        """The command is idempotent."""
        run_seed_demo()
        counts_before = (
            User.objects.count(),
            Category.objects.count(),
            Expense.objects.count(),
            ExpenseParticipant.objects.count(),
            Budget.objects.count(),
        )

        run_seed_demo()

        counts_after = (
            User.objects.count(),
            Category.objects.count(),
            Expense.objects.count(),
            ExpenseParticipant.objects.count(),
            Budget.objects.count(),
        )
        self.assertEqual(counts_before, counts_after)

    def test_running_it_twice_does_not_change_the_password(self):
        """Someone's existing demo login keeps working."""
        run_seed_demo(password="a-demo-password-42")

        run_seed_demo()

        self.assertTrue(User.objects.get(username="Rahul").check_password("a-demo-password-42"))

    def test_random_password_is_printed_once_when_users_are_created(self):
        """Without --password the user still needs a way to log in."""
        first_output = run_seed_demo()
        second_output = run_seed_demo()

        self.assertIn("Password for the new demo users", first_output)
        self.assertNotIn("Password for the new demo users", second_output)

    def test_given_password_is_not_printed(self):
        """A password typed by the user is not repeated on the screen."""
        output = run_seed_demo(password="a-demo-password-42")

        self.assertNotIn("a-demo-password-42", output)

    def test_demo_pages_load_with_the_data(self):
        """The dashboard shows the demo numbers: 1200 + 350 + 1000 of a budget of 5000."""
        run_seed_demo()
        self.client.force_login(User.objects.get(username="Rahul"))

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "2550.00")
        self.assertContains(response, "Rohit owes you")


class SeedDemoSafetyTests(TestCase):
    @override_settings(DEBUG=False)
    def test_refuses_to_run_with_debug_off(self):
        """SAFETY: demo accounts must never reach a real site."""
        with self.assertRaisesMessage(CommandError, "only runs with DEBUG on"):
            run_seed_demo()

        self.assertFalse(User.objects.filter(username="Rahul").exists())
