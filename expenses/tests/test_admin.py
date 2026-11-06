"""Tests for the Django admin: access, list pages, and the money rules on the expense page."""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from expenses.models import Budget, Category, Expense, ExpenseParticipant

from .helpers import create_expense, create_user, default_category


class AdminAccessTests(TestCase):
    def test_anonymous_visitor_is_sent_to_the_site_login_and_not_into_the_admin(self):
        """LoginRequiredMiddleware protects the admin like every other page."""
        admin_url = reverse("admin:expenses_expense_changelist")

        response = self.client.get(admin_url)

        self.assertRedirects(
            response, f"{reverse('login')}?next={admin_url}", fetch_redirect_response=False
        )

    def test_staff_user_logging_in_through_the_site_login_reaches_the_admin(self):
        """The next address brings a staff user back to the admin page they wanted."""
        admin_url = reverse("admin:expenses_expense_changelist")
        User.objects.create_superuser("admin", "admin@example.com", "correct-horse-battery-42")

        response = self.client.post(
            reverse("login"),
            {"username": "admin", "password": "correct-horse-battery-42", "next": admin_url},
        )

        self.assertRedirects(response, admin_url)

    def test_regular_user_cannot_open_the_admin(self):
        """Logging in is not enough, because the admin needs a staff account."""
        self.client.force_login(create_user("rahul"))

        response = self.client.get(reverse("admin:expenses_expense_changelist"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])


class AdminPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser("admin", "admin@example.com")
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")
        cls.food = default_category()

    def setUp(self):
        self.client.force_login(self.admin_user)

    def expense_form_data(self, *rows, **overrides) -> dict:
        """Build the POST data of the admin expense page. Each row is (user, paid, share)."""
        data = {
            "title": "Dinner",
            "description": "",
            "amount": "3000.00",
            "date": "2026-11-01",
            "category": self.food.pk,
            "payment_method": "upi",
            "expense_type": "shared",
            "created_by": self.rahul.pk,
            "participants-TOTAL_FORMS": len(rows),
            "participants-INITIAL_FORMS": 0,
            "participants-MIN_NUM_FORMS": 0,
            "participants-MAX_NUM_FORMS": 1000,
        }
        for index, (user, paid_amount, share_amount) in enumerate(rows):
            data[f"participants-{index}-user"] = user.pk
            data[f"participants-{index}-paid_amount"] = paid_amount
            data[f"participants-{index}-share_amount"] = share_amount
        data.update(overrides)
        return data

    def post_expense(self, *rows, **overrides):
        url = reverse("admin:expenses_expense_add")
        return self.client.post(url, self.expense_form_data(*rows, **overrides))

    def assert_nothing_was_saved(self):
        self.assertEqual(Expense.objects.count(), 0)
        self.assertEqual(ExpenseParticipant.objects.count(), 0)

    def test_every_admin_list_page_loads_with_search_and_filters(self):
        """A wrong field name in list_display, search_fields or list_filter would fail here."""
        expense = create_expense(self.rahul)
        ExpenseParticipant.objects.create(expense=expense, user=self.rahul)
        Budget.objects.create(user=self.rahul, year=2026, month=11, amount=Decimal("100.00"))
        page_names = (
            "admin:auth_user_changelist",
            "admin:expenses_category_changelist",
            "admin:expenses_expense_changelist",
            "admin:expenses_expenseparticipant_changelist",
            "admin:expenses_budget_changelist",
        )

        for page_name in page_names:
            with self.subTest(page=page_name):
                url = reverse(page_name)
                self.assertEqual(self.client.get(url).status_code, 200)
                self.assertEqual(self.client.get(url, {"q": "rahul"}).status_code, 200)

    def test_category_can_be_created_in_the_admin(self):
        """Staff can add a custom category for a user."""
        response = self.client.post(
            reverse("admin:expenses_category_add"), {"name": "Pets", "owner": self.rahul.pk}
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Category.objects.filter(name="Pets", owner=self.rahul).exists())

    def test_budget_can_be_created_in_the_admin(self):
        """Staff can add a monthly budget for a user."""
        response = self.client.post(
            reverse("admin:expenses_budget_add"),
            {"user": self.rahul.pk, "year": 2026, "month": 11, "amount": "5000.00"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Budget.objects.filter(user=self.rahul, month=11).exists())

    def test_duplicate_budget_shows_an_error_in_the_admin(self):
        """The unique rule gives a form error instead of a server error."""
        Budget.objects.create(user=self.rahul, year=2026, month=11, amount=Decimal("100.00"))

        response = self.client.post(
            reverse("admin:expenses_budget_add"),
            {"user": self.rahul.pk, "year": 2026, "month": 11, "amount": "5000.00"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Budget.objects.count(), 1)

    def test_shared_expense_with_participants_can_be_created_in_the_admin(self):
        """The dinner example is saved with one row per person."""
        response = self.post_expense(
            (self.rahul, "1500.00", "1500.00"),
            (self.aman, "1500.00", "1500.00"),
        )

        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertEqual(expense.participants.count(), 2)

    def test_expense_whose_paid_amounts_do_not_add_up_is_not_saved(self):
        """Paid amounts must equal the bill, and nothing is saved when they do not."""
        response = self.post_expense(
            (self.rahul, "1000.00", "1500.00"),
            (self.aman, "1500.00", "1500.00"),
        )

        self.assertContains(response, "Paid amounts must add up")
        self.assert_nothing_was_saved()

    def test_expense_whose_shares_do_not_add_up_is_not_saved(self):
        """Shares must equal the bill, and nothing is saved when they do not."""
        response = self.post_expense(
            (self.rahul, "1500.00", "1000.00"),
            (self.aman, "1500.00", "1500.00"),
        )

        self.assertContains(response, "Custom shares must add up")
        self.assert_nothing_was_saved()

    def test_expense_without_participants_is_not_saved(self):
        """An expense with no participant rows would count for nobody."""
        response = self.post_expense()

        self.assertContains(response, "At least one participant")
        self.assert_nothing_was_saved()

    def test_same_participant_twice_is_not_saved(self):
        """Two rows for one person are refused with a form error, not a server error."""
        response = self.post_expense(
            (self.rahul, "1500.00", "1500.00"),
            (self.rahul, "1500.00", "1500.00"),
        )

        self.assertEqual(response.status_code, 200)
        self.assert_nothing_was_saved()

    def test_personal_expense_for_its_creator_is_saved(self):
        """A personal expense has one row, for the creator, with paid = share = amount."""
        response = self.post_expense(
            (self.rahul, "500.00", "500.00"), expense_type="personal", amount="500.00"
        )

        self.assertEqual(response.status_code, 302)
        participant = ExpenseParticipant.objects.get()
        self.assertEqual(participant.user, self.rahul)
        self.assertEqual(participant.paid_amount, participant.expense.amount)
        self.assertEqual(participant.share_amount, participant.expense.amount)

    def test_personal_expense_for_someone_else_is_not_saved(self):
        """A personal expense belongs to its creator only."""
        response = self.post_expense(
            (self.aman, "500.00", "500.00"), expense_type="personal", amount="500.00"
        )

        self.assertContains(response, "exactly one participant")
        self.assert_nothing_was_saved()

    def test_deleting_an_expense_in_the_admin_also_removes_its_participants(self):
        """The view-only participant list does not block deleting the whole expense."""
        expense = create_expense(self.rahul)
        ExpenseParticipant.objects.create(expense=expense, user=self.rahul)

        response = self.client.post(
            reverse("admin:expenses_expense_delete", args=[expense.pk]), {"post": "yes"}
        )

        self.assertEqual(response.status_code, 302)
        self.assert_nothing_was_saved()

    def test_a_single_participant_row_cannot_be_added_changed_or_deleted_on_its_own(self):
        """Changing one row alone would break the sums, so that list is view-only."""
        expense = create_expense(self.rahul, amount=Decimal("100.00"))
        participant = ExpenseParticipant.objects.create(
            expense=expense,
            user=self.rahul,
            paid_amount=Decimal("100.00"),
            share_amount=Decimal("100.00"),
        )
        add_url = reverse("admin:expenses_expenseparticipant_add")
        change_url = reverse("admin:expenses_expenseparticipant_change", args=[participant.pk])
        delete_url = reverse("admin:expenses_expenseparticipant_delete", args=[participant.pk])

        self.assertEqual(self.client.get(add_url).status_code, 403)
        self.assertEqual(self.client.post(change_url, {"paid_amount": "1"}).status_code, 403)
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
        participant.refresh_from_db()
        self.assertEqual(participant.paid_amount, Decimal("100.00"))
