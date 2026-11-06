"""Tests for monthly budgets: usage, the warning threshold, CRUD and isolation."""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from expenses.forms import BudgetForm
from expenses.models import Budget, ExpenseParticipant, ExpenseType
from expenses.selectors import get_budget_for_month, get_budgets_for_user
from expenses.services import calculate_budget_usage

from .helpers import create_expense, create_personal_expense, create_user


def money(value: str) -> Decimal:
    return Decimal(value)


class BudgetUsageCalculationTests(SimpleTestCase):
    def usage(self, amount: str, spent: str, threshold: int = 80):
        return calculate_budget_usage(money(amount), money(spent), threshold)

    def test_usage_and_remaining_are_calculated(self):
        """A budget of 5000 with 2000 spent: 3000 left and 40% used."""
        usage = self.usage("5000.00", "2000.00")

        self.assertEqual(usage.spent, money("2000.00"))
        self.assertEqual(usage.remaining, money("3000.00"))
        self.assertEqual(usage.usage_percent, money("40.00"))
        self.assertFalse(usage.is_warning)
        self.assertFalse(usage.is_over)

    def test_nothing_spent_gives_zero_usage(self):
        """A new month starts at zero."""
        usage = self.usage("5000.00", "0.00")

        self.assertEqual(usage.remaining, money("5000.00"))
        self.assertEqual(usage.usage_percent, money("0.00"))

    def test_warning_starts_exactly_at_the_threshold(self):
        """4000 of 5000 is exactly 80%, and the warning is on."""
        usage = self.usage("5000.00", "4000.00")

        self.assertEqual(usage.usage_percent, money("80.00"))
        self.assertTrue(usage.is_warning)
        self.assertFalse(usage.is_over)

    def test_no_warning_one_paisa_below_the_threshold(self):
        """3999.99 of 5000 is just under 80%."""
        self.assertFalse(self.usage("5000.00", "3999.99").is_warning)

    def test_rounding_of_the_percentage_does_not_trigger_the_warning(self):
        """2399.99 of 3000 shows as 80.00%, but it is below 80%, so there is no warning."""
        usage = self.usage("3000.00", "2399.99")

        self.assertEqual(usage.usage_percent, money("80.00"))
        self.assertFalse(usage.is_warning)

    def test_threshold_can_be_changed(self):
        """The same spending is a warning at 50% but not at 80%."""
        self.assertTrue(self.usage("5000.00", "3000.00", threshold=50).is_warning)
        self.assertFalse(self.usage("5000.00", "3000.00", threshold=80).is_warning)

    def test_spending_exactly_the_budget_is_not_over(self):
        """Hitting the limit is not going over it."""
        usage = self.usage("5000.00", "5000.00")

        self.assertEqual(usage.remaining, money("0.00"))
        self.assertEqual(usage.usage_percent, money("100.00"))
        self.assertFalse(usage.is_over)

    def test_over_budget_gives_a_negative_remaining(self):
        """5500 of 5000 is 110%, and 500 over."""
        usage = self.usage("5000.00", "5500.00")

        self.assertEqual(usage.remaining, money("-500.00"))
        self.assertEqual(usage.over_amount, money("500.00"))
        self.assertEqual(usage.usage_percent, money("110.00"))
        self.assertTrue(usage.is_over)
        self.assertTrue(usage.is_warning)

    def test_one_paisa_over_counts_as_over(self):
        """There is no tolerance."""
        self.assertTrue(self.usage("5000.00", "5000.01").is_over)


class BudgetTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rahul = create_user("rahul")
        cls.aman = create_user("aman")

    def setUp(self):
        self.client.force_login(self.rahul)

    def create_budget(self, user=None, **overrides) -> Budget:
        values = {"year": 2026, "month": 11, "amount": money("5000.00")}
        values.update(overrides)
        return Budget.objects.create(user=user or self.rahul, **values)

    def form_data(self, **overrides) -> dict:
        data = {"year": "2026", "month": "11", "amount": "5000.00"}
        data.update(overrides)
        return data


class BudgetSpendingTests(BudgetTestCase):
    def test_spent_counts_every_personal_expense_of_that_month(self):
        """100 + 200 in November."""
        create_personal_expense(self.rahul, amount=money("100.00"), date=date(2026, 11, 5))
        create_personal_expense(self.rahul, amount=money("200.00"), date=date(2026, 11, 6))
        self.create_budget()

        budget = get_budget_for_month(self.rahul, 2026, 11)

        self.assertEqual(budget.usage.spent, money("300.00"))
        self.assertEqual(budget.usage.remaining, money("4700.00"))

    def test_first_and_last_day_of_the_month_count_and_the_neighbours_do_not(self):
        """1 Nov and 30 Nov are in. 31 Oct and 1 Dec are out."""
        for day in (date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 30), date(2026, 12, 1)):
            create_personal_expense(self.rahul, amount=money("1.00"), date=day)
        self.create_budget()

        budget = get_budget_for_month(self.rahul, 2026, 11)

        self.assertEqual(budget.usage.spent, money("2.00"))

    def test_same_month_of_another_year_does_not_count(self):
        """November 2025 is not November 2026."""
        create_personal_expense(self.rahul, amount=money("50.00"), date=date(2025, 11, 5))
        self.create_budget()

        budget = get_budget_for_month(self.rahul, 2026, 11)

        self.assertEqual(budget.usage.spent, money("0.00"))

    def test_spent_uses_my_share_not_what_i_paid(self):
        """RULE: I paid 3000 for the dinner, but my share is 1000."""
        dinner = create_expense(
            self.aman,
            amount=money("3000.00"),
            date=date(2026, 11, 10),
            expense_type=ExpenseType.SHARED,
        )
        ExpenseParticipant.objects.create(
            expense=dinner,
            user=self.rahul,
            paid_amount=money("3000.00"),
            share_amount=money("1000.00"),
        )
        ExpenseParticipant.objects.create(
            expense=dinner, user=self.aman, share_amount=money("2000.00")
        )
        self.create_budget()

        budget = get_budget_for_month(self.rahul, 2026, 11)

        self.assertEqual(budget.usage.spent, money("1000.00"))

    def test_expense_i_created_without_taking_part_does_not_count(self):
        """RULE: only expenses where I am a participant count for me."""
        expense = create_expense(
            self.rahul,
            amount=money("900.00"),
            date=date(2026, 11, 10),
            expense_type=ExpenseType.SHARED,
        )
        ExpenseParticipant.objects.create(
            expense=expense,
            user=self.aman,
            paid_amount=money("900.00"),
            share_amount=money("900.00"),
        )
        self.create_budget()

        budget = get_budget_for_month(self.rahul, 2026, 11)

        self.assertEqual(budget.usage.spent, money("0.00"))

    def test_another_users_spending_does_not_count(self):
        """Isolation: Aman's expenses do not use up Rahul's budget."""
        create_personal_expense(self.aman, amount=money("7777.00"), date=date(2026, 11, 5))
        self.create_budget()

        budget = get_budget_for_month(self.rahul, 2026, 11)

        self.assertEqual(budget.usage.spent, money("0.00"))

    def test_month_without_a_budget_gives_none(self):
        """Setting a budget is optional."""
        self.assertIsNone(get_budget_for_month(self.rahul, 2026, 11))

    def test_another_users_budget_is_not_returned(self):
        """Isolation also applies to the budget itself."""
        self.create_budget(user=self.aman)

        self.assertIsNone(get_budget_for_month(self.rahul, 2026, 11))
        self.assertEqual(get_budgets_for_user(self.rahul), [])

    def test_each_budget_in_the_list_has_its_own_months_spending(self):
        """The list works out spending per budget month."""
        create_personal_expense(self.rahul, amount=money("10.00"), date=date(2026, 10, 5))
        create_personal_expense(self.rahul, amount=money("20.00"), date=date(2026, 11, 5))
        self.create_budget(month=10)
        self.create_budget(month=11)

        november, october = get_budgets_for_user(self.rahul)

        self.assertEqual(november.usage.spent, money("20.00"))
        self.assertEqual(october.usage.spent, money("10.00"))

    @override_settings(BUDGET_WARNING_THRESHOLD_PERCENT=50)
    def test_warning_threshold_comes_from_the_setting(self):
        """60% used is a warning when the setting says 50."""
        create_personal_expense(self.rahul, amount=money("3000.00"), date=date(2026, 11, 5))
        self.create_budget()

        self.assertTrue(get_budget_for_month(self.rahul, 2026, 11).usage.is_warning)

    def test_warning_threshold_defaults_to_80(self):
        """60% used is not a warning by default."""
        create_personal_expense(self.rahul, amount=money("3000.00"), date=date(2026, 11, 5))
        self.create_budget()

        self.assertFalse(get_budget_for_month(self.rahul, 2026, 11).usage.is_warning)


class BudgetPageTests(BudgetTestCase):
    def test_urls_match_the_agreed_addresses(self):
        """The list lives at /budget/, as the project URLs require."""
        self.assertEqual(reverse("budget_list"), "/budget/")
        self.assertEqual(reverse("budget_create"), "/budget/add/")
        self.assertEqual(reverse("budget_edit", args=[5]), "/budget/5/edit/")
        self.assertEqual(reverse("budget_delete", args=[5]), "/budget/5/delete/")

    def test_every_budget_page_sends_anonymous_visitors_to_login(self):
        """Budgets are private."""
        budget = self.create_budget()
        self.client.logout()
        urls = (
            reverse("budget_list"),
            reverse("budget_create"),
            reverse("budget_edit", args=[budget.pk]),
            reverse("budget_delete", args=[budget.pk]),
        )

        for url in urls:
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")

    def test_add_form_starts_with_the_current_month(self):
        """The most common case is budgeting for this month."""
        with mock.patch("expenses.views.timezone.localdate", return_value=date(2026, 3, 9)):
            response = self.client.get(reverse("budget_create"))

        self.assertEqual(response.context["form"].initial, {"year": 2026, "month": 3})

    def test_valid_budget_is_saved_for_the_logged_in_user(self):
        """The owner is whoever is logged in."""
        response = self.client.post(reverse("budget_create"), self.form_data())

        self.assertRedirects(response, reverse("budget_list"))
        budget = Budget.objects.get()
        self.assertEqual(budget.user, self.rahul)
        self.assertEqual(budget.amount, money("5000.00"))

    def test_owner_in_the_submitted_data_is_ignored(self):
        """A user cannot set a budget for someone else."""
        self.client.post(reverse("budget_create"), self.form_data(user=self.aman.pk))

        self.assertEqual(Budget.objects.get().user, self.rahul)

    def test_second_budget_for_the_same_month_is_refused(self):
        """One budget per month, with a message instead of a server error."""
        self.create_budget()

        response = self.client.post(reverse("budget_create"), self.form_data(amount="9000.00"))

        self.assertContains(response, "You already have a budget for this month")
        self.assertEqual(Budget.objects.get().amount, money("5000.00"))

    def test_duplicate_that_gets_past_the_form_check_gives_an_error_not_a_crash(self):
        """If two requests race, the database refuses one and the user sees a message."""
        self.create_budget()

        with mock.patch.object(BudgetForm, "clean", lambda form: form.cleaned_data):
            response = self.client.post(reverse("budget_create"), self.form_data())

        self.assertContains(response, "You already have a budget for this month")
        self.assertEqual(Budget.objects.count(), 1)

    def test_another_month_and_another_user_are_allowed(self):
        """Uniqueness covers the user, year and month together."""
        self.create_budget()
        self.create_budget(user=self.aman)

        self.client.post(reverse("budget_create"), self.form_data(month="12"))
        self.client.post(reverse("budget_create"), self.form_data(year="2027"))

        self.assertEqual(Budget.objects.filter(user=self.rahul).count(), 3)

    def test_invalid_input_is_refused_and_saves_nothing(self):
        """Server-side validation covers every field."""
        invalid_inputs = (
            {"amount": "0"},
            {"amount": "-5"},
            {"amount": "abc"},
            {"amount": ""},
            {"amount": "1.234"},
            {"month": "0"},
            {"month": "13"},
            {"month": ""},
            {"year": "1999"},
            {"year": "2101"},
            {"year": ""},
        )
        for overrides in invalid_inputs:
            with self.subTest(overrides=overrides):
                response = self.client.post(reverse("budget_create"), self.form_data(**overrides))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(Budget.objects.count(), 0)

    def test_owner_can_edit_their_budget(self):
        """A valid edit is saved."""
        budget = self.create_budget()

        response = self.client.post(
            reverse("budget_edit", args=[budget.pk]), self.form_data(amount="6000.00")
        )

        self.assertRedirects(response, reverse("budget_list"))
        budget.refresh_from_db()
        self.assertEqual(budget.amount, money("6000.00"))

    def test_keeping_the_same_month_when_editing_is_not_a_duplicate(self):
        """A budget does not conflict with itself."""
        budget = self.create_budget()

        response = self.client.post(reverse("budget_edit", args=[budget.pk]), self.form_data())

        self.assertRedirects(response, reverse("budget_list"))

    def test_moving_a_budget_onto_a_month_that_has_one_is_refused(self):
        """The duplicate rule also applies to edits."""
        self.create_budget(month=10)
        budget = self.create_budget(month=11)

        response = self.client.post(
            reverse("budget_edit", args=[budget.pk]), self.form_data(month="10")
        )

        self.assertContains(response, "You already have a budget for this month")
        budget.refresh_from_db()
        self.assertEqual(budget.month, 11)

    def test_owner_can_delete_their_budget_after_confirming(self):
        """GET asks, and POST deletes."""
        budget = self.create_budget()
        url = reverse("budget_delete", args=[budget.pk])

        self.assertContains(self.client.get(url), "5000.00")
        self.assertTrue(Budget.objects.filter(pk=budget.pk).exists())

        self.assertRedirects(self.client.post(url), reverse("budget_list"))
        self.assertFalse(Budget.objects.filter(pk=budget.pk).exists())

    def test_another_users_budget_cannot_be_edited_or_deleted(self):
        """IDOR check: changing the id in the URL gives a 404 and changes nothing."""
        budget = self.create_budget(user=self.aman)
        edit_url = reverse("budget_edit", args=[budget.pk])
        delete_url = reverse("budget_delete", args=[budget.pk])

        self.assertEqual(self.client.get(edit_url).status_code, 404)
        self.assertEqual(self.client.post(edit_url, self.form_data(amount="1")).status_code, 404)
        self.assertEqual(self.client.get(delete_url).status_code, 404)
        self.assertEqual(self.client.post(delete_url).status_code, 404)
        budget.refresh_from_db()
        self.assertEqual(budget.amount, money("5000.00"))

    def test_list_shows_only_the_users_own_budgets(self):
        """Other people's budgets never appear."""
        self.create_budget(amount=money("1111.00"))
        self.create_budget(user=self.aman, amount=money("2222.00"))

        response = self.client.get(reverse("budget_list"))

        self.assertContains(response, "1111.00")
        self.assertNotContains(response, "2222.00")

    def test_list_shows_the_usage_status(self):
        """The list shows the warning and over-budget messages."""
        create_personal_expense(self.rahul, amount=money("90.00"), date=date(2026, 10, 5))
        create_personal_expense(self.rahul, amount=money("110.00"), date=date(2026, 11, 5))
        self.create_budget(month=10, amount=money("100.00"))
        self.create_budget(month=11, amount=money("100.00"))

        response = self.client.get(reverse("budget_list"))

        self.assertContains(response, "Close to the limit: 10.00 left")
        self.assertContains(response, "Over budget by 10.00")

    def test_list_query_count_does_not_grow_with_the_number_of_budgets(self):
        """No N+1: session, user, the budgets, and one query for spending per month."""
        for month in range(1, 13):
            self.create_budget(month=month)

        with self.assertNumQueries(4):
            response = self.client.get(reverse("budget_list"))

        self.assertEqual(len(response.context["budgets"]), 12)

    def test_form_without_a_csrf_token_is_rejected(self):
        """CSRF protection is on for this form."""
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.rahul)

        response = strict_client.post(reverse("budget_create"), self.form_data())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Budget.objects.count(), 0)

    def test_navigation_links_to_budgets(self):
        """The page can be reached from the navigation."""
        self.assertContains(self.client.get(reverse("dashboard")), reverse("budget_list"))


class BudgetOnDashboardTests(BudgetTestCase):
    TODAY = date(2026, 11, 11)

    def open_dashboard(self):
        with mock.patch("dashboard.views.timezone.localdate", return_value=self.TODAY):
            return self.client.get(reverse("dashboard"))

    def test_dashboard_shows_this_months_budget(self):
        """Spent, the budget and the percentage all appear."""
        create_personal_expense(self.rahul, amount=money("1000.00"), date=self.TODAY)
        self.create_budget()

        response = self.open_dashboard()

        self.assertContains(response, "1000.00 of 5000.00")
        self.assertContains(response, "20.00%")
        self.assertContains(response, "4000.00 left")

    def test_dashboard_shows_the_warning(self):
        """At 80% the warning message appears."""
        create_personal_expense(self.rahul, amount=money("4000.00"), date=self.TODAY)
        self.create_budget()

        self.assertContains(self.open_dashboard(), "Close to the limit")

    def test_dashboard_shows_over_budget(self):
        """Above 100% the message says by how much."""
        create_personal_expense(self.rahul, amount=money("5500.00"), date=self.TODAY)
        self.create_budget()

        self.assertContains(self.open_dashboard(), "Over budget by 500.00")

    def test_dashboard_offers_to_set_a_budget_when_there_is_none(self):
        """No budget is a normal state."""
        response = self.open_dashboard()

        self.assertContains(response, "No budget set for this month")
        self.assertContains(response, reverse("budget_create"))

    def test_budget_of_another_month_is_not_shown_as_this_months(self):
        """Only the current month's budget belongs on the card."""
        self.create_budget(month=10)

        self.assertContains(self.open_dashboard(), "No budget set for this month")

    def test_another_users_budget_is_not_shown(self):
        """Isolation."""
        self.create_budget(user=self.aman)

        self.assertContains(self.open_dashboard(), "No budget set for this month")
